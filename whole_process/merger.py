"""跨块实体归并、稳定标识、字段冲突保留和基础归一化。"""

from __future__ import annotations

import copy
from datetime import date, timedelta
from typing import Any, Callable

from io_utils import stable_hash
from normalizers import (
    clean_text,
    clean_verbatim_text,
    finite_number,
    finite_signed_number,
    normalize_company_name,
    normalize_credit_code,
    normalize_date,
    normalize_money,
    normalize_string_list,
    parse_money_text,
)


META_FIELDS = {"qualityLevel", "profileAction", "conflictRefs"}


def _normalize_enum(value: Any, allowed: set[str], fallback: str, aliases: dict[str, str] | None = None) -> str:
    text = clean_text(value, 100)
    if not text:
        return fallback
    normalized = (aliases or {}).get(text.casefold(), text.upper())
    return normalized if normalized in allowed else fallback


def _present(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _merge_values(
    left: Any,
    right: Any,
    *,
    entity_type: str,
    entity_key: str,
    field_path: str,
    conflicts: list[dict[str, Any]],
    left_evidence_refs: list[str] | None = None,
    right_evidence_refs: list[str] | None = None,
) -> Any:
    if not _present(left):
        return copy.deepcopy(right)
    if not _present(right):
        return left
    if left == right:
        return left
    if isinstance(left, list) and isinstance(right, list):
        result = copy.deepcopy(left)
        fingerprints = {stable_hash(item) for item in result}
        for item in right:
            fingerprint = stable_hash(item)
            if fingerprint not in fingerprints:
                fingerprints.add(fingerprint)
                result.append(copy.deepcopy(item))
        return result
    if isinstance(left, dict) and isinstance(right, dict):
        result = copy.deepcopy(left)
        for key, value in right.items():
            result[key] = _merge_values(
                result.get(key),
                value,
                entity_type=entity_type,
                entity_key=entity_key,
                field_path=f"{field_path}.{key}" if field_path else key,
                conflicts=conflicts,
                left_evidence_refs=left_evidence_refs,
                right_evidence_refs=right_evidence_refs,
            )
        return result
    if field_path.rsplit(".", 1)[-1] in META_FIELDS:
        return left
    conflict_id = f"conflict:{entity_type.lower()}:{stable_hash(entity_key, field_path, left, right, length=18)}"
    if not any(item.get("conflictId") == conflict_id for item in conflicts):
        conflicts.append({
            "conflictId": conflict_id,
            "entityType": entity_type,
            "entityKey": entity_key,
            "fieldPath": field_path,
            "candidates": [
                {"value": left, "evidenceRefs": sorted(set(left_evidence_refs or []))},
                {"value": right, "evidenceRefs": sorted(set(right_evidence_refs or []))},
            ],
            "resolution": {"status": "UNRESOLVED", "selectedValue": None, "rule": None},
            "reviewRequired": True,
        })
    return left


def _merge_dicts(items: list[dict[str, Any]], entity_type: str, entity_key: str, conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in items:
        left_refs = result.get("evidenceRefs") if isinstance(result.get("evidenceRefs"), list) else []
        right_refs = item.get("evidenceRefs") if isinstance(item.get("evidenceRefs"), list) else []
        for key, value in item.items():
            result[key] = _merge_values(
                result.get(key),
                value,
                entity_type=entity_type,
                entity_key=entity_key,
                field_path=key,
                conflicts=conflicts,
                left_evidence_refs=left_refs,
                right_evidence_refs=right_refs,
            )
    return result


def _attach_conflict_refs(item: dict[str, Any], entity_key: str, conflicts: list[dict[str, Any]]) -> None:
    item["conflictRefs"] = sorted({
        str(conflict.get("conflictId"))
        for conflict in conflicts
        if conflict.get("entityKey") == entity_key and conflict.get("conflictId")
    })


def _identity(parts: list[Any], fallback: dict[str, Any]) -> str:
    values = [clean_text(value, 500) for value in parts]
    meaningful = [value.casefold() for value in values if value]
    return stable_hash(meaningful if meaningful else fallback, length=24)


def _group_entities(
    items: list[dict[str, Any]],
    *,
    entity_type: str,
    key_name: str,
    identity: Callable[[dict[str, Any]], str],
    conflicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not any(_present(value) for key, value in item.items() if key not in META_FIELDS):
            continue
        key = identity(item)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    result: list[dict[str, Any]] = []
    for key in order:
        stable_key = f"{entity_type.lower().replace('_', '-')}:sha256:{key}"
        merged = _merge_dicts(groups[key], entity_type, stable_key, conflicts)
        merged[key_name] = stable_key
        _attach_conflict_refs(merged, stable_key, conflicts)
        result.append(merged)
    return result


def _entity_token(label: str, *parts: Any) -> str | None:
    normalized = [clean_text(part, 1000) for part in parts]
    if not normalized or not all(normalized):
        return None
    return f"{label}:{stable_hash([part.casefold() for part in normalized if part], length=24)}"


def _fact_fallback(item: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if key not in META_FIELDS
        and key not in {"evidenceRefs", "profileAction"}
        and not key.lower().endswith("key")
    }


def _group_entities_by_tokens(
    items: list[dict[str, Any]],
    *,
    entity_type: str,
    key_name: str,
    tokens: Callable[[dict[str, Any]], list[str]],
    strong_labels: set[str] | None = None,
    conflicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """以任一共享强标识连接跨材料事实，支持合同号与项目名等标识的传递归并。"""

    values = [
        item for item in items
        if isinstance(item, dict) and any(_present(value) for key, value in item.items() if key not in META_FIELDS)
    ]
    parents = list(range(len(values)))
    strong_labels = strong_labels or set()

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int, shared_token: str) -> bool:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return True
        shared_label = shared_token.rsplit(":", 1)[0]
        if shared_label not in strong_labels:
            for label in strong_labels:
                left_values = strong_tokens[left_root].get(label, set())
                right_values = strong_tokens[right_root].get(label, set())
                if left_values and right_values and left_values.isdisjoint(right_values):
                    return False
        parents[right_root] = left_root
        for label, token_values in strong_tokens[right_root].items():
            strong_tokens[left_root].setdefault(label, set()).update(token_values)
        return True

    tokens_by_index: list[list[str]] = []
    for index, item in enumerate(values):
        item_tokens = sorted(set(tokens(item)))
        if not item_tokens:
            item_tokens = [f"99:fallback:{stable_hash(_fact_fallback(item), length=24)}"]
        tokens_by_index.append(item_tokens)
    strong_tokens: list[dict[str, set[str]]] = []
    for item_tokens in tokens_by_index:
        grouped_tokens: dict[str, set[str]] = {}
        for token in item_tokens:
            label = token.rsplit(":", 1)[0]
            if label in strong_labels:
                grouped_tokens.setdefault(label, set()).add(token)
        strong_tokens.append(grouped_tokens)

    token_owner: dict[str, int] = {}
    for index, item_tokens in enumerate(tokens_by_index):
        for token in item_tokens:
            if token in token_owner:
                owner = token_owner[token]
                if owner >= 0 and not union(index, owner, token):
                    # 同一弱键指向多个强标识冲突实体后即作废，后续无强标识条目不得任意挂靠。
                    token_owner[token] = -1
            else:
                token_owner[token] = index

    components: dict[int, list[int]] = {}
    for index in range(len(values)):
        components.setdefault(find(index), []).append(index)

    result: list[dict[str, Any]] = []
    for indices in components.values():
        canonical_token = min(token for index in indices for token in tokens_by_index[index])
        stable_key = f"{entity_type.lower().replace('_', '-')}:sha256:{stable_hash(canonical_token, length=24)}"
        merged = _merge_dicts([values[index] for index in indices], entity_type, stable_key, conflicts)
        merged[key_name] = stable_key
        _attach_conflict_refs(merged, stable_key, conflicts)
        result.append(merged)
    return result


def _payload_lists(domain_payloads: dict[str, list[dict[str, Any]]], domain: str, key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for payload in domain_payloads.get(domain, []):
        value = payload.get(key)
        if isinstance(value, list):
            result.extend(item for item in value if isinstance(item, dict))
    return result


def _normalize_company(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["companyName"] = clean_verbatim_text(result.get("companyName"), 200)
    result["normalizedCompanyName"] = normalize_company_name(result.get("companyName"))
    original_credit_code = clean_text(result.get("creditCode"), 64)
    result["creditCode"] = normalize_credit_code(original_credit_code)
    if original_credit_code and not result["creditCode"]:
        result.setdefault("warnings", []).append("统一社会信用代码格式未通过本地校验")
    result["legalPerson"] = clean_text(result.get("legalPerson"), 64)
    result["registeredCapital"] = normalize_money(result.get("registeredCapital"))
    result["establishedDate"] = normalize_date(result.get("establishedDate"))
    result["companyType"] = clean_text(result.get("companyType"), 128)
    result["businessScope"] = clean_text(result.get("businessScope"), 20000)
    result["registeredAddress"] = clean_text(result.get("registeredAddress"), 1000)
    result["province"] = clean_text(result.get("province"), 64)
    result["city"] = clean_text(result.get("city"), 64)
    result["employeeCount"] = finite_number(result.get("employeeCount"))
    result["documentAsOfDate"] = normalize_date(result.get("documentAsOfDate"))
    result.setdefault("profileAction", "REVIEW_REQUIRED")
    result.setdefault("qualityLevel", "UNRESOLVED")
    result.setdefault("evidenceRefs", [])
    return result


def _subject_match_status(holder_name: Any, company_name: Any) -> str:
    holder = normalize_company_name(holder_name)
    company = normalize_company_name(company_name)
    if not holder or not company:
        return "UNKNOWN"
    return "MATCH" if holder == company else "MISMATCH"


def _certificate_status(issue_date: str | None, expiry_date: str | None, reported_status: Any, as_of: date) -> str:
    if str(reported_status or "").upper() == "REVOKED":
        return "REVOKED"
    if issue_date and date.fromisoformat(issue_date) > as_of:
        return "UNKNOWN"
    if expiry_date:
        expiry = date.fromisoformat(expiry_date)
        if expiry < as_of:
            return "EXPIRED"
        if expiry <= as_of + timedelta(days=90):
            return "EXPIRING"
        return "VALID"
    return reported_status if reported_status in {"VALID", "EXPIRING", "EXPIRED", "REVOKED", "UNKNOWN"} else "UNKNOWN"


def _normalize_qualification(item: dict[str, Any], *, as_of: date) -> dict[str, Any]:
    result = copy.deepcopy(item)
    for key, length in (("certName", 200), ("certLevel", 64), ("certNo", 128), ("issuingAuthority", 200), ("certScope", 5000), ("holderName", 200)):
        result[key] = clean_text(result.get(key), length)
    result["issueDate"] = normalize_date(result.get("issueDate"))
    result["expiryDate"] = normalize_date(result.get("expiryDate"))
    result["status"] = _certificate_status(result["issueDate"], result["expiryDate"], result.get("status"), as_of)
    return result


def _normalize_intellectual_property(item: dict[str, Any], *, as_of: date) -> dict[str, Any]:
    result = copy.deepcopy(item)
    result["propertyType"] = _normalize_enum(
        result.get("propertyType"),
        {"PATENT", "TRADEMARK", "SOFTWARE_COPYRIGHT", "STANDARD", "OTHER"},
        "OTHER",
    )
    for key, length in (
        ("propertyName", 512), ("registrationNo", 128), ("applicationNo", 128),
        ("holderName", 200), ("status", 64),
    ):
        result[key] = clean_text(result.get(key), length)
    for key in ("applicationDate", "issueDate", "expiryDate"):
        result[key] = normalize_date(result.get(key))
    result["status"] = _normalize_enum(
        result.get("status"),
        {"VALID", "ACTIVE", "PENDING", "EXPIRED", "REVOKED", "INVALID", "UNKNOWN"},
        "UNKNOWN",
        {
            "有效": "VALID", "授权": "ACTIVE", "已授权": "ACTIVE", "登记": "ACTIVE", "已登记": "ACTIVE",
            "申请中": "PENDING", "审核中": "PENDING", "已过期": "EXPIRED", "失效": "INVALID", "无效": "INVALID",
            "撤销": "REVOKED", "已撤销": "REVOKED",
        },
    )
    if result.get("expiryDate"):
        expiry = date.fromisoformat(result["expiryDate"])
        if expiry < as_of:
            result["status"] = "EXPIRED"
    return result


def _normalize_financial(item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(item)
    year = finite_number(result.get("fiscalYear"))
    result["fiscalYear"] = int(year) if isinstance(year, (int, float)) and 1900 <= year <= 2200 else None
    result["currency"] = clean_text(result.get("currency"), 8) or "CNY"
    result["unit"] = clean_text(result.get("unit"), 32)
    for key in ("totalAssets", "totalLiabilities", "revenue"):
        result[key] = finite_number(result.get(key))
    for key in ("operatingProfit", "netProfit", "netAssets", "cashFlow"):
        result[key] = finite_signed_number(result.get(key))
    result["auditorName"] = clean_text(result.get("auditorName"), 200)
    result["auditOpinion"] = clean_text(result.get("auditOpinion"), 2000)
    result["reportDate"] = normalize_date(result.get("reportDate"))
    return result


def _normalize_risk(item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(item)
    result["riskType"] = _normalize_enum(
        result.get("riskType"),
        {"ADMIN_PENALTY", "DISHONESTY", "LITIGATION", "ENFORCEMENT", "BUSINESS_ANOMALY", "TAX", "QUALIFICATION_EXPIRY", "OTHER"},
        "OTHER",
    )
    result["title"] = clean_text(result.get("title"), 1000)
    for key in ("occurredAt", "resolvedAt", "queryAsOfDate"):
        result[key] = normalize_date(result.get(key))
    result["amountYuan"] = finite_number(result.get("amountYuan"))
    return result


def _normalize_personnel(item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(item)
    result["personnelType"] = _normalize_enum(
        result.get("personnelType"),
        {"CORE_MEMBER", "PROFESSIONAL", "BID_TEAM_ONLY"},
        "BID_TEAM_ONLY",
    )
    for key, length in (
        ("personName", 64), ("personRole", 128), ("roleInBid", 128),
        ("profession", 128), ("professionalTitle", 128), ("education", 128),
    ):
        result[key] = clean_text(result.get(key), length)
    certificates: list[dict[str, Any]] = []
    for certificate in result.get("certificates", []):
        if not isinstance(certificate, dict):
            continue
        normalized = copy.deepcopy(certificate)
        for key in ("issueDate", "validFrom", "expiryDate"):
            normalized[key] = normalize_date(normalized.get(key))
        normalized["additionalProfessions"] = normalize_string_list(
            normalized.get("additionalProfessions"),
            limit=20,
            max_item_length=128,
        )
        certificates.append(normalized)
    result["certificates"] = certificates
    employment = result.get("employmentEvidence") if isinstance(result.get("employmentEvidence"), dict) else {}
    employment = copy.deepcopy(employment)
    employment["asOfDate"] = normalize_date(employment.get("asOfDate"))
    employment["currentEmploymentConfirmed"] = employment.get("currentEmploymentConfirmed") is True
    result["employmentEvidence"] = employment
    return result


def _normalize_performance(item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(item)
    result["role"] = _normalize_enum(
        result.get("role"),
        {"OWNER", "CONTRACTOR", "SUPPLIER", "SERVICE_PROVIDER", "CONSORTIUM_MEMBER", "UNKNOWN"},
        "UNKNOWN",
    )
    result["performanceStatus"] = _normalize_enum(
        result.get("performanceStatus"),
        {"AWARDED", "CONTRACTED", "ONGOING", "COMPLETED", "ACCEPTED", "UNKNOWN"},
        "UNKNOWN",
        {"已中标": "AWARDED", "已签约": "CONTRACTED", "在建": "ONGOING", "进行中": "ONGOING", "已完成": "COMPLETED", "已验收": "ACCEPTED"},
    )
    for key in ("isWinner", "isConsortium", "isConsortiumLeader"):
        result[key] = result.get(key) if isinstance(result.get(key), bool) else None
    for key in ("awardDate", "acceptanceDate"):
        result[key] = normalize_date(result.get(key))
    for key in ("bidAmountYuan", "estimatedAmountYuan"):
        result[key] = finite_number(result.get(key))
    result["industry"] = normalize_string_list(result.get("industry"), limit=20, max_item_length=100)
    result["scopeItems"] = normalize_string_list(result.get("scopeItems"), limit=100, max_item_length=1000)
    result["deliveredCapabilities"] = normalize_string_list(result.get("deliveredCapabilities"), limit=100, max_item_length=300)
    contract = result.get("contract") if isinstance(result.get("contract"), dict) else {}
    contract = copy.deepcopy(contract)
    contract["amountYuan"] = finite_number(contract.get("amountYuan"))
    if contract["amountYuan"] is None and clean_text(contract.get("rawAmountText"), 200):
        contract["amountYuan"] = parse_money_text(str(contract["rawAmountText"]))
    for key in ("signDate", "startDate", "plannedEndDate", "actualEndDate"):
        contract[key] = normalize_date(contract.get(key))
    contract["contractContent"] = clean_text(contract.get("contractContent"), 20000)
    contract["performanceStatus"] = _normalize_enum(
        contract.get("performanceStatus"),
        {"AWARDED", "CONTRACTED", "ONGOING", "COMPLETED", "ACCEPTED", "UNKNOWN"},
        "UNKNOWN",
    )
    result["contract"] = contract
    return result


def _normalize_relationship(item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(item)
    result["relationType"] = _normalize_enum(
        result.get("relationType"),
        {"CUSTOMER", "SUPPLIER", "PARTNER", "CONSORTIUM"},
        "CUSTOMER",
    )
    for key in ("firstRelationshipDate", "lastRelationshipDate"):
        result[key] = normalize_date(result.get(key))
    result["projectCount"] = finite_number(result.get("projectCount"))
    result["totalAmountYuan"] = finite_number(result.get("totalAmountYuan"))
    return result


def _normalize_product(item: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(item)
    result["companyRole"] = _normalize_enum(
        result.get("companyRole"),
        {"OWNER", "MANUFACTURER", "AUTHORIZED_RESELLER", "INTEGRATOR", "PROPOSED_SUPPLIER", "UNKNOWN"},
        "UNKNOWN",
    )
    result["specifications"] = normalize_string_list(result.get("specifications"), limit=200, max_item_length=1000)
    return result


def _normalize_claim(item: dict[str, Any], *, delivery: bool) -> dict[str, Any]:
    result = copy.deepcopy(item)
    allowed = (
        {"IMPLEMENTATION", "QUALITY", "AFTER_SALES", "TRAINING", "ACCEPTANCE", "RISK_MANAGEMENT", "OTHER"}
        if delivery
        else {"SOFTWARE_SOLUTION", "SYSTEM_INTEGRATION", "TECHNICAL_CAPABILITY", "PRODUCT", "OTHER"}
    )
    result["category"] = _normalize_enum(result.get("category"), allowed, "OTHER")
    # 方案和承诺材料不能自行把声明升级为已经交付的能力。
    result["claimType"] = "DECLARED"
    result["keywords"] = normalize_string_list(result.get("keywords"), limit=100, max_item_length=100)
    return result


def _retain_resolved_performance_refs(
    items: list[dict[str, Any]],
    field_name: str,
    performance_keys: set[str],
) -> None:
    for item in items:
        refs = item.get(field_name)
        if not isinstance(refs, list):
            item[field_name] = []
            continue
        resolved = sorted({str(ref) for ref in refs if str(ref) in performance_keys})
        unresolved = sorted({str(ref) for ref in refs if ref and str(ref) not in performance_keys})
        item[field_name] = resolved
        if unresolved:
            existing = item.get("unresolvedReferenceCandidates")
            existing = existing if isinstance(existing, list) else []
            item["unresolvedReferenceCandidates"] = sorted(set([
                *existing,
                *unresolved,
            ]))


def _apply_machine_policy(items: list[dict[str, Any]], *, profile_action: str, quality_level: str) -> None:
    for item in items:
        item["profileAction"] = profile_action
        item["qualityLevel"] = quality_level
        item["verificationStatus"] = "UNVERIFIED"


def annotate_conflict_source_strengths(merged: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    evidence_by_id = {
        str(item.get("evidenceId")): item
        for item in evidence
        if isinstance(item, dict) and item.get("evidenceId")
    }
    strength_order = {"A": 0, "B": 1, "C": 2, "D": 3}
    for conflict in merged.get("conflicts", []):
        if not isinstance(conflict, dict):
            continue
        for candidate in conflict.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            strengths = [
                str(evidence_by_id[ref].get("sourceStrength"))
                for ref in candidate.get("evidenceRefs", [])
                if ref in evidence_by_id and evidence_by_id[ref].get("sourceStrength") in strength_order
            ]
            candidate["sourceStrength"] = min(strengths, key=lambda value: strength_order[value]) if strengths else None


def merge_domain_payloads(domain_payloads: dict[str, list[dict[str, Any]]], *, as_of: date) -> dict[str, Any]:
    conflicts: list[dict[str, Any]] = []
    enterprise_items = [
        payload["companyCreateCandidate"]
        for payload in domain_payloads.get("enterprise", [])
        if isinstance(payload.get("companyCreateCandidate"), dict)
    ]
    company = _normalize_company(_merge_dicts(enterprise_items, "COMPANY", "company:new", conflicts))
    _attach_conflict_refs(company, "company:new", conflicts)
    aliases = []
    for payload in domain_payloads.get("enterprise", []):
        aliases.extend(item for item in payload.get("aliases", []) if isinstance(item, dict))
    aliases = _group_entities(
        aliases,
        entity_type="ALIAS",
        key_name="aliasKey",
        identity=lambda item: _identity([normalize_company_name(item.get("aliasName"))], item),
        conflicts=conflicts,
    )

    qualifications = [_normalize_qualification(item, as_of=as_of) for item in _payload_lists(domain_payloads, "qualifications", "qualifications")]
    qualifications = _group_entities_by_tokens(
        qualifications,
        entity_type="QUALIFICATION",
        key_name="qualificationKey",
        tokens=lambda item: [token for token in [
            _entity_token("00:cert_no", item.get("certNo")),
            _entity_token("01:name_level_expiry", item.get("certName"), item.get("certLevel"), item.get("expiryDate")),
            _entity_token("02:name_holder_issuer", item.get("certName"), item.get("holderName"), item.get("issuingAuthority")),
        ] if token],
        strong_labels={"00:cert_no"},
        conflicts=conflicts,
    )
    intellectual_properties = _group_entities_by_tokens(
        [_normalize_intellectual_property(item, as_of=as_of) for item in _payload_lists(domain_payloads, "intellectual_properties", "intellectualProperties")],
        entity_type="INTELLECTUAL_PROPERTY",
        key_name="propertyKey",
        tokens=lambda item: [token for token in [
            _entity_token("00:registration_no", item.get("registrationNo")),
            _entity_token("01:application_no", item.get("applicationNo")),
            _entity_token("02:type_name", item.get("propertyType"), item.get("propertyName")),
        ] if token],
        strong_labels={"00:registration_no", "01:application_no"},
        conflicts=conflicts,
    )
    for item in qualifications:
        item["subjectMatchStatus"] = _subject_match_status(item.get("holderName"), company.get("companyName"))
    for item in intellectual_properties:
        item["subjectMatchStatus"] = _subject_match_status(item.get("holderName"), company.get("companyName"))
    financial_history = _group_entities(
        [_normalize_financial(item) for item in _payload_lists(domain_payloads, "finance", "financialHistory")],
        entity_type="FINANCIAL_HISTORY",
        key_name="financialKey",
        identity=lambda item: _identity([item.get("fiscalYear")], item),
        conflicts=conflicts,
    )
    risks = _group_entities(
        [_normalize_risk(item) for item in _payload_lists(domain_payloads, "risk", "riskAndCompliance")],
        entity_type="RISK",
        key_name="riskKey",
        identity=lambda item: _identity([item.get("riskType"), item.get("title"), item.get("occurredAt")], item),
        conflicts=conflicts,
    )
    personnel = _group_entities_by_tokens(
        [_normalize_personnel(item) for item in _payload_lists(domain_payloads, "personnel", "personnelHistory")],
        entity_type="PERSONNEL",
        key_name="personKey",
        tokens=lambda item: [token for token in [
            *[
                _entity_token("00:name_cert", item.get("personName"), cert.get("certNo"))
                for cert in item.get("certificates", [])
                if isinstance(cert, dict) and cert.get("certNo")
            ],
            _entity_token("01:name_role", item.get("personName"), item.get("personRole") or item.get("roleInBid")),
        ] if token],
        conflicts=conflicts,
    )
    performances = [_normalize_performance(item) for item in _payload_lists(domain_payloads, "performances", "performances")]
    performances = _group_entities_by_tokens(
        performances,
        entity_type="PERFORMANCE",
        key_name="performanceKey",
        tokens=lambda item: [token for token in [
            _entity_token("00:contract_no", (item.get("contract") or {}).get("contractNo") if isinstance(item.get("contract"), dict) else None),
            _entity_token("01:project_code", item.get("projectCode")),
            _entity_token("02:tender_code", item.get("tenderCode")),
            _entity_token("03:name_customer", item.get("projectName"), item.get("customerName")),
            _entity_token("03:name_customer", item.get("projectName"), item.get("ownerCompanyName")),
            _entity_token("04:name_contract", item.get("projectName"), (item.get("contract") or {}).get("contractName") if isinstance(item.get("contract"), dict) else None),
            _entity_token("05:name_amount_date", item.get("projectName"), (item.get("contract") or {}).get("amountYuan") if isinstance(item.get("contract"), dict) else item.get("bidAmountYuan"), item.get("awardDate")),
        ] if token],
        strong_labels={"00:contract_no", "01:project_code", "02:tender_code"},
        conflicts=conflicts,
    )
    relationships = _group_entities(
        [_normalize_relationship(item) for item in _payload_lists(domain_payloads, "performances", "customerRelationships")],
        entity_type="BUSINESS_RELATION",
        key_name="relationshipKey",
        identity=lambda item: _identity([item.get("relatedCompanyName"), item.get("relationType")], item),
        conflicts=conflicts,
    )
    products = _group_entities(
        [_normalize_product(item) for item in _payload_lists(domain_payloads, "products", "productsAndEquipment")],
        entity_type="PRODUCT",
        key_name="productKey",
        identity=lambda item: _identity([item.get("manufacturer"), item.get("brand"), item.get("model"), item.get("productName")], item),
        conflicts=conflicts,
    )
    solutions = _group_entities(
        [_normalize_claim(item, delivery=False) for item in _payload_lists(domain_payloads, "solutions", "solutionClaims")],
        entity_type="SOLUTION_CLAIM",
        key_name="claimKey",
        identity=lambda item: _identity([item.get("category"), item.get("name")], item),
        conflicts=conflicts,
    )
    delivery = _group_entities(
        [_normalize_claim(item, delivery=True) for item in _payload_lists(domain_payloads, "delivery", "deliveryClaims")],
        entity_type="DELIVERY_CLAIM",
        key_name="claimKey",
        identity=lambda item: _identity([item.get("category"), item.get("name")], item),
        conflicts=conflicts,
    )
    bid_items = [payload.get("bidSpecific") for payload in domain_payloads.get("bid", []) if isinstance(payload.get("bidSpecific"), dict)]
    bid_specific = _merge_dicts(bid_items, "BID_SPECIFIC", "bid:current", conflicts)
    _attach_conflict_refs(bid_specific, "bid:current", conflicts)
    performance_keys = {str(item.get("performanceKey")) for item in performances if item.get("performanceKey")}
    _retain_resolved_performance_refs(personnel, "projectExperienceRefs", performance_keys)
    _retain_resolved_performance_refs(products, "usedInPerformanceRefs", performance_keys)
    _retain_resolved_performance_refs(solutions, "supportedByPerformanceRefs", performance_keys)
    _retain_resolved_performance_refs(delivery, "supportedByPerformanceRefs", performance_keys)
    company["profileAction"] = "REVIEW_REQUIRED"
    company["qualityLevel"] = "UNRESOLVED"
    company["verificationStatus"] = "UNVERIFIED"
    _apply_machine_policy(aliases, profile_action="REVIEW_REQUIRED", quality_level="UNRESOLVED")
    _apply_machine_policy(qualifications, profile_action="REVIEW_REQUIRED", quality_level="UNRESOLVED")
    _apply_machine_policy(intellectual_properties, profile_action="REVIEW_REQUIRED", quality_level="UNRESOLVED")
    _apply_machine_policy(financial_history, profile_action="HISTORY_ONLY", quality_level="UNRESOLVED")
    _apply_machine_policy(risks, profile_action="REVIEW_REQUIRED", quality_level="UNRESOLVED")
    _apply_machine_policy(personnel, profile_action="HISTORY_ONLY", quality_level="UNRESOLVED")
    _apply_machine_policy(performances, profile_action="REVIEW_REQUIRED", quality_level="UNRESOLVED")
    _apply_machine_policy(relationships, profile_action="REVIEW_REQUIRED", quality_level="UNRESOLVED")
    _apply_machine_policy(products, profile_action="REVIEW_REQUIRED", quality_level="UNRESOLVED")
    _apply_machine_policy(solutions, profile_action="CLAIM_ONLY", quality_level="MEDIUM")
    _apply_machine_policy(delivery, profile_action="BID_ONLY", quality_level="MEDIUM")
    bid_specific["profileAction"] = "BID_ONLY"
    bid_specific["verificationStatus"] = "UNVERIFIED"

    return {
        "companyCreateCandidate": company,
        "aliases": aliases,
        "qualifications": qualifications,
        "intellectualProperties": intellectual_properties,
        "financialHistory": financial_history,
        "riskAndCompliance": risks,
        "personnelHistory": personnel,
        "performances": performances,
        "customerRelationships": relationships,
        "productsAndEquipment": products,
        "solutionClaims": solutions,
        "deliveryClaims": delivery,
        "bidSpecific": bid_specific,
        "conflicts": conflicts,
    }


__all__ = ["annotate_conflict_source_strengths", "merge_domain_payloads"]
