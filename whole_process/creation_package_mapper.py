"""把完整抽取事实映射为新 Company 初始化候选包。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from io_utils import stable_hash
from normalizers import normalize_company_name
from profile_derivation import profile_data_gaps
from server_contract import PROFILE_FIELD_CODES, server_target_contract


SCHEMA_VERSION = "company-tender-profile/v2"


def _all_refs(items: list[dict[str, Any]]) -> list[str]:
    result: set[str] = set()
    for item in items:
        refs = item.get("evidenceRefs")
        if isinstance(refs, list):
            result.update(str(ref) for ref in refs if ref)
    return sorted(result)


def _field_refs(evidence: list[dict[str, Any]], *path_fragments: str) -> list[str]:
    fragments = tuple(fragment.casefold() for fragment in path_fragments)
    return sorted({
        str(item["evidenceId"])
        for item in evidence
        if item.get("evidenceId")
        and any(fragment in str(item.get("fieldPath") or "").casefold() for fragment in fragments)
    })


def _candidate(
    code: str,
    value: Any,
    *,
    source_ref: str,
    evidence_refs: list[str],
    derived: bool = False,
) -> dict[str, Any]:
    return {
        "fieldCode": code,
        "value": value,
        "profileAction": "PROFILE_CANDIDATE",
        "verificationStatus": "UNVERIFIED",
        "sourceTypeCandidate": "DERIVED" if derived else "DOCUMENT_AI",
        "sourceRef": source_ref,
        "evidenceRefs": sorted(set(evidence_refs)),
    }


def _nonempty(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _creation_readiness(
    company: dict[str, Any],
    evidence: list[dict[str, Any]],
    subject_company_name: str | None,
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    name = company.get("companyName")
    credit_code = company.get("creditCode")
    name_refs = _field_refs(evidence, "companyname", "enterprise.name")
    credit_refs = _field_refs(evidence, "creditcode", "credit_code")
    evidence_by_id = {str(item.get("evidenceId")): item for item in evidence}
    name_strong = any(evidence_by_id.get(ref, {}).get("sourceStrength") == "A" for ref in name_refs)
    credit_strong = any(evidence_by_id.get(ref, {}).get("sourceStrength") == "A" for ref in credit_refs)
    issues: list[str] = []
    warnings: list[str] = []
    if not name:
        issues.append("MISSING_COMPANY_NAME")
    elif not name_refs:
        issues.append("COMPANY_NAME_WITHOUT_EVIDENCE")
    elif not name_strong:
        warnings.append("企业名称缺少 A 级主体证据")
    if not credit_code:
        issues.append("MISSING_OR_INVALID_CREDIT_CODE")
    elif not credit_refs:
        issues.append("CREDIT_CODE_WITHOUT_EVIDENCE")
    elif not credit_strong:
        warnings.append("统一社会信用代码缺少 A 级主体证据")
    if subject_company_name and name and normalize_company_name(subject_company_name) != normalize_company_name(name):
        issues.append("SUBJECT_COMPANY_NAME_MISMATCH")
    identity_conflicts = [
        item for item in conflicts
        if item.get("entityType") == "COMPANY"
        and str(item.get("fieldPath") or "") in {"companyName", "creditCode"}
        and item.get("reviewRequired") is True
    ]
    if identity_conflicts:
        issues.append("UNRESOLVED_COMPANY_IDENTITY_CONFLICT")
    elif any(item.get("reviewRequired") is True for item in conflicts):
        warnings.append(f"存在 {sum(1 for item in conflicts if item.get('reviewRequired') is True)} 项未解决文档冲突")
    if issues:
        status = "IDENTITY_INCOMPLETE"
    elif warnings:
        status = "NEEDS_REVIEW"
    else:
        status = "READY_FOR_DUPLICATE_CHECK"
    return {
        "status": status,
        "duplicateCheckStatus": "NOT_RUN",
        "blockingIssues": issues,
        "warnings": warnings,
        "requiredNextAction": (
            "MANUAL_IDENTITY_REVIEW"
            if issues
            else "MANUAL_CONFLICT_REVIEW_THEN_DUPLICATE_CHECK"
            if warnings
            else "CHECK_CREDIT_CODE_AND_NORMALIZED_NAME_IN_DATABASE"
        ),
    }


def _entity_candidates(
    merged: dict[str, Any],
    buyer_relationships: list[dict[str, Any]],
    capability_assessments: list[dict[str, Any]],
    capability_evidence: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    historical_projects: list[dict[str, Any]] = []
    project_relations: list[dict[str, Any]] = []
    project_persons: list[dict[str, Any]] = []
    consortium_members: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    for performance in merged.get("performances", []):
        performance_key = performance.get("performanceKey")
        historical_projects.append({
            key: performance.get(key)
            for key in (
                "performanceKey", "projectCode", "tenderCode", "projectName", "projectNature", "industry", "projectType",
                "tenderMethod", "organizationForm", "province", "city", "locationText", "ownerCompanyName",
                "customerName", "agencyCompanyName", "role", "relationType", "ranking", "isWinner",
                "isConsortium", "isConsortiumLeader", "bidAmountYuan", "estimatedAmountYuan", "awardDate", "duration", "qualityRequirement",
                "performanceStatus", "acceptanceDate", "scopeItems", "deliveredCapabilities", "evidenceRefs",
            )
        })
        project_relations.append({
            "performanceRef": performance_key,
            "companyId": None,
            "companyName": merged.get("companyCreateCandidate", {}).get("companyName"),
            "relationType": performance.get("relationType") or performance.get("role"),
            "ranking": performance.get("ranking"),
            "bidAmount": performance.get("bidAmountYuan"),
            "isWinner": performance.get("isWinner"),
            "isConsortium": performance.get("isConsortium"),
            "isConsortiumLeader": performance.get("isConsortiumLeader"),
            "evidenceRefs": performance.get("evidenceRefs", []),
        })
        for item in performance.get("projectPersonnel", []):
            if isinstance(item, dict):
                project_persons.append({"performanceRef": performance_key, **item})
        for item in performance.get("consortiumMembers", []):
            if isinstance(item, dict):
                consortium_members.append({"performanceRef": performance_key, **item})
        contract = performance.get("contract")
        if isinstance(contract, dict) and any(
            _nonempty(contract.get(key))
            for key in ("contractNo", "contractName", "amountYuan", "signDate", "startDate", "contractContent")
        ):
            contracts.append({
                "performanceRef": performance_key,
                "sellerCompanyId": None,
                "sellerName": merged.get("companyCreateCandidate", {}).get("companyName"),
                "buyerName": performance.get("customerName") or performance.get("ownerCompanyName"),
                **contract,
                "evidenceRefs": performance.get("evidenceRefs", []),
            })
    profile_tags = [
        {
            "companyId": None,
            "tagType": "CAPABILITY",
            "tagValue": item.get("capabilityName"),
            "normalizedValue": str(item.get("capabilityName") or "").casefold(),
            "sourceTypeCandidate": "DERIVED",
            "sourceRef": item.get("capabilityKey"),
            "evidenceRefs": item.get("evidenceRefs", []),
            "verificationStatus": "UNVERIFIED",
        }
        for item in capability_evidence
        if item.get("supportStatus") == "SUPPORTED"
        and item.get("claimType") in {"DELIVERED", "CONTRACTED", "INFERRED"}
    ]
    return {
        "aliases": merged.get("aliases", []),
        "qualifications": merged.get("qualifications", []),
        "personnel": merged.get("personnelHistory", []),
        "intellectualProperties": merged.get("intellectualProperties", []),
        "historicalProjects": historical_projects,
        "projectRelations": project_relations,
        "projectPersons": project_persons,
        "consortiumMembers": consortium_members,
        "contracts": contracts,
        "customerRelationships": buyer_relationships,
        "financialHistory": merged.get("financialHistory", []),
        "productsAndEquipment": merged.get("productsAndEquipment", []),
        "solutionClaims": merged.get("solutionClaims", []),
        "deliveryClaims": merged.get("deliveryClaims", []),
        "riskFacts": merged.get("riskAndCompliance", []),
        "profileTags": profile_tags,
        "capabilityAssessments": capability_assessments,
        "evidenceAttachments": [
            {
                "evidenceId": item.get("evidenceId"),
                "physicalPage": item.get("physicalPage"),
                "fileName": None,
                "storageStatus": "NOT_UPLOADED",
            }
            for item in evidence
        ],
    }


def build_profile_projection(
    merged: dict[str, Any],
    derived: dict[str, Any],
    capability_assessments: list[dict[str, Any]],
    capability_evidence: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    source_hash: str,
) -> dict[str, Any]:
    company = merged.get("companyCreateCandidate", {})
    field_candidates: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    def add_direct(code: str, value: Any, paths: tuple[str, ...]) -> None:
        if not _nonempty(value):
            return
        refs = _field_refs(evidence, *paths)
        candidate = _candidate(
            code,
            value,
            source_ref=f"document:{source_hash}#{paths[0]}",
            evidence_refs=refs,
        )
        if refs:
            field_candidates.append(candidate)
        else:
            blocked.append({**candidate, "blockedReason": "MISSING_FIELD_EVIDENCE"})

    add_direct("enterprise.name", company.get("companyName"), ("companyCreateCandidate.companyName", "companyname"))
    add_direct("enterprise.credit_code", company.get("creditCode"), ("companyCreateCandidate.creditCode", "creditcode"))
    add_direct("enterprise.legal_person", company.get("legalPerson"), ("companyCreateCandidate.legalPerson", "legalperson"))
    capital = company.get("registeredCapital") if isinstance(company.get("registeredCapital"), dict) else {}
    add_direct("enterprise.registered_capital", capital.get("amountYuan"), ("registeredCapital",))
    add_direct("enterprise.established_date", company.get("establishedDate"), ("establishedDate",))
    add_direct("enterprise.business_scope", company.get("businessScope"), ("businessScope",))
    location = {
        "province": company.get("province"),
        "city": company.get("city"),
        "address": company.get("registeredAddress"),
    }
    add_direct("enterprise.location", location if any(_nonempty(value) for value in location.values()) else None, ("registeredAddress", "province", "city"))
    add_direct("enterprise.employee_count", company.get("employeeCount"), ("employeeCount",))

    entity_values = (
        ("qualification.items", merged.get("qualifications", [])),
        ("personnel.core_members", [
            item for item in merged.get("personnelHistory", [])
            if item.get("personnelType") == "CORE_MEMBER"
            and isinstance(item.get("employmentEvidence"), dict)
            and item["employmentEvidence"].get("currentEmploymentConfirmed") is True
        ]),
        ("personnel.professionals", [
            item for item in merged.get("personnelHistory", [])
            if item.get("personnelType") == "PROFESSIONAL"
            and isinstance(item.get("employmentEvidence"), dict)
            and item["employmentEvidence"].get("currentEmploymentConfirmed") is True
        ]),
        ("capability.intellectual_properties", merged.get("intellectualProperties", [])),
        ("capability.project_performances", merged.get("performances", [])),
        ("risk.records", merged.get("riskAndCompliance", [])),
    )
    for code, items in entity_values:
        typed_items = [item for item in items if isinstance(item, dict)]
        if not typed_items:
            continue
        ownership_required = code in {"qualification.items", "capability.intellectual_properties"}
        ownership_blocked = [
            item for item in typed_items
            if ownership_required and item.get("subjectMatchStatus") != "MATCH"
        ]
        publishable = [
            item for item in typed_items
            if item.get("evidenceRefs")
            and not item.get("conflictRefs")
            and not (ownership_required and item.get("subjectMatchStatus") != "MATCH")
        ]
        conflicted = [item for item in typed_items if item.get("conflictRefs")]
        without_evidence = [
            item for item in typed_items
            if not item.get("evidenceRefs") and item not in ownership_blocked
        ]
        if publishable:
            refs = _all_refs(publishable)
            candidate = _candidate(code, publishable, source_ref=f"document:{source_hash}#{code}", evidence_refs=refs)
            field_candidates.append(candidate)
        if conflicted:
            blocked.append({
                **_candidate(
                    code,
                    conflicted,
                    source_ref=f"document:{source_hash}#{code}.conflicted",
                    evidence_refs=_all_refs(conflicted),
                ),
                "blockedReason": "UNRESOLVED_ENTITY_CONFLICT",
            })
        if ownership_blocked:
            blocked.append({
                **_candidate(
                    code,
                    ownership_blocked,
                    source_ref=f"document:{source_hash}#{code}.ownershipUnconfirmed",
                    evidence_refs=_all_refs(ownership_blocked),
                ),
                "blockedReason": "SUBJECT_OWNERSHIP_NOT_CONFIRMED",
            })
        if without_evidence:
            blocked.append({
                **_candidate(
                    code,
                    without_evidence,
                    source_ref=f"document:{source_hash}#{code}.withoutEvidence",
                    evidence_refs=[],
                ),
                "blockedReason": "MISSING_ENTITY_EVIDENCE",
            })

    derived_values = (
        ("capability.main_industries", derived.get("mainIndustries"), _all_refs(merged.get("performances", []))),
        ("capability.main_regions", derived.get("mainRegions"), _all_refs(merged.get("performances", []))),
        ("capability.average_win_amount", derived.get("averageWinAmount"), _all_refs(merged.get("performances", []))),
        ("capability.win_project_count_3y", derived.get("winProjectCount3y"), _all_refs(merged.get("performances", []))),
        ("capability.buyer_relationships", derived.get("buyerRelationships"), _all_refs(derived.get("buyerRelationships", []))),
        ("tag.capabilities", derived.get("capabilityTags"), _all_refs(capability_evidence)),
        ("tag.risks", derived.get("riskTags"), _all_refs(merged.get("riskAndCompliance", []))),
    )
    for code, value, refs in derived_values:
        if _nonempty(value) and refs:
            field_candidates.append(_candidate(
                code,
                value,
                source_ref=f"derived:{source_hash}#{code}",
                evidence_refs=refs,
                derived=True,
            ))

    candidate_codes = {item["fieldCode"] for item in field_candidates}
    gaps = [item for item in profile_data_gaps(candidate_codes | {"quality.data_gaps"}) if item["fieldCode"] != "quality.data_gaps"]
    gap_refs = sorted({ref for candidate in field_candidates for ref in candidate.get("evidenceRefs", [])})
    field_candidates.append(_candidate(
        "quality.data_gaps",
        gaps,
        source_ref=f"derived:{source_hash}#quality.data_gaps",
        evidence_refs=gap_refs,
        derived=True,
    ))
    return {
        "operation": "CREATE_COMPANY",
        "companyId": None,
        "companyCreateCandidateRef": "#/companyCreateCandidate",
        "fieldCandidates": field_candidates,
        "entityCandidates": _entity_candidates(
            merged,
            _all_relationship_candidates(derived),
            capability_assessments,
            capability_evidence,
            evidence,
        ),
        "derivedFieldCandidates": {
            "capability.main_industries": derived.get("mainIndustries"),
            "capability.main_regions": derived.get("mainRegions"),
            "capability.average_win_amount": derived.get("averageWinAmount"),
            "capability.win_project_count_3y": derived.get("winProjectCount3y"),
            "capability.buyer_relationships": derived.get("buyerRelationships"),
            "tag.capabilities": derived.get("capabilityTags"),
            "tag.risks": derived.get("riskTags"),
            "evaluation.scores": None,
            "quality.data_gaps": gaps,
        },
        "suggestedPreferences": derived.get("suggestedPreferences"),
        "blockedCandidates": blocked,
    }


def _invoice_index(page_manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = {int(item["physicalPage"]): item for item in page_manifest}
    result: list[dict[str, Any]] = []
    related_types = {"CONTRACT", "ACCEPTANCE_REPORT", "AWARD_NOTICE", "PERFORMANCE_SUMMARY"}
    for page in page_manifest:
        number = int(page["physicalPage"])
        if page.get("documentType") != "INVOICE":
            continue
        related = [
            candidate
            for candidate in range(max(1, number - 3), number + 4)
            if candidate in indexed and indexed[candidate].get("documentType") in related_types
        ]
        result.append({
            "physicalPage": number,
            "detailExtraction": "SKIPPED",
            "relatedPageCandidates": related,
            "associationStatus": "CANDIDATE" if related else "UNRESOLVED",
        })
    return result


def _token_total(value: Any) -> int:
    if isinstance(value, dict):
        total = 0
        for key, item in value.items():
            if key == "total_tokens" and isinstance(item, (int, float)) and not isinstance(item, bool):
                total += int(item)
            elif key != "total_tokens":
                total += _token_total(item)
        return total
    if isinstance(value, list):
        return sum(_token_total(item) for item in value)
    return 0


def _api_usage_summary(requests: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"ocrRequests": 0, "visionRequests": 0, "textRequests": 0}
    total_tokens = 0
    for request in requests:
        if request.get("cached") is True:
            continue
        stage = str(request.get("stage") or "")
        request_count = int(request.get("requestCount") or 1)
        if stage == "OCR":
            counts["ocrRequests"] += request_count
        elif stage == "VISION":
            counts["visionRequests"] += request_count
        elif stage == "CLASSIFICATION" or request.get("domain"):
            counts["textRequests"] += request_count
        total_tokens += _token_total(request.get("usage"))
    return {
        **counts,
        "totalTokens": total_tokens or None,
        "requests": requests,
    }


def _section_manifest(page_manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for page in sorted(page_manifest, key=lambda item: int(item.get("physicalPage") or 0)):
        number = int(page.get("physicalPage") or 0)
        document_type = str(page.get("documentType") or "OTHER")
        section_code = str(page.get("sectionCode") or document_type)
        identity = (document_type, section_code)
        if sections and sections[-1]["_identity"] == identity and number == sections[-1]["endPhysicalPage"] + 1:
            sections[-1]["endPhysicalPage"] = number
            sections[-1]["pageCount"] += 1
            continue
        sections.append({
            "sectionId": f"section:sha256:{stable_hash(number, document_type, section_code, length=20)}",
            "documentType": document_type,
            "sectionCode": section_code,
            "startPhysicalPage": number,
            "endPhysicalPage": number,
            "pageCount": 1,
            "_identity": identity,
        })
    for section in sections:
        section.pop("_identity", None)
    return sections


def _profile_action_counts(merged: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for value in merged.values():
        rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        for row in rows:
            if isinstance(row, dict) and row.get("profileAction"):
                counts[str(row["profileAction"])] += 1
    return counts


def _all_relationship_candidates(derived: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *[item for item in derived.get("buyerRelationships", []) if isinstance(item, dict)],
        *[item for item in derived.get("unverifiedRelationshipCandidates", []) if isinstance(item, dict)],
    ]


def build_creation_package(
    *,
    run: dict[str, Any],
    source_document: dict[str, Any],
    subject_company_name: str | None,
    page_manifest: list[dict[str, Any]],
    merged: dict[str, Any],
    evidence: list[dict[str, Any]],
    capability_evidence: list[dict[str, Any]],
    capability_assessments: list[dict[str, Any]],
    derived: dict[str, Any],
    sensitive_findings: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    api_usage: list[dict[str, Any]],
) -> dict[str, Any]:
    company = merged.get("companyCreateCandidate", {})
    readiness = _creation_readiness(company, evidence, subject_company_name, merged.get("conflicts", []))
    projection = build_profile_projection(
        merged,
        derived,
        capability_assessments,
        capability_evidence,
        evidence,
        source_hash=str(source_document["sha256"]),
    )
    projection["creationReadiness"] = readiness
    relationship_candidates = _all_relationship_candidates(derived)
    document_type_counts = Counter(str(item.get("documentType") or "OTHER") for item in page_manifest)
    classification_confidence_counts = Counter(str(item.get("classificationConfidence") or "LOW") for item in page_manifest)
    profile_action_counts = _profile_action_counts(merged)
    sections = _section_manifest(page_manifest)
    failed_pages = [
        int(item["physicalPage"])
        for item in page_manifest
        if item.get("contentSource") in {"OCR_FAILED", "OCR_LOW_QUALITY", "UNAVAILABLE"}
    ]
    domain_errors = [item for item in errors if item.get("stage") == "DOMAIN_EXTRACTION"]
    successful_domain_requests = [item for item in api_usage if item.get("domain")]
    if domain_errors and not successful_domain_requests:
        status = "FAILED"
    else:
        status = "PARTIAL" if failed_pages or errors else "SUCCESS"
    return {
        "schemaVersion": SCHEMA_VERSION,
        "operation": "CREATE_COMPANY",
        "companyId": None,
        "run": run,
        "sourceDocument": source_document,
        "subjectCompany": {
            "providedName": subject_company_name,
            "normalizedProvidedName": normalize_company_name(subject_company_name),
            "usedAsEvidence": False,
        },
        "creationReadiness": readiness,
        "pageManifestSummary": {
            "processedPages": len(page_manifest),
            "documentTypeCounts": dict(sorted(document_type_counts.items())),
            "classificationConfidenceCounts": dict(sorted(classification_confidence_counts.items())),
            "sections": sections,
            "invoicePages": _invoice_index(page_manifest),
            "failedPages": failed_pages,
        },
        "companyCreateCandidate": company,
        "aliases": merged.get("aliases", []),
        "qualifications": merged.get("qualifications", []),
        "intellectualProperties": merged.get("intellectualProperties", []),
        "financialHistory": merged.get("financialHistory", []),
        "riskAndCompliance": merged.get("riskAndCompliance", []),
        "personnelHistory": merged.get("personnelHistory", []),
        "performances": merged.get("performances", []),
        "customerRelationships": relationship_candidates,
        "productsAndEquipment": merged.get("productsAndEquipment", []),
        "solutionClaims": merged.get("solutionClaims", []),
        "deliveryClaims": merged.get("deliveryClaims", []),
        "customerAndRegionExperience": {
            "mainIndustries": derived.get("mainIndustries", []),
            "mainRegions": derived.get("mainRegions", []),
            "buyerRelationships": derived.get("buyerRelationships", []),
            "unverifiedRelationshipCandidates": derived.get("unverifiedRelationshipCandidates", []),
        },
        "bidSpecific": merged.get("bidSpecific", {}),
        "capabilityEvidence": capability_evidence,
        "capabilityAssessments": capability_assessments,
        "derivedProfileCandidates": derived,
        "profileProjectionCandidate": projection,
        "serverTargetContract": server_target_contract(),
        "evidence": evidence,
        "conflicts": merged.get("conflicts", []),
        "sensitiveFindings": sensitive_findings,
        "quality": {
            "status": status,
            "operation": "CREATE_COMPANY",
            "creationReadiness": readiness["status"],
            "companyId": None,
            "totalPages": source_document.get("totalPages"),
            "processedPages": len(page_manifest),
            "classifiedPages": len(page_manifest),
            "businessClassifiedPages": sum(count for kind, count in document_type_counts.items() if kind != "OTHER"),
            "otherPages": document_type_counts.get("OTHER", 0),
            "ocrPages": sum(1 for item in page_manifest if item.get("contentSource") == "GLM_OCR"),
            "visionReviewedPages": sum(1 for item in page_manifest if item.get("visualReviewed") is True),
            "failedPages": failed_pages,
            "unresolvedFields": [item["fieldCode"] for item in projection.get("derivedFieldCandidates", {}).get("quality.data_gaps", [])],
            "unresolvedConflicts": [item.get("conflictId") for item in merged.get("conflicts", [])],
            "sensitiveItemsExcluded": sum(int(item.get("count") or 0) for item in sensitive_findings),
            "profileCandidateCount": len(projection["fieldCandidates"]),
            "qualificationCount": len(merged.get("qualifications", [])),
            "professionalCount": sum(1 for item in merged.get("personnelHistory", []) if item.get("personnelType") == "PROFESSIONAL"),
            "intellectualPropertyCount": len(merged.get("intellectualProperties", [])),
            "performanceCount": len(merged.get("performances", [])),
            "customerRelationshipCount": len(relationship_candidates),
            "demonstratedCustomerRelationshipCount": len(derived.get("buyerRelationships", [])),
            "capabilityEvidenceCount": len(capability_evidence),
            "coveredServerFieldCount": len({item["fieldCode"] for item in projection["fieldCandidates"]}),
            "serverFieldCount": len(PROFILE_FIELD_CODES),
            "historyOnlyCount": profile_action_counts.get("HISTORY_ONLY", 0),
            "claimOnlyCount": profile_action_counts.get("CLAIM_ONLY", 0),
            "bidOnlyCount": profile_action_counts.get("BID_ONLY", 0),
            "warnings": warnings,
            "errors": errors,
            "apiUsage": _api_usage_summary(api_usage),
        },
    }


__all__ = ["SCHEMA_VERSION", "build_creation_package", "build_profile_projection"]
