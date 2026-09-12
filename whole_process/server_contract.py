"""当前服务器企业画像契约的只读代码快照。

该文件只定义抽取输出允许映射的字段，不执行数据库连接或写入。服务器字段发生变化时，
应重新只读导出并更新 CONTRACT_SNAPSHOT_DATE，而不是让模型动态发明字段代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


CONTRACT_SNAPSHOT_DATE: Final = "2026-09-09"


@dataclass(frozen=True)
class ProfileFieldContract:
    field_code: str
    field_group: str
    value_type: str
    extraction_mode: str
    required_for_creation: bool = False


def _field(
    field_code: str,
    field_group: str,
    value_type: str,
    extraction_mode: str,
    required_for_creation: bool = False,
) -> ProfileFieldContract:
    return ProfileFieldContract(field_code, field_group, value_type, extraction_mode, required_for_creation)


PROFILE_FIELDS: Final[tuple[ProfileFieldContract, ...]] = (
    _field("enterprise.name", "BUSINESS_REGISTRATION", "STRING", "DIRECT", True),
    _field("enterprise.credit_code", "BUSINESS_REGISTRATION", "STRING", "DIRECT", True),
    _field("enterprise.legal_person", "BUSINESS_REGISTRATION", "STRING", "DIRECT"),
    _field("enterprise.registered_capital", "BUSINESS_REGISTRATION", "MONEY", "DIRECT"),
    _field("enterprise.established_date", "BUSINESS_REGISTRATION", "DATE", "DIRECT"),
    _field("enterprise.business_scope", "BUSINESS_REGISTRATION", "TEXT", "DIRECT"),
    _field("enterprise.location", "BUSINESS_REGISTRATION", "OBJECT", "DIRECT"),
    _field("enterprise.employee_count", "BUSINESS_REGISTRATION", "NUMBER", "DIRECT"),
    _field("qualification.items", "QUALIFICATION", "ARRAY", "ENTITY_AND_FIELD"),
    _field("personnel.core_members", "PERSONNEL", "ARRAY", "ENTITY_AND_FIELD"),
    _field("personnel.professionals", "PERSONNEL", "ARRAY", "ENTITY_AND_FIELD"),
    _field("capability.main_industries", "CAPABILITY", "ARRAY", "DERIVED"),
    _field("capability.intellectual_properties", "CAPABILITY", "ARRAY", "ENTITY_AND_FIELD"),
    _field("capability.project_performances", "CAPABILITY", "ARRAY", "ENTITY_AND_FIELD"),
    _field("capability.main_regions", "CAPABILITY", "ARRAY", "DERIVED"),
    _field("capability.average_win_amount", "CAPABILITY", "MONEY", "DERIVED"),
    _field("capability.win_project_count_3y", "CAPABILITY", "NUMBER", "DERIVED"),
    _field("capability.buyer_relationships", "CAPABILITY", "ARRAY", "DERIVED"),
    _field("capability.tender_records", "CAPABILITY", "OBJECT", "POST_IMPORT_DERIVED"),
    _field("preference.target_industries", "DECISION", "ARRAY", "USER_CONFIRMED_ONLY"),
    _field("preference.target_regions", "DECISION", "ARRAY", "USER_CONFIRMED_ONLY"),
    _field("preference.project_types", "DECISION", "ARRAY", "USER_CONFIRMED_ONLY"),
    _field("preference.amount_range", "DECISION", "OBJECT", "USER_CONFIRMED_ONLY"),
    _field("preference.products_solutions", "DECISION", "ARRAY", "USER_CONFIRMED_ONLY"),
    _field("risk.records", "RISK", "ARRAY", "ENTITY_AND_FIELD"),
    _field("tag.capabilities", "TAG", "ARRAY", "DERIVED"),
    _field("tag.risks", "TAG", "ARRAY", "DERIVED"),
    _field("evaluation.scores", "EVALUATION", "OBJECT", "POST_IMPORT_DERIVED"),
    _field("quality.api_coverage", "DATA_QUALITY", "ARRAY", "EXTERNAL_ONLY"),
    _field("quality.data_gaps", "DATA_QUALITY", "ARRAY", "DERIVED"),
)

PROFILE_FIELD_BY_CODE: Final = {field.field_code: field for field in PROFILE_FIELDS}
PROFILE_FIELD_CODES: Final = frozenset(PROFILE_FIELD_BY_CODE)
LEGACY_FIELD_CODES: Final = frozenset({"capability.similar_projects"})


CAPABILITY_DIMENSIONS: Final[dict[str, dict[str, object]]] = {
    "industry_capability": {
        "name": "行业能力",
        "fieldCodes": ["capability.main_industries", "capability.tender_records"],
    },
    "technical_capability": {
        "name": "技术能力",
        "fieldCodes": ["qualification.items", "capability.intellectual_properties", "tag.capabilities"],
    },
    "similar_performance_capability": {
        "name": "相似业绩能力",
        "fieldCodes": ["capability.project_performances", "capability.tender_records"],
    },
    "regional_delivery_capability": {
        "name": "区域交付能力",
        "fieldCodes": ["enterprise.location", "capability.main_regions", "capability.project_performances"],
    },
    "amount_experience_capability": {
        "name": "金额经验能力",
        "fieldCodes": ["capability.average_win_amount", "capability.win_project_count_3y"],
    },
    "personnel_resource_capability": {
        "name": "人员资源能力",
        "fieldCodes": ["enterprise.employee_count", "personnel.core_members", "personnel.professionals"],
    },
    "buyer_relationship_capability": {
        "name": "客户关系能力",
        "fieldCodes": ["capability.buyer_relationships", "capability.project_performances"],
    },
    "tender_performance_capability": {
        "name": "投标表现能力",
        "fieldCodes": ["capability.tender_records", "capability.project_performances"],
    },
}


def server_target_contract() -> dict[str, object]:
    return {
        "snapshotDate": CONTRACT_SNAPSHOT_DATE,
        "enabledFieldCount": len(PROFILE_FIELDS),
        "enabledFields": [
            {
                "fieldCode": item.field_code,
                "fieldGroup": item.field_group,
                "valueType": item.value_type,
                "extractionMode": item.extraction_mode,
                "requiredForCreation": item.required_for_creation,
            }
            for item in PROFILE_FIELDS
        ],
        "capabilityDimensionCount": len(CAPABILITY_DIMENSIONS),
        "capabilityDimensions": CAPABILITY_DIMENSIONS,
        "warnings": [
            "服务器评分指标仍引用旧字段 capability.similar_projects；本输出只使用 capability.project_performances。",
            "当前 ProfileSourceType 没有 DOCUMENT_AI；本输出只是候选包，不能直接发布。",
        ],
    }


__all__ = [
    "CAPABILITY_DIMENSIONS",
    "CONTRACT_SNAPSHOT_DATE",
    "LEGACY_FIELD_CODES",
    "PROFILE_FIELDS",
    "PROFILE_FIELD_BY_CODE",
    "PROFILE_FIELD_CODES",
    "ProfileFieldContract",
    "server_target_contract",
]
