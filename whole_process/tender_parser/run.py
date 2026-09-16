"""招标 PDF 字段抽取命令行入口。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any

from jsonschema import validate

from src.document import Document
from src.fields import extract_fields, finalize
from src.qualifications import extract as extract_qualifications
from src.qualifications_jcebid import extract as extract_jcebid_qualifications
from src.schema import schema


ROOT = Path(__file__).resolve().parent
FIELDS_PATH = ROOT / "resources" / "fields.json"
SCHEMA_VERSION = "0.3.4"
PARSER_NAME = "pdf-inspector 1.18.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="使用 pdf-inspector 抽取招标 PDF 字段")
    parser.add_argument("--pdf", type=Path, required=True, help="单个待处理招标 PDF")
    parser.add_argument("--output", type=Path, default=ROOT / "output", help="内部结果输出目录")
    return parser.parse_args()


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify_evidence(value: Any, nodes_by_id: dict[str, dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if {"item_id", "start", "end", "text", "bbox"}.issubset(value):
            item_id = value["item_id"]
            node = nodes_by_id.get(item_id)
            if node is None:
                raise ValueError(f"证据引用不存在：{item_id}")
            start = value["start"]
            end = value["end"]
            if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
                raise ValueError(f"证据文本偏移无效：{item_id}")
            if end > len(node["text"]) or node["text"][start:end] != value["text"]:
                raise ValueError(f"证据文本偏移不一致：{item_id}")
            if node["page"] != value.get("page"):
                raise ValueError(f"证据页码不一致：{item_id}")
        for item in value.values():
            verify_evidence(item, nodes_by_id)
    elif isinstance(value, list):
        for item in value:
            verify_evidence(item, nodes_by_id)


def has_business_value(record: dict[str, Any]) -> bool:
    """Treat an empty structured qualification container as an empty field."""
    value = record.get("value")
    if value is None:
        return False
    if record.get("field") == "资格要求结构化项" and isinstance(value, dict):
        return bool(value.get("raw_text") or value.get("items"))
    return True


def run(pdf: Path, output: Path) -> dict[str, Any]:
    pdf = pdf.expanduser().resolve()
    output = output.expanduser().resolve()
    if not pdf.is_file() or pdf.suffix.casefold() != ".pdf":
        raise ValueError("--pdf 必须是存在的 PDF 文件")
    if not FIELDS_PATH.is_file():
        raise ValueError(f"字段配置不存在：{FIELDS_PATH}")
    if (output / "extraction_review.xlsx").exists():
        raise ValueError("已有审核工作簿，拒绝覆盖；请使用新输出目录")

    source_hash = file_sha256(pdf)
    started_at = perf_counter()
    document = Document(pdf, output)
    parse_seconds = perf_counter() - started_at
    definitions = json.loads(FIELDS_PATH.read_text(encoding="utf-8"))
    records, qualification_clauses, unmapped = extract_fields(document, definitions)
    qualification_candidates = [
        ("generic", extract_qualifications(document, qualification_clauses)),
        ("jcebid", extract_jcebid_qualifications(document, qualification_clauses)),
    ]
    qualification_profile, qualifications = max(
        qualification_candidates,
        key=lambda pair: len(pair[1].get("items", [])),
    )
    # The integrated pipeline keeps review metadata and full evidence internally.
    # The standalone batch export strips those fields only at its database boundary.
    for item in qualifications.get("items", []):
        item.setdefault("status", "REVIEW_REQUIRED")
        item.setdefault("note", "资格结构化项为机器抽取结果，需人工审核。")
        item.setdefault("review", {"status": "UNREVIEWED", "value": None, "note": ""})
    extensions = [
        ("资格要求结构化项", qualifications, "field_042"),
    ]
    for name, value, field_id in extensions:
        record = finalize(name, [])
        evidence = value["raw_text_evidence"]
        record.update(
            value=value,
            status="REVIEW_REQUIRED",
            field_id=field_id,
            group="扩展字段",
            page=sorted({item["page"] for item in evidence}),
            evidence=evidence,
            note="结构化内容未审核，不作为训练标签。",
        )
        records.append(record)

    data = {
        "schema_version": SCHEMA_VERSION,
        "parser": PARSER_NAME,
        "source_pdf": str(pdf),
        "source_pdf_sha256": source_hash,
        "training_ready": False,
        "ocr_performed": False,
        "fields": records,
        "unmapped_identifiers": unmapped,
    }
    if len(records) != 42 or len({record["field"] for record in records}) != 42:
        raise ValueError(f"招标字段数量或名称不符合 Schema {SCHEMA_VERSION}")
    if not all(not has_business_value(record) or record["evidence"] for record in records):
        raise ValueError("存在没有证据的非空招标字段")
    verify_evidence(data, {node["item_id"]: node for node in document.items})
    contract = schema([record["field"] for record in records])
    validate(data, contract)

    document.write(output / "schema.json", contract)
    document.write(output / "extraction_result.json", data)
    summary = {
        "page_count": len(document.pages),
        "parse_seconds": parse_seconds,
        "total_seconds": perf_counter() - started_at,
        "statuses": dict(Counter(record["status"] for record in records)),
        "qualification_items": len(qualifications["items"]),
        "qualification_sources": len(qualifications["source_items"]),
        "qualification_profile": qualification_profile,
        "blank_pages": [page["pdf_page"] for page in document.pages if not page["text"].strip()],
        "sections": document.groups,
        "section_notes": document.section_notes,
    }
    document.write(output / "report.json", summary)
    return summary


def main() -> int:
    args = parse_args()
    summary = run(args.pdf, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
