"""按页面领域调用文本模型，校验页码并生成稳定证据标识。"""

from __future__ import annotations

import copy
import re
import unicodedata
from pathlib import Path
from typing import Any

from io_utils import atomic_write_json, read_json, stable_hash
from normalizers import clean_text, clean_verbatim_text, redact_sensitive_text
from prompts import BASE_SYSTEM_PROMPT, PROMPT_VERSION, build_domain_user_prompt
from zhipu_client import ZhipuApiError, ZhipuClient


DOMAIN_OUTPUT_KEYS: dict[str, tuple[str, ...]] = {
    "enterprise": ("companyCreateCandidate", "aliases"),
    "qualifications": ("qualifications",),
    "intellectual_properties": ("intellectualProperties",),
    "finance": ("financialHistory",),
    "risk": ("riskAndCompliance",),
    "personnel": ("personnelHistory",),
    "performances": ("performances", "customerRelationships"),
    "products": ("productsAndEquipment",),
    "solutions": ("solutionClaims",),
    "delivery": ("deliveryClaims",),
    "bid": ("bidSpecific",),
}

ALLOWED_FIELDS_BY_OUTPUT: dict[str, set[str]] = {
    "companyCreateCandidate": {
        "companyName", "creditCode", "legalPerson", "registeredCapital", "establishedDate", "companyType",
        "businessScope", "registeredAddress", "province", "city", "employeeCount", "documentAsOfDate",
        "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "aliases": {"aliasName", "aliasType", "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs"},
    "qualifications": {
        "qualificationKey", "certName", "certLevel", "certNo", "issuingAuthority", "issueDate", "expiryDate",
        "status", "certScope", "holderName", "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "intellectualProperties": {
        "propertyKey", "propertyType", "propertyName", "registrationNo", "applicationNo", "holderName", "status",
        "applicationDate", "issueDate", "expiryDate", "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "financialHistory": {
        "financialKey", "fiscalYear", "currency", "unit", "totalAssets", "totalLiabilities", "revenue",
        "operatingProfit", "netProfit", "netAssets", "cashFlow", "auditorName", "auditOpinion", "reportDate",
        "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "riskAndCompliance": {
        "riskKey", "riskType", "title", "status", "severity", "occurredAt", "resolvedAt", "amountYuan",
        "queryAsOfDate", "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "personnelHistory": {
        "personKey", "personnelType", "personName", "personRole", "roleInBid", "profession", "professionalTitle",
        "education", "workExperience", "certificates", "projectExperienceRefs", "employmentEvidence",
        "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "performances": {
        "performanceKey", "projectName", "projectCode", "tenderCode", "projectNature", "tenderMethod",
        "organizationForm", "customerName", "ownerCompanyName", "agencyCompanyName", "industry", "projectType",
        "province", "city", "locationText", "role", "relationType", "ranking", "isWinner", "isConsortium",
        "isConsortiumLeader", "consortiumMembers", "projectPersonnel", "awardDate", "bidAmountYuan",
        "estimatedAmountYuan", "contract", "performanceStatus", "acceptanceDate", "duration", "qualityRequirement",
        "scopeItems", "deliveredCapabilities", "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "customerRelationships": {
        "relationshipKey", "relatedCompanyName", "relationType", "firstRelationshipDate", "lastRelationshipDate",
        "projectRefs", "projectCount", "totalAmountYuan", "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "productsAndEquipment": {
        "productKey", "productName", "brand", "model", "manufacturer", "rightHolder", "companyRole",
        "specifications", "certificates", "usedInPerformanceRefs", "profileAction", "qualityLevel",
        "evidenceRefs", "conflictRefs",
    },
    "solutionClaims": {
        "claimKey", "category", "name", "description", "keywords", "claimType", "supportedByPerformanceRefs",
        "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "deliveryClaims": {
        "claimKey", "category", "name", "description", "keywords", "claimType", "supportedByPerformanceRefs",
        "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
    "bidSpecific": {
        "projectName", "tenderCode", "buyerName", "bidAmountYuan", "durationCommitment", "qualityCommitment",
        "guaranteeAmountYuan", "deviations", "profileAction", "qualityLevel", "evidenceRefs", "conflictRefs",
    },
}

CONTRACT_FIELDS = {
    "contractNo", "contractName", "amountYuan", "rawAmountText", "rawUnit", "currency", "signDate", "startDate",
    "plannedEndDate", "actualEndDate", "contractContent", "performanceStatus", "evidenceRefs", "conflictRefs",
}
PERSON_CERTIFICATE_FIELDS = {
    "certName", "certNo", "qualificationType", "certLevel", "profession", "additionalProfessions",
    "issuingAuthority", "issueDate", "validFrom", "expiryDate", "registrationStatus", "scope", "evidenceRefs",
}
EMPLOYMENT_FIELDS = {"asOfDate", "hasSocialInsuranceEvidence", "currentEmploymentConfirmed", "evidenceRefs"}

SOURCE_STRENGTH_BY_TYPE = {
    "BUSINESS_LICENSE": "A",
    "QUALIFICATION_CERTIFICATE": "A",
    "INTELLECTUAL_PROPERTY_CERTIFICATE": "A",
    "AWARD_NOTICE": "A",
    "CONTRACT": "A",
    "ACCEPTANCE_REPORT": "A",
    "AUDIT_REPORT": "A",
    "PRODUCT_CERTIFICATE": "B",
    "SOCIAL_INSURANCE": "B",
    "PERSONNEL_CERTIFICATE": "B",
    "CREDIT_REPORT": "B",
    "TAX_CERTIFICATE": "B",
    "PERFORMANCE_SUMMARY": "C",
    "COMPANY_PROFILE": "C",
    "PERSONNEL_RESUME": "C",
    "TECHNICAL_SOLUTION": "D",
    "IMPLEMENTATION_PLAN": "D",
    "QUALITY_PLAN": "D",
    "RISK_PLAN": "D",
    "AFTER_SALES_PLAN": "D",
    "TRAINING_PLAN": "D",
    "ACCEPTANCE_PLAN": "D",
    "BID_LETTER": "D",
    "PRICE_SCHEDULE": "D",
}


def _filter_domain_payload(domain: str, payload: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    extras = sorted(set(payload) - set(DOMAIN_OUTPUT_KEYS[domain]))
    if extras:
        warnings.append(f"{domain} 丢弃模型返回的未声明顶层字段：{', '.join(extras)}")
    result: dict[str, Any] = {}
    for output_key in DOMAIN_OUTPUT_KEYS[domain]:
        raw_value = payload.get(output_key)
        allowed = ALLOWED_FIELDS_BY_OUTPUT[output_key]
        if output_key in {"companyCreateCandidate", "bidSpecific"}:
            rows = [raw_value] if isinstance(raw_value, dict) else []
        else:
            rows = [item for item in raw_value if isinstance(item, dict)] if isinstance(raw_value, list) else []
        filtered_rows: list[dict[str, Any]] = []
        for row in rows:
            extras = sorted(set(row) - allowed)
            if extras:
                warnings.append(f"{output_key} 丢弃模型返回的未声明字段：{', '.join(extras)}")
            filtered = {key: copy.deepcopy(value) for key, value in row.items() if key in allowed}
            if output_key == "performances" and isinstance(filtered.get("contract"), dict):
                contract_extras = sorted(set(filtered["contract"]) - CONTRACT_FIELDS)
                if contract_extras:
                    warnings.append(f"performances.contract 丢弃模型返回的未声明字段：{', '.join(contract_extras)}")
                filtered["contract"] = {key: value for key, value in filtered["contract"].items() if key in CONTRACT_FIELDS}
            if output_key == "personnelHistory":
                if isinstance(filtered.get("certificates"), list):
                    certificates: list[dict[str, Any]] = []
                    for certificate in filtered["certificates"]:
                        if not isinstance(certificate, dict):
                            continue
                        certificate_extras = sorted(set(certificate) - PERSON_CERTIFICATE_FIELDS)
                        if certificate_extras:
                            warnings.append(f"personnelHistory.certificates 丢弃模型返回的未声明字段：{', '.join(certificate_extras)}")
                        certificates.append({key: value for key, value in certificate.items() if key in PERSON_CERTIFICATE_FIELDS})
                    filtered["certificates"] = certificates
                if isinstance(filtered.get("employmentEvidence"), dict):
                    employment_extras = sorted(set(filtered["employmentEvidence"]) - EMPLOYMENT_FIELDS)
                    if employment_extras:
                        warnings.append(f"personnelHistory.employmentEvidence 丢弃模型返回的未声明字段：{', '.join(employment_extras)}")
                    filtered["employmentEvidence"] = {
                        key: value for key, value in filtered["employmentEvidence"].items() if key in EMPLOYMENT_FIELDS
                    }
            filtered_rows.append(filtered)
        result[output_key] = (filtered_rows[0] if filtered_rows else {}) if output_key in {"companyCreateCandidate", "bidSpecific"} else filtered_rows
    return result


def _chunk_pages(
    page_numbers: list[int],
    page_texts: dict[int, str],
    *,
    max_pages: int,
    max_chars: int,
) -> list[list[int]]:
    chunks: list[list[int]] = []
    current: list[int] = []
    current_chars = 0
    for page in sorted(set(page_numbers)):
        page_chars = len(page_texts.get(page, ""))
        if current and (len(current) >= max_pages or current_chars + page_chars > max_chars):
            chunks.append(current)
            current, current_chars = [], 0
        current.append(page)
        current_chars += page_chars
    if current:
        chunks.append(current)
    return chunks


def _remap_refs(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [_remap_refs(item, mapping) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "evidenceRefs" and isinstance(item, list):
                result[key] = [
                    mapped
                    for ref in item
                    if (mapped := mapping.get(str(ref), str(ref)))
                ]
            else:
                result[key] = _remap_refs(item, mapping)
        return result
    return value


def _normalize_evidence(
    domain: str,
    payload: dict[str, Any],
    *,
    allowed_pages: set[int],
    page_metadata: dict[int, dict[str, Any]],
    page_texts: dict[int, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        raw_evidence = []
    normalized: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}

    def compact(value: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))

    for index, item in enumerate(raw_evidence):
        if not isinstance(item, dict):
            continue
        old_id = str(item.get("evidenceId") or f"evidence-{index + 1}")
        try:
            physical_page = int(item.get("physicalPage") or 0)
        except (TypeError, ValueError):
            physical_page = 0
        if physical_page not in allowed_pages:
            mapping[old_id] = ""
            warnings.append(f"丢弃越界证据页码：{physical_page}")
            continue
        source_text = clean_verbatim_text(item.get("sourceText"), 1000)
        source_text = redact_sensitive_text(source_text)[0] if source_text else None
        field_path = clean_text(item.get("fieldPath"), 500)
        if not source_text or not field_path:
            mapping[old_id] = ""
            warnings.append(f"第 {physical_page} 页证据缺少 fieldPath 或 sourceText")
            continue
        redacted_page_text, _ = redact_sensitive_text(page_texts.get(physical_page, ""))
        if compact(source_text) not in compact(redacted_page_text):
            mapping[old_id] = ""
            warnings.append(f"第 {physical_page} 页证据原文无法在页面文本中定位：{field_path}")
            continue
        metadata = page_metadata.get(physical_page, {})
        document_type = str(metadata.get("documentType") or "OTHER")
        normalized_value = item.get("normalizedValue")
        normalized_text = str(normalized_value or "").strip()
        normalized_field_path = re.sub(r"[^a-z0-9]", "", field_path.casefold())
        is_company_credit_code = "creditcode" in normalized_field_path or "统一社会信用代码" in field_path
        if not is_company_credit_code and (
            re.fullmatch(r"\d{16,19}", normalized_text)
            or re.fullmatch(r"1[3-9]\d{9}", normalized_text)
        ):
            normalized_value = None
            warnings.append(f"第 {physical_page} 页证据规范值疑似个人敏感标识，已移除：{field_path}")
        evidence_id = f"evidence:{domain}:{stable_hash(physical_page, field_path, source_text, length=20)}"
        mapping[old_id] = evidence_id
        normalized.append({
            "evidenceId": evidence_id,
            "physicalPage": physical_page,
            "printedPage": metadata.get("printedPage"),
            "sectionCode": metadata.get("sectionCode") or document_type,
            "documentType": document_type,
            "fieldPath": field_path,
            "sourceText": source_text,
            "normalizedValue": normalized_value,
            "sourceStrength": SOURCE_STRENGTH_BY_TYPE.get(document_type, "C"),
            "ocrSource": metadata.get("contentSource") or "UNKNOWN",
            "visualReviewed": metadata.get("visualReviewed") is True,
            "pageImageHash": None,
            "warnings": [],
        })
    cleaned_payload = copy.deepcopy(payload)
    cleaned_payload.pop("evidence", None)
    cleaned_payload.pop("warnings", None)
    filtered_payload = _filter_domain_payload(domain, cleaned_payload, warnings)
    return _remap_refs(filtered_payload, mapping), normalized, warnings


def _default_domain_payload(domain: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in DOMAIN_OUTPUT_KEYS[domain]:
        result[key] = {} if key in {"companyCreateCandidate", "bidSpecific"} else []
    return result


def extract_domains(
    domain_to_pages: dict[str, list[int]],
    page_texts: dict[int, str],
    page_manifest: list[dict[str, Any]],
    *,
    client: ZhipuClient,
    cache_dir: Path,
    raw_dir: Path,
    max_pages: int,
    max_chars: int,
    overwrite: bool,
) -> dict[str, Any]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest_by_page = {int(item["physicalPage"]): item for item in page_manifest}
    domain_payloads: dict[str, list[dict[str, Any]]] = {domain: [] for domain in DOMAIN_OUTPUT_KEYS}
    all_evidence: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    usage: list[dict[str, Any]] = []

    for domain in DOMAIN_OUTPUT_KEYS:
        pages = domain_to_pages.get(domain, [])
        chunks = _chunk_pages(pages, page_texts, max_pages=max_pages, max_chars=max_chars)
        if not chunks:
            domain_payloads[domain].append(_default_domain_payload(domain))
            continue
        for chunk_index, chunk_pages in enumerate(chunks, 1):
            prompt_pages: list[dict[str, Any]] = []
            sensitive_counts: dict[str, int] = {}
            for page in chunk_pages:
                text, findings = redact_sensitive_text(page_texts.get(page, ""))
                for finding in findings:
                    kind = str(finding["type"])
                    sensitive_counts[kind] = sensitive_counts.get(kind, 0) + 1
                manifest = manifest_by_page[page]
                prompt_pages.append({
                    "physicalPage": page,
                    "printedPage": manifest.get("printedPage"),
                    "documentType": manifest.get("documentType"),
                    "sectionCode": manifest.get("sectionCode"),
                    "text": text,
                })
            cache_key = stable_hash(
                domain,
                chunk_pages,
                [page_texts.get(page, "") for page in chunk_pages],
                PROMPT_VERSION,
                client.settings.text_model,
                length=24,
            )
            normalized_path = cache_dir / f"{domain}_{chunk_index:03d}_{cache_key}.json"
            raw_path = raw_dir / f"{domain}_{chunk_index:03d}_{cache_key}.json"
            cached = read_json(normalized_path)
            if isinstance(cached, dict) and not overwrite:
                domain_payloads[domain].append(cached.get("payload") or _default_domain_payload(domain))
                all_evidence.extend(cached.get("evidence") or [])
                warnings.extend(cached.get("warnings") or [])
                usage.append({
                    "domain": domain,
                    "pages": chunk_pages,
                    "requestId": None,
                    "usage": {},
                    "cached": True,
                    "requestCount": 0,
                })
                continue
            try:
                response = client.chat_json(
                    system_prompt=BASE_SYSTEM_PROMPT,
                    user_prompt=build_domain_user_prompt(domain, prompt_pages),
                    request_id=f"extract_{domain}_{cache_key}",
                )
                payload = response.get("data")
                if not isinstance(payload, dict):
                    raise ValueError("领域抽取响应不是 JSON 对象")
                normalized_payload, evidence, evidence_warnings = _normalize_evidence(
                    domain,
                    payload,
                    allowed_pages=set(chunk_pages),
                    page_metadata={page: manifest_by_page[page] for page in chunk_pages},
                    page_texts={page: page_texts.get(page, "") for page in chunk_pages},
                )
                for key in DOMAIN_OUTPUT_KEYS[domain]:
                    if key not in normalized_payload:
                        normalized_payload[key] = {} if key in {"companyCreateCandidate", "bidSpecific"} else []
                chunk_warnings = [
                    {"stage": "DOMAIN_EXTRACTION", "domain": domain, "pages": chunk_pages, "message": message}
                    for message in evidence_warnings
                ]
                if sensitive_counts:
                    chunk_warnings.append({
                        "stage": "SENSITIVE_REDACTION",
                        "domain": domain,
                        "pages": chunk_pages,
                        "message": "二次模型输入前已脱敏",
                        "counts": sensitive_counts,
                    })
                domain_payloads[domain].append(normalized_payload)
                all_evidence.extend(evidence)
                warnings.extend(chunk_warnings)
                usage.append({
                    "domain": domain,
                    "pages": chunk_pages,
                    "requestId": response.get("requestId"),
                    "usage": response.get("usage"),
                    "requestCount": 2 if response.get("repaired") is True else 1,
                })
                atomic_write_json(raw_path, {
                    "requestId": response.get("requestId"),
                    "model": response.get("model"),
                    "pages": chunk_pages,
                    "content": response.get("rawContent"),
                })
                atomic_write_json(normalized_path, {
                    "domain": domain,
                    "pages": chunk_pages,
                    "payload": normalized_payload,
                    "evidence": evidence,
                    "warnings": chunk_warnings,
                })
            except (ZhipuApiError, ValueError, TypeError) as exc:
                error = {
                    "stage": "DOMAIN_EXTRACTION",
                    "domain": domain,
                    "pages": chunk_pages,
                    "errorType": type(exc).__name__,
                    "message": str(exc)[:500],
                }
                errors.append(error)
                domain_payloads[domain].append(_default_domain_payload(domain))
                # API 的非重试型错误通常来自模型、参数或请求规模配置，继续跑其他领域只会重复消耗。
                if isinstance(exc, ZhipuApiError) and (exc.fatal or not exc.retriable):
                    raise

    unique_evidence = {item["evidenceId"]: item for item in all_evidence if isinstance(item, dict) and item.get("evidenceId")}
    return {
        "domainPayloads": domain_payloads,
        "evidence": list(unique_evidence.values()),
        "warnings": warnings,
        "errors": errors,
        "usage": usage,
    }


__all__ = ["DOMAIN_OUTPUT_KEYS", "extract_domains"]
