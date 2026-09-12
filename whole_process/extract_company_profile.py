"""投标 PDF 全量抽取入口：输出新 Company 完整初始化候选 JSON，不写数据库。"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any

from capability_derivation import derive_capability_assessments, derive_capability_evidence
from config import SCRIPT_DIR, Settings, load_local_env
from creation_package_mapper import SCHEMA_VERSION, build_creation_package
from domain_extractors import extract_domains
from io_utils import StateStore, atomic_write_json, read_json, sha256_file, stable_hash, utc_now_iso
from merger import annotate_conflict_source_strengths, merge_domain_payloads
from normalizers import redact_sensitive_text, remove_person_names, sanitize_sensitive_output
from page_classifier import apply_local_classification, domain_pages, refine_ambiguous_pages
from pdf_pipeline import (
    contiguous_ranges,
    infer_printed_page,
    ocr_page_texts,
    open_reader,
    parse_page_range,
    preflight_pdf,
    render_page_image,
    text_readability,
    write_page_content_cache,
    write_pdf_chunk,
)
from profile_derivation import CURRENT_EMPLOYMENT_FRESHNESS_DAYS, derive_profile_fields, enforce_current_employment_policy
from prompts import PROMPT_VERSION
from validators import validate_creation_package
from visual_reviewer import review_failed_page
from zhipu_client import ZhipuApiError, ZhipuClient


EXTRACTOR_VERSION = "1.1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从企业投标 PDF 生成新 Company 的完整画像初始化候选 JSON")
    parser.add_argument("--input", required=True, type=Path, help="单个待处理 PDF")
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR / "output", help="输出根目录")
    parser.add_argument("--company-name", default=None, help="可选，仅用于主体一致性核验，不作为文档证据")
    parser.add_argument("--pdf-password", default=None, help="可选，仅用于打开加密 PDF；不会写入输出和日志")
    parser.add_argument("--as-of-date", default=None, help="派生近三年指标的基准日，格式 YYYY-MM-DD，默认今天")
    parser.add_argument("--resume", action="store_true", help="复用相同版本的成功中间结果")
    parser.add_argument("--overwrite", action="store_true", help="忽略成功缓存并重新处理；不删除原 PDF")
    parser.add_argument("--page-range", default=None, help="仅处理指定物理页，例如 1-20,35；结果禁止发布")
    parser.add_argument("--max-concurrency", type=int, default=None, help="并发上限 1..8")
    name_group = parser.add_mutually_exclusive_group()
    name_group.add_argument("--include-person-names", dest="include_person_names", action="store_true", default=None, help="最终结构化实体保留人员姓名")
    name_group.add_argument("--exclude-person-names", dest="include_person_names", action="store_false", help="最终结构化实体移除人员姓名")
    parser.add_argument("--no-model-classifier", action="store_true", help="只使用本地页面分类规则")
    parser.add_argument("--no-vision-fallback", action="store_true", help="OCR 失败后不使用视觉模型复核")
    parser.add_argument("--ocr-only", action="store_true", help="仅对 --page-range 指定页执行 OCR 诊断，不做视觉、分类和画像抽取")
    parser.add_argument("--dry-run", action="store_true", help="只做 PDF 预检和本地页面分类，不调用智谱 API")
    return parser.parse_args()


def _safe_stem(value: str) -> str:
    normalized = "".join("_" if char in '<>:"/\\|?*' else char for char in value).strip(" .")
    return normalized[:100] or "document"


def _as_of_date(value: str | None) -> date:
    if not value:
        return date.today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("--as-of-date 必须为 YYYY-MM-DD") from exc


def _prepare_ocr_jobs(
    source_pdf: Path,
    ranges: list[tuple[int, int]],
    *,
    temp_dir: Path,
    max_file_bytes: int,
    password: str | None,
    render_dpi: int,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []

    def prepare(start_page: int, end_page: int) -> None:
        chunk_path = temp_dir / f"chunk_{start_page:05d}_{end_page:05d}.pdf"
        write_pdf_chunk(source_pdf, chunk_path, start_page=start_page, end_page=end_page, password=password)
        if chunk_path.stat().st_size <= max_file_bytes:
            jobs.append({"startPage": start_page, "endPage": end_page, "path": chunk_path, "kind": "PDF"})
            return
        if start_page < end_page:
            if chunk_path.is_file():
                chunk_path.unlink()
            midpoint = (start_page + end_page) // 2
            prepare(start_page, midpoint)
            prepare(midpoint + 1, end_page)
            return
        image_path = temp_dir / f"page_{start_page:05d}.jpg"
        keep_image = False
        try:
            # 单页 PDF 块已经完成解密；从该块第 1 页渲染可避免再次处理原 PDF 的密码。
            for candidate_dpi in dict.fromkeys([render_dpi, min(render_dpi, 150), 120, 96]):
                render_page_image(chunk_path, image_path, physical_page=1, dpi=candidate_dpi)
                if image_path.stat().st_size <= 10_000_000:
                    jobs.append({"startPage": start_page, "endPage": end_page, "path": image_path, "kind": "IMAGE"})
                    keep_image = True
                    return
            raise RuntimeError(f"第 {start_page} 页渲染图片仍超过 GLM-OCR 10 MB 限制")
        finally:
            if chunk_path.is_file():
                chunk_path.unlink()
            if not keep_image and image_path.is_file():
                image_path.unlink()

    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        for start_page, end_page in ranges:
            prepare(start_page, end_page)
    except Exception:
        for job in jobs:
            path = Path(job["path"])
            if path.is_file():
                path.unlink()
        raise
    return jobs


def _run_ocr_job(
    job: dict[str, Any],
    *,
    client: ZhipuClient,
    cache_dir: Path,
    overwrite: bool,
) -> dict[str, Any]:
    start_page, end_page = int(job["startPage"]), int(job["endPage"])
    cache_key = stable_hash(
        client.settings.ocr_model,
        SCHEMA_VERSION,
        EXTRACTOR_VERSION,
        job["kind"],
        job["path"].stat().st_size,
        length=16,
    )
    cache_path = cache_dir / f"chunk_{start_page:05d}_{end_page:05d}_{cache_key}.json"
    cached = read_json(cache_path)
    if isinstance(cached, dict) and not overwrite:
        return {**cached, "cached": True}
    request_id = f"ocr_{start_page}_{end_page}_{cache_key}"
    response = client.layout_parse(job["path"], request_id=request_id)
    texts = ocr_page_texts(response, end_page - start_page + 1)
    result = {
        "startPage": start_page,
        "endPage": end_page,
        "kind": job["kind"],
        "requestId": response.get("request_id") or request_id,
        "model": response.get("model") or client.settings.ocr_model,
        "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
        "pageTexts": [
            {"physicalPage": start_page + offset, "text": text}
            for offset, text in enumerate(texts)
        ],
        "layoutDetails": response.get("layout_details") if isinstance(response.get("layout_details"), list) else [],
        "markdown": response.get("md_results") if isinstance(response.get("md_results"), str) else None,
        "cached": False,
    }
    atomic_write_json(cache_path, result)
    return result


def _apply_ocr(
    source_pdf: Path,
    page_manifest: list[dict[str, Any]],
    page_texts: dict[int, str],
    *,
    client: ZhipuClient,
    job_dir: Path,
    settings: Settings,
    password: str | None,
    overwrite: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    ocr_pages = [int(item["physicalPage"]) for item in page_manifest if item.get("needsOcr")]
    ranges = contiguous_ranges(ocr_pages, settings.ocr_chunk_pages)
    temp_dir = job_dir / "tmp" / "pdfs"
    cache_dir = job_dir / "ocr"
    jobs = _prepare_ocr_jobs(
        source_pdf,
        ranges,
        temp_dir=temp_dir,
        max_file_bytes=settings.ocr_max_file_bytes,
        password=password,
        render_dpi=settings.render_dpi,
    )
    manifest_by_page = {int(item["physicalPage"]): item for item in page_manifest}
    usage: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=settings.max_concurrency) as executor:
            futures = {
                executor.submit(_run_ocr_job, job, client=client, cache_dir=cache_dir, overwrite=overwrite): job
                for job in jobs
            }
            try:
                for future in as_completed(futures):
                    job = futures[future]
                    try:
                        results.append(future.result())
                    except ZhipuApiError as exc:
                        errors.append({
                            "stage": "OCR",
                            "pages": [job["startPage"], job["endPage"]],
                            "errorType": type(exc).__name__,
                            "message": str(exc)[:500],
                        })
                        # 认证、余额、模型或请求格式错误不会因换一批页面而自行恢复。
                        if exc.fatal or not exc.retriable:
                            for pending in futures:
                                if pending is not future:
                                    pending.cancel()
                            raise
                    except Exception as exc:
                        errors.append({
                            "stage": "OCR",
                            "pages": [job["startPage"], job["endPage"]],
                            "errorType": type(exc).__name__,
                            "message": str(exc)[:500],
                        })
            except ZhipuApiError:
                # ThreadPoolExecutor 退出时只等待已经开始的少量任务；已排队任务已取消。
                raise
        for result in sorted(results, key=lambda item: int(item["startPage"])):
            usage.append({
                "stage": "OCR",
                "pages": [result["startPage"], result["endPage"]],
                "requestId": result.get("requestId"),
                "usage": result.get("usage", {}),
                "cached": result.get("cached") is True,
            })
            for row in result.get("pageTexts", []):
                page = int(row["physicalPage"])
                text = str(row.get("text") or "")
                if text.strip():
                    page_texts[page] = text
                    readability = text_readability(text)
                    manifest_by_page[page].update(readability)
                    if readability["textStatus"] == "READABLE":
                        manifest_by_page[page]["needsOcr"] = False
                        manifest_by_page[page]["contentSource"] = "GLM_OCR"
                    else:
                        manifest_by_page[page]["needsOcr"] = True
                        manifest_by_page[page]["contentSource"] = "OCR_LOW_QUALITY"
                        manifest_by_page[page].setdefault("warnings", []).append("GLM-OCR 返回文本仍未通过本地可读性检查")
                else:
                    manifest_by_page[page]["contentSource"] = "OCR_FAILED"
                    manifest_by_page[page].setdefault("warnings", []).append("GLM-OCR 未返回该页文本")
        completed_pages = {
            int(row["physicalPage"])
            for result in results
            for row in result.get("pageTexts", [])
            if str(row.get("text") or "").strip()
        }
        for page in ocr_pages:
            if page not in completed_pages:
                manifest_by_page[page]["contentSource"] = "OCR_FAILED"
        return usage, errors, jobs
    finally:
        for job in jobs:
            path = Path(job["path"])
            if path.is_file():
                path.unlink()


def _vision_fallback(
    source_pdf: Path,
    page_manifest: list[dict[str, Any]],
    page_texts: dict[int, str],
    *,
    client: ZhipuClient,
    job_dir: Path,
    settings: Settings,
    password: str | None,
    overwrite: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    usage: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    sensitive: list[dict[str, Any]] = []
    failed = [item for item in page_manifest if item.get("contentSource") in {"OCR_FAILED", "OCR_LOW_QUALITY"}]
    for item in failed:
        page = int(item["physicalPage"])
        review_source = source_pdf
        decrypted_page: Path | None = None
        try:
            if password:
                decrypted_page = job_dir / "tmp" / "pdfs" / f"vision_source_{page:05d}.pdf"
                write_pdf_chunk(source_pdf, decrypted_page, start_page=page, end_page=page, password=password)
                review_source = decrypted_page
            result = review_failed_page(
                review_source,
                physical_page=page,
                client=client,
                work_dir=job_dir / "raw" / "vision",
                dpi=settings.render_dpi,
                render_page=1 if decrypted_page else page,
                overwrite=overwrite,
            )
            if result.get("rawText"):
                page_texts[page] = str(result["rawText"])
                item.update(text_readability(page_texts[page]))
                item["needsOcr"] = False
                item["contentSource"] = "GLM_VISION"
                item["visualReviewed"] = True
            sensitive.extend({"physicalPage": page, **finding} for finding in result.get("sensitiveFindings", []))
            usage.append({
                "stage": "VISION",
                "pages": [page, page],
                "requestId": result.get("requestId"),
                "usage": result.get("usage", {}),
                "cached": result.get("cached") is True,
            })
        except ZhipuApiError as exc:
            if exc.fatal or not exc.retriable:
                raise
            item["contentSource"] = "UNAVAILABLE"
            item.setdefault("warnings", []).append("视觉复核 API 失败")
            errors.append({"stage": "VISION", "pages": [page, page], "errorType": type(exc).__name__, "message": str(exc)[:500]})
        except Exception as exc:
            item["contentSource"] = "UNAVAILABLE"
            item.setdefault("warnings", []).append(f"视觉复核失败：{type(exc).__name__}")
            errors.append({"stage": "VISION", "pages": [page, page], "errorType": type(exc).__name__, "message": str(exc)[:500]})
        finally:
            if decrypted_page and decrypted_page.is_file():
                decrypted_page.unlink()
    return usage, errors, sensitive


def _sensitive_summary(page_texts: dict[int, str]) -> list[dict[str, Any]]:
    counts: Counter[tuple[int, str]] = Counter()
    for page, text in page_texts.items():
        _, findings = redact_sensitive_text(text)
        for finding in findings:
            counts[(page, str(finding["type"]))] += 1
    return [
        {"physicalPage": page, "type": kind, "count": count, "action": "SENSITIVE_EXCLUDE"}
        for (page, kind), count in sorted(counts.items())
    ]


def _merge_sensitive_findings(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[tuple[int, str]] = Counter()
    for group in groups:
        for item in group:
            page = int(item.get("physicalPage") or 0)
            kind = str(item.get("type") or "")
            if page > 0 and kind:
                counts[(page, kind)] += max(1, int(item.get("count") or 1))
    return [
        {"physicalPage": page, "type": kind, "count": count, "action": "SENSITIVE_EXCLUDE"}
        for (page, kind), count in sorted(counts.items())
    ]


def _reclassify_recovered_ocr_errors(
    errors: list[dict[str, Any]],
    page_manifest: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_page = {int(item["physicalPage"]): item for item in page_manifest}
    unresolved: list[dict[str, Any]] = []
    recovered: list[dict[str, Any]] = []
    for error in errors:
        if error.get("stage") != "OCR":
            unresolved.append(error)
            continue
        bounds = error.get("pages") if isinstance(error.get("pages"), list) else []
        if len(bounds) != 2:
            unresolved.append(error)
            continue
        pages = range(int(bounds[0]), int(bounds[1]) + 1)
        if all(by_page.get(page, {}).get("contentSource") not in {"OCR_FAILED", "OCR_LOW_QUALITY", "UNAVAILABLE"} for page in pages):
            recovered.append({**error, "stage": "OCR_RECOVERED_BY_VISION"})
        else:
            unresolved.append(error)
    return unresolved, recovered


def run(args: argparse.Namespace) -> Path | None:
    input_path = args.input.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    if not input_path.is_file() or input_path.suffix.lower() != ".pdf":
        raise ValueError("--input 必须是存在的 PDF 文件")
    as_of = _as_of_date(args.as_of_date)
    source_hash = sha256_file(input_path)
    reader = open_reader(input_path, args.pdf_password)
    selected_pages = parse_page_range(args.page_range, len(reader.pages))
    if args.ocr_only and selected_pages is None:
        raise ValueError("--ocr-only 必须配合 --page-range，禁止误对整本 PDF 发起诊断请求")
    if args.ocr_only and args.dry_run:
        raise ValueError("--ocr-only 与 --dry-run 不能同时使用")
    page_range_suffix = f"__pages_{stable_hash(args.page_range, length=8)}" if selected_pages is not None else ""
    job_dir = output_root / f"{_safe_stem(input_path.stem)}__{source_hash[:10]}{page_range_suffix}"
    final_path = job_dir / "company_full_extraction.json"
    settings = None if args.dry_run else Settings.from_env().with_cli_overrides(
        max_concurrency=args.max_concurrency,
        include_person_names=args.include_person_names,
    )
    processing_config = None if settings is None else {
        "ocrChunkPages": settings.ocr_chunk_pages,
        "ocrMaxFileBytes": settings.ocr_max_file_bytes,
        "classifierBatchPages": settings.classifier_batch_pages,
        "domainMaxPages": settings.domain_max_pages,
        "domainMaxChars": settings.domain_max_chars,
        "renderDpi": settings.render_dpi,
        "includePersonNames": settings.include_person_names,
        "classifierModel": settings.classifier_model,
        "modelClassifierEnabled": not args.no_model_classifier,
        "visionFallbackEnabled": not args.no_vision_fallback,
        "currentEmploymentFreshnessDays": CURRENT_EMPLOYMENT_FRESHNESS_DAYS,
    }
    if final_path.exists() and not args.resume and not args.overwrite:
        raise ValueError(f"输出已存在：{final_path}；请使用 --resume 或 --overwrite")
    if final_path.exists() and args.resume and not args.overwrite:
        existing = read_json(final_path)
        compatible = (
            isinstance(existing, dict)
            and existing.get("schemaVersion") == SCHEMA_VERSION
            and existing.get("sourceDocument", {}).get("sha256") == source_hash
            and existing.get("run", {}).get("promptVersion") == PROMPT_VERSION
            and existing.get("run", {}).get("extractorVersion") == EXTRACTOR_VERSION
            and existing.get("run", {}).get("asOfDate") == as_of.isoformat()
            and existing.get("run", {}).get("pageRange") == args.page_range
            and existing.get("run", {}).get("processingConfig") == processing_config
            and existing.get("subjectCompany", {}).get("providedName") == args.company_name
            and settings is not None
            and existing.get("run", {}).get("models") == {
                "ocr": settings.ocr_model,
                "vision": settings.vision_model,
                "classifier": settings.classifier_model,
                "text": settings.text_model,
            }
        )
        if compatible and existing.get("quality", {}).get("status") == "SUCCESS":
            print(final_path)
            return final_path
        if not compatible:
            raise ValueError("现有结果与当前模型、Prompt、Schema、页范围或抽取器版本不一致；请使用 --overwrite 或新的输出目录")
    state = StateStore(job_dir / "state.json", source_hash=source_hash, schema_version=SCHEMA_VERSION)
    state.stage("preflight", "PREFLIGHTED")
    preflight = preflight_pdf(
        input_path,
        source_hash=source_hash,
        password=args.pdf_password,
        selected_pages=selected_pages,
    )
    apply_local_classification(preflight.page_manifest, preflight.page_texts)
    atomic_write_json(job_dir / "page_manifest.json", preflight.page_manifest)
    if args.dry_run:
        write_page_content_cache(job_dir / "ocr" / "page_content.json", preflight.page_texts)
        state.finish("PREFLIGHTED", dryRun=True, output=str(job_dir / "page_manifest.json"))
        print(job_dir / "page_manifest.json")
        return None

    assert settings is not None
    settings.validate_for_api()
    if not args.no_vision_fallback and not settings.vision_model:
        raise ValueError("启用视觉降级时必须配置 ZHIPU_VISION_MODEL")
    client = ZhipuClient(settings)
    all_warnings: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []
    api_usage: list[dict[str, Any]] = []
    vision_sensitive_findings: list[dict[str, Any]] = []
    requested_ocr_pages = [
        int(item["physicalPage"])
        for item in preflight.page_manifest
        if item.get("needsOcr")
    ]
    state.stage("ocr", "OCR_RUNNING")
    ocr_usage, ocr_errors, _ = _apply_ocr(
        input_path,
        preflight.page_manifest,
        preflight.page_texts,
        client=client,
        job_dir=job_dir,
        settings=settings,
        password=args.pdf_password,
        overwrite=args.overwrite,
    )
    api_usage.extend(ocr_usage)
    all_errors.extend(ocr_errors)
    if args.ocr_only:
        for item in preflight.page_manifest:
            page = int(item["physicalPage"])
            item["printedPage"] = infer_printed_page(
                preflight.page_texts.get(page, ""),
                total_pages=int(preflight.source["totalPages"]),
            )
            item["contentHash"] = stable_hash(preflight.page_texts.get(page, ""))
        failed_pages = [
            int(item["physicalPage"])
            for item in preflight.page_manifest
            if item.get("contentSource") in {"OCR_FAILED", "OCR_LOW_QUALITY", "UNAVAILABLE"}
        ]
        diagnostic_status = "PARTIAL" if failed_pages or all_errors else "SUCCESS"
        diagnostic_path = job_dir / "ocr_diagnostic.json"
        atomic_write_json(job_dir / "page_manifest.json", preflight.page_manifest)
        write_page_content_cache(job_dir / "ocr" / "page_content.json", preflight.page_texts)
        atomic_write_json(diagnostic_path, {
            "mode": "OCR_ONLY",
            "status": diagnostic_status,
            "sourceDocument": preflight.source,
            "processedPages": sorted(selected_pages),
            "requestedOcrPages": requested_ocr_pages,
            "ocrPages": [
                int(item["physicalPage"])
                for item in preflight.page_manifest
                if item.get("contentSource") == "GLM_OCR"
            ],
            "failedPages": failed_pages,
            "errors": all_errors,
            "apiUsage": api_usage,
            "quality": {"status": diagnostic_status},
        })
        state.finish(diagnostic_status, mode="OCR_ONLY", output=str(diagnostic_path))
        print(f"OCR 诊断状态：{diagnostic_status}", file=sys.stderr)
        print(diagnostic_path)
        return diagnostic_path
    if not args.no_vision_fallback:
        vision_usage, vision_errors, vision_sensitive_findings = _vision_fallback(
            input_path,
            preflight.page_manifest,
            preflight.page_texts,
            client=client,
            job_dir=job_dir,
            settings=settings,
            password=args.pdf_password,
            overwrite=args.overwrite,
        )
        api_usage.extend(vision_usage)
        all_errors.extend(vision_errors)
        all_errors, recovered_ocr = _reclassify_recovered_ocr_errors(all_errors, preflight.page_manifest)
        all_warnings.extend(recovered_ocr)

    apply_local_classification(preflight.page_manifest, preflight.page_texts)
    if not args.no_model_classifier:
        classification = refine_ambiguous_pages(
            preflight.page_manifest,
            preflight.page_texts,
            client=client,
            batch_size=settings.classifier_batch_pages,
            cache_dir=job_dir / "classification",
            overwrite=args.overwrite,
        )
        all_warnings.extend(classification["warnings"])
        all_errors.extend(classification["errors"])
        api_usage.extend(classification["usage"])
    unclassified_pages = [
        int(item["physicalPage"])
        for item in preflight.page_manifest
        if item.get("documentType") == "OTHER"
    ]
    if unclassified_pages:
        all_warnings.append({
            "stage": "CLASSIFICATION",
            "code": "UNCLASSIFIED_PAGES",
            "pages": unclassified_pages,
            "message": "这些页面未能映射到业务文档类型，原文已保留但不会生成领域事实。",
        })
    for item in preflight.page_manifest:
        page = int(item["physicalPage"])
        item["printedPage"] = infer_printed_page(
            preflight.page_texts.get(page, ""),
            total_pages=int(preflight.source["totalPages"]),
        )
        item["contentHash"] = stable_hash(preflight.page_texts.get(page, ""))
    atomic_write_json(job_dir / "page_manifest.json", preflight.page_manifest)
    write_page_content_cache(job_dir / "ocr" / "page_content.json", preflight.page_texts)

    state.stage("extraction", "EXTRACTING")
    extraction = extract_domains(
        domain_pages(preflight.page_manifest),
        preflight.page_texts,
        preflight.page_manifest,
        client=client,
        cache_dir=job_dir / "domain",
        raw_dir=job_dir / "raw" / "domains",
        max_pages=settings.domain_max_pages,
        max_chars=settings.domain_max_chars,
        overwrite=args.overwrite,
    )
    all_warnings.extend(extraction["warnings"])
    all_errors.extend(extraction["errors"])
    api_usage.extend(extraction["usage"])

    state.stage("merge", "MERGING")
    merged = merge_domain_payloads(extraction["domainPayloads"], as_of=as_of)
    merged = sanitize_sensitive_output(merged)
    normalized_evidence = sanitize_sensitive_output(extraction["evidence"])
    annotate_conflict_source_strengths(merged, normalized_evidence)
    if not settings.include_person_names:
        merged = remove_person_names(merged)
        all_warnings.append({
            "stage": "PRIVACY",
            "code": "STRUCTURED_PERSON_NAMES_REMOVED",
            "message": "结构化人员姓名已移除；为保持证据可审计性，最小证据摘录中仍可能出现姓名，应按敏感文档权限管理输出目录。",
        })
    all_warnings.extend(enforce_current_employment_policy(merged, normalized_evidence, as_of=as_of))
    capability_evidence = derive_capability_evidence(merged, normalized_evidence)
    derived = derive_profile_fields(merged, capability_evidence, as_of=as_of)
    capability_assessments = derive_capability_assessments(
        merged,
        derived,
        conflicts=merged.get("conflicts", []),
    )
    sensitive_findings = _merge_sensitive_findings(
        _sensitive_summary(preflight.page_texts),
        vision_sensitive_findings,
    )
    package = build_creation_package(
        run={
            "runId": f"run:{stable_hash(source_hash, utc_now_iso(), length=24)}",
            "generatedAt": utc_now_iso(),
            "asOfDate": as_of.isoformat(),
            "promptVersion": PROMPT_VERSION,
            "schemaVersion": SCHEMA_VERSION,
            "extractorVersion": EXTRACTOR_VERSION,
            "models": {
                "ocr": settings.ocr_model,
                "vision": settings.vision_model,
                "classifier": settings.classifier_model,
                "text": settings.text_model,
            },
            "pageRange": args.page_range,
            "processingConfig": processing_config,
        },
        source_document=preflight.source,
        subject_company_name=args.company_name,
        page_manifest=preflight.page_manifest,
        merged=merged,
        evidence=normalized_evidence,
        capability_evidence=capability_evidence,
        capability_assessments=capability_assessments,
        derived=derived,
        sensitive_findings=sensitive_findings,
        warnings=all_warnings,
        errors=all_errors,
        api_usage=api_usage,
    )
    if selected_pages is not None:
        package["creationReadiness"]["status"] = "IDENTITY_INCOMPLETE"
        package["creationReadiness"]["blockingIssues"].append("PARTIAL_PAGE_RANGE")
        package["creationReadiness"]["requiredNextAction"] = "RUN_FULL_DOCUMENT_EXTRACTION"
        package["profileProjectionCandidate"]["creationReadiness"] = package["creationReadiness"]
        package["quality"]["creationReadiness"] = "IDENTITY_INCOMPLETE"
        package["quality"]["status"] = "PARTIAL"

    state.stage("validation", "VALIDATING")
    validation_errors, validation_warnings = validate_creation_package(package, page_manifest=preflight.page_manifest)
    package["quality"]["validationErrors"] = validation_errors
    package["quality"]["validationWarnings"] = validation_warnings
    if validation_errors:
        package["quality"]["status"] = "FAILED"
    elif package["quality"]["status"] != "PARTIAL" and validation_warnings:
        package["quality"]["status"] = "SUCCESS"
    atomic_write_json(final_path, package)
    atomic_write_json(job_dir / "errors.json", {"errors": all_errors, "validationErrors": validation_errors})
    state.finish(package["quality"]["status"], output=str(final_path), validationErrorCount=len(validation_errors))
    print(f"完成状态：{package['quality']['status']}；创建就绪度：{package['creationReadiness']['status']}", file=sys.stderr)
    print(final_path)
    return final_path


def _record_fatal_state(args: argparse.Namespace, exc: Exception) -> None:
    try:
        input_path = args.input.expanduser().resolve()
        if not input_path.is_file():
            return
        source_hash = sha256_file(input_path)
        page_range_suffix = f"__pages_{stable_hash(args.page_range, length=8)}" if args.page_range else ""
        job_dir = args.output_root.expanduser().resolve() / f"{_safe_stem(input_path.stem)}__{source_hash[:10]}{page_range_suffix}"
        state_path = job_dir / "state.json"
        state = read_json(state_path)
        if not isinstance(state, dict):
            return
        state["status"] = "FAILED"
        state["updatedAt"] = utc_now_iso()
        state["fatalError"] = {
            "errorType": type(exc).__name__,
            "message": str(exc)[:500],
        }
        atomic_write_json(state_path, state)
    except Exception:
        # 原始异常优先；状态落盘失败不能遮蔽真正失败原因。
        return


def main() -> int:
    args: argparse.Namespace | None = None
    try:
        load_local_env(SCRIPT_DIR / ".env")
        args = parse_args()
        output = run(args)
        if output and output.is_file():
            package = read_json(output, {})
            status = package.get("quality", {}).get("status") if isinstance(package, dict) else "FAILED"
            if status == "FAILED":
                return 1
            if status == "PARTIAL":
                return 2
        return 0
    except KeyboardInterrupt:
        print("任务已中断，状态和成功缓存已保留", file=sys.stderr)
        return 130
    except Exception as exc:
        if args is not None:
            _record_fatal_state(args, exc)
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
