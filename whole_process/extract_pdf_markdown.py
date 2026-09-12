"""使用 pdf-inspector 将投标 PDF 全量转换为可追溯 Markdown。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from io_utils import (
    atomic_write_json,
    atomic_write_text,
    sha256_file,
    stable_hash,
    utc_now_iso,
)
from pdf_pipeline import open_reader, parse_page_range, text_readability


SCRIPT_DIR = Path(__file__).resolve().parent
QUALITY_SCHEMA_VERSION = "2.0.0"
DEFAULT_OCR_DPI = 150.0
DEFAULT_RETRY_DPI = 240.0
DEFAULT_QUALITY_THRESHOLD = 0.5
DEFAULT_PDFIUM_PATH = SCRIPT_DIR / "tmp" / "runtime" / "pdfium-native-v7988" / "bin" / "pdfium.dll"
DEFAULT_MODEL_DIRECTORY = (
    SCRIPT_DIR / "tmp" / "runtime" / "model-cache" / "pp-ocrv6-small" / "oar-ocr-v0.7.0"
)
_DLL_DIRECTORY_HANDLES: list[Any] = []


@dataclass(frozen=True)
class PageCandidate:
    page_number: int
    markdown: str
    needs_ocr: bool
    ocr_reason: str | None
    source: str
    ocr_confidence: float | None
    render_dpi: float | None
    hosted_recommended: bool
    warnings: tuple[str, ...]
    model_name: str | None
    model_revision: str | None
    pass_name: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="使用 pdf-inspector 原生文本解析，将投标 PDF 输出为逐页可追溯 Markdown",
    )
    parser.add_argument("--input", required=True, type=Path, help="单个待处理 PDF")
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR / "output", help="输出根目录")
    parser.add_argument("--pdf-password", default=None, help="可选 PDF 密码；不会写入输出或日志")
    parser.add_argument("--page-range", default=None, help="可选物理页范围，例如 1-20,35；默认处理全部页面")
    parser.add_argument("--ocr-dpi", type=float, default=DEFAULT_OCR_DPI, help="首次本地 OCR DPI，默认 150")
    parser.add_argument("--retry-dpi", type=float, default=DEFAULT_RETRY_DPI, help="低质量页重试 DPI，默认 240")
    parser.add_argument(
        "--quality-threshold",
        type=float,
        default=DEFAULT_QUALITY_THRESHOLD,
        help="低于该置信度时进行高 DPI 重试，范围 0..1，默认 0.5",
    )
    parser.add_argument("--no-retry", action="store_true", help="禁用低质量 OCR 页的高 DPI 重试")
    parser.add_argument("--no-ocr", action="store_true", help="只做原生文本解析，不处理扫描页")
    parser.add_argument("--model-directory", type=Path, default=None, help="可选本地 PP-OCRv6 Small 模型目录")
    parser.add_argument("--pdfium-lib", type=Path, default=None, help="可选 PDFium 动态库路径")
    parser.add_argument("--onnxruntime-lib", type=Path, default=None, help="可选 ONNX Runtime 动态库路径")
    parser.add_argument("--overwrite", action="store_true", help="原子覆盖同一源文件的已有 Markdown 和质量报告")
    return parser.parse_args()


def _pdf_inspector() -> Any:
    try:
        import pdf_inspector
    except ImportError as exc:
        raise RuntimeError(
            "缺少 pdf-inspector；请安装 requirements.txt 中的依赖，"
            "或从本地 pdf-inspector 源码构建 Python 包",
        ) from exc
    return pdf_inspector


def _package_version(module: Any) -> str:
    try:
        return importlib.metadata.version("pdf-inspector")
    except importlib.metadata.PackageNotFoundError:
        value = getattr(module, "__version__", None)
        return str(value) if value else "unknown"


def _safe_stem(value: str) -> str:
    normalized = "".join("_" if char in '<>:"/\\|?*' else char for char in value).strip(" .")
    return normalized[:100] or "document"


def _validate_dpi(name: str, value: float) -> None:
    if not math.isfinite(value) or not 72.0 <= value <= 400.0:
        raise ValueError(f"{name} 必须是 72..400 之间的有限数字")


def _validate_ratio(name: str, value: float) -> None:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} 必须是 0..1 之间的有限数字")


def _resolve_runtime_path(explicit: Path | None, environment_name: str, fallback: Path) -> Path | None:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"{environment_name} 指定的动态库不存在：{path.name}")
        return path
    configured = os.environ.get(environment_name, "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"环境变量 {environment_name} 指向的动态库不存在：{path.name}")
        return path
    return fallback.resolve() if fallback.is_file() else None


def _default_onnxruntime_path() -> Path:
    if sys.platform == "win32":
        return Path(sys.prefix) / "Lib" / "site-packages" / "onnxruntime" / "capi" / "onnxruntime.dll"
    if sys.platform == "darwin":
        return Path(sys.prefix) / "lib" / "python3" / "site-packages" / "onnxruntime" / "capi" / "libonnxruntime.dylib"
    return Path(sys.prefix) / "lib" / "python3" / "site-packages" / "onnxruntime" / "capi" / "libonnxruntime.so"


def _configure_local_ocr(args: argparse.Namespace) -> tuple[Path | None, Path | None, Path | None]:
    if args.no_ocr:
        return None, None, None
    pdfium_path = _resolve_runtime_path(args.pdfium_lib, "PDFIUM_LIB_PATH", DEFAULT_PDFIUM_PATH)
    onnxruntime_path = _resolve_runtime_path(
        args.onnxruntime_lib,
        "ORT_DYLIB_PATH",
        _default_onnxruntime_path(),
    )
    model_directory = (
        args.model_directory.expanduser().resolve()
        if args.model_directory is not None
        else DEFAULT_MODEL_DIRECTORY.resolve()
    )
    if not model_directory.is_dir():
        model_directory = None
    if pdfium_path is not None:
        os.environ["PDFIUM_LIB_PATH"] = str(pdfium_path)
    if onnxruntime_path is not None:
        os.environ["ORT_DYLIB_PATH"] = str(onnxruntime_path)
    if sys.platform == "win32":
        library_dirs = [str(path.parent) for path in (pdfium_path, onnxruntime_path) if path is not None]
        if library_dirs:
            current_path = os.environ.get("PATH", "")
            os.environ["PATH"] = os.pathsep.join([*library_dirs, current_path])
            if hasattr(os, "add_dll_directory"):
                for library_dir in library_dirs:
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(library_dir))
    return pdfium_path, onnxruntime_path, model_directory


def _safe_error_message(exc: Exception, secret: str | None = None) -> str:
    message = str(exc)
    if secret:
        message = message.replace(secret, "[REDACTED]")
    return message[:500]


def _optional_finite_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        return result if math.isfinite(result) else None
    return None


def _native_page_candidate(page: Any) -> PageCandidate:
    page_number = int(getattr(page, "page")) + 1
    raw_reason = getattr(page, "ocr_reason", None)
    return PageCandidate(
        page_number=page_number,
        markdown=str(getattr(page, "markdown", "") or ""),
        needs_ocr=bool(getattr(page, "needs_ocr", False)),
        ocr_reason=str(raw_reason)[:500] if raw_reason else None,
        source="native",
        ocr_confidence=None,
        render_dpi=None,
        hosted_recommended=False,
        warnings=(),
        model_name=None,
        model_revision=None,
        pass_name="native",
    )


def _ocr_page_candidate(page: Any, *, pass_name: str) -> PageCandidate:
    page_number = int(getattr(page, "page_number"))
    provenance = getattr(page, "provenance", None)
    model = getattr(provenance, "ocr_model", None)
    markdown = str(getattr(page, "markdown", "") or "")
    return PageCandidate(
        page_number=page_number,
        markdown=markdown,
        needs_ocr=not bool(markdown.strip()),
        ocr_reason="local_ocr_empty" if not markdown.strip() else None,
        source=str(getattr(provenance, "source", "ocr") or "ocr"),
        ocr_confidence=_optional_finite_float(getattr(provenance, "ocr_confidence", None)),
        render_dpi=_optional_finite_float(getattr(provenance, "render_dpi", None)),
        hosted_recommended=bool(getattr(provenance, "hosted_recommended", False)),
        warnings=tuple(str(item)[:500] for item in (getattr(provenance, "warnings", []) or [])),
        model_name=str(getattr(model, "name", "") or "") or None,
        model_revision=str(getattr(model, "revision", "") or "") or None,
        pass_name=pass_name,
    )


def _candidate_rank(candidate: PageCandidate) -> tuple[int, int, int, int, float, int]:
    markdown = candidate.markdown.strip()
    readability = text_readability(markdown)
    return (
        int(bool(markdown)),
        int(not candidate.needs_ocr),
        int(not candidate.hosted_recommended),
        int(readability["textStatus"] == "READABLE"),
        candidate.ocr_confidence if candidate.ocr_confidence is not None else -1.0,
        len(markdown),
    )


def _best_candidate(*candidates: PageCandidate | None) -> PageCandidate | None:
    available = [candidate for candidate in candidates if candidate is not None]
    return max(available, key=_candidate_rank) if available else None


def _assemble_markdown(page_numbers: list[int], pages: dict[int, PageCandidate]) -> str:
    sections: list[str] = []
    for page_number in page_numbers:
        candidate = pages.get(page_number)
        markdown = candidate.markdown.strip() if candidate is not None else ""
        section = f"<!-- PDF page {page_number} -->"
        if markdown:
            section = f"{section}\n\n{markdown}"
        elif candidate is None:
            section = f"{section}\n\n> pdf-inspector 未返回该页结果。"
        elif candidate.needs_ocr:
            section = f"{section}\n\n> 本页缺少可靠文本层，需要 OCR；原生文本模式未生成正文。"
        sections.append(section)
    return "\n\n\n".join(sections) + "\n"


def _page_quality(
    page_number: int,
    selected: PageCandidate | None,
    *,
    native: PageCandidate | None,
    initial_ocr: PageCandidate | None,
    retry_ocr: PageCandidate | None,
    ocr_requested: set[int],
    ocr_attempted: set[int],
    table_pages: set[int],
    column_pages: set[int],
) -> dict[str, Any]:
    if selected is None:
        return {
            "physicalPage": page_number,
            "status": "MISSING_RESULT",
            "contentSource": "missing",
            "textLength": 0,
            "textStatus": "EXTRACTION_FAILED",
            "needsOcr": page_number in ocr_requested,
            "ocrReason": None,
            "ocrAttempted": page_number in ocr_attempted,
            "ocrCompleted": False,
            "hasTable": page_number in table_pages,
            "hasColumns": page_number in column_pages,
            "warnings": ["pdf-inspector 未返回该页结果"],
        }

    readability = text_readability(selected.markdown)
    attempted = page_number in ocr_attempted
    ocr_completed = any(
        candidate is not None
        and candidate.source in {"ocr", "fused"}
        and bool(candidate.markdown.strip())
        for candidate in (initial_ocr, retry_ocr)
    )
    needs_ocr = page_number in ocr_requested or bool(native.needs_ocr if native is not None else False)
    if needs_ocr and not ocr_completed:
        status = "OCR_FAILED" if attempted else "OCR_REQUIRED"
    elif not selected.markdown.strip():
        status = "EMPTY"
    elif selected.hosted_recommended:
        status = "LOW_CONFIDENCE"
    else:
        status = "AVAILABLE"

    warnings = list(selected.warnings)
    if status == "OCR_FAILED":
        warnings.append("该页需要 OCR，但本地 OCR 未生成结果")
    elif status == "OCR_REQUIRED":
        warnings.append("该页需要 OCR，但本次未执行本地 OCR")
    if retry_ocr is not None and selected.pass_name != "retry":
        warnings.append("高 DPI 重试未优于已有结果，保留质量更高的版本")
    return {
        "physicalPage": page_number,
        "status": status,
        "contentSource": selected.source,
        "selectedPass": selected.pass_name,
        "textLength": len(selected.markdown),
        "textStatus": readability["textStatus"],
        "readableRatio": readability["readableRatio"],
        "garbledRatio": readability["garbledRatio"],
        "needsOcr": needs_ocr,
        "ocrReason": native.ocr_reason if native is not None else None,
        "ocrAttempted": attempted,
        "ocrCompleted": ocr_completed,
        "ocrConfidence": selected.ocr_confidence,
        "renderDpi": selected.render_dpi,
        "hostedRecommended": selected.hosted_recommended,
        "hasTable": page_number in table_pages,
        "hasColumns": page_number in column_pages,
        "ocrModel": (
            {"name": selected.model_name, "revision": selected.model_revision}
            if selected.model_name or selected.model_revision
            else None
        ),
        "warnings": warnings,
    }


def run(args: argparse.Namespace) -> tuple[Path, Path, str]:
    input_path = args.input.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    if not input_path.is_file() or input_path.suffix.lower() != ".pdf":
        raise ValueError("--input 必须是存在的 PDF 文件")
    _validate_dpi("--ocr-dpi", args.ocr_dpi)
    _validate_dpi("--retry-dpi", args.retry_dpi)
    _validate_ratio("--quality-threshold", args.quality_threshold)
    if not args.no_retry and args.retry_dpi <= args.ocr_dpi:
        raise ValueError("启用 OCR 重试时，--retry-dpi 必须大于 --ocr-dpi")

    reader = open_reader(input_path, args.pdf_password)
    total_pages = len(reader.pages)
    selected_pages = parse_page_range(args.page_range, total_pages)
    page_numbers = (
        sorted(selected_pages)
        if selected_pages is not None
        else list(range(1, total_pages + 1))
    )
    page_number_set = set(page_numbers)
    source_hash = sha256_file(input_path)
    safe_stem = _safe_stem(input_path.stem)
    range_suffix = f"__pages_{stable_hash(args.page_range, length=8)}" if args.page_range else ""
    job_dir = output_root / f"{safe_stem}__{source_hash[:10]}{range_suffix}"
    markdown_path = job_dir / f"{safe_stem}.md"
    quality_path = job_dir / "page_quality.json"
    if not args.overwrite and (markdown_path.exists() or quality_path.exists()):
        raise ValueError(f"输出已存在：{job_dir}；如需原子覆盖请使用 --overwrite")

    pdfium_path, onnxruntime_path, model_directory = _configure_local_ocr(args)
    module = _pdf_inspector()
    native_result = module.extract_pages_markdown(
        str(input_path),
        pages=[page - 1 for page in page_numbers] if selected_pages is not None else None,
    )
    native_candidates = {
        candidate.page_number: candidate
        for candidate in (_native_page_candidate(page) for page in native_result.pages)
    }
    native_ocr_pages = {
        page_number
        for page_number, candidate in native_candidates.items()
        if candidate.needs_ocr and page_number in page_number_set
    }
    ocr_requested = set(native_ocr_pages)
    initial_ocr_candidates: dict[int, PageCandidate] = {}
    retry_ocr_candidates: dict[int, PageCandidate] = {}
    ocr_attempted: set[int] = set()
    retry_requested: set[int] = set()
    ocr_errors: list[dict[str, str]] = []
    ocr_table_pages: set[int] = set()
    ocr_column_pages: set[int] = set()

    if not args.no_ocr:
        try:
            ocr_result = module.process_pdf_with_ocr(
                str(input_path),
                mode="auto",
                page_numbers=page_numbers if selected_pages is not None else None,
                password=args.pdf_password,
                dpi=args.ocr_dpi,
                minimum_confidence=0.0,
                hosted_recommendation_confidence=args.quality_threshold,
                model_directory=str(model_directory) if model_directory is not None else None,
                offline=model_directory is not None,
            )
            initial_ocr_candidates = {
                candidate.page_number: candidate
                for candidate in (
                    _ocr_page_candidate(page, pass_name="initial")
                    for page in ocr_result.pages
                )
            }
            ocr_requested = {
                int(value)
                for value in ocr_result.pages_routed_to_ocr
                if int(value) in page_number_set
            }
            ocr_attempted.update(ocr_requested)
            ocr_table_pages.update(int(value) for value in ocr_result.pages_with_tables)
            ocr_column_pages.update(int(value) for value in ocr_result.pages_with_columns)
            retry_requested = {
                page_number
                for page_number, candidate in initial_ocr_candidates.items()
                if candidate.hosted_recommended and page_number in page_number_set
            }
        except Exception as exc:
            ocr_attempted.update(native_ocr_pages)
            ocr_errors.append(
                {
                    "stage": "initial_ocr",
                    "errorType": type(exc).__name__,
                    "message": _safe_error_message(exc, args.pdf_password),
                }
            )

    if retry_requested and not args.no_retry:
        try:
            retry_result = module.process_pdf_with_ocr(
                str(input_path),
                mode="force",
                page_numbers=sorted(retry_requested),
                password=args.pdf_password,
                dpi=args.retry_dpi,
                minimum_confidence=0.0,
                hosted_recommendation_confidence=args.quality_threshold,
                model_directory=str(model_directory) if model_directory is not None else None,
                offline=model_directory is not None,
            )
            retry_ocr_candidates = {
                candidate.page_number: candidate
                for candidate in (
                    _ocr_page_candidate(page, pass_name="retry")
                    for page in retry_result.pages
                )
            }
            ocr_table_pages.update(int(value) for value in retry_result.pages_with_tables)
            ocr_column_pages.update(int(value) for value in retry_result.pages_with_columns)
        except Exception as exc:
            ocr_errors.append(
                {
                    "stage": "retry_ocr",
                    "errorType": type(exc).__name__,
                    "message": _safe_error_message(exc, args.pdf_password),
                }
            )

    selected_candidates = {
        page_number: selected
        for page_number in page_numbers
        if (
            selected := (
                _best_candidate(
                    initial_ocr_candidates.get(page_number),
                    retry_ocr_candidates.get(page_number),
                )
                if initial_ocr_candidates
                else native_candidates.get(page_number)
            )
        ) is not None
    }
    table_pages = {
        int(value)
        for value in native_result.pages_with_tables
        if int(value) in page_number_set
    } | (ocr_table_pages & page_number_set)
    column_pages = {
        int(value)
        for value in native_result.pages_with_columns
        if int(value) in page_number_set
    } | (ocr_column_pages & page_number_set)

    page_quality = [
        _page_quality(
            page_number,
            selected_candidates.get(page_number),
            native=native_candidates.get(page_number),
            initial_ocr=initial_ocr_candidates.get(page_number),
            retry_ocr=retry_ocr_candidates.get(page_number),
            ocr_requested=ocr_requested,
            ocr_attempted=ocr_attempted,
            table_pages=table_pages,
            column_pages=column_pages,
        )
        for page_number in page_numbers
    ]
    counts: dict[str, int] = {}
    for item in page_quality:
        status = str(item["status"])
        counts[status] = counts.get(status, 0) + 1
    unresolved_ocr_pages = sorted(
        int(item["physicalPage"])
        for item in page_quality
        if item["status"] in {"OCR_REQUIRED", "OCR_FAILED", "LOW_CONFIDENCE"}
    )
    missing_pages = sorted(
        int(item["physicalPage"])
        for item in page_quality
        if item["status"] == "MISSING_RESULT"
    )
    available_pages = sum(item["status"] in {"AVAILABLE", "LOW_CONFIDENCE"} for item in page_quality)
    if available_pages == 0 and (missing_pages or unresolved_ocr_pages):
        overall_status = "FAILED"
    elif missing_pages or unresolved_ocr_pages or ocr_errors:
        overall_status = "COMPLETE_WITH_WARNINGS"
    else:
        overall_status = "COMPLETE"

    markdown = _assemble_markdown(page_numbers, selected_candidates)
    report = {
        "schemaVersion": QUALITY_SCHEMA_VERSION,
        "status": overall_status,
        "generatedAt": utc_now_iso(),
        "sourceDocument": {
            "fileName": input_path.name,
            "sha256": source_hash,
            "sizeBytes": input_path.stat().st_size,
            "totalPages": total_pages,
            "encrypted": bool(reader.is_encrypted),
        },
        "parser": {
            "name": "pdf-inspector",
            "version": _package_version(module),
            "mode": "auto_then_force_retry",
        },
        "outputArtifact": {
            "fileName": markdown_path.name,
            "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "sizeBytes": len(markdown.encode("utf-8")),
            "pageMarker": "<!-- PDF page N -->",
        },
        "configuration": {
            "pageRange": args.page_range,
            "ocrEnabled": not args.no_ocr,
            "ocrDpi": args.ocr_dpi,
            "retryDpi": args.retry_dpi,
            "qualityThreshold": args.quality_threshold,
            "retryEnabled": not args.no_retry,
            "pdfiumConfigured": pdfium_path is not None,
            "onnxRuntimeConfigured": onnxruntime_path is not None,
            "modelDirectoryConfigured": model_directory is not None,
            "offline": model_directory is not None,
        },
        "summary": {
            "expectedPages": len(page_numbers),
            "pageMarkersWritten": len(page_numbers),
            "pagesWithParserResult": len(page_numbers) - len(missing_pages),
            "statusCounts": counts,
            "pagesNeedingOcr": sorted(ocr_requested),
            "ocrAttemptedPages": sorted(ocr_attempted),
            "ocrCompletedPages": sorted(
                int(item["physicalPage"])
                for item in page_quality
                if item.get("ocrCompleted") is True
            ),
            "retryRequestedPages": sorted(retry_requested),
            "retryCompletedPages": sorted(retry_ocr_candidates),
            "unresolvedOcrPages": unresolved_ocr_pages,
            "emptyPages": sorted(
                int(item["physicalPage"])
                for item in page_quality
                if item["status"] == "EMPTY"
            ),
            "missingPages": missing_pages,
            "tablePages": sorted(table_pages),
            "columnPages": sorted(column_pages),
        },
        "ocrErrors": ocr_errors,
        "pages": page_quality,
    }

    atomic_write_text(markdown_path, markdown)
    atomic_write_json(quality_path, report)
    print(f"转换状态：{overall_status}；输出页数：{len(page_numbers)}", file=sys.stderr)
    if ocr_requested:
        print(
            "本地 OCR："
            f"请求 {len(ocr_requested)} 页，"
            f"完成 {sum(item.get('ocrCompleted') is True for item in page_quality)} 页，"
            f"仍需处理 {len(unresolved_ocr_pages)} 页",
            file=sys.stderr,
        )
    print(markdown_path)
    print(quality_path)
    return markdown_path, quality_path, overall_status


def main() -> int:
    args: argparse.Namespace | None = None
    try:
        args = parse_args()
        _, _, status = run(args)
        return 2 if status == "FAILED" else 0
    except KeyboardInterrupt:
        print("任务已中断；原 PDF 未被修改", file=sys.stderr)
        return 130
    except Exception as exc:
        password = args.pdf_password if args is not None else None
        print(f"错误：{_safe_error_message(exc, password)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
