"""将招标、投标内部结构化结果渲染为最终 Markdown。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from normalizers import redact_sensitive_text, sanitize_sensitive_output


MARKDOWN_SCHEMA_VERSION = "project-document-markdown/v1"
PIPELINE_VERSION = "unified-project-parser/1.1.0"
# The 1.1.0 change is tender-only; keep existing bid/profile resume metadata valid.
BID_PIPELINE_VERSION = "unified-project-parser/1.0.0"

LABELS = {
    "aliases": "企业别名",
    "amountRange": "金额范围",
    "amountYuan": "金额（元）",
    "assessmentUid": "评估标识",
    "blockingIssues": "阻断问题",
    "businessScope": "经营范围",
    "buyerRelationships": "客户关系",
    "calculatedAt": "计算时间",
    "capabilityTags": "能力标签",
    "category": "类别",
    "certNo": "证书编号",
    "city": "城市",
    "claimsJson": "结论",
    "companyName": "企业名称",
    "companyRole": "企业角色",
    "companyType": "企业类型",
    "conflictRefs": "冲突引用",
    "contractAmount": "合同金额",
    "creditCode": "统一社会信用代码",
    "currency": "币种",
    "customerName": "客户名称",
    "dimensionCode": "能力维度代码",
    "dimensionName": "能力维度",
    "documentAsOfDate": "材料时点",
    "employeeCount": "员工人数",
    "establishedDate": "成立日期",
    "evidenceJson": "证据摘要",
    "fieldPath": "字段路径",
    "generatedAt": "生成时间",
    "historyOnly": "仅历史事实",
    "isCurrent": "是否当前评估",
    "issueDate": "发证日期",
    "legalPerson": "法定代表人",
    "level": "等级",
    "mainIndustries": "主要行业",
    "mainRegions": "主要区域",
    "maxYuan": "最高金额（元）",
    "mergeType": "归并类型",
    "minYuan": "最低金额（元）",
    "name": "名称",
    "normalizedCompanyName": "标准化企业名称",
    "normalizedProvidedName": "标准化提示名称",
    "note": "说明",
    "operation": "操作",
    "personName": "人员姓名",
    "personnelType": "人员类型",
    "productName": "产品名称",
    "profileAction": "画像动作",
    "projectCode": "项目编号",
    "projectName": "项目名称",
    "province": "省份",
    "providedName": "提示企业名称",
    "qualificationName": "资质名称",
    "qualityLevel": "质量等级",
    "rawText": "原始文本",
    "rawUnit": "原始单位",
    "registeredAddress": "注册地址",
    "registeredCapital": "注册资本",
    "registrationNo": "登记编号",
    "relationshipType": "关系类型",
    "requirement": "要求",
    "requiredNextAction": "后续动作",
    "riskTags": "风险标签",
    "role": "角色",
    "score": "分数",
    "status": "状态",
    "summary": "摘要",
    "supportStatus": "证据支持状态",
    "tenderCode": "招标编号",
    "title": "标题",
    "type": "类型",
    "unknownsJson": "未知项",
    "usedAsEvidence": "是否作为证据",
    "validFrom": "有效期起",
    "validTo": "有效期止",
    "verificationStatus": "核验状态",
    "warnings": "警告",
}

SKIPPED_DETAIL_KEYS = {
    "apiUsage",
    "bbox",
    "candidates",
    "calls",
    "evidence",
    "evidenceJson",
    "evidences",
    "evidenceRefs",
    "evidence_ref",
    "evidence_json",
    "rawText",
    "raw_text_evidence",
    "requestId",
    "review",
    "sourceRef",
    "source_item_id",
}


def _label(value: object) -> str:
    text = str(value)
    return LABELS.get(text, text)


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _front_matter(metadata: Mapping[str, Any]) -> list[str]:
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.extend(["---", ""])
    return lines


def _escape_table(value: Any) -> str:
    text = str(value).replace("|", "\\|").replace("\r", "")
    return text.replace("\n", "<br>")


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    if not rows:
        return []
    output = [
        "| " + " | ".join(_escape_table(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    output.extend("| " + " | ".join(_escape_table(item) for item in row) + " |" for row in rows)
    return output


def _pages(value: Any) -> str:
    if not isinstance(value, list):
        return "—"
    pages = [str(item) for item in value if isinstance(item, int)]
    return "、".join(pages) if pages else "—"


def _plain_value(value: Any) -> str | None:
    if value is None:
        return "未提取"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        return "、".join(str(item) for item in value) if value else "无"
    if isinstance(value, dict):
        if "amountYuan" in value:
            amount = value.get("amountYuan")
            currency = value.get("currency") or "CNY"
            raw_text = value.get("rawText")
            if amount is not None:
                return f"{amount} {currency}" + (f"（原文：{raw_text}）" if raw_text else "")
            if raw_text:
                return str(raw_text)
        if value and set(value).issubset({"minYuan", "maxYuan"}):
            return f"{value.get('minYuan')} — {value.get('maxYuan')} 元"
    return None


def _record_heading(value: Mapping[str, Any], index: int) -> str:
    for key in (
        "name",
        "qualificationName",
        "projectName",
        "productName",
        "dimensionName",
        "companyName",
        "type",
        "fieldPath",
    ):
        candidate = value.get(key)
        if isinstance(candidate, (str, int, float)) and str(candidate).strip():
            return str(candidate).strip()
    return f"记录 {index}"


def _render_structure(value: Any, *, level: int = 3) -> list[str]:
    level = min(max(level, 1), 6)
    if value is None:
        return ["未提取到可可靠采用的数据。"]
    if isinstance(value, list):
        if not value:
            return ["未提取到可可靠采用的数据。"]
        if all(not isinstance(item, (dict, list)) for item in value):
            return [f"- {item}" for item in value]
        lines: list[str] = []
        for index, item in enumerate(value, 1):
            if isinstance(item, Mapping):
                lines.extend([f"{'#' * level} {_record_heading(item, index)}", ""])
                lines.extend(_render_structure(item, level=level + 1))
            else:
                lines.append(f"- {_plain_value(item) or str(item)}")
            lines.append("")
        return lines
    if isinstance(value, Mapping):
        rows: list[list[str]] = []
        nested: list[tuple[str, Any]] = []
        for key, item in value.items():
            if key in SKIPPED_DETAIL_KEYS:
                continue
            plain = _plain_value(item)
            if plain is None:
                nested.append((_label(key), item))
            else:
                rows.append([_label(key), plain])
        lines = _table(["字段", "内容"], rows)
        for key, item in nested:
            if lines:
                lines.append("")
            lines.extend([f"{'#' * level} {key}", ""])
            lines.extend(_render_structure(item, level=level + 1))
        return lines or ["未提取到可可靠采用的数据。"]
    return [str(value)]


def _join_document(lines: list[str]) -> str:
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n"


def _quality_status_from_tender(report: Mapping[str, Any]) -> str:
    statuses = report.get("statuses") if isinstance(report.get("statuses"), Mapping) else {}
    blank_pages = report.get("blank_pages") if isinstance(report.get("blank_pages"), list) else []
    warning_count = sum(
        int(statuses.get(name, 0) or 0)
        for name in ("MISSING", "CONFLICT", "REVIEW_REQUIRED")
    )
    return "SUCCESS_WITH_WARNINGS" if warning_count or blank_pages else "SUCCESS"


def render_tender_markdown(
    *,
    extraction: Mapping[str, Any],
    report: Mapping[str, Any],
    raw_markdown: str | None,
    project_key: str,
    source_file: str,
    source_sha256: str,
    generated_at: str,
) -> tuple[str, str]:
    safe_extraction = sanitize_sensitive_output(dict(extraction))
    safe_report = sanitize_sensitive_output(dict(report))
    status = _quality_status_from_tender(safe_report)
    lines = _front_matter(
        {
            "schema_version": MARKDOWN_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "document_type": "tender",
            "project_key": project_key,
            "company_name": None,
            "source_file": source_file,
            "source_sha256": source_sha256,
            "parser": safe_extraction.get("parser") or "pdf-inspector",
            "parser_schema_version": safe_extraction.get("schema_version"),
            "status": status,
            "generated_at": generated_at,
        }
    )
    lines.extend([f"# {project_key}招标文件解析", ""])
    lines.append("> 本文件由程序生成。字段状态为 `MISSING`、`CONFLICT` 或 `REVIEW_REQUIRED` 的内容不能视为已核验事实。")
    lines.append("")

    fields = safe_extraction.get("fields") if isinstance(safe_extraction.get("fields"), list) else []
    ordinary_fields = [
        field
        for field in fields
        if isinstance(field, Mapping) and field.get("field") != "资格要求结构化项"
    ]
    rows = []
    for field in ordinary_fields:
        display = _plain_value(field.get("value"))
        rows.append(
            [
                field.get("field") or "未命名字段",
                display if display is not None else "结构化内容见原始解析",
                field.get("status") or "UNKNOWN",
                _pages(field.get("page")),
                field.get("note") or "",
            ]
        )
    lines.extend(["## 招标项目字段", ""])
    lines.extend(_table(["字段", "内容", "状态", "证据页", "说明"], rows) or ["未生成招标字段。"])
    lines.append("")

    qualification_field = next(
        (
            field
            for field in fields
            if isinstance(field, Mapping) and field.get("field") == "资格要求结构化项"
        ),
        None,
    )
    qualification_value = qualification_field.get("value") if isinstance(qualification_field, Mapping) else None
    qualification_items = (
        qualification_value.get("items")
        if isinstance(qualification_value, Mapping) and isinstance(qualification_value.get("items"), list)
        else []
    )
    lines.extend(["## 投标人资格要求", ""])
    if qualification_items:
        for item in qualification_items:
            if not isinstance(item, Mapping):
                continue
            lines.extend(
                [
                    f"### {item.get('name') or item.get('type') or '资格要求'}",
                    "",
                    str(item.get("requirement") or "未提取要求正文"),
                    "",
                    f"- 类型：`{item.get('type') or 'UNKNOWN'}`",
                    f"- 状态：`{item.get('status') or 'REVIEW_REQUIRED'}`",
                    f"- 证据页：{_pages(item.get('page'))}",
                    "",
                ]
            )
    else:
        lines.extend(["未提取到结构化资格要求。", ""])

    lines.extend(["## 解析质量", ""])
    statuses = safe_report.get("statuses") if isinstance(safe_report.get("statuses"), Mapping) else {}
    quality_rows = [
        ["总页数", safe_report.get("page_count", "未知")],
        ["字段状态", "、".join(f"{key}={value}" for key, value in statuses.items()) or "无"],
        ["资格要求项", safe_report.get("qualification_items", 0)],
        ["无文本页", _pages(safe_report.get("blank_pages"))],
        ["OCR", "未执行" if safe_extraction.get("ocr_performed") is False else "状态未知"],
    ]
    lines.extend(_table(["指标", "结果"], quality_rows))
    lines.extend(
        [
            "",
            "> 当前招标解析器不执行 OCR。无文本层页面以及未被版式规则覆盖的内容可能缺失，其他项目不能沿用首份样本的准确率结论。",
            "",
        ]
    )

    if raw_markdown:
        redacted_raw, _ = redact_sensitive_text(raw_markdown)
        lines.extend(["## 原始逐页解析", "", "> 以下内容经过基础敏感信息脱敏，保留原解析页标记。", "", redacted_raw.strip(), ""])
    return _join_document(lines), status


def _render_bid_quality(raw_quality: Mapping[str, Any] | None) -> list[str]:
    raw_summary = raw_quality.get("summary") if isinstance(raw_quality, Mapping) else None
    raw_summary = raw_summary if isinstance(raw_summary, Mapping) else {}
    source = raw_quality.get("sourceDocument") if isinstance(raw_quality, Mapping) else None
    source = source if isinstance(source, Mapping) else {}
    parser = raw_quality.get("parser") if isinstance(raw_quality, Mapping) else None
    parser = parser if isinstance(parser, Mapping) else {}
    ocr_errors = raw_quality.get("ocrErrors") if isinstance(raw_quality, Mapping) else None
    rows = [
        ["处理方式", "pdf-inspector 原生文本解析 + 本地 OCR（无外部 API）"],
        ["解析器", parser.get("name") or "pdf-inspector"],
        ["解析器版本", parser.get("version") or "未知"],
        ["源文件总页数", source.get("totalPages", "未知")],
        ["逐页解析状态", raw_quality.get("status", "未知") if isinstance(raw_quality, Mapping) else "未生成"],
        ["预期解析页数", raw_summary.get("expectedPages", "未知")],
        ["已写入页标记数", raw_summary.get("pageMarkersWritten", "未知")],
        ["无内容页数", len(raw_summary.get("emptyPages") or [])],
        ["逐页解析缺失页数", len(raw_summary.get("missingPages") or [])],
        ["需要 OCR 页数", len(raw_summary.get("pagesNeedingOcr") or [])],
        ["本地 OCR 完成页数", len(raw_summary.get("ocrCompletedPages") or [])],
        ["本地 OCR 后仍未解决页数", len(raw_summary.get("unresolvedOcrPages") or [])],
        ["本地 OCR 阶段错误数", len(ocr_errors) if isinstance(ocr_errors, list) else 0],
    ]
    return _table(["指标", "结果"], rows)


def render_bid_markdown(
    *,
    raw_markdown: str | None,
    raw_quality: Mapping[str, Any] | None,
    project_key: str,
    company_name: str,
    source_file: str,
    source_sha256: str,
    generated_at: str,
    processing_errors: Sequence[str] = (),
) -> tuple[str, str]:
    redacted_raw = None
    if raw_markdown:
        redacted_raw, _ = redact_sensitive_text(raw_markdown)
    raw_status = raw_quality.get("status") if isinstance(raw_quality, Mapping) else None
    if redacted_raw and raw_status == "COMPLETE":
        status = "SUCCESS"
    elif redacted_raw and raw_status == "COMPLETE_WITH_WARNINGS":
        status = "SUCCESS_WITH_WARNINGS"
    elif redacted_raw:
        status = "PARTIAL"
    else:
        status = "FAILED"
    if processing_errors and status in {"SUCCESS", "SUCCESS_WITH_WARNINGS"}:
        status = "PARTIAL"

    parser = "pdf-inspector-local-ocr"
    parser_schema = raw_quality.get("schemaVersion") if isinstance(raw_quality, Mapping) else None
    lines = _front_matter(
        {
            "schema_version": MARKDOWN_SCHEMA_VERSION,
            "pipeline_version": BID_PIPELINE_VERSION,
            "document_type": "bid",
            "project_key": project_key,
            "company_name": company_name,
            "source_file": source_file,
            "source_sha256": source_sha256,
            "processing_mode": "auto_then_force_retry",
            "parser": parser,
            "parser_schema_version": parser_schema,
            "status": status,
            "generated_at": generated_at,
        }
    )
    lines.extend([f"# {company_name}投标文件解析", ""])
    lines.extend(
        [
            "> 本文件使用 pdf-inspector 原生文本解析，并仅对缺少可靠文本层的页面执行本地 OCR；不调用智谱或其他外部 API。",
            "> 财务、资质、业绩等内容如存在，只保留在下方原始逐页解析中，未进行字段级提取或事实核验。",
            "",
        ]
    )

    lines.extend(["## 解析质量", ""])
    lines.extend(_render_bid_quality(raw_quality))
    if processing_errors:
        lines.extend(["", "### 处理错误", ""])
        for error in processing_errors:
            safe_error, _ = redact_sensitive_text(str(error))
            lines.append(f"- {safe_error}")
    lines.append("")

    if redacted_raw:
        lines.extend(["## 原始逐页解析", "", "> 以下内容经过基础敏感信息脱敏，保留原解析页标记。", "", redacted_raw.strip(), ""])
    return _join_document(lines), status


def render_project_report(
    *,
    project_key: str,
    generated_at: str,
    status: str,
    tender: Mapping[str, Any] | None,
    bids: Sequence[Mapping[str, Any]],
    errors: Sequence[str],
    workdir_retained: bool,
) -> str:
    lines = _front_matter(
        {
            "schema_version": MARKDOWN_SCHEMA_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "document_type": "project_report",
            "project_key": project_key,
            "company_name": None,
            "source_file": None,
            "source_sha256": None,
            "parser": PIPELINE_VERSION,
            "parser_schema_version": None,
            "status": status,
            "generated_at": generated_at,
        }
    )
    lines.extend([f"# {project_key}处理报告", ""])
    lines.extend(_table(["项目", "结果"], [["总体状态", status], ["保留工作目录", "是" if workdir_retained else "否"]]))
    lines.append("")

    lines.extend(["## 招标文件", ""])
    if tender:
        lines.extend(
            _table(
                ["源文件", "SHA256 前缀", "解析器", "Schema", "解析状态", "中间输出", "最终发布", "最终输出"],
                [[
                    tender.get("source_file", ""),
                    str(tender.get("source_sha256", ""))[:10],
                    tender.get("parser") or "—",
                    tender.get("parser_schema_version") or "—",
                    tender.get("status", "UNKNOWN"),
                    tender.get("output") or "—",
                    tender.get("final_status") or "—",
                    tender.get("final_output") or "—",
                ]],
            )
        )
        for warning in tender.get("warnings") or []:
            lines.append(f"- {warning}")
    else:
        lines.append("未处理招标文件。")
    lines.append("")

    lines.extend(["## 投标文件", ""])
    bid_rows = [
        [
            item.get("company_name", ""),
            item.get("source_file", ""),
            str(item.get("source_sha256", ""))[:10],
            item.get("parser") or "—",
            item.get("parser_schema_version") or "—",
            item.get("status", "UNKNOWN"),
            item.get("output") or "—",
            item.get("award_status") or "—",
            item.get("award_amount") or "—",
            item.get("summary_model") or "—",
            item.get("final_status") or "—",
            item.get("final_output") or "—",
        ]
        for item in bids
    ]
    lines.extend(_table(
        [
            "公司",
            "源文件",
            "SHA256 前缀",
            "解析器",
            "Schema",
            "解析状态",
            "中间输出",
            "中标状态",
            "中标金额",
            "画像模型",
            "画像状态",
            "最终输出",
        ],
        bid_rows,
    ) or ["未处理投标文件。"])
    bid_warnings = [
        (item.get("company_name") or "未知公司", item.get("source_file") or "未知文件", warning)
        for item in bids
        for warning in (item.get("warnings") or [])
    ]
    if bid_warnings:
        lines.extend(["", "### 投标解析警告", ""])
        lines.extend(f"- {company}/{source_file}：{warning}" for company, source_file, warning in bid_warnings)
    lines.append("")

    lines.extend(["## 错误与待处理事项", ""])
    if errors:
        for error in errors:
            safe_error, _ = redact_sensitive_text(str(error))
            lines.append(f"- {safe_error}")
    else:
        lines.append("无。")
    lines.extend(["", "> 该报告只表示程序处理状态，不代表抽取内容已经人工核验。", ""])
    return _join_document(lines)


__all__ = [
    "BID_PIPELINE_VERSION",
    "MARKDOWN_SCHEMA_VERSION",
    "PIPELINE_VERSION",
    "render_bid_markdown",
    "render_project_report",
    "render_tender_markdown",
]
