"""逐项目目录读取 PDF，并按文件名调度招标与投标解析器。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from award_results import (
    MATCHED,
    AwardRegistry,
    AwardResolution,
    format_award_amount,
    load_award_registry,
    normalize_company_name,
)
from config import load_local_env
from deepseek_profile import (
    DeepSeekSettings,
    generate_company_profile,
    profile_processing_mode,
)
from io_utils import atomic_write_text, read_json, sha256_file, utc_now_iso
from markdown_renderers import (
    BID_PIPELINE_VERSION,
    PIPELINE_VERSION,
    render_bid_markdown,
    render_project_report,
    render_tender_markdown,
)
from normalizers import redact_sensitive_text


SCRIPT_DIR = Path(__file__).resolve().parent
TENDER_SCRIPT = SCRIPT_DIR / "tender_parser" / "run.py"
BID_MARKDOWN_SCRIPT = SCRIPT_DIR / "extract_pdf_markdown.py"
TENDER_SCHEMA_VERSION = "0.3.4"
BID_RAW_SCHEMA_VERSION = "2.0.0"
BID_PROCESSING_MODE = "auto_then_force_retry"
SUCCESS_STATUSES = {"SUCCESS", "SUCCESS_WITH_WARNINGS", "SKIPPED"}
USABLE_STATUSES = SUCCESS_STATUSES | {"PARTIAL"}
PROJECT_ID_PATTERN = re.compile(r"[0-9a-fA-F]{32}")
AWARD_WINNER = "中标公司"
AWARD_NOT_WINNER = "未中标"
AWARD_REVIEW_REQUIRED = "中标结果待核验"


@dataclass
class DocumentOutcome:
    document_type: str
    source_file: str
    source_sha256: str
    status: str
    company_name: str | None = None
    output: str | None = None
    parser: str | None = None
    parser_schema_version: str | None = None
    final_status: str | None = None
    final_output: str | None = None
    summary_model: str | None = None
    award_status: str | None = None
    award_amount: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def report_value(self) -> dict[str, Any]:
        return {
            "document_type": self.document_type,
            "source_file": self.source_file,
            "source_sha256": self.source_sha256,
            "company_name": self.company_name,
            "status": self.status,
            "output": self.output,
            "parser": self.parser,
            "parser_schema_version": self.parser_schema_version,
            "final_status": self.final_status,
            "final_output": self.final_output,
            "summary_model": self.summary_model,
            "award_status": self.award_status,
            "award_amount": self.award_amount,
            "warnings": self.warnings,
            "errors": self.errors,
        }


@dataclass
class ProjectOutcome:
    project_key: str
    status: str
    report_path: Path | None
    tender: DocumentOutcome | None
    bids: list[DocumentOutcome]
    errors: list[str]


@dataclass
class ProjectInput:
    project_id: str | None
    project_key: str
    tender_files: list[Path]
    bid_inputs: list[tuple[str, Path]]
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CompanyAwardDecision:
    status: str
    amount: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="逐项目解析招投标 PDF，并使用 DeepSeek 将投标 Markdown 生成完整的八维企业画像",
    )
    parser.add_argument("--input-root", type=Path, default=SCRIPT_DIR / "input", help="项目输入根目录")
    parser.add_argument(
        "--intermediate-output-root",
        "--output2-root",
        dest="intermediate_output_root",
        type=Path,
        default=SCRIPT_DIR / "output2",
        help="PDF 解析 Markdown 中间输出根目录",
    )
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR / "output", help="最终 Markdown 输出根目录")
    parser.add_argument("--work-root", type=Path, default=SCRIPT_DIR / ".work", help="内部中间结果目录")
    parser.add_argument(
        "--award-file",
        type=Path,
        default=SCRIPT_DIR / "get project company" / "中标项目汇总表2.xlsx",
        help="只读中标结果 Excel；匹配异常时仅将企业画像标记为待核验",
    )
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="只处理指定项目目录名或 32 位项目标识；可重复传入",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="仅复用源哈希、版本和处理模式一致的结果")
    mode.add_argument("--overwrite", action="store_true", help="显式覆盖匹配文档的已有结果")
    parser.add_argument(
        "--profiles-only",
        "--profile-only",
        action="store_true",
        help="仅读取 output2 中已有的投标 Markdown 批量生成企业画像，不处理 PDF 或招标文件",
    )
    parser.add_argument("--keep-workdir", action="store_true", help="成功后仍保留内部中间结果")
    error_mode = parser.add_mutually_exclusive_group()
    error_mode.add_argument(
        "--continue-on-error",
        dest="fail_fast",
        action="store_false",
        help="单个项目失败后继续处理其他项目（默认行为）",
    )
    error_mode.add_argument(
        "--fail-fast",
        dest="fail_fast",
        action="store_true",
        help="首个非成功项目后停止",
    )
    parser.set_defaults(fail_fast=False)
    return parser.parse_args()


def _safe_name(value: str, *, max_length: int = 100) -> str:
    normalized = "".join("_" if char in '<>:"/\\|?*' else char for char in value).strip(" .")
    return normalized[:max_length] or "unnamed"


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or _is_relative_to(first, second) or _is_relative_to(second, first)


def _validate_roots(
    input_root: Path,
    intermediate_output_root: Path,
    output_root: Path,
    work_root: Path,
) -> None:
    if not input_root.is_dir():
        raise ValueError(f"输入根目录不存在或不是目录：{input_root}")
    if input_root.is_symlink():
        raise ValueError("输入根目录不能是符号链接")
    roots = (
        ("输入根目录", input_root),
        ("中间输出根目录", intermediate_output_root),
        ("最终输出根目录", output_root),
        ("工作根目录", work_root),
    )
    for index, (left_label, left) in enumerate(roots):
        for right_label, right in roots[index + 1:]:
            if _paths_overlap(left, right):
                raise ValueError(f"{left_label}与{right_label}不能相同或互相包含")
    for label, path in roots[1:]:
        if path.exists() and (not path.is_dir() or path.is_symlink()):
            raise ValueError(f"{label}必须是普通目录，不能是文件或符号链接")
    for script in (TENDER_SCRIPT, BID_MARKDOWN_SCRIPT):
        if not script.is_file():
            raise ValueError(f"解析入口不存在：{script.name}")


def _ensure_markdown_only(directory: Path) -> None:
    if not directory.exists():
        return
    if directory.is_symlink():
        raise ValueError(f"项目输出目录不能是符号链接：{directory.name}")
    symlinks = [path for path in directory.rglob("*") if path.is_symlink()]
    if symlinks:
        raise ValueError(f"项目输出目录含符号链接，拒绝写入：{symlinks[0].name}")
    non_markdown = [
        path
        for path in directory.rglob("*")
        if path.is_file() and path.suffix.casefold() != ".md"
    ]
    if non_markdown:
        names = "、".join(path.name for path in non_markdown[:5])
        raise ValueError(f"项目输出目录含非 Markdown 文件，拒绝混写：{names}")


def _parse_input_filename(path: Path) -> tuple[str, str | None, str]:
    """返回文档类型、可选项目标识和文件名前缀。"""
    matches = list(PROJECT_ID_PATTERN.finditer(path.stem))
    if len(matches) > 1:
        raise ValueError(f"文件名最多只能包含一个 32 位项目标识：{path.name}")

    project_id: str | None = None
    if matches:
        match = matches[0]
        project_id = match.group(0).casefold()
        prefix = path.stem[:match.start()].rstrip(" _-")
        suffix = path.stem[match.end():].lstrip(" _-")
    elif path.stem.endswith("招标文件"):
        prefix = path.stem[:-len("招标文件")].rstrip(" _-")
        suffix = "招标文件"
    elif path.stem.endswith("投标文件"):
        prefix = path.stem[:-len("投标文件")].rstrip(" _-")
        suffix = "投标文件"
    else:
        raise ValueError(
            f"文件名必须以“招标文件”或“投标文件”结尾，32 位项目标识可选：{path.name}"
        )

    if not prefix:
        raise ValueError(f"项目名或公司名不能为空：{path.name}")
    if suffix.startswith("招标文件"):
        document_type = "tender"
    elif suffix.startswith("投标文件"):
        document_type = "bid"
    else:
        raise ValueError(f"项目标识后必须紧接“招标文件”或“投标文件”：{path.name}")
    return document_type, project_id, prefix


def _discover_project(project_dir: Path) -> ProjectInput:
    errors: list[str] = []
    parsed_tenders: list[tuple[str | None, str, Path]] = []
    parsed_bids: list[tuple[str | None, str, Path]] = []
    for path in sorted(project_dir.iterdir(), key=lambda item: item.name.casefold()):
        if path.name.startswith(".") or path.name.startswith("~$"):
            continue
        if path.is_dir():
            errors.append(f"项目目录内只接受平铺 PDF，不接受子目录：{path.name}")
            continue
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            continue
        if path.is_symlink():
            errors.append(f"拒绝符号链接输入：{path.name}")
            continue
        try:
            document_type, project_id, prefix = _parse_input_filename(path)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        record = (project_id, prefix, path.resolve())
        if document_type == "tender":
            parsed_tenders.append(record)
        else:
            parsed_bids.append(record)

    parsed_tenders.sort(key=lambda item: item[2].name.casefold())
    tender_input_valid = len(parsed_tenders) == 1
    project_id = parsed_tenders[0][0] if tender_input_valid else None
    if not parsed_tenders:
        errors.append("项目目录中没有符合命名规则的招标文件")
    elif len(parsed_tenders) > 1:
        errors.append(f"项目目录中存在 {len(parsed_tenders)} 个招标文件，首期要求恰好一个")

    bid_inputs: list[tuple[str, Path]] = []
    for bid_project_id, company_name, path in parsed_bids:
        if not tender_input_valid:
            continue
        if project_id is not None and bid_project_id is not None and bid_project_id != project_id:
            errors.append(
                f"投标文件项目标识 {bid_project_id} 与招标文件项目标识 {project_id} 不一致：{path.name}"
            )
            continue
        bid_inputs.append((company_name, path))
    bid_inputs.sort(key=lambda item: (item[0].casefold(), item[1].name.casefold()))
    return ProjectInput(
        project_id=project_id,
        project_key=project_dir.name,
        tender_files=[path for _, _, path in parsed_tenders],
        bid_inputs=bid_inputs,
        errors=errors,
    )


def _discover_projects(input_root: Path, selected: list[str]) -> list[ProjectInput]:
    project_dirs: list[Path] = []
    root_errors: list[str] = []
    for path in sorted(input_root.iterdir(), key=lambda item: item.name.casefold()):
        if path.name.startswith(".") or path.name.startswith("~$"):
            continue
        if path.is_symlink():
            root_errors.append(f"拒绝符号链接项目目录：{path.name}")
        elif path.is_dir():
            project_dirs.append(path)
        elif path.is_file() and path.suffix.casefold() == ".pdf":
            root_errors.append(f"PDF 必须放入项目子目录，不能直接放在 input 根目录：{path.name}")
    if root_errors:
        details = "；".join(root_errors[:10])
        if len(root_errors) > 10:
            details += f"；另有 {len(root_errors) - 10} 个输入结构错误"
        raise ValueError(details)

    projects = [_discover_project(path) for path in project_dirs]
    if not selected:
        return projects
    requested = {value.casefold() for value in selected}
    matched: set[str] = set()
    filtered: list[ProjectInput] = []
    for project in projects:
        aliases = {project.project_key.casefold()}
        if project.project_id:
            aliases.add(project.project_id.casefold())
        hits = requested & aliases
        if hits:
            matched.update(hits)
            filtered.append(project)
    missing = sorted(requested - matched)
    if missing:
        raise ValueError(f"未找到指定项目目录名或项目标识：{'、'.join(missing)}")
    return filtered


def _read_front_matter(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    values: dict[str, Any] = {}
    with path.open("r", encoding="utf-8") as stream:
        if stream.readline().rstrip("\r\n") != "---":
            return {}
        for _ in range(100):
            line = stream.readline()
            if not line:
                return {}
            stripped = line.rstrip("\r\n")
            if stripped == "---":
                return values
            if ":" not in stripped:
                continue
            key, raw_value = stripped.split(":", 1)
            raw_value = raw_value.strip()
            try:
                values[key.strip()] = json.loads(raw_value)
            except json.JSONDecodeError:
                values[key.strip()] = raw_value
    return {}


def _existing_output_matches(
    path: Path,
    source_hash: str,
    *,
    pipeline_version: str = PIPELINE_VERSION,
    processing_mode: str | None = None,
    parser_schema_version: str | None = None,
) -> bool:
    metadata = _read_front_matter(path)
    base_matches = (
        metadata.get("source_sha256") == source_hash
        and metadata.get("pipeline_version") == pipeline_version
    )
    if not base_matches:
        return False
    return (
        (processing_mode is None or metadata.get("processing_mode") == processing_mode)
        and (
            parser_schema_version is None
            or metadata.get("parser_schema_version") == parser_schema_version
        )
    )


def _tender_output_can_refresh(path: Path, source_hash: str, project_key: str) -> bool:
    metadata = _read_front_matter(path)
    return (
        metadata.get("document_type") == "tender"
        and metadata.get("source_sha256") == source_hash
        and metadata.get("project_key") == project_key
    )


def _guard_output(
    path: Path,
    source_hash: str,
    *,
    resume: bool,
    overwrite: bool,
    pipeline_version: str = PIPELINE_VERSION,
    processing_mode: str | None = None,
    parser_schema_version: str | None = None,
) -> bool:
    """返回 True 表示 resume 已命中，可以跳过。"""
    if not path.exists():
        return False
    if not path.is_file() or path.suffix.casefold() != ".md":
        raise ValueError(f"最终输出路径不是 Markdown 文件：{path.name}")
    if overwrite:
        return False
    if resume and _existing_output_matches(
        path,
        source_hash,
        pipeline_version=pipeline_version,
        processing_mode=processing_mode,
        parser_schema_version=parser_schema_version,
    ):
        return True
    if resume:
        raise ValueError(
            f"已有输出与当前源文件、流水线版本、处理模式或解析 Schema 不一致：{path.name}"
        )
    raise FileExistsError(f"最终输出已存在：{path.name}；请使用 --resume 或 --overwrite")


def _safe_rmtree(path: Path, work_root: Path) -> None:
    resolved_root = work_root.resolve()
    resolved_path = path.resolve()
    if resolved_path == resolved_root or not _is_relative_to(resolved_path, resolved_root):
        raise ValueError("拒绝清理工作目录范围外的路径")
    if path.is_symlink():
        raise ValueError("拒绝递归清理符号链接工作目录")
    if path.exists():
        shutil.rmtree(path)


def _sanitize_error(message: str, *, roots: list[Path]) -> str:
    value = str(message)
    for root in sorted((str(path) for path in roots), key=len, reverse=True):
        value = value.replace(root, "[LOCAL_ROOT]")
    value, _ = redact_sensitive_text(value)
    return value.strip()[:1500]


def _run_child(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _child_error(completed: subprocess.CompletedProcess[str], *, roots: list[Path]) -> str:
    output = completed.stderr.strip() or completed.stdout.strip() or "子进程没有返回错误详情"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    summary = " | ".join(lines[-6:])
    return _sanitize_error(f"退出码 {completed.returncode}：{summary}", roots=roots)


def _load_tender_work(work_dir: Path, source_hash: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    extraction_path = work_dir / "extraction_result.json"
    report_path = work_dir / "report.json"
    markdown_path = work_dir / "document.md"
    extraction = read_json(extraction_path)
    report = read_json(report_path)
    if not isinstance(extraction, dict) or not isinstance(report, dict) or not markdown_path.is_file():
        raise ValueError("招标解析器未生成完整的必需中间结果")
    if extraction.get("schema_version") != TENDER_SCHEMA_VERSION:
        raise ValueError(f"招标结果 Schema 不兼容：{extraction.get('schema_version')}")
    if extraction.get("source_pdf_sha256") != source_hash:
        raise ValueError("招标中间结果与源文件 SHA256 不一致")
    raw_markdown = markdown_path.read_text(encoding="utf-8")
    return extraction, report, raw_markdown


def _load_bid_raw(
    work_dir: Path,
    source_hash: str,
    safe_stem: str,
    *,
    require_complete: bool = False,
) -> tuple[str, dict[str, Any]]:
    markdown_path = work_dir / f"{safe_stem}.md"
    quality_path = work_dir / "page_quality.json"
    quality = read_json(quality_path)
    if not markdown_path.is_file() or not isinstance(quality, dict):
        raise ValueError("投标逐页 Markdown 中间结果不完整")
    if quality.get("schemaVersion") != BID_RAW_SCHEMA_VERSION:
        raise ValueError(f"投标逐页 Markdown 质量 Schema 不兼容：{quality.get('schemaVersion')}")
    source_document = quality.get("sourceDocument")
    if not isinstance(source_document, dict) or source_document.get("sha256") != source_hash:
        raise ValueError("投标逐页 Markdown 与源文件 SHA256 不一致")
    status = quality.get("status")
    allowed_statuses = {"COMPLETE", "COMPLETE_WITH_WARNINGS", "FAILED"}
    if status not in allowed_statuses:
        raise ValueError(f"投标逐页 Markdown 质量状态无效：{status}")
    if require_complete and status not in {"COMPLETE", "COMPLETE_WITH_WARNINGS"}:
        raise ValueError(f"投标逐页 Markdown 缓存不完整：{status}")
    return markdown_path.read_text(encoding="utf-8"), quality


def _relative_output(path: Path, project_output: Path) -> str:
    return path.relative_to(project_output).as_posix()


def _publish_tender_markdown(
    *,
    outcome: DocumentOutcome,
    project_key: str,
    intermediate_project_output: Path,
    final_project_output: Path,
    resume: bool,
    overwrite: bool,
) -> None:
    if outcome.status not in USABLE_STATUSES or not outcome.output:
        return
    source_path = intermediate_project_output / outcome.output
    if not source_path.is_file():
        raise ValueError("招标解析 Markdown 中间结果不存在")
    final_path = final_project_output / "招标解析.md"
    if resume and final_path.exists() and _tender_output_can_refresh(
        final_path,
        outcome.source_sha256,
        project_key,
    ) and not _existing_output_matches(
        final_path,
        outcome.source_sha256,
        parser_schema_version=outcome.parser_schema_version,
    ):
        skipped = False
    else:
        skipped = _guard_output(
            final_path,
            outcome.source_sha256,
            resume=resume,
            overwrite=overwrite,
            parser_schema_version=outcome.parser_schema_version,
        )
    if not skipped:
        final_project_output.mkdir(parents=True, exist_ok=True)
        atomic_write_text(final_path, source_path.read_text(encoding="utf-8"))
    outcome.final_status = "SKIPPED" if skipped else "SUCCESS"
    outcome.final_output = _relative_output(final_path, final_project_output)


def _company_award_decisions(
    *,
    project_key: str,
    outcomes: list[DocumentOutcome],
    registry: AwardRegistry,
) -> tuple[list[CompanyAwardDecision], list[str]]:
    resolution: AwardResolution = registry.resolve(project_key)
    errors: list[str] = []
    decisions: list[CompanyAwardDecision] = []

    if resolution.status != MATCHED or resolution.record is None:
        message = resolution.message or "中标结果无法确定"
        errors.append(f"中标结果：{message}")
        decisions = [CompanyAwardDecision(AWARD_REVIEW_REQUIRED) for _ in outcomes]
    else:
        record = resolution.record
        winner_key = normalize_company_name(record.winner_name or "")
        amount = format_award_amount(record.amount)
        for outcome in outcomes:
            company_key = normalize_company_name(outcome.company_name or "")
            if company_key and company_key == winner_key:
                decisions.append(CompanyAwardDecision(AWARD_WINNER, amount or "待核验"))
            else:
                decisions.append(CompanyAwardDecision(AWARD_NOT_WINNER))

        if resolution.message:
            errors.append(f"中标结果：{resolution.message}")
        if outcomes and not any(item.status == AWARD_WINNER for item in decisions):
            errors.append(
                "中标结果：Excel 中标公司“"
                f"{record.winner_name}”不在本项目已识别的投标公司中，"
                "未生成对应的中标企业画像"
            )

    for outcome, decision in zip(outcomes, decisions):
        outcome.award_status = decision.status
        outcome.award_amount = decision.amount if decision.status == AWARD_WINNER else None
    return decisions, errors


def _profile_filename(company_name: str, decision: CompanyAwardDecision) -> str:
    safe_company = _safe_name(company_name)
    if decision.status == AWARD_WINNER:
        suffix = f"（中标公司，金额{decision.amount or '待核验'}）"
    elif decision.status == AWARD_NOT_WINNER:
        suffix = "（未中标）"
    else:
        suffix = "（中标结果待核验）"
    return f"{safe_company}{suffix}.md"


def _is_company_profile_for(
    path: Path,
    *,
    project_key: str,
    company_name: str,
) -> bool:
    metadata = _read_front_matter(path)
    return (
        metadata.get("document_type") == "company_profile"
        and metadata.get("project_key") == project_key
        and isinstance(metadata.get("company_name"), str)
        and normalize_company_name(str(metadata["company_name"]))
        == normalize_company_name(company_name)
    )


def _prepare_profile_output_path(
    *,
    final_project_output: Path,
    project_key: str,
    company_name: str,
    decision: CompanyAwardDecision,
    resume: bool,
    overwrite: bool,
) -> tuple[Path, Path | None]:
    final_path = final_project_output / _profile_filename(company_name, decision)
    if not final_project_output.is_dir():
        return final_path, None

    existing_profiles = [
        path
        for path in final_project_output.glob("*.md")
        if path.is_file()
        and _is_company_profile_for(
            path,
            project_key=project_key,
            company_name=company_name,
        )
    ]
    if len(existing_profiles) > 1:
        names = "、".join(path.name for path in existing_profiles)
        raise ValueError(f"同一公司存在多份最终企业画像：{names}")

    existing_path = existing_profiles[0] if existing_profiles else None
    if final_path.exists() and not _is_company_profile_for(
        final_path,
        project_key=project_key,
        company_name=company_name,
    ):
        raise FileExistsError(f"企业画像目标文件名已被其他文档占用：{final_path.name}")

    if existing_path is not None and existing_path != final_path:
        if final_path.exists():
            raise ValueError(
                f"同一公司存在旧、新两份企业画像："
                f"{existing_path.name}、{final_path.name}"
            )
        if not (resume or overwrite):
            raise FileExistsError(
                f"企业画像已以旧命名存在：{existing_path.name}；"
                "请使用 --resume 或 --overwrite 安全更新中标标记"
            )
    return final_path, existing_path


def _generate_bid_profile(
    *,
    outcome: DocumentOutcome,
    award_decision: CompanyAwardDecision,
    project_key: str,
    intermediate_project_output: Path,
    final_project_output: Path,
    project_work: Path,
    work_root: Path,
    settings: DeepSeekSettings,
    resume: bool,
    overwrite: bool,
    keep_workdir: bool,
) -> None:
    if outcome.status not in USABLE_STATUSES or not outcome.output or not outcome.company_name:
        return
    source_path = intermediate_project_output / outcome.output
    if not source_path.is_file():
        raise ValueError("投标解析 Markdown 中间结果不存在")
    final_path, existing_path = _prepare_profile_output_path(
        final_project_output=final_project_output,
        project_key=project_key,
        company_name=outcome.company_name,
        decision=award_decision,
        resume=resume,
        overwrite=overwrite,
    )
    current_path = existing_path or final_path
    processing_mode = profile_processing_mode(settings.model)
    if resume and current_path.exists() and not _existing_output_matches(
        current_path,
        outcome.source_sha256,
        pipeline_version=BID_PIPELINE_VERSION,
        processing_mode=processing_mode,
    ):
        skipped = False
    else:
        skipped = _guard_output(
            current_path,
            outcome.source_sha256,
            resume=resume,
            overwrite=overwrite,
            pipeline_version=BID_PIPELINE_VERSION,
            processing_mode=processing_mode,
        )
    outcome.summary_model = settings.model
    if skipped:
        if current_path != final_path:
            current_path.replace(final_path)
        outcome.final_status = "SKIPPED"
        outcome.final_output = _relative_output(final_path, final_project_output)
        return

    cache_dir = (
        project_work
        / "deepseek"
        / _safe_name(outcome.company_name)
        / outcome.source_sha256[:10]
    )
    if cache_dir.exists():
        if overwrite:
            _safe_rmtree(cache_dir, work_root)
        elif not resume:
            raise FileExistsError("DeepSeek 分块缓存已存在；请使用 --resume 或 --overwrite")

    result = generate_company_profile(
        source_markdown=source_path.read_text(encoding="utf-8"),
        project_key=project_key,
        company_name=outcome.company_name,
        source_file=outcome.source_file,
        source_sha256=outcome.source_sha256,
        pipeline_version=BID_PIPELINE_VERSION,
        cache_dir=cache_dir,
        settings=settings,
        resume=resume,
    )
    final_project_output.mkdir(parents=True, exist_ok=True)
    # Keep the old label intact if DeepSeek generation or publishing fails.
    atomic_write_text(final_path, result.markdown)
    if existing_path is not None and existing_path != final_path:
        existing_path.unlink()
    outcome.final_status = "SUCCESS"
    outcome.final_output = _relative_output(final_path, final_project_output)
    outcome.summary_model = result.model
    if not keep_workdir:
        _safe_rmtree(cache_dir, work_root)


def _tender_warnings(report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    statuses = report.get("statuses") if isinstance(report.get("statuses"), dict) else {}
    for name in ("MISSING", "CONFLICT", "REVIEW_REQUIRED"):
        count = int(statuses.get(name, 0) or 0)
        if count:
            warnings.append(f"{name} 字段 {count} 项")
    blank_pages = report.get("blank_pages")
    if isinstance(blank_pages, list) and blank_pages:
        warnings.append(f"无文本页：{'、'.join(str(page) for page in blank_pages)}")
    return warnings


def _bid_warnings(
    raw_quality: dict[str, Any] | None,
) -> list[str]:
    warnings: list[str] = []
    if raw_quality is None:
        warnings.append("未生成逐页 Markdown")
    else:
        raw_status = raw_quality.get("status")
        if raw_status != "COMPLETE":
            warnings.append(f"逐页 Markdown 质量状态：{raw_status}")
        summary = raw_quality.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        missing_pages = summary.get("missingPages")
        if isinstance(missing_pages, list) and missing_pages:
            warnings.append(f"逐页 Markdown 缺失 {len(missing_pages)} 页")
        unresolved_ocr_pages = summary.get("unresolvedOcrPages")
        if isinstance(unresolved_ocr_pages, list) and unresolved_ocr_pages:
            warnings.append(f"本地 OCR 后仍未解决 {len(unresolved_ocr_pages)} 页")
        ocr_errors = raw_quality.get("ocrErrors")
        if isinstance(ocr_errors, list) and ocr_errors:
            warnings.append(f"本地 OCR 阶段错误 {len(ocr_errors)} 项")
    return warnings


def process_tender(
    *,
    pdf: Path,
    project_key: str,
    project_output: Path,
    project_work: Path,
    output_root: Path,
    work_root: Path,
    input_root: Path,
    resume: bool,
    overwrite: bool,
    keep_workdir: bool,
) -> DocumentOutcome:
    source_hash = ""
    final_path = project_output / "招标解析.md"
    roots = [SCRIPT_DIR, input_root, output_root, work_root]
    try:
        source_hash = sha256_file(pdf)
        if resume and final_path.exists() and _tender_output_can_refresh(
            final_path,
            source_hash,
            project_key,
        ) and not _existing_output_matches(
            final_path,
            source_hash,
            parser_schema_version=TENDER_SCHEMA_VERSION,
        ):
            skipped = False
        else:
            skipped = _guard_output(
                final_path,
                source_hash,
                resume=resume,
                overwrite=overwrite,
                parser_schema_version=TENDER_SCHEMA_VERSION,
            )
        if skipped:
            metadata = _read_front_matter(final_path)
            return DocumentOutcome(
                document_type="tender",
                source_file=pdf.name,
                source_sha256=source_hash,
                status="SKIPPED",
                output=_relative_output(final_path, project_output),
                parser=str(metadata.get("parser") or "pdf-inspector"),
                parser_schema_version=str(metadata.get("parser_schema_version") or TENDER_SCHEMA_VERSION),
                warnings=["命中相同源文件与流水线版本，已跳过"],
            )

        work_dir = project_work / "tender" / f"{TENDER_SCHEMA_VERSION}__{source_hash[:10]}"
        use_cached = False
        if work_dir.exists():
            if overwrite:
                _safe_rmtree(work_dir, work_root)
            elif resume:
                _load_tender_work(work_dir, source_hash)
                use_cached = True
            else:
                raise FileExistsError("招标工作目录已存在；请使用 --resume 或 --overwrite")

        if not use_cached:
            command = [
                sys.executable,
                str(TENDER_SCRIPT),
                "--pdf",
                str(pdf),
                "--output",
                str(work_dir),
            ]
            completed = _run_child(command, cwd=TENDER_SCRIPT.parent)
            if completed.returncode != 0:
                raise RuntimeError(f"招标解析失败：{_child_error(completed, roots=roots)}")

        extraction, report, raw_markdown = _load_tender_work(work_dir, source_hash)
        generated_at = utc_now_iso()
        markdown, status = render_tender_markdown(
            extraction=extraction,
            report=report,
            raw_markdown=raw_markdown,
            project_key=project_key,
            source_file=pdf.name,
            source_sha256=source_hash,
            generated_at=generated_at,
        )
        atomic_write_text(final_path, markdown)
        warnings = _tender_warnings(report)
        if status in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} and not keep_workdir:
            _safe_rmtree(work_dir, work_root)
        return DocumentOutcome(
            document_type="tender",
            source_file=pdf.name,
            source_sha256=source_hash,
            status=status,
            output=_relative_output(final_path, project_output),
            parser=str(extraction.get("parser") or "pdf-inspector"),
            parser_schema_version=str(extraction.get("schema_version") or TENDER_SCHEMA_VERSION),
            warnings=warnings,
        )
    except Exception as exc:
        error = _sanitize_error(str(exc), roots=roots)
        return DocumentOutcome(
            document_type="tender",
            source_file=pdf.name,
            source_sha256=source_hash,
            status="FAILED",
            parser="pdf-inspector",
            parser_schema_version=TENDER_SCHEMA_VERSION,
            errors=[error],
        )


def _bid_output_path(
    *,
    project_output: Path,
    company_name: str,
    pdf: Path,
    source_hash: str,
    reserved: set[str],
) -> Path:
    company_dir = project_output / "投标解析" / _safe_name(company_name)
    stem = _safe_name(pdf.stem)
    candidate = company_dir / f"{stem}.md"
    key = str(candidate).casefold()
    if key in reserved:
        candidate = company_dir / f"{stem}__{source_hash[:10]}.md"
        key = str(candidate).casefold()
    if key in reserved:
        raise ValueError(f"投标输出文件名冲突：{pdf.name}")
    reserved.add(key)
    return candidate


def process_bid(
    *,
    pdf: Path,
    company_name: str,
    project_key: str,
    project_output: Path,
    project_work: Path,
    reserved_outputs: set[str],
    output_root: Path,
    work_root: Path,
    input_root: Path,
    resume: bool,
    overwrite: bool,
    keep_workdir: bool,
) -> DocumentOutcome:
    source_hash = ""
    roots = [SCRIPT_DIR, input_root, output_root, work_root]
    try:
        source_hash = sha256_file(pdf)
        final_path = _bid_output_path(
            project_output=project_output,
            company_name=company_name,
            pdf=pdf,
            source_hash=source_hash,
            reserved=reserved_outputs,
        )
        if _guard_output(
            final_path,
            source_hash,
            resume=resume,
            overwrite=overwrite,
            pipeline_version=BID_PIPELINE_VERSION,
            processing_mode=BID_PROCESSING_MODE,
        ):
            metadata = _read_front_matter(final_path)
            parser_schema = metadata.get("parser_schema_version")
            return DocumentOutcome(
                document_type="bid",
                source_file=pdf.name,
                source_sha256=source_hash,
                company_name=company_name,
                status="SKIPPED",
                output=_relative_output(final_path, project_output),
                parser=str(metadata.get("parser") or "pdf-inspector-local-ocr"),
                parser_schema_version=str(parser_schema) if parser_schema is not None else None,
                warnings=["命中相同源文件、流水线版本和本地处理模式，已跳过"],
            )

        safe_company = _safe_name(company_name)
        safe_stem = _safe_name(pdf.stem)
        company_work_root = project_work / "bids" / safe_company
        work_dir = company_work_root / f"{safe_stem}__{source_hash[:10]}"
        if work_dir.exists() and overwrite:
            _safe_rmtree(work_dir, work_root)
        elif work_dir.exists() and not resume:
            raise FileExistsError("投标工作目录已存在；请使用 --resume 或 --overwrite")

        raw_markdown: str | None = None
        raw_quality: dict[str, Any] | None = None
        errors: list[str] = []
        warnings: list[str] = []

        if resume and work_dir.exists():
            try:
                raw_markdown, raw_quality = _load_bid_raw(
                    work_dir,
                    source_hash,
                    safe_stem,
                    require_complete=True,
                )
            except (OSError, ValueError, json.JSONDecodeError):
                raw_markdown = None
                raw_quality = None
        if raw_markdown is None:
            command = [
                sys.executable,
                str(BID_MARKDOWN_SCRIPT),
                "--input",
                str(pdf),
                "--output-root",
                str(company_work_root),
            ]
            if overwrite or (resume and work_dir.exists()):
                command.append("--overwrite")
            completed = _run_child(command, cwd=SCRIPT_DIR)
            if completed.returncode in {0, 2}:
                try:
                    raw_markdown, raw_quality = _load_bid_raw(work_dir, source_hash, safe_stem)
                    if completed.returncode == 2:
                        warnings.append("逐页 Markdown 解析返回 FAILED 状态，已保留可用页面")
                except Exception as exc:
                    errors.append(_sanitize_error(f"投标逐页 Markdown 校验失败：{exc}", roots=roots))
            else:
                errors.append(f"投标逐页 Markdown 解析失败：{_child_error(completed, roots=roots)}")

        if raw_markdown is None:
            return DocumentOutcome(
                document_type="bid",
                source_file=pdf.name,
                source_sha256=source_hash,
                company_name=company_name,
                status="FAILED",
                parser="pdf-inspector-local-ocr",
                parser_schema_version=None,
                warnings=warnings,
                errors=errors or ["投标本地逐页解析未生成可用结果"],
            )

        markdown, status = render_bid_markdown(
            raw_markdown=raw_markdown,
            raw_quality=raw_quality,
            project_key=project_key,
            company_name=company_name,
            source_file=pdf.name,
            source_sha256=source_hash,
            generated_at=utc_now_iso(),
            processing_errors=errors,
        )
        atomic_write_text(final_path, markdown)
        warnings.extend(_bid_warnings(raw_quality))
        warnings = list(dict.fromkeys(warnings))
        if status in {"SUCCESS", "SUCCESS_WITH_WARNINGS"} and not keep_workdir:
            _safe_rmtree(work_dir, work_root)
        return DocumentOutcome(
            document_type="bid",
            source_file=pdf.name,
            source_sha256=source_hash,
            company_name=company_name,
            status=status,
            output=_relative_output(final_path, project_output),
            parser="pdf-inspector-local-ocr",
            parser_schema_version=(
                str(raw_quality.get("schemaVersion"))
                if isinstance(raw_quality, dict) and raw_quality.get("schemaVersion") is not None
                else None
            ),
            warnings=warnings,
            errors=errors,
        )
    except Exception as exc:
        error = _sanitize_error(str(exc), roots=roots)
        return DocumentOutcome(
            document_type="bid",
            source_file=pdf.name,
            source_sha256=source_hash,
            company_name=company_name,
            status="FAILED",
            parser="pdf-inspector-local-ocr",
            parser_schema_version=None,
            errors=[error],
        )


def _project_status(tender: DocumentOutcome | None, bids: list[DocumentOutcome], errors: list[str]) -> str:
    documents = ([tender] if tender is not None else []) + bids
    usable = [item for item in documents if item.status in USABLE_STATUSES]
    if not usable:
        return "FAILED"
    if errors or any(item.status not in SUCCESS_STATUSES for item in documents):
        return "PARTIAL"
    return "SUCCESS"


def _workdir_has_files(path: Path) -> bool:
    return path.is_dir() and any(item.is_file() for item in path.rglob("*"))


def _discover_profile_project_dirs(
    intermediate_output_root: Path,
    selected: list[str],
) -> list[Path]:
    if not intermediate_output_root.is_dir():
        raise ValueError("output2 中间输出根目录不存在或不是目录")
    project_dirs: list[Path] = []
    for path in sorted(intermediate_output_root.iterdir(), key=lambda item: item.name.casefold()):
        if path.name.startswith(".") or path.name.startswith("~$"):
            continue
        if path.is_symlink():
            raise ValueError(f"拒绝符号链接项目目录：{path.name}")
        if path.is_dir():
            project_dirs.append(path)

    if not selected:
        return project_dirs
    requested = {value.casefold() for value in selected}
    matched: set[str] = set()
    filtered: list[Path] = []
    for project_dir in project_dirs:
        aliases = {project_dir.name.casefold()}
        for markdown_path in project_dir.rglob("*.md"):
            metadata = _read_front_matter(markdown_path)
            project_key = metadata.get("project_key")
            if isinstance(project_key, str) and project_key.strip():
                aliases.add(project_key.casefold())
            source_file = metadata.get("source_file")
            if isinstance(source_file, str):
                aliases.update(match.group(0).casefold() for match in PROJECT_ID_PATTERN.finditer(source_file))
        hits = requested & aliases
        if hits:
            matched.update(hits)
            filtered.append(project_dir)
    missing = sorted(requested - matched)
    if missing:
        raise ValueError(f"output2 中未找到指定项目：{'、'.join(missing)}")
    return filtered


def _load_profile_bid_outcomes(project_dir: Path) -> tuple[list[DocumentOutcome], list[str]]:
    outcomes: list[DocumentOutcome] = []
    errors: list[str] = []
    companies: dict[str, str] = {}
    for path in sorted(project_dir.rglob("*.md"), key=lambda item: str(item).casefold()):
        if path.is_symlink():
            errors.append(f"拒绝符号链接 Markdown：{path.name}")
            continue
        metadata = _read_front_matter(path)
        if metadata.get("document_type") != "bid":
            continue
        company_name = metadata.get("company_name")
        source_file = metadata.get("source_file")
        source_sha256 = metadata.get("source_sha256")
        status = metadata.get("status")
        if not all(isinstance(value, str) and value.strip() for value in (
            company_name, source_file, source_sha256, status,
        )):
            errors.append(f"投标 Markdown 元数据不完整：{path.name}")
            continue
        assert isinstance(company_name, str)
        assert isinstance(source_file, str)
        assert isinstance(source_sha256, str)
        assert isinstance(status, str)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha256):
            errors.append(f"投标 Markdown 源文件 SHA256 无效：{path.name}")
            continue
        company_key = _safe_name(company_name).casefold()
        previous = companies.get(company_key)
        if previous is not None:
            errors.append(f"同一项目同一公司存在多份投标 Markdown：{previous}、{path.name}")
            continue
        companies[company_key] = path.name
        outcomes.append(DocumentOutcome(
            document_type="bid",
            source_file=source_file,
            source_sha256=source_sha256.lower(),
            company_name=company_name,
            status=status,
            output=_relative_output(path, project_dir),
            parser=str(metadata.get("parser") or "unknown"),
            parser_schema_version=(
                str(metadata["parser_schema_version"])
                if metadata.get("parser_schema_version") is not None
                else None
            ),
        ))
    return outcomes, errors


def process_profiles_only_project(
    project_dir: Path,
    *,
    award_registry: AwardRegistry,
    intermediate_output_root: Path,
    output_root: Path,
    work_root: Path,
    deepseek_settings: DeepSeekSettings,
    resume: bool,
    overwrite: bool,
    keep_workdir: bool,
) -> ProjectOutcome:
    project_key = project_dir.name
    final_project_output = output_root / _safe_name(project_key)
    project_work = work_root / _safe_name(project_key)
    roots = [SCRIPT_DIR, intermediate_output_root, output_root, work_root]
    bid_outcomes, errors = _load_profile_bid_outcomes(project_dir)
    if not bid_outcomes:
        errors.append("output2 项目目录中没有可用的投标 Markdown")
    _ensure_markdown_only(final_project_output)
    award_decisions, award_errors = _company_award_decisions(
        project_key=project_key,
        outcomes=bid_outcomes,
        registry=award_registry,
    )
    errors.extend(award_errors)
    for outcome, award_decision in zip(bid_outcomes, award_decisions):
        try:
            _generate_bid_profile(
                outcome=outcome,
                award_decision=award_decision,
                project_key=project_key,
                intermediate_project_output=project_dir,
                final_project_output=final_project_output,
                project_work=project_work,
                work_root=work_root,
                settings=deepseek_settings,
                resume=resume,
                overwrite=overwrite,
                keep_workdir=keep_workdir,
            )
        except Exception as exc:
            outcome.final_status = "FAILED"
            outcome.summary_model = deepseek_settings.model
            error = _sanitize_error(str(exc), roots=roots)
            outcome.errors.append(error)
            errors.append(f"{outcome.company_name}/{outcome.source_file} 企业画像：{error}")
    status = _project_status(None, bid_outcomes, errors)
    return ProjectOutcome(project_key, status, None, None, bid_outcomes, errors)


def process_project(
    project: ProjectInput,
    *,
    award_registry: AwardRegistry,
    input_root: Path,
    intermediate_output_root: Path,
    output_root: Path,
    work_root: Path,
    deepseek_settings: DeepSeekSettings,
    resume: bool,
    overwrite: bool,
    keep_workdir: bool,
) -> ProjectOutcome:
    project_key = project.project_key
    project_output = intermediate_output_root / _safe_name(project_key)
    final_project_output = output_root / _safe_name(project_key)
    project_work = work_root / _safe_name(project_key)
    report_path = project_output / "处理报告.md"
    errors: list[str] = list(project.errors)
    tender_outcome: DocumentOutcome | None = None
    bid_outcomes: list[DocumentOutcome] = []
    roots = [SCRIPT_DIR, input_root, intermediate_output_root, output_root, work_root]

    try:
        _ensure_markdown_only(project_output)
        _ensure_markdown_only(final_project_output)
        if report_path.exists() and not (resume or overwrite):
            raise FileExistsError("项目处理报告已存在；请使用 --resume 或 --overwrite")

        tender_files = project.tender_files
        tender_input_valid = len(tender_files) == 1
        bid_inputs: list[tuple[str, Path]] = []
        company_outputs: dict[str, str] = {}
        for company_name, pdf in project.bid_inputs:
            safe_company = _safe_name(company_name).casefold()
            previous = company_outputs.get(safe_company)
            if previous is not None:
                errors.append(
                    f"同一项目的公司画像只能对应一份投标 PDF，输出文件名冲突：{previous}、{company_name}"
                )
                continue
            company_outputs[safe_company] = company_name
            bid_inputs.append((company_name, pdf))

        if tender_input_valid:
            tender_outcome = process_tender(
                pdf=tender_files[0],
                project_key=project_key,
                project_output=project_output,
                project_work=project_work,
                output_root=intermediate_output_root,
                work_root=work_root,
                input_root=input_root,
                resume=resume,
                overwrite=overwrite,
                keep_workdir=keep_workdir,
            )
            errors.extend(f"招标文件：{item}" for item in tender_outcome.errors)

            reserved_outputs: set[str] = set()
            for company_name, bid_pdf in bid_inputs:
                outcome = process_bid(
                    pdf=bid_pdf,
                    company_name=company_name,
                    project_key=project_key,
                    project_output=project_output,
                    project_work=project_work,
                    reserved_outputs=reserved_outputs,
                    output_root=intermediate_output_root,
                    work_root=work_root,
                    input_root=input_root,
                    resume=resume,
                    overwrite=overwrite,
                    keep_workdir=keep_workdir,
                )
                bid_outcomes.append(outcome)
                errors.extend(f"{company_name}/{bid_pdf.name}：{item}" for item in outcome.errors)
        else:
            errors.append("同一项目标识必须恰好对应一个招标文件，本项目的投标文件未执行")

        if not bid_inputs:
            errors.append("本项目没有可处理的投标 PDF")

        if tender_outcome is not None:
            try:
                _publish_tender_markdown(
                    outcome=tender_outcome,
                    project_key=project_key,
                    intermediate_project_output=project_output,
                    final_project_output=final_project_output,
                    resume=resume,
                    overwrite=overwrite,
                )
            except Exception as exc:
                tender_outcome.final_status = "FAILED"
                error = _sanitize_error(str(exc), roots=roots)
                tender_outcome.errors.append(error)
                errors.append(f"招标文件最终发布：{error}")

        award_decisions, award_errors = _company_award_decisions(
            project_key=project_key,
            outcomes=bid_outcomes,
            registry=award_registry,
        )
        errors.extend(award_errors)
        for outcome, award_decision in zip(bid_outcomes, award_decisions):
            try:
                _generate_bid_profile(
                    outcome=outcome,
                    award_decision=award_decision,
                    project_key=project_key,
                    intermediate_project_output=project_output,
                    final_project_output=final_project_output,
                    project_work=project_work,
                    work_root=work_root,
                    settings=deepseek_settings,
                    resume=resume,
                    overwrite=overwrite,
                    keep_workdir=keep_workdir,
                )
            except Exception as exc:
                outcome.final_status = "FAILED"
                outcome.summary_model = deepseek_settings.model
                error = _sanitize_error(str(exc), roots=roots)
                outcome.errors.append(error)
                errors.append(f"{outcome.company_name}/{outcome.source_file} 企业画像：{error}")

        status = _project_status(tender_outcome, bid_outcomes, errors)
        generated_at = utc_now_iso()
        workdir_retained = _workdir_has_files(project_work)
        report = render_project_report(
            project_key=project_key,
            generated_at=generated_at,
            status=status,
            tender=tender_outcome.report_value() if tender_outcome else None,
            bids=[item.report_value() for item in bid_outcomes],
            errors=errors,
            workdir_retained=workdir_retained,
        )
        atomic_write_text(report_path, report)
        return ProjectOutcome(project_key, status, report_path, tender_outcome, bid_outcomes, errors)
    except Exception as exc:
        errors.append(_sanitize_error(str(exc), roots=roots))
        return ProjectOutcome(project_key, "FAILED", None, tender_outcome, bid_outcomes, errors)


def run(args: argparse.Namespace) -> tuple[list[ProjectOutcome], int]:
    load_local_env(SCRIPT_DIR / ".env")
    deepseek_settings = DeepSeekSettings.from_env()
    award_file = args.award_file.expanduser()
    if not award_file.is_absolute():
        award_file = (Path.cwd() / award_file).absolute()
    award_registry = load_award_registry(award_file)
    raw_roots = {
        "输入根目录": args.input_root.expanduser(),
        "中间输出根目录": args.intermediate_output_root.expanduser(),
        "最终输出根目录": args.output_root.expanduser(),
        "工作根目录": args.work_root.expanduser(),
    }
    for label, path in raw_roots.items():
        if path.is_symlink():
            raise ValueError(f"{label}不能是符号链接")
    input_root = raw_roots["输入根目录"].resolve()
    intermediate_output_root = raw_roots["中间输出根目录"].resolve()
    output_root = raw_roots["最终输出根目录"].resolve()
    work_root = raw_roots["工作根目录"].resolve()
    _validate_roots(input_root, intermediate_output_root, output_root, work_root)
    _ensure_markdown_only(intermediate_output_root)
    if args.profiles_only:
        project_dirs = _discover_profile_project_dirs(intermediate_output_root, args.project)
        if not project_dirs:
            raise ValueError("output2 中没有可处理的项目子目录")
        output_root.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True)
        outcomes: list[ProjectOutcome] = []
        for project_dir in project_dirs:
            outcome = process_profiles_only_project(
                project_dir,
                award_registry=award_registry,
                intermediate_output_root=intermediate_output_root,
                output_root=output_root,
                work_root=work_root,
                deepseek_settings=deepseek_settings,
                resume=args.resume,
                overwrite=args.overwrite,
                keep_workdir=args.keep_workdir,
            )
            outcomes.append(outcome)
            print(f"[{outcome.status}] {outcome.project_key}")
            for error in outcome.errors[:5]:
                print(f"  - {error}", file=sys.stderr)
            if args.fail_fast and outcome.status != "SUCCESS":
                break
        statuses = [item.status for item in outcomes]
        if statuses and all(status == "SUCCESS" for status in statuses):
            return outcomes, 0
        if any(status in {"SUCCESS", "PARTIAL"} for status in statuses):
            return outcomes, 1
        return outcomes, 2

    projects = _discover_projects(input_root, args.project)
    if not projects:
        raise ValueError("输入根目录中没有可处理的项目子目录")

    safe_keys: dict[str, str] = {}
    for project in projects:
        safe_key = _safe_name(project.project_key).casefold()
        if safe_key in safe_keys:
            raise ValueError(f"项目输出目录名冲突：{safe_keys[safe_key]}、{project.project_key}")
        safe_keys[safe_key] = project.project_key

    intermediate_output_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    work_root.mkdir(parents=True, exist_ok=True)
    outcomes: list[ProjectOutcome] = []
    for project in projects:
        outcome = process_project(
            project,
            award_registry=award_registry,
            input_root=input_root,
            intermediate_output_root=intermediate_output_root,
            output_root=output_root,
            work_root=work_root,
            deepseek_settings=deepseek_settings,
            resume=args.resume,
            overwrite=args.overwrite,
            keep_workdir=args.keep_workdir,
        )
        outcomes.append(outcome)
        print(f"[{outcome.status}] {outcome.project_key}")
        for error in outcome.errors[:5]:
            print(f"  - {error}", file=sys.stderr)
        if args.fail_fast and outcome.status != "SUCCESS":
            break

    statuses = [item.status for item in outcomes]
    if statuses and all(status == "SUCCESS" for status in statuses):
        return outcomes, 0
    if any(status in {"SUCCESS", "PARTIAL"} for status in statuses):
        return outcomes, 1
    return outcomes, 2


def main() -> int:
    args = parse_args()
    try:
        outcomes, exit_code = run(args)
    except KeyboardInterrupt:
        print("任务已中断；源 PDF 未被修改，已生成的工作目录予以保留", file=sys.stderr)
        return 130
    except Exception as exc:
        message, _ = redact_sensitive_text(str(exc))
        print(f"错误：{message}", file=sys.stderr)
        return 2

    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    summary = "；".join(f"{key}={value}" for key, value in sorted(counts.items()))
    print(f"项目处理完成：{summary}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
