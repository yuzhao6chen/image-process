"""从原子项目、人员、资质和风险事实派生统一画像字段。"""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
import re
from typing import Any, Iterable

from io_utils import stable_hash
from normalizers import clean_text, finite_number, normalize_company_name, normalize_date
from server_contract import PROFILE_FIELDS


CURRENT_EMPLOYMENT_FRESHNESS_DAYS = 180


def enforce_current_employment_policy(
    merged: dict[str, Any],
    evidence: list[dict[str, Any]],
    *,
    as_of: date,
    freshness_days: int = CURRENT_EMPLOYMENT_FRESHNESS_DAYS,
) -> list[dict[str, Any]]:
    """只允许近期社保证据支持“当前在职”，历史材料保持历史事实。"""

    evidence_by_id = {
        str(item.get("evidenceId")): item
        for item in evidence
        if isinstance(item, dict) and item.get("evidenceId")
    }
    warnings: list[dict[str, Any]] = []
    lower_bound = as_of - timedelta(days=freshness_days)
    for person in merged.get("personnelHistory", []):
        if not isinstance(person, dict):
            continue
        employment = person.get("employmentEvidence")
        if not isinstance(employment, dict):
            continue
        refs = [str(ref) for ref in employment.get("evidenceRefs", []) if ref] if isinstance(employment.get("evidenceRefs"), list) else []
        has_social_insurance = any(
            evidence_by_id.get(ref, {}).get("documentType") == "SOCIAL_INSURANCE"
            for ref in refs
        )
        employment["hasSocialInsuranceEvidence"] = has_social_insurance
        observed_text = normalize_date(employment.get("asOfDate"))
        observed = date.fromisoformat(observed_text) if observed_text else None
        fresh = observed is not None and lower_bound <= observed <= as_of
        requested_current = employment.get("currentEmploymentConfirmed") is True
        employment["currentEmploymentConfirmed"] = requested_current and has_social_insurance and fresh
        if requested_current and not employment["currentEmploymentConfirmed"]:
            warnings.append({
                "stage": "PERSONNEL_POLICY",
                "code": "CURRENT_EMPLOYMENT_DOWNGRADED_TO_HISTORY",
                "personRef": person.get("personKey"),
                "message": f"当前在职候选缺少基准日前 {freshness_days} 天内的社保证据，已降级为历史人员事实。",
            })
    return warnings


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if clean_text(value, 200) else []
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = clean_text(item, 200)
        elif isinstance(item, dict):
            text = clean_text(item.get("name") or item.get("value"), 200)
        else:
            text = None
        if text:
            result.append(text)
    return result


def _weighted(values: Iterable[str]) -> list[dict[str, Any]]:
    counts = Counter(value for value in values if value)
    total = sum(counts.values())
    return [
        {"name": name, "value": count, "ratio": round(count / total, 4)}
        for name, count in counts.most_common()
    ] if total else []


def _performance_amount(item: dict[str, Any]) -> float | int | None:
    contract = item.get("contract") if isinstance(item.get("contract"), dict) else {}
    contract_amount = finite_number(contract.get("amountYuan"))
    return contract_amount if contract_amount is not None else finite_number(item.get("bidAmountYuan"))


def _date_in_last_three_years(value: Any, as_of: date) -> bool:
    normalized = normalize_date(value)
    if not normalized:
        return False
    observed = date.fromisoformat(normalized)
    try:
        lower = as_of.replace(year=as_of.year - 3)
    except ValueError:
        lower = as_of.replace(year=as_of.year - 3, day=28)
    return lower <= observed <= as_of


def _derive_relationships(merged: dict[str, Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in merged.get("customerRelationships", []):
        name = clean_text(item.get("relatedCompanyName"), 200)
        relation_type = str(item.get("relationType") or "CUSTOMER")
        if name:
            group_name = normalize_company_name(name) or name.casefold()
            groups[(group_name, relation_type)] = {
                "relatedCompanyName": name,
                "relationType": relation_type,
                "firstRelationshipDate": normalize_date(item.get("firstRelationshipDate")),
                "lastRelationshipDate": normalize_date(item.get("lastRelationshipDate")),
                "projectRefs": [],
                "projectCount": 0,
                # 金额和次数只从已归并的历史项目重新计算，避免模型汇总值与项目明细重复计数。
                "totalAmountYuan": 0,
                "evidenceRefs": sorted(set(item.get("evidenceRefs", []))) if isinstance(item.get("evidenceRefs"), list) else [],
                "relationshipBasis": "DOCUMENT_DECLARATION",
                "profileAction": "REVIEW_REQUIRED",
                "verificationStatus": "UNVERIFIED",
            }
    for performance in merged.get("performances", []):
        if performance.get("conflictRefs") or not performance.get("evidenceRefs"):
            continue
        contract = performance.get("contract") if isinstance(performance.get("contract"), dict) else {}
        demonstrated_relation = (
            performance.get("isWinner") is True
            or performance.get("performanceStatus") in {"AWARDED", "CONTRACTED", "ONGOING", "COMPLETED", "ACCEPTED"}
            or any(
                contract.get(key) is not None and contract.get(key) != ""
                for key in ("contractNo", "contractName", "amountYuan", "signDate", "startDate", "contractContent")
            )
        )
        if not demonstrated_relation:
            continue
        name = clean_text(performance.get("customerName") or performance.get("ownerCompanyName"), 200)
        if not name:
            continue
        key = (normalize_company_name(name) or name.casefold(), "CUSTOMER")
        target = groups.setdefault(key, {
            "relatedCompanyName": name,
            "relationType": "CUSTOMER",
            "firstRelationshipDate": None,
            "lastRelationshipDate": None,
            "projectRefs": [],
            "projectCount": 0,
            "totalAmountYuan": 0,
            "evidenceRefs": [],
            "relationshipBasis": "PROJECT_EVIDENCE",
            "profileAction": "PROFILE_CANDIDATE",
            "verificationStatus": "UNVERIFIED",
        })
        if not isinstance(target.get("projectRefs"), list):
            target["projectRefs"] = []
        if not isinstance(target.get("evidenceRefs"), list):
            target["evidenceRefs"] = []
        project_key = performance.get("performanceKey")
        if project_key and project_key not in target["projectRefs"]:
            target["projectRefs"].append(project_key)
        target["relationshipBasis"] = "PROJECT_EVIDENCE"
        target["profileAction"] = "PROFILE_CANDIDATE"
        target["projectCount"] = len(target["projectRefs"])
        amount = _performance_amount(performance)
        if amount is not None:
            target["totalAmountYuan"] = (finite_number(target.get("totalAmountYuan")) or 0) + amount
        observed = normalize_date(performance.get("awardDate") or contract.get("signDate"))
        if observed:
            existing_dates = [
                normalized for value in [target.get("firstRelationshipDate"), target.get("lastRelationshipDate"), observed]
                if (normalized := normalize_date(value))
            ]
            target["firstRelationshipDate"] = min(existing_dates)
            target["lastRelationshipDate"] = max(existing_dates)
        refs = performance.get("evidenceRefs") if isinstance(performance.get("evidenceRefs"), list) else []
        target["evidenceRefs"] = sorted(set([*target.get("evidenceRefs", []), *refs]))
    result = list(groups.values())
    for item in result:
        item["relationshipKey"] = f"business-relation:sha256:{stable_hash(item.get('relatedCompanyName', '').casefold(), item.get('relationType'), length=24)}"
        if not item.get("projectRefs"):
            item["totalAmountYuan"] = None
    return result


def derive_profile_fields(
    merged: dict[str, Any],
    capability_evidence: list[dict[str, Any]],
    *,
    as_of: date,
) -> dict[str, Any]:
    performances = [
        item for item in merged.get("performances", [])
        if isinstance(item, dict) and item.get("evidenceRefs") and not item.get("conflictRefs")
    ]
    industries = [name for item in performances for name in _names(item.get("industry"))]
    regions: list[str] = []
    project_types: list[str] = []
    for item in performances:
        region = "".join(value for value in [clean_text(item.get("province"), 64), clean_text(item.get("city"), 64)] if value)
        if region:
            regions.append(region)
        project_type = clean_text(item.get("projectType"), 100)
        if project_type:
            project_types.append(project_type)
    awarded = [
        item for item in performances
        if item.get("isWinner") is True or item.get("performanceStatus") in {"AWARDED", "CONTRACTED", "ONGOING", "COMPLETED", "ACCEPTED"}
    ]
    amounts = [amount for item in awarded if (amount := _performance_amount(item)) is not None]
    dated_awarded = [item for item in awarded if normalize_date(item.get("awardDate"))]
    recent_count = sum(1 for item in dated_awarded if _date_in_last_three_years(item.get("awardDate"), as_of))
    relationship_candidates = _derive_relationships(merged)
    relationships = [item for item in relationship_candidates if item.get("projectRefs")]
    unverified_relationships = [item for item in relationship_candidates if not item.get("projectRefs")]
    capability_tags = sorted({
        str(item.get("capabilityName"))
        for item in capability_evidence
        if item.get("capabilityName")
        and item.get("supportStatus") == "SUPPORTED"
        and item.get("claimType") in {"DELIVERED", "CONTRACTED", "INFERRED"}
    })
    risk_tags = sorted({
        str(item.get("riskType"))
        for item in merged.get("riskAndCompliance", [])
        if item.get("riskType")
        and item.get("evidenceRefs")
        and not item.get("conflictRefs")
        and str(item.get("status") or "").upper() not in {"RESOLVED", "CLOSED", "NOT_FOUND", "NONE", "CLEAR", "NO_RECORD"}
        and not re.search(r"未发现|未查询到|无.{0,6}(?:记录|异常|风险)", str(item.get("title") or ""))
    })
    derived = {
        "mainIndustries": _weighted(industries),
        "mainRegions": _weighted(regions),
        "averageWinAmount": round(sum(float(value) for value in amounts) / len(amounts), 2) if amounts else None,
        "winProjectCount3y": recent_count if dated_awarded else None,
        "buyerRelationships": relationships,
        "unverifiedRelationshipCandidates": unverified_relationships,
        "capabilityTags": capability_tags,
        "riskTags": risk_tags,
        "tenderRecordSummary": None,
        "evaluationScores": None,
        "suggestedPreferences": {
            "targetIndustries": _weighted(industries),
            "targetRegions": _weighted(regions),
            "projectTypes": _weighted(project_types),
            "amountRange": {
                "minYuan": min(amounts) if amounts else None,
                "maxYuan": max(amounts) if amounts else None,
            },
            "status": "NOT_FOR_AUTOMATIC_IMPORT",
            "reason": "历史投标和履约事实不能代表企业当前主动经营偏好",
        },
    }
    return derived


def profile_data_gaps(candidate_codes: set[str]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for field in PROFILE_FIELDS:
        if field.field_code in candidate_codes:
            continue
        reason = {
            "USER_CONFIRMED_ONLY": "USER_NOT_PROVIDED",
            "POST_IMPORT_DERIVED": "DEPENDENCY_MISSING",
            "EXTERNAL_ONLY": "NOT_COLLECTED",
        }.get(field.extraction_mode, "SOURCE_EMPTY")
        gaps.append({
            "fieldCode": field.field_code,
            "reason": reason,
            "extractionMode": field.extraction_mode,
        })
    return gaps


__all__ = ["CURRENT_EMPLOYMENT_FRESHNESS_DAYS", "derive_profile_fields", "enforce_current_employment_policy", "profile_data_gaps"]
