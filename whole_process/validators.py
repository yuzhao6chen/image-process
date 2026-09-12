"""最终新企业初始化候选包的结构、证据、字段契约和业务边界校验。"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from normalizers import BANK_CARD_PATTERN, CREDIT_CODE_PATTERN, PERSONAL_ID_PATTERN, PHONE_PATTERN
from server_contract import CAPABILITY_DIMENSIONS, LEGACY_FIELD_CODES, PROFILE_FIELD_BY_CODE, PROFILE_FIELD_CODES


REQUIRED_TOP_LEVEL = {
    "schemaVersion", "operation", "companyId", "run", "sourceDocument", "subjectCompany",
    "creationReadiness", "pageManifestSummary", "companyCreateCandidate", "aliases",
    "qualifications", "intellectualProperties", "financialHistory", "riskAndCompliance",
    "personnelHistory", "performances", "customerRelationships", "productsAndEquipment",
    "solutionClaims", "deliveryClaims", "customerAndRegionExperience", "bidSpecific",
    "capabilityEvidence", "capabilityAssessments", "derivedProfileCandidates",
    "profileProjectionCandidate", "serverTargetContract", "evidence", "conflicts",
    "sensitiveFindings", "quality",
}
FORBIDDEN_KEYS = {
    "idnumber", "identitynumber", "bankaccount", "bankcardnumber", "phonenumber", "signatureimage",
    "id_number", "identity_number", "bank_account", "bank_card_number", "phone_number", "signature_image",
}
USER_PREFERENCE_PREFIX = "preference."
ALLOWED_PROFILE_ACTIONS = {"PROFILE_CANDIDATE", "REVIEW_REQUIRED", "HISTORY_ONLY", "CLAIM_ONLY", "BID_ONLY"}
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "company_full_extraction.schema.json"
DATE_KEYS = {
    "establishedDate", "documentAsOfDate", "issueDate", "expiryDate", "applicationDate", "reportDate",
    "occurredAt", "resolvedAt", "queryAsOfDate", "asOfDate", "validFrom", "awardDate", "signDate",
    "startDate", "plannedEndDate", "actualEndDate", "acceptanceDate", "firstRelationshipDate", "lastRelationshipDate",
}


def _walk(value: Any, path: str = "$") -> Iterable[tuple[str, str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{path}.{key}"
            yield current, key, item
            yield from _walk(item, current)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def _validate_field_type(code: str, value: Any) -> bool:
    value_type = PROFILE_FIELD_BY_CODE[code].value_type
    if value_type == "ARRAY":
        return isinstance(value, list)
    if value_type == "OBJECT":
        return isinstance(value, dict)
    if value_type == "NUMBER":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "MONEY":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
    return isinstance(value, str) and bool(value.strip())


def _json_schema_errors(package: dict[str, Any]) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RuntimeError("缺少 jsonschema；请先安装 requirements.txt 中的依赖") from exc
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    result: list[str] = []
    for error in sorted(validator.iter_errors(package), key=lambda item: list(item.absolute_path)):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        result.append(f"JSON Schema：{path} {error.message}")
    return result


def validate_creation_package(
    package: dict[str, Any],
    *,
    page_manifest: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    errors: list[str] = _json_schema_errors(package)
    warnings: list[str] = []
    missing_top = sorted(REQUIRED_TOP_LEVEL - set(package))
    unknown_top = sorted(set(package) - REQUIRED_TOP_LEVEL)
    if missing_top:
        errors.append(f"缺少顶层字段：{', '.join(missing_top)}")
    if unknown_top:
        errors.append(f"存在未知顶层字段：{', '.join(unknown_top)}")
    if package.get("schemaVersion") != "company-tender-profile/v2":
        errors.append("schemaVersion 必须为 company-tender-profile/v2")
    if package.get("operation") != "CREATE_COMPANY":
        errors.append("operation 必须为 CREATE_COMPANY")
    if package.get("companyId") is not None:
        errors.append("抽取输出的 companyId 必须为 null")

    pages = [int(item.get("physicalPage") or 0) for item in page_manifest]
    if len(pages) != len(set(pages)) or any(page <= 0 for page in pages):
        errors.append("PageManifest 物理页码必须为唯一正整数")
    allowed_pages = set(pages)
    evidence = package.get("evidence") if isinstance(package.get("evidence"), list) else []
    evidence_ids: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            errors.append("evidence 数组元素必须是对象")
            continue
        evidence_id = str(item.get("evidenceId") or "")
        if not evidence_id:
            errors.append("存在缺少 evidenceId 的证据")
        evidence_ids.append(evidence_id)
        if int(item.get("physicalPage") or 0) not in allowed_pages:
            errors.append(f"证据 {evidence_id or '<missing>'} 引用了无效物理页码")
        if not str(item.get("sourceText") or "").strip():
            errors.append(f"证据 {evidence_id or '<missing>'} 缺少最小原文")
    if len(evidence_ids) != len(set(evidence_ids)):
        errors.append("evidenceId 必须唯一")
    evidence_id_set = set(evidence_ids)

    referenced_ids: set[str] = set()
    referenced_conflicts: set[str] = set()
    for path, key, value in _walk(package):
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"禁止输出非有限数值：{path}")
        if key.endswith("AmountYuan") and value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value < 0:
                errors.append(f"金额必须是有限非负数：{path}")
        if key in DATE_KEYS and value is not None:
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                errors.append(f"日期必须为 YYYY-MM-DD：{path}")
        if key.casefold() in FORBIDDEN_KEYS and not (value is None or value == "" or value == [] or value == {}):
            errors.append(f"禁止输出敏感字段：{path}")
        if key in {"companyId", "projectId", "userId"} and value is not None:
            errors.append(f"文档抽取不得绑定或生成在线实体 ID：{path}")
        if key == "profileAction" and value not in ALLOWED_PROFILE_ACTIONS:
            errors.append(f"非法 profileAction：{path}={value}")
        if key == "verificationStatus" and value != "UNVERIFIED":
            errors.append(f"机器抽取事实必须为 UNVERIFIED：{path}")
        if key == "evidenceRefs":
            if not isinstance(value, list):
                errors.append(f"{path} 必须是数组")
            else:
                referenced_ids.update(str(item) for item in value if item)
        if key == "evidenceJson":
            if not isinstance(value, list):
                errors.append(f"{path} 必须是数组")
            else:
                referenced_ids.update(str(item) for item in value if item)
        if key == "conflictRefs":
            if not isinstance(value, list):
                errors.append(f"{path} 必须是数组")
            else:
                referenced_conflicts.update(str(item) for item in value if item)
        if key in {"sourceText", "contractContent", "description"} and isinstance(value, str):
            if PERSONAL_ID_PATTERN.search(value):
                errors.append(f"发现疑似未脱敏身份证号：{path}")
            if PHONE_PATTERN.search(value):
                errors.append(f"发现疑似未脱敏手机号：{path}")
            if BANK_CARD_PATTERN.search(value):
                errors.append(f"发现疑似未脱敏银行账号：{path}")
    dangling = sorted(referenced_ids - evidence_id_set)
    if dangling:
        errors.append(f"存在悬空证据引用：{', '.join(dangling[:20])}")

    conflicts = package.get("conflicts")
    conflict_ids = [
        str(item.get("conflictId") or "")
        for item in conflicts
        if isinstance(item, dict)
    ] if isinstance(conflicts, list) else []
    if any(not conflict_id for conflict_id in conflict_ids) or len(conflict_ids) != len(set(conflict_ids)):
        errors.append("conflictId 必须存在且唯一")
    dangling_conflicts = sorted(referenced_conflicts - set(conflict_ids))
    if dangling_conflicts:
        errors.append(f"存在悬空冲突引用：{', '.join(dangling_conflicts[:20])}")

    performance_values = package.get("performances")
    performance_keys = {
        str(item.get("performanceKey"))
        for item in performance_values
        if isinstance(item, dict) and item.get("performanceKey")
    } if isinstance(performance_values, list) else set()
    entity_keys = {
        str(value)
        for _, key, value in _walk(package)
        if key.endswith("Key") and isinstance(value, str) and value
    }
    performance_ref_fields = {"projectExperienceRefs", "projectRefs", "usedInPerformanceRefs", "supportedByPerformanceRefs"}
    for path, key, value in _walk(package):
        if key in performance_ref_fields and isinstance(value, list):
            invalid = sorted({str(ref) for ref in value if ref and str(ref) not in performance_keys})
            if invalid:
                errors.append(f"存在悬空历史项目引用：{path} -> {', '.join(invalid[:10])}")
        elif key == "performanceRef" and value and str(value) not in performance_keys:
            errors.append(f"存在悬空历史项目引用：{path} -> {value}")
        elif key == "supportingEntityRefs" and isinstance(value, list):
            invalid = sorted({str(ref) for ref in value if ref and str(ref) not in entity_keys})
            if invalid:
                errors.append(f"存在悬空实体引用：{path} -> {', '.join(invalid[:10])}")

    projection = package.get("profileProjectionCandidate")
    projection = projection if isinstance(projection, dict) else {}
    if projection.get("operation") != "CREATE_COMPANY" or projection.get("companyId") is not None:
        errors.append("profileProjectionCandidate 必须保持 CREATE_COMPANY/companyId=null")
    candidates = projection.get("fieldCandidates")
    candidates = candidates if isinstance(candidates, list) else []
    seen_codes: set[str] = set()
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            errors.append(f"fieldCandidates[{index}] 必须是对象")
            continue
        code = str(candidate.get("fieldCode") or "")
        if code in LEGACY_FIELD_CODES:
            errors.append(f"禁止输出旧画像字段：{code}")
        elif code not in PROFILE_FIELD_CODES:
            errors.append(f"服务器未启用画像字段：{code}")
            continue
        if code in seen_codes:
            errors.append(f"画像字段候选重复：{code}")
        seen_codes.add(code)
        if code.startswith(USER_PREFERENCE_PREFIX):
            errors.append(f"历史投标文件不得自动生成正式经营偏好：{code}")
        if not _validate_field_type(code, candidate.get("value")):
            errors.append(f"画像字段 {code} 的值不符合 {PROFILE_FIELD_BY_CODE[code].value_type}")
        refs = candidate.get("evidenceRefs")
        if code != "quality.data_gaps" and (not isinstance(refs, list) or not refs):
            errors.append(f"画像字段 {code} 缺少证据")
        if candidate.get("verificationStatus") != "UNVERIFIED":
            errors.append(f"机器抽取字段 {code} 必须为 UNVERIFIED")

    company = package.get("companyCreateCandidate")
    company = company if isinstance(company, dict) else {}
    credit_code = company.get("creditCode")
    if credit_code is not None and not CREDIT_CODE_PATTERN.fullmatch(str(credit_code)):
        errors.append("companyCreateCandidate.creditCode 格式无效")
    readiness = package.get("creationReadiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    if readiness.get("status") == "READY_FOR_DUPLICATE_CHECK":
        if not company.get("companyName") or not credit_code:
            errors.append("READY_FOR_DUPLICATE_CHECK 必须具备企业名称和统一社会信用代码")

    personnel = package.get("personnelHistory")
    for index, person in enumerate(personnel if isinstance(personnel, list) else []):
        if not isinstance(person, dict):
            continue
        employment = person.get("employmentEvidence")
        if person.get("personnelType") not in {"CORE_MEMBER", "PROFESSIONAL", "BID_TEAM_ONLY"}:
            errors.append(f"personnelHistory[{index}].personnelType 无效")
        if isinstance(employment, dict) and employment.get("currentEmploymentConfirmed") is True:
            refs = employment.get("evidenceRefs")
            if not employment.get("asOfDate") or not isinstance(refs, list) or not refs:
                errors.append(f"personnelHistory[{index}] 当前任职确认缺少时点或证据")

    performances = package.get("performances")
    if not isinstance(performances, list):
        errors.append("performances 必须是数组")
        performances = []
    for index, performance in enumerate(performances):
        if not isinstance(performance, dict):
            errors.append(f"performances[{index}] 必须是对象")
            continue
        if not performance.get("projectName"):
            warnings.append(f"performances[{index}] 缺少项目名称")
        if performance.get("role") not in {"OWNER", "CONTRACTOR", "SUPPLIER", "SERVICE_PROVIDER", "CONSORTIUM_MEMBER", "UNKNOWN"}:
            errors.append(f"performances[{index}].role 无效")
        if performance.get("performanceStatus") not in {"AWARDED", "CONTRACTED", "ONGOING", "COMPLETED", "ACCEPTED", "UNKNOWN"}:
            errors.append(f"performances[{index}].performanceStatus 无效")
        missing_detail = [
            name for name, value in {
                "项目内容": performance.get("scopeItems"),
                "行业": performance.get("industry"),
                "项目类型": performance.get("projectType"),
                "区域": performance.get("province") or performance.get("city") or performance.get("locationText"),
                "履约状态": performance.get("performanceStatus"),
            }.items() if value is None or value == "" or value == "UNKNOWN" or value == []
        ]
        if missing_detail:
            warnings.append(f"performances[{index}] 仍缺少：{', '.join(missing_detail)}")

    for index, item in enumerate(package.get("qualifications", []) if isinstance(package.get("qualifications"), list) else []):
        if isinstance(item, dict):
            if item.get("status") not in {"VALID", "EXPIRING", "EXPIRED", "REVOKED", "UNKNOWN"}:
                errors.append(f"qualifications[{index}].status 无效")
            if item.get("subjectMatchStatus") not in {"MATCH", "MISMATCH", "UNKNOWN"}:
                errors.append(f"qualifications[{index}].subjectMatchStatus 无效")
    for index, item in enumerate(package.get("intellectualProperties", []) if isinstance(package.get("intellectualProperties"), list) else []):
        if not isinstance(item, dict):
            continue
        if item.get("propertyType") not in {"PATENT", "TRADEMARK", "SOFTWARE_COPYRIGHT", "STANDARD", "OTHER"}:
            errors.append(f"intellectualProperties[{index}].propertyType 无效")
        if item.get("status") not in {"VALID", "ACTIVE", "PENDING", "EXPIRED", "REVOKED", "INVALID", "UNKNOWN"}:
            errors.append(f"intellectualProperties[{index}].status 无效")
        if item.get("subjectMatchStatus") not in {"MATCH", "MISMATCH", "UNKNOWN"}:
            errors.append(f"intellectualProperties[{index}].subjectMatchStatus 无效")
    for index, item in enumerate(package.get("riskAndCompliance", []) if isinstance(package.get("riskAndCompliance"), list) else []):
        if isinstance(item, dict) and item.get("riskType") not in {"ADMIN_PENALTY", "DISHONESTY", "LITIGATION", "ENFORCEMENT", "BUSINESS_ANOMALY", "TAX", "QUALIFICATION_EXPIRY", "OTHER"}:
            errors.append(f"riskAndCompliance[{index}].riskType 无效")
    for index, item in enumerate(package.get("customerRelationships", []) if isinstance(package.get("customerRelationships"), list) else []):
        if isinstance(item, dict) and item.get("relationType") not in {"CUSTOMER", "SUPPLIER", "PARTNER", "CONSORTIUM"}:
            errors.append(f"customerRelationships[{index}].relationType 无效")
    for index, item in enumerate(package.get("productsAndEquipment", []) if isinstance(package.get("productsAndEquipment"), list) else []):
        if isinstance(item, dict) and item.get("companyRole") not in {"OWNER", "MANUFACTURER", "AUTHORIZED_RESELLER", "INTEGRATOR", "PROPOSED_SUPPLIER", "UNKNOWN"}:
            errors.append(f"productsAndEquipment[{index}].companyRole 无效")
    for collection_name, allowed_categories in (
        ("solutionClaims", {"SOFTWARE_SOLUTION", "SYSTEM_INTEGRATION", "TECHNICAL_CAPABILITY", "PRODUCT", "OTHER"}),
        ("deliveryClaims", {"IMPLEMENTATION", "QUALITY", "AFTER_SALES", "TRAINING", "ACCEPTANCE", "RISK_MANAGEMENT", "OTHER"}),
    ):
        for index, item in enumerate(package.get(collection_name, []) if isinstance(package.get(collection_name), list) else []):
            if not isinstance(item, dict):
                continue
            if item.get("category") not in allowed_categories:
                errors.append(f"{collection_name}[{index}].category 无效")
            if item.get("claimType") != "DECLARED":
                errors.append(f"{collection_name}[{index}].claimType 必须为 DECLARED")

    assessments = package.get("capabilityAssessments")
    if not isinstance(assessments, list):
        errors.append("capabilityAssessments 必须是数组")
        assessments = []
    dimension_codes = [str(item.get("dimensionCode") or "") for item in assessments if isinstance(item, dict)]
    if set(dimension_codes) != set(CAPABILITY_DIMENSIONS):
        errors.append("capabilityAssessments 必须且只能覆盖服务器八个能力维度")
    if len(dimension_codes) != len(set(dimension_codes)):
        errors.append("能力评价维度不能重复")
    for index, assessment in enumerate(assessments):
        if not isinstance(assessment, dict):
            errors.append(f"capabilityAssessments[{index}] 必须是对象")
            continue
        if assessment.get("supportStatus") not in {"SUPPORTED", "INSUFFICIENT_DATA", "CONFLICTED"}:
            errors.append(f"capabilityAssessments[{index}] supportStatus 无效")

    capability_items = package.get("capabilityEvidence")
    for index, capability in enumerate(capability_items if isinstance(capability_items, list) else []):
        if not isinstance(capability, dict):
            errors.append(f"capabilityEvidence[{index}] 必须是对象")
            continue
        if capability.get("claimType") not in {"DELIVERED", "CONTRACTED", "DECLARED", "INFERRED", "UNKNOWN"}:
            errors.append(f"capabilityEvidence[{index}] claimType 无效")
        if capability.get("supportStatus") not in {"SUPPORTED", "INSUFFICIENT_DATA"}:
            errors.append(f"capabilityEvidence[{index}] supportStatus 无效")
        if capability.get("supportStatus") == "SUPPORTED":
            if not capability.get("evidenceRefs") or capability.get("sourceStrength") not in {"A", "B", "C", "D"}:
                errors.append(f"capabilityEvidence[{index}] 已支持能力缺少有效证据或证据等级")

    invoices = package.get("pageManifestSummary", {}).get("invoicePages", [])
    for invoice in invoices if isinstance(invoices, list) else []:
        if invoice.get("detailExtraction") != "SKIPPED":
            warnings.append("存在未按默认策略跳过明细的发票页")
    return errors, warnings


__all__ = ["validate_creation_package"]
