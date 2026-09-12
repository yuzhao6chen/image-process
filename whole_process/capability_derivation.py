"""从已抽取事实确定性生成能力证据和八类能力评估候选。"""

from __future__ import annotations

from typing import Any

from io_utils import stable_hash, utc_now_iso
from normalizers import clean_text
from server_contract import CAPABILITY_DIMENSIONS


def _refs(item: dict[str, Any]) -> list[str]:
    values = item.get("evidenceRefs")
    return [str(value) for value in values if value] if isinstance(values, list) else []


def _capability(
    name: str,
    *,
    category: str,
    claim_type: str,
    source_refs: list[str],
    entity_refs: list[str],
    evidence_by_id: dict[str, dict[str, Any]],
    observed_at: str | None = None,
    support_allowed: bool = True,
) -> dict[str, Any]:
    normalized = clean_text(name, 300) or ""
    strength_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    strengths = [
        str(evidence_by_id[ref].get("sourceStrength"))
        for ref in source_refs
        if ref in evidence_by_id and evidence_by_id[ref].get("sourceStrength") in strength_order
    ]
    source_strength = min(strengths, key=lambda value: strength_order[value]) if strengths else None
    dimension = {
        "QUALIFICATION": "TECHNICAL",
        "INTELLECTUAL_PROPERTY": "TECHNICAL",
        "PROJECT_DELIVERY": "SIMILAR_PERFORMANCE",
    }.get(category, "TECHNICAL" if "TECHNICAL" in category or "SOLUTION" in category else "DELIVERY")
    supported = bool(source_refs) and support_allowed
    return {
        "capabilityKey": f"capability:sha256:{stable_hash(category, normalized.casefold(), claim_type, length=20)}",
        "capabilityCode": f"DOCUMENT_{stable_hash(normalized.casefold(), length=12).upper()}",
        "capabilityName": normalized,
        "dimension": dimension,
        "category": category,
        "claimType": claim_type,
        "supportStatus": "SUPPORTED" if supported else "INSUFFICIENT_DATA",
        "summary": f"{normalized} 由 {len(set(source_refs))} 条文档证据支持" if supported else f"{normalized} 缺少可用于画像发布的有效证据",
        "supportingEntityRefs": sorted({ref for ref in entity_refs if ref and ref != "None"}),
        "evidenceRefs": sorted(set(source_refs)),
        "firstObservedAt": observed_at,
        "lastObservedAt": observed_at,
        "projectCount": len({ref for ref in entity_refs if ref and ref != "None"}) if category == "PROJECT_DELIVERY" else 0,
        "sourceStrength": source_strength,
        "profileAction": "CLAIM_ONLY" if claim_type == "DECLARED" else "PROFILE_CANDIDATE",
        "verificationStatus": "UNVERIFIED",
    }


def derive_capability_evidence(merged: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    evidence_by_id = {str(item.get("evidenceId")): item for item in evidence if item.get("evidenceId")}
    for qualification in merged.get("qualifications", []):
        name = clean_text(qualification.get("certName"), 200)
        if name:
            level = clean_text(qualification.get("certLevel"), 64)
            display = f"{name} {level}" if level else name
            candidates.append(_capability(
                display,
                category="QUALIFICATION",
                claim_type="INFERRED",
                source_refs=_refs(qualification),
                entity_refs=[str(qualification.get("qualificationKey"))],
                evidence_by_id=evidence_by_id,
                observed_at=qualification.get("issueDate"),
                support_allowed=(
                    qualification.get("status") in {"VALID", "EXPIRING"}
                    and qualification.get("subjectMatchStatus") == "MATCH"
                    and not qualification.get("conflictRefs")
                ),
            ))
    for item in merged.get("intellectualProperties", []):
        name = clean_text(item.get("propertyName"), 512)
        if name:
            candidates.append(_capability(
                name,
                category="INTELLECTUAL_PROPERTY",
                claim_type="INFERRED",
                source_refs=_refs(item),
                entity_refs=[str(item.get("propertyKey"))],
                evidence_by_id=evidence_by_id,
                observed_at=item.get("issueDate") or item.get("applicationDate"),
                support_allowed=(
                    str(item.get("status") or "").upper() in {"VALID", "ACTIVE"}
                    and item.get("subjectMatchStatus") == "MATCH"
                    and not item.get("conflictRefs")
                ),
            ))
    for performance in merged.get("performances", []):
        status = str(performance.get("performanceStatus") or "UNKNOWN")
        performance_refs = _refs(performance)
        source_types = {
            str(evidence_by_id[ref].get("documentType") or "")
            for ref in performance_refs
            if ref in evidence_by_id
        }
        if status in {"COMPLETED", "ACCEPTED"} and "ACCEPTANCE_REPORT" in source_types:
            claim_type = "DELIVERED"
        elif status in {"AWARDED", "CONTRACTED", "ONGOING", "COMPLETED", "ACCEPTED"} and source_types.intersection({"CONTRACT", "AWARD_NOTICE"}):
            claim_type = "CONTRACTED"
        else:
            claim_type = "UNKNOWN"
        for name in performance.get("deliveredCapabilities", []):
            if clean_text(name, 300):
                candidates.append(_capability(
                    str(name),
                    category="PROJECT_DELIVERY",
                    claim_type=claim_type,
                    source_refs=performance_refs,
                    entity_refs=[str(performance.get("performanceKey"))],
                    evidence_by_id=evidence_by_id,
                    observed_at=(
                        performance.get("acceptanceDate")
                        or (performance.get("contract") or {}).get("actualEndDate")
                        or performance.get("awardDate")
                        or (performance.get("contract") or {}).get("signDate")
                    ),
                    support_allowed=claim_type != "UNKNOWN" and not performance.get("conflictRefs"),
                ))
    for claim in [*merged.get("solutionClaims", []), *merged.get("deliveryClaims", [])]:
        name = clean_text(claim.get("name"), 300)
        if name:
            candidates.append(_capability(
                name,
                category=str(claim.get("category") or "DECLARED"),
                claim_type="DECLARED",
                source_refs=_refs(claim),
                entity_refs=[str(claim.get("claimKey"))],
                evidence_by_id=evidence_by_id,
                support_allowed=not claim.get("conflictRefs"),
            ))

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in candidates:
        key = (item["capabilityName"].casefold(), item["category"], item["claimType"])
        if key not in grouped:
            grouped[key] = item
            continue
        grouped[key]["supportingEntityRefs"] = sorted(set(grouped[key]["supportingEntityRefs"] + item["supportingEntityRefs"]))
        grouped[key]["evidenceRefs"] = sorted(set(grouped[key]["evidenceRefs"] + item["evidenceRefs"]))
        observed = [value for value in [grouped[key].get("firstObservedAt"), grouped[key].get("lastObservedAt"), item.get("firstObservedAt"), item.get("lastObservedAt")] if value]
        grouped[key]["firstObservedAt"] = min(observed) if observed else None
        grouped[key]["lastObservedAt"] = max(observed) if observed else None
        grouped[key]["projectCount"] = len(grouped[key]["supportingEntityRefs"]) if grouped[key]["category"] == "PROJECT_DELIVERY" else 0
        if grouped[key]["evidenceRefs"] and item.get("supportStatus") == "SUPPORTED":
            grouped[key]["supportStatus"] = "SUPPORTED"
        strengths = [value for value in [grouped[key].get("sourceStrength"), item.get("sourceStrength")] if value]
        if strengths:
            strength_order = {"A": 0, "B": 1, "C": 2, "D": 3}
            grouped[key]["sourceStrength"] = min(strengths, key=lambda value: strength_order.get(value, 99))
        grouped[key]["summary"] = (
            f"{grouped[key]['capabilityName']} 由 {len(grouped[key]['evidenceRefs'])} 条文档证据支持"
            if grouped[key]["supportStatus"] == "SUPPORTED"
            else f"{grouped[key]['capabilityName']} 缺少可用于画像发布的有效证据"
        )
    return list(grouped.values())


def _dimension_input(merged: dict[str, Any], derived: dict[str, Any], dimension: str) -> tuple[list[str], list[str], list[str]]:
    claims: list[str] = []
    evidence: list[str] = []
    unknowns: list[str] = []
    performances = merged.get("performances", [])
    if dimension == "industry_capability":
        claims = [str(item.get("name")) for item in derived.get("mainIndustries", []) if item.get("name")]
        evidence = [ref for item in performances for ref in _refs(item)]
        if not claims:
            unknowns.append("缺少已核验历史项目行业")
    elif dimension == "technical_capability":
        sources = [
            *[item for item in merged.get("qualifications", []) if item.get("status") in {"VALID", "EXPIRING"} and item.get("subjectMatchStatus") == "MATCH" and not item.get("conflictRefs")],
            *[item for item in merged.get("intellectualProperties", []) if str(item.get("status") or "").upper() in {"VALID", "ACTIVE"} and item.get("subjectMatchStatus") == "MATCH" and not item.get("conflictRefs")],
        ]
        claims = [
            str(item.get("certName") or item.get("propertyName"))
            for item in sources
            if item.get("certName") or item.get("propertyName")
        ]
        evidence = [ref for item in sources for ref in _refs(item)]
        if not claims:
            unknowns.append("缺少企业资质或自有知识产权证据")
    elif dimension == "similar_performance_capability":
        claims = [str(item.get("projectName")) for item in performances if item.get("projectName")]
        evidence = [ref for item in performances for ref in _refs(item)]
        if not claims:
            unknowns.append("缺少可识别的历史项目业绩")
    elif dimension == "regional_delivery_capability":
        claims = [str(item.get("name")) for item in derived.get("mainRegions", []) if item.get("name")]
        evidence = [ref for item in performances for ref in _refs(item)]
        if not claims:
            unknowns.append("缺少历史项目实施区域")
    elif dimension == "amount_experience_capability":
        average = derived.get("averageWinAmount")
        count = derived.get("winProjectCount3y")
        if average is not None:
            claims.append(f"平均中标金额 {average} 元")
        if count is not None:
            claims.append(f"近三年中标项目 {count} 个")
        evidence = [ref for item in performances for ref in _refs(item)]
        if not claims:
            unknowns.append("缺少已确认的中标金额或中标日期")
    elif dimension == "personnel_resource_capability":
        people = [
            item for item in merged.get("personnelHistory", [])
            if isinstance(item.get("employmentEvidence"), dict)
            and item["employmentEvidence"].get("currentEmploymentConfirmed") is True
        ]
        claims = [str(item.get("personRole") or item.get("roleInBid") or item.get("profession")) for item in people if item.get("personRole") or item.get("roleInBid") or item.get("profession")]
        evidence = [ref for item in people for ref in _refs(item)]
        if not claims:
            unknowns.append("缺少带当前时点任职证据的专业人员信息；历史简历不代表当前人员资源")
    elif dimension == "buyer_relationship_capability":
        relationships = [item for item in derived.get("buyerRelationships", []) if item.get("projectRefs")]
        claims = [str(item.get("relatedCompanyName")) for item in relationships if item.get("relatedCompanyName")]
        evidence = [ref for item in relationships for ref in item.get("evidenceRefs", [])]
        if not claims:
            unknowns.append("缺少由项目或合同支持的客户关系")
    elif dimension == "tender_performance_capability":
        tender_records = [item for item in performances if item.get("isWinner") is not None or item.get("ranking") is not None]
        claims = [str(item.get("projectName")) for item in tender_records if item.get("projectName")]
        evidence = [ref for item in tender_records for ref in _refs(item)]
        if not claims:
            unknowns.append("缺少参标、候选、排名或最终中标信息")
    return sorted(set(claims)), sorted(set(evidence)), unknowns


CONFLICT_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "industry_capability": ("industry", "projecttype"),
    "technical_capability": ("cert", "qualification", "property", "capability", "solution"),
    "similar_performance_capability": ("project", "performance", "contract", "scope", "customer"),
    "regional_delivery_capability": ("province", "city", "location", "region"),
    "amount_experience_capability": ("amount", "awarddate", "signdate", "iswinner"),
    "personnel_resource_capability": ("person", "profession", "roleinbid", "certificate"),
    "buyer_relationship_capability": ("customer", "owner", "buyer", "relatedcompany", "relationtype"),
    "tender_performance_capability": ("tender", "ranking", "iswinner", "bidamount", "awarddate"),
}


def _has_relevant_conflict(conflicts: list[dict[str, Any]], dimension: str) -> bool:
    hints = CONFLICT_FIELD_HINTS.get(dimension, ())
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        field_path = str(conflict.get("fieldPath") or "").replace("_", "").casefold()
        if any(hint in field_path for hint in hints):
            return True
    return False


def derive_capability_assessments(
    merged: dict[str, Any],
    derived: dict[str, Any],
    *,
    conflicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    calculated_at = utc_now_iso()
    result: list[dict[str, Any]] = []
    for dimension_code, definition in CAPABILITY_DIMENSIONS.items():
        claims, evidence, unknowns = _dimension_input(merged, derived, dimension_code)
        status = "SUPPORTED" if claims and evidence else "INSUFFICIENT_DATA"
        if claims and _has_relevant_conflict(conflicts, dimension_code):
            status = "CONFLICTED"
            unknowns.append("该能力维度依赖的字段存在未解决冲突，发布前必须复核")
        result.append({
            "assessmentUid": None,
            "companyId": None,
            "dimensionCode": dimension_code,
            "dimensionName": definition["name"],
            "supportStatus": status,
            "summary": f"发现 {len(claims)} 项候选事实、{len(evidence)} 条证据" if claims else "当前投标文件未提供足够证据",
            "claimsJson": claims,
            "evidenceJson": evidence,
            "unknownsJson": unknowns,
            "snapshotUid": None,
            "algorithmVersion": "document-ai-rules-v1",
            "calculatedAt": calculated_at,
            "isCurrent": True,
        })
    return result


__all__ = ["derive_capability_assessments", "derive_capability_evidence"]
