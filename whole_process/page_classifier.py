"""确定性页面分类、模型复核和页面到业务领域的路由。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from io_utils import atomic_write_json, batched, read_json, stable_hash
from normalizers import clean_text, redact_sensitive_text
from prompts import CLASSIFIER_SYSTEM_PROMPT, DOCUMENT_TYPES, PROMPT_VERSION, build_classifier_user_prompt
from zhipu_client import ZhipuApiError, ZhipuClient


TYPE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("TABLE_OF_CONTENTS", (r"目\s*录", r"contents")),
    ("BUSINESS_LICENSE", (r"营业执照", r"统一社会信用代码", r"法定代表人")),
    ("COMPANY_PROFILE", (r"投标人基本情况", r"企业基本情况", r"公司简介")),
    ("QUALIFICATION_CERTIFICATE", (r"资质证书", r"建筑业企业资质", r"认证证书", r"高新技术企业")),
    ("INTELLECTUAL_PROPERTY_CERTIFICATE", (r"软件著作权", r"计算机软件著作权", r"专利证书", r"商标注册证")),
    ("SOCIAL_INSURANCE", (r"社会保险", r"社保缴费", r"参保证明")),
    ("PERSONNEL_CERTIFICATE", (r"注册证书", r"执业资格证", r"专业技术资格", r"安全生产考核合格")),
    ("PERSONNEL_RESUME", (r"人员简历", r"项目经理简历", r"主要人员简历", r"拟投入.*人员")),
    ("AWARD_NOTICE", (r"中标通知书", r"成交通知书")),
    ("ACCEPTANCE_REPORT", (r"验收报告", r"竣工验收", r"验收意见", r"验收证书")),
    ("CONTRACT", (r"合同协议书", r"合同书", r"甲\s*方", r"乙\s*方", r"合同金额")),
    ("INVOICE", (r"增值税.*发票", r"发票号码", r"价税合计")),
    ("PERFORMANCE_SUMMARY", (r"类似项目业绩", r"已完成项目", r"在建项目", r"业绩一览表")),
    ("AUDIT_REPORT", (r"审计报告", r"资产负债表", r"利润表", r"现金流量表")),
    ("CREDIT_REPORT", (r"信用报告", r"信用中国", r"企业信用信息公示")),
    ("LITIGATION_STATEMENT", (r"诉讼", r"仲裁", r"被执行人", r"失信被执行")),
    ("TAX_CERTIFICATE", (r"纳税证明", r"完税证明", r"税收缴款")),
    ("PRODUCT_CERTIFICATE", (r"产品认证证书", r"检测报告", r"检验报告", r"厂家授权")),
    ("PRODUCT_SPECIFICATION", (r"技术参数", r"产品参数", r"设备参数", r"规格型号")),
    ("IMPLEMENTATION_PLAN", (r"实施方案", r"施工组织", r"进度计划")),
    ("QUALITY_PLAN", (r"质量保证", r"质量管理", r"质量控制")),
    ("AFTER_SALES_PLAN", (r"售后服务", r"运维服务", r"服务承诺")),
    ("TRAINING_PLAN", (r"培训方案", r"培训计划")),
    ("ACCEPTANCE_PLAN", (r"验收方案", r"验收计划")),
    ("RISK_PLAN", (r"风险管理", r"应急预案")),
    ("TECHNICAL_SOLUTION", (r"技术方案", r"系统架构", r"功能设计", r"总体设计")),
    ("PRICE_SCHEDULE", (r"报价一览表", r"投标报价", r"分项报价")),
    ("COMMERCIAL_DEVIATION", (r"商务偏离表",)),
    ("TECHNICAL_DEVIATION", (r"技术偏离表",)),
    ("BID_GUARANTEE", (r"投标保证金", r"保函")),
    ("AUTHORIZATION", (r"授权委托书", r"法定代表人授权")),
    ("LEGAL_REPRESENTATIVE", (r"法定代表人身份证明",)),
    ("BID_LETTER", (r"投标函", r"投标书")),
    ("COVER", (r"投标文件", r"正本", r"副本")),
)

DOMAIN_BY_DOCUMENT_TYPE: dict[str, tuple[str, ...]] = {
    "COVER": ("enterprise",),
    "LEGAL_REPRESENTATIVE": ("enterprise",),
    "AUTHORIZATION": ("enterprise",),
    "BUSINESS_LICENSE": ("enterprise",),
    "COMPANY_PROFILE": ("enterprise",),
    "CREDIT_REPORT": ("enterprise", "risk"),
    "QUALIFICATION_CERTIFICATE": ("qualifications",),
    "INTELLECTUAL_PROPERTY_CERTIFICATE": ("intellectual_properties",),
    "AUDIT_REPORT": ("enterprise", "finance"),
    "LITIGATION_STATEMENT": ("risk",),
    "TAX_CERTIFICATE": ("risk",),
    "PERSONNEL_RESUME": ("personnel",),
    "PERSONNEL_CERTIFICATE": ("personnel",),
    "SOCIAL_INSURANCE": ("personnel",),
    "PERFORMANCE_SUMMARY": ("performances",),
    "CONTRACT": ("performances",),
    "ACCEPTANCE_REPORT": ("performances",),
    "AWARD_NOTICE": ("performances",),
    "PRODUCT_CERTIFICATE": ("products",),
    "PRODUCT_SPECIFICATION": ("products",),
    "TECHNICAL_SOLUTION": ("solutions",),
    "IMPLEMENTATION_PLAN": ("delivery",),
    "QUALITY_PLAN": ("delivery",),
    "RISK_PLAN": ("delivery",),
    "AFTER_SALES_PLAN": ("delivery",),
    "TRAINING_PLAN": ("delivery",),
    "ACCEPTANCE_PLAN": ("delivery",),
    "BID_LETTER": ("bid",),
    "BID_GUARANTEE": ("bid",),
    "COMMERCIAL_DEVIATION": ("bid",),
    "TECHNICAL_DEVIATION": ("bid",),
    "PRICE_SCHEDULE": ("bid",),
}


def classify_text(text: str) -> tuple[str, str]:
    preview = text[:12000]
    best_type = "OTHER"
    best_score = 0
    for document_type, patterns in TYPE_PATTERNS:
        score = sum(1 for pattern in patterns if re.search(pattern, preview, re.IGNORECASE))
        if score > best_score:
            best_type, best_score = document_type, score
    confidence = "HIGH" if best_score >= 2 else "MEDIUM" if best_score == 1 else "LOW"
    return best_type, confidence


def apply_local_classification(page_manifest: list[dict[str, Any]], page_texts: dict[int, str]) -> None:
    for page in page_manifest:
        physical_page = int(page["physicalPage"])
        document_type, confidence = classify_text(page_texts.get(physical_page, ""))
        page["documentType"] = document_type
        page["classificationConfidence"] = confidence
        page["classificationSource"] = "LOCAL_RULE"
        page["sectionCode"] = document_type


def refine_ambiguous_pages(
    page_manifest: list[dict[str, Any]],
    page_texts: dict[int, str],
    *,
    client: ZhipuClient,
    batch_size: int,
    cache_dir: Path | None = None,
    overwrite: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []
    candidates = [
        page for page in page_manifest
        if page.get("classificationConfidence") != "HIGH"
        and page.get("documentType") != "INVOICE"
        and page_texts.get(int(page["physicalPage"]), "").strip()
    ]
    by_page = {int(page["physicalPage"]): page for page in page_manifest}
    consecutive_failures = 0
    for batch in batched(candidates, batch_size):
        prompt_pages: list[dict[str, Any]] = []
        allowed_pages: set[int] = set()
        for page in batch:
            number = int(page["physicalPage"])
            allowed_pages.add(number)
            page_text = page_texts[number]
            preview_source = page_text[:900]
            if len(page_text) > 1100:
                preview_source += "\n...[页面中部省略]...\n" + page_text[-200:]
            redacted, _ = redact_sensitive_text(preview_source)
            neighbor_context: list[dict[str, Any]] = []
            for neighbor_number in (number - 1, number + 1):
                if neighbor_number not in by_page or not page_texts.get(neighbor_number, "").strip():
                    continue
                neighbor_text, _ = redact_sensitive_text(page_texts[neighbor_number][:240])
                neighbor_context.append({
                    "physicalPage": neighbor_number,
                    "localCandidate": by_page[neighbor_number].get("documentType"),
                    "textPreview": neighbor_text,
                })
            prompt_pages.append({
                "physicalPage": number,
                "localCandidate": page.get("documentType"),
                "textPreview": redacted,
                "neighborContext": neighbor_context,
            })
        request_key = stable_hash(
            "classification",
            prompt_pages,
            PROMPT_VERSION,
            client.settings.classifier_model,
            length=20,
        )
        try:
            cache_path = cache_dir / f"classification_{request_key}.json" if cache_dir else None
            cached = read_json(cache_path) if cache_path and not overwrite else None
            if isinstance(cached, dict):
                response = cached
            else:
                response = client.chat_json(
                    system_prompt=CLASSIFIER_SYSTEM_PROMPT,
                    user_prompt=build_classifier_user_prompt(prompt_pages),
                    request_id=f"class_{request_key}",
                    max_tokens=min(client.settings.max_tokens, 2500),
                    model=client.settings.classifier_model,
                )
                if cache_path:
                    atomic_write_json(cache_path, response)
            rows = response.get("data", {}).get("pages")
            if not isinstance(rows, list):
                raise ValueError("分类响应缺少 pages 数组")
            usage.append({
                "stage": "CLASSIFICATION",
                "pages": sorted(allowed_pages),
                "requestId": response.get("requestId"),
                "usage": response.get("usage", {}),
                "cached": isinstance(cached, dict),
                "requestCount": 2 if response.get("repaired") is True else 1,
            })
            returned: set[int] = set()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                number = int(row.get("physicalPage") or 0)
                document_type = str(row.get("documentType") or "OTHER")
                if number not in allowed_pages or document_type not in DOCUMENT_TYPES:
                    continue
                returned.add(number)
                target = by_page[number]
                target["documentType"] = document_type
                target["sectionCode"] = clean_text(row.get("sectionCode"), 200) or document_type
                target["classificationConfidence"] = row.get("confidence") if row.get("confidence") in {"HIGH", "MEDIUM", "LOW"} else "LOW"
                target["classificationSource"] = "GLM_TEXT"
            missing = sorted(allowed_pages - returned)
            if missing:
                warnings.append({"stage": "CLASSIFICATION", "pages": missing, "message": "模型未返回全部页码，保留本地分类"})
            consecutive_failures = 0
        except (ZhipuApiError, ValueError, TypeError) as exc:
            if isinstance(exc, ZhipuApiError) and exc.fatal:
                raise
            consecutive_failures += 1
            warnings.append({
                "stage": "CLASSIFICATION",
                "pages": sorted(allowed_pages),
                "message": f"页面分类复核失败，保留本地结果：{type(exc).__name__}",
            })
            errors.append({
                "stage": "CLASSIFICATION",
                "pages": sorted(allowed_pages),
                "errorType": type(exc).__name__,
                "message": clean_text(str(exc), 500),
            })
            # 模型/参数错误对后续批次通常是确定性的；连续结构错误也不应重复消耗额度。
            if (isinstance(exc, ZhipuApiError) and not exc.retriable) or consecutive_failures >= 2:
                warnings.append({
                    "stage": "CLASSIFICATION",
                    "code": "CLASSIFIER_CIRCUIT_OPEN",
                    "message": "分类器已停止后续批次，避免对确定性错误继续发起请求。",
                })
                break
    return {"warnings": warnings, "errors": errors, "usage": usage}


def domain_pages(page_manifest: list[dict[str, Any]]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for page in page_manifest:
        document_type = str(page.get("documentType") or "OTHER")
        if document_type == "INVOICE":
            continue
        for domain in DOMAIN_BY_DOCUMENT_TYPE.get(document_type, ()):
            result.setdefault(domain, []).append(int(page["physicalPage"]))
    return result


__all__ = [
    "DOMAIN_BY_DOCUMENT_TYPE",
    "apply_local_classification",
    "classify_text",
    "domain_pages",
    "refine_ambiguous_pages",
]
