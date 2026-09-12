"""确定性的文本、标识、日期、金额和敏感信息归一化。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


NULL_TEXTS = {"", "null", "none", "unknown", "未知", "未识别", "未显示", "--", "—", "-"}
DATE_PATTERN = re.compile(r"^(\d{4})\s*(?:年|[-./])\s*(\d{1,2})\s*(?:月|[-./])\s*(\d{1,2})\s*日?$")
CREDIT_CODE_PATTERN = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{18}$")
PERSONAL_ID_PATTERN = re.compile(r"(?<![0-9A-Z])\d{17}[0-9Xx](?![0-9A-Z])")
PHONE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
BANK_CARD_PATTERN = re.compile(r"(?<!\d)\d{16,19}(?!\d)")


def clean_text(value: Any, max_length: int = 12000) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]", "", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if text.casefold() in NULL_TEXTS:
        return None
    return text[:max_length] or None


def clean_verbatim_text(value: Any, max_length: int = 12000) -> str | None:
    """保留全半角等原始字形，仅移除控制字符和多余横向空白。"""

    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    text = str(value)
    text = re.sub(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]", "", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if unicodedata.normalize("NFKC", text).casefold() in NULL_TEXTS:
        return None
    return text[:max_length] or None


def normalize_company_name(value: Any) -> str | None:
    text = clean_text(value, 200)
    if not text:
        return None
    return re.sub(r"\s+", "", text).replace("（", "(").replace("）", ")")


def normalize_credit_code(value: Any) -> str | None:
    text = clean_text(value, 64)
    if not text:
        return None
    normalized = re.sub(r"[\s-]", "", text).upper()
    return normalized if CREDIT_CODE_PATTERN.fullmatch(normalized) else None


def normalize_date(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = clean_text(value, 32)
    if not text:
        return None
    match = DATE_PATTERN.fullmatch(text)
    candidate = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}" if match else text
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    integral = number.to_integral_value()
    return int(integral) if number == integral else float(number)


def finite_signed_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    integral = number.to_integral_value()
    return int(integral) if number == integral else float(number)


def normalize_money(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        amount = finite_number(value.get("amountYuan"))
        raw_text = clean_text(value.get("rawText"), 200)
        raw_unit = clean_text(value.get("rawUnit"), 32)
        currency = clean_text(value.get("currency"), 8) or "CNY"
        parsed_from_raw = False
        if amount is None and raw_text:
            amount = parse_money_text(raw_text)
            parsed_from_raw = amount is not None
        if amount is None and not raw_text:
            return None
        return {
            "amountYuan": amount,
            "currency": currency,
            "rawText": raw_text,
            "rawUnit": raw_unit,
            "normalizationRule": clean_text(value.get("normalizationRule"), 64) or ("MONEY_TEXT_TO_YUAN" if parsed_from_raw else None),
        }
    raw_text = clean_text(value, 200)
    if not raw_text:
        return None
    return {
        "amountYuan": parse_money_text(raw_text),
        "currency": "CNY",
        "rawText": raw_text,
        "rawUnit": None,
        "normalizationRule": "MONEY_TEXT_TO_YUAN" if parse_money_text(raw_text) is not None else None,
    }


def parse_arabic_money_text(text: str) -> float | int | None:
    normalized = unicodedata.normalize("NFKC", text).replace(",", "")
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(亿元|万元|万|千元|元)(?![\d万亿])", normalized)
    if not match:
        return None
    amount = Decimal(match.group(1))
    multiplier = {
        "亿元": Decimal("100000000"),
        "万元": Decimal("10000"),
        "万": Decimal("10000"),
        "千元": Decimal("1000"),
        "元": Decimal("1"),
    }[match.group(2)]
    result = amount * multiplier
    integral = result.to_integral_value()
    return int(integral) if result == integral else float(result)


def parse_chinese_money_text(text: str) -> float | int | None:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"[人民币￥¥\s,，]", "", normalized)
    match = re.search(r"([零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億]+元(?:[零〇一二三四五六七八九壹贰叁肆伍陆柒捌玖][角分]){0,2}(?:整|正)?)", normalized)
    if not match:
        return None
    money = match.group(1)
    integer_text, fraction_text = money.split("元", 1)
    digits = {
        "零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "三": 3, "叁": 3,
        "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6, "七": 7, "柒": 7,
        "八": 8, "捌": 8, "九": 9, "玖": 9,
    }
    small_units = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
    big_units = {"万": 10_000, "萬": 10_000, "亿": 100_000_000, "億": 100_000_000}
    total = section = number = 0
    for char in integer_text:
        if char in digits:
            number = digits[char]
        elif char in small_units:
            section += (number or 1) * small_units[char]
            number = 0
        elif char in big_units:
            total += (section + number) * big_units[char]
            section = number = 0
        else:
            return None
    amount = Decimal(total + section + number)
    for char, unit in re.findall(r"([零〇一二三四五六七八九壹贰叁肆伍陆柒捌玖])([角分])", fraction_text):
        amount += Decimal(digits[char]) * (Decimal("0.1") if unit == "角" else Decimal("0.01"))
    integral = amount.to_integral_value()
    return int(integral) if amount == integral else float(amount)


def parse_money_text(text: str) -> float | int | None:
    arabic = parse_arabic_money_text(text)
    return arabic if arabic is not None else parse_chinese_money_text(text)


def normalize_string_list(value: Any, *, limit: int = 200, max_item_length: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value[:limit]:
        text = clean_text(item, max_item_length)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            result.append(text)
    return result


def redact_sensitive_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []

    def replace(pattern: re.Pattern[str], kind: str, source: str) -> str:
        def replacer(match: re.Match[str]) -> str:
            findings.append({"type": kind, "count": 1})
            return f"[{kind}_REDACTED]"

        return pattern.sub(replacer, source)

    redacted = replace(PERSONAL_ID_PATTERN, "PERSONAL_ID", text)
    redacted = replace(PHONE_PATTERN, "PHONE", redacted)
    redacted = replace(BANK_CARD_PATTERN, "BANK_ACCOUNT_CANDIDATE", redacted)
    return redacted, findings


def remove_person_names(value: Any) -> Any:
    if isinstance(value, list):
        return [remove_person_names(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        conflict_is_person_name = str(value.get("fieldPath") or "").replace("_", "").casefold().endswith("personname")
        for key, item in value.items():
            normalized_key = key.replace("_", "").casefold()
            if normalized_key == "personname":
                result[key] = None
            elif conflict_is_person_name and key in {"candidates", "resolution"}:
                result[key] = [] if key == "candidates" else {
                    "status": "REDACTED",
                    "selectedValue": None,
                    "rule": "OUTPUT_EXCLUDE_PERSON_NAMES",
                }
            else:
                result[key] = remove_person_names(item)
        return result
    return value


SENSITIVE_OUTPUT_KEYS = {
    "idnumber", "identitynumber", "bankaccount", "bankcardnumber", "phonenumber", "signatureimage",
}
IDENTIFIER_KEYS = {
    "creditcode", "sha256", "evidenceid", "runid", "requestid", "conflictid", "assessmentuid",
    "snapshotuid", "sourceref", "certno", "projectcode", "tendercode", "contractno",
    "registrationno", "applicationno", "evidencejson",
}


def sanitize_sensitive_output(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, list):
        return [sanitize_sensitive_output(item, parent_key=parent_key) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = key.replace("_", "").casefold()
            if normalized_key in SENSITIVE_OUTPUT_KEYS:
                result[key] = None
            else:
                result[key] = sanitize_sensitive_output(item, parent_key=key)
        return result
    if isinstance(value, str):
        normalized_parent = parent_key.replace("_", "").casefold()
        if normalized_parent in IDENTIFIER_KEYS or normalized_parent.endswith("key") or normalized_parent.endswith("refs"):
            return value
        redacted, _ = redact_sensitive_text(value)
        return redacted
    return value


__all__ = [
    "clean_text",
    "clean_verbatim_text",
    "finite_number",
    "finite_signed_number",
    "normalize_company_name",
    "normalize_credit_code",
    "normalize_date",
    "normalize_money",
    "normalize_string_list",
    "parse_arabic_money_text",
    "parse_chinese_money_text",
    "parse_money_text",
    "redact_sensitive_text",
    "remove_person_names",
    "sanitize_sensitive_output",
]
