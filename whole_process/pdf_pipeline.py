"""PDF 预检、文本可读性判断、切块、OCR 映射和复杂页渲染。"""

from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from io_utils import atomic_write_json


def _pypdf() -> tuple[Any, Any]:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError("缺少 pypdf；请先安装 requirements.txt 中的依赖") from exc
    return PdfReader, PdfWriter


@dataclass(frozen=True)
class PdfPreflight:
    source: dict[str, Any]
    page_manifest: list[dict[str, Any]]
    page_texts: dict[int, str]


PRINTED_PAGE_PATTERN = re.compile(r"^(?:第\s*)?(\d{1,4})(?:\s*页)?$")
DECORATED_PAGE_PATTERN = re.compile(r"^[-—–·•]\s*(\d{1,4})\s*[-—–·•]$")


def _image_count(page: Any) -> int | None:
    try:
        resources = page.get("/Resources") or {}
        x_objects = resources.get("/XObject") or {}
        count = 0
        for value in x_objects.values():
            obj = value.get_object()
            if obj.get("/Subtype") == "/Image":
                count += 1
        return count
    except Exception:
        return None


def text_readability(text: str | None) -> dict[str, Any]:
    value = text or ""
    nonspace = [char for char in value if not char.isspace()]
    if not nonspace:
        return {
            "textStatus": "EMPTY",
            "needsOcr": True,
            "textLength": 0,
            "readableRatio": 0.0,
            "garbledRatio": 0.0,
        }
    readable = sum(char.isalnum() or "\u4e00" <= char <= "\u9fff" or char in "，。；：！？、（）()[]【】%￥¥.-_/" for char in nonspace)
    garbled = sum(char in {"�", "□", "■", "?"} or ord(char) < 32 for char in nonspace)
    readable_ratio = readable / len(nonspace)
    garbled_ratio = garbled / len(nonspace)
    status = "READABLE"
    if len(nonspace) < 12:
        status = "EMPTY"
    elif readable_ratio < 0.55 or garbled_ratio > 0.08:
        status = "GARBLED"
    return {
        "textStatus": status,
        "needsOcr": status != "READABLE",
        "textLength": len(value),
        "readableRatio": round(readable_ratio, 4),
        "garbledRatio": round(garbled_ratio, 4),
    }


def infer_printed_page(text: str | None, *, total_pages: int) -> int | None:
    lines = [unicodedata.normalize("NFKC", line).strip() for line in (text or "").splitlines() if line.strip()]
    # 页脚优先，其次页眉；正文中孤立的编号不参与推断。
    candidates = [*reversed(lines[-6:]), *lines[:6]]
    for line in candidates:
        match = PRINTED_PAGE_PATTERN.fullmatch(line) or DECORATED_PAGE_PATTERN.fullmatch(line)
        if not match:
            continue
        page = int(match.group(1))
        if 1 <= page <= total_pages + 100:
            return page
    return None


def open_reader(pdf_path: Path, password: str | None = None) -> Any:
    PdfReader, _ = _pypdf()
    reader = PdfReader(str(pdf_path), strict=False)
    if reader.is_encrypted:
        if not password:
            raise ValueError("PDF 已加密；请通过 --pdf-password 提供密码")
        result = reader.decrypt(password)
        if not result:
            raise ValueError("PDF 密码错误或当前加密算法不受支持")
    return reader


def preflight_pdf(
    pdf_path: Path,
    *,
    source_hash: str,
    password: str | None = None,
    selected_pages: set[int] | None = None,
) -> PdfPreflight:
    reader = open_reader(pdf_path, password)
    total_pages = len(reader.pages)
    page_manifest: list[dict[str, Any]] = []
    page_texts: dict[int, str] = {}
    for index in range(1, total_pages + 1):
        if selected_pages is not None and index not in selected_pages:
            continue
        warnings: list[str] = []
        try:
            page = reader.pages[index - 1]
        except Exception as exc:
            page_texts[index] = ""
            page_manifest.append({
                "physicalPage": index,
                "printedPage": None,
                "widthPoints": None,
                "heightPoints": None,
                "rotation": 0,
                "estimatedImageCount": None,
                "textStatus": "EXTRACTION_FAILED",
                "needsOcr": True,
                "textLength": 0,
                "readableRatio": 0.0,
                "garbledRatio": 0.0,
                "documentType": "OTHER",
                "sectionCode": None,
                "classificationSource": "LOCAL_RULE",
                "classificationConfidence": "LOW",
                "contentSource": "PENDING_OCR",
                "contentHash": None,
                "warnings": [f"PDF 页面对象读取失败：{type(exc).__name__}"],
            })
            continue
        try:
            text = page.extract_text() or ""
            readability = text_readability(text)
        except Exception as exc:
            text = ""
            readability = {
                "textStatus": "EXTRACTION_FAILED",
                "needsOcr": True,
                "textLength": 0,
                "readableRatio": 0.0,
                "garbledRatio": 0.0,
            }
            warnings.append(f"本地文本提取失败：{type(exc).__name__}")
        page_texts[index] = text
        try:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
        except Exception:
            width = height = None
        page_manifest.append({
            "physicalPage": index,
            "printedPage": None,
            "widthPoints": width,
            "heightPoints": height,
            "rotation": int(page.get("/Rotate") or 0) % 360,
            "estimatedImageCount": _image_count(page),
            **readability,
            "documentType": "OTHER",
            "sectionCode": None,
            "classificationSource": "LOCAL_RULE",
            "classificationConfidence": "LOW",
            "contentSource": "LOCAL_TEXT" if readability["textStatus"] == "READABLE" else "PENDING_OCR",
            "contentHash": None,
            "warnings": warnings,
        })
    return PdfPreflight(
        source={
            "fileName": pdf_path.name,
            "sha256": source_hash,
            "sizeBytes": pdf_path.stat().st_size,
            "mimeType": "application/pdf",
            "totalPages": total_pages,
            "encrypted": bool(reader.is_encrypted),
        },
        page_manifest=page_manifest,
        page_texts=page_texts,
    )


def parse_page_range(value: str | None, total_pages: int) -> set[int] | None:
    if not value:
        return None
    pages: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise ValueError(f"页码范围无效：{token}") from exc
            if start > end:
                raise ValueError(f"页码范围起点不能大于终点：{token}")
            pages.update(range(start, end + 1))
        else:
            try:
                pages.add(int(token))
            except ValueError as exc:
                raise ValueError(f"页码无效：{token}") from exc
    if not pages or min(pages) < 1 or max(pages) > total_pages:
        raise ValueError(f"--page-range 必须位于 1..{total_pages}")
    return pages


def contiguous_ranges(page_numbers: Iterable[int], max_pages: int) -> list[tuple[int, int]]:
    values = sorted(set(page_numbers))
    if not values:
        return []
    ranges: list[tuple[int, int]] = []
    start = previous = values[0]
    for page in values[1:]:
        if page != previous + 1 or page - start >= max_pages:
            ranges.append((start, previous))
            start = page
        previous = page
    ranges.append((start, previous))
    return ranges


def write_pdf_chunk(
    source_pdf: Path,
    output_pdf: Path,
    *,
    start_page: int,
    end_page: int,
    password: str | None = None,
) -> None:
    _, PdfWriter = _pypdf()
    reader = open_reader(source_pdf, password)
    if start_page < 1 or end_page > len(reader.pages) or start_page > end_page:
        raise ValueError("PDF 切块页码范围无效")
    writer = PdfWriter()
    for index in range(start_page - 1, end_page):
        writer.add_page(reader.pages[index])
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_pdf.with_suffix(".tmp.pdf")
    with temporary.open("wb") as stream:
        writer.write(stream)
    temporary.replace(output_pdf)


def ocr_page_texts(response: dict[str, Any], expected_pages: int) -> list[str]:
    details = response.get("layout_details")
    if isinstance(details, list) and details:
        page_values: list[str] = []
        for page in details[:expected_pages]:
            blocks = page if isinstance(page, list) else []
            ordered = sorted(
                (item for item in blocks if isinstance(item, dict)),
                key=lambda item: int(item.get("index") or 0),
            )
            contents = [str(item.get("content") or "").strip() for item in ordered]
            page_values.append("\n".join(value for value in contents if value))
        while len(page_values) < expected_pages:
            page_values.append("")
        if any(page_values):
            return page_values
    markdown = response.get("md_results")
    if expected_pages == 1 and isinstance(markdown, str):
        return [markdown]
    return [""] * expected_pages


def render_page_image(
    pdf_path: Path,
    output_path: Path,
    *,
    physical_page: int,
    dpi: int,
) -> Path:
    executable = shutil.which("pdftoppm")
    if not executable:
        raise RuntimeError("未找到 pdftoppm，无法执行视觉复核页面渲染")
    suffix = output_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        format_args = ["-jpeg", "-jpegopt", "quality=82,optimize=y,progressive=y"]
    elif suffix == ".png":
        format_args = ["-png"]
    else:
        raise ValueError("页面渲染输出只支持 .jpg/.jpeg/.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prefix = output_path.with_suffix("")
    command = [
        executable,
        "-f", str(physical_page),
        "-l", str(physical_page),
        "-r", str(dpi),
        *format_args,
        "-singlefile",
        str(pdf_path),
        str(prefix),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if completed.returncode != 0 or not output_path.is_file():
        message = (completed.stderr or completed.stdout or "pdftoppm 执行失败").strip()
        raise RuntimeError(message[:500])
    return output_path


def render_page_png(
    pdf_path: Path,
    output_path: Path,
    *,
    physical_page: int,
    dpi: int,
) -> Path:
    if output_path.suffix.lower() != ".png":
        raise ValueError("render_page_png 的输出路径必须以 .png 结尾")
    return render_page_image(
        pdf_path,
        output_path,
        physical_page=physical_page,
        dpi=dpi,
    )


def write_page_content_cache(path: Path, page_texts: dict[int, str]) -> None:
    atomic_write_json(path, [
        {"physicalPage": page, "text": page_texts[page]}
        for page in sorted(page_texts)
    ])


__all__ = [
    "PdfPreflight",
    "contiguous_ranges",
    "infer_printed_page",
    "ocr_page_texts",
    "open_reader",
    "parse_page_range",
    "preflight_pdf",
    "render_page_image",
    "render_page_png",
    "text_readability",
    "write_page_content_cache",
    "write_pdf_chunk",
]
