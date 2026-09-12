"""使用智谱视觉模型抽取员工证书图片，生成可审计的 Markdown/JSON。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from prompt import SYSTEM_PROMPT, build_user_prompt


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE_NAME = ".employee_extraction_state.json"
SUPPORTED_MIME_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
DOCUMENT_CONFIG = {
    "employee_certificate": {"directory": "员工证书结果", "label": "员工证书", "summary_stem": "员工证书汇总"},
    "unknown": {"directory": "其他文档结果", "label": "其他文档", "summary_stem": "其他文档汇总"},
}
PERSON_FIELDS = (
    ("name", "姓名"), ("gender", "性别"), ("birth_date", "出生日期"),
    ("id_number", "身份证号/证件号码"), ("company_name", "工作/注册单位"),
    ("position", "职务/岗位"), ("title", "职称"),
)
CERTIFICATE_FIELDS = (
    ("cert_name", "证书名称"), ("cert_no", "证书编号"),
    ("qualification_type", "资格类型"), ("level", "等级"),
    ("profession", "专业"), ("additional_professions", "增项专业"),
    ("issuing_authority", "发证机关"), ("issue_date", "发证日期"),
    ("valid_from", "有效起始日期"), ("expiry_date", "有效截止日期"),
    ("registration_status", "图片登记状态"), ("scope", "执业/作业范围"),
    ("status", "本地派生状态"),
)
NULL_TEXT_VALUES = {"", "null", "none", "未知", "未识别", "未显示", "--", "—"}
MAX_RESPONSE_BYTES = 3 * 1024 * 1024
MAX_TEXT_LENGTH = 12000
DATE_PATTERN = re.compile(r"^(\d{4})\s*(?:年|[-./])\s*(\d{1,2})\s*(?:月|[-./])\s*(\d{1,2})\s*日?$")


class ExtractionError(RuntimeError):
    """单张图片可记录并继续的错误。"""


class ApiError(ExtractionError):
    def __init__(self, message: str, *, retriable: bool = False, fatal: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.fatal = fatal


def load_local_env(path: Path) -> None:
    if not path.is_file():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            print(f"警告：忽略 .env 第 {number} 行（缺少等号）", file=sys.stderr)
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            print(f"警告：忽略 .env 第 {number} 行（变量名无效）", file=sys.stderr)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else fallback
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc


def env_float(name: str, fallback: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else fallback
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数字") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="调用智谱视觉模型OCR员工证书，生成单图与汇总 Markdown/JSON。")
    parser.add_argument("--input-dir", required=True, type=Path, help="待处理员工证书图片目录")
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR, help="输出根目录，默认脚本所在目录")
    parser.add_argument("--company-name", default="", help="目标企业，仅用于归属核验，不用于补全")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--model", default=os.environ.get("ZHIPU_MODEL", "").strip())
    parser.add_argument("--timeout", type=float, default=env_float("ZHIPU_TIMEOUT_SECONDS", 180.0))
    parser.add_argument("--max-retries", type=int, default=env_int("ZHIPU_MAX_RETRIES", 2))
    parser.add_argument("--max-tokens", type=int, default=env_int("ZHIPU_MAX_TOKENS", 5000))
    parser.add_argument("--max-image-mb", type=float, default=env_float("ZHIPU_MAX_IMAGE_MB", 20.0))
    parser.add_argument("--review-confidence", type=float, default=env_float("EMPLOYEE_CERT_REVIEW_CONFIDENCE", 0.8))
    parser.add_argument("--expiring-days", type=int, default=env_int("EMPLOYEE_CERT_EXPIRING_DAYS", 90))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--limit", type=int)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    args.input_dir, args.output_root = args.input_dir.expanduser().resolve(), args.output_root.expanduser().resolve()
    if not args.input_dir.is_dir():
        raise ValueError(f"输入目录不存在或不是目录：{args.input_dir}")
    if args.timeout <= 0 or args.max_tokens <= 0 or args.max_image_mb <= 0:
        raise ValueError("超时、max-tokens 和图片大小限制必须大于0")
    if args.max_retries < 0 or args.expiring_days < 0:
        raise ValueError("重试次数和临期天数不能小于0")
    if not 0 <= args.review_confidence <= 1:
        raise ValueError("--review-confidence 必须在0到1之间")
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit 必须大于0")


def clean_text(value: Any, max_length: int = MAX_TEXT_LENGTH) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()[:max_length]


def clean_optional_text(value: Any, max_length: int = MAX_TEXT_LENGTH) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = clean_text(value, max_length)
    return None if text.casefold() in NULL_TEXT_VALUES else (text or None)


def append_warning(warnings: list[str], message: str) -> None:
    value = clean_text(message, 500)
    if value and value not in warnings:
        warnings.append(value)


def normalize_list(value: Any, limit: int = 50, max_length: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        normalized = clean_optional_text(item, max_length)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def build_endpoint(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ZHIPU_BASE_URL 必须是有效 HTTP/HTTPS 地址")
    return normalized if normalized.endswith("/chat/completions") else f"{normalized}/chat/completions"


def extract_error_message(raw: bytes, fallback: str) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return clean_text(error["message"], 500)
    return clean_text(payload.get("message"), 500) if isinstance(payload, dict) and payload.get("message") else fallback


def extract_message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ApiError("智谱响应缺少 choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        text = "".join(x.get("text", "") for x in content if isinstance(x, dict)).strip()
        if text:
            return text
    raise ApiError("智谱响应没有可解析文本")


def extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            payload, _ = decoder.raw_decode(text[match.start():])
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            continue
    raise ExtractionError("模型返回内容中没有合法 JSON 对象")


class ZhipuVisionClient:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float, max_retries: int, max_tokens: int) -> None:
        self.api_key, self.endpoint, self.model = api_key.strip(), build_endpoint(base_url), model.strip()
        self.timeout, self.max_retries, self.max_tokens = timeout, max_retries, max_tokens

    def extract(self, image_path: Path, mime_type: str, target_company: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.api_key:
            raise ApiError("未配置 ZHIPU_API_KEY，请填写脚本目录下的 .env", fatal=True)
        if not self.model:
            raise ApiError("未配置 ZHIPU_MODEL，请填写 .env 或使用 --model", fatal=True)
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "text", "text": build_user_prompt(image_path.name, target_company or None)},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
                ]},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        request_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        last_error: ApiError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response, headers = self._send(request_body)
                raw = extract_json_object(extract_message_content(response))
                usage = response.get("usage") if isinstance(response.get("usage"), dict) else None
                return raw, {"request_id": response.get("id") or headers.get("x-request-id"), "usage": usage}
            except ApiError as exc:
                last_error = exc
                if exc.fatal or not exc.retriable or attempt >= self.max_retries:
                    raise
                delay = min(2 ** attempt, 8)
                print(f"请求失败，{delay}秒后重试（{attempt + 1}/{self.max_retries}）：{exc}", file=sys.stderr)
                time.sleep(delay)
        raise last_error or ApiError("智谱请求失败")

    def _send(self, body: bytes) -> tuple[dict[str, Any], Any]:
        request = urllib.request.Request(self.endpoint, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
            "Accept": "application/json", "User-Agent": "employee-certificate-image-extractor/1.0",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ApiError("智谱响应超过大小限制")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ApiError("智谱响应顶层不是 JSON 对象")
                return payload, response.headers
        except urllib.error.HTTPError as exc:
            fallback = f"智谱请求失败（HTTP {exc.code}）"
            message = extract_error_message(exc.read(64 * 1024), fallback)
            if exc.code in {400, 401, 403, 404}:
                raise ApiError(f"{fallback}：{message}", fatal=True) from exc
            if exc.code == 413:
                raise ApiError(f"图片请求体过大：{message}") from exc
            if exc.code in {408, 409, 425, 429} or 500 <= exc.code <= 599:
                raise ApiError(fallback, retriable=True) from exc
            raise ApiError(f"{fallback}：{message}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"智谱网络请求异常：{clean_text(getattr(exc, 'reason', None) or exc, 300)}", retriable=True) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("智谱响应不是有效 JSON") from exc


def normalize_date(value: Any, warnings: list[str], field: str) -> str | None:
    original = clean_optional_text(value, 64)
    if not original:
        return None
    match = DATE_PATTERN.fullmatch(original)
    if not match:
        append_warning(warnings, f"{field} 无法可靠归一化，已保留原文：{original}")
        return original
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        append_warning(warnings, f"{field} 不是有效日期，已保留原文：{original}")
        return original


def derive_status(expiry_date: str | None, expiring_days: int) -> str:
    if not expiry_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry_date):
        return "UNKNOWN"
    expiry = date.fromisoformat(expiry_date)
    if expiry < date.today():
        return "EXPIRED"
    if expiry <= date.today() + timedelta(days=expiring_days):
        return "EXPIRING"
    return "VALID"


def normalize_confidence(value: Any, warnings: list[str]) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        append_warning(warnings, "模型未提供有效 confidence，已按0处理")
        return 0.0
    if not math.isfinite(confidence):
        append_warning(warnings, "模型 confidence 不是有限数字，已按0处理")
        return 0.0
    if 1 < confidence <= 100:
        append_warning(warnings, "模型 confidence 使用百分制，已归一化到0-1")
        confidence /= 100
    if not 0 <= confidence <= 1:
        append_warning(warnings, "模型 confidence 超出0-1，已截断")
    return round(max(0.0, min(1.0, confidence)), 4)


def company_match(candidate: str | None, target: str) -> str:
    if not target:
        return "NOT_CHECKED"
    if not candidate:
        return "UNKNOWN"
    compact = lambda value: re.sub(r"[\s（）()·,，。]", "", value)
    actual, expected = compact(candidate), compact(target)
    if actual == expected or actual in expected or expected in actual:
        return "MATCHED"
    core = re.sub(r"(有限责任公司|有限公司|集团)$", "", expected)
    return "PROBABLE" if len(core) >= 4 and core in actual else "OTHER_COMPANY"


def normalize_extraction(raw: dict[str, Any], source: Path, target_company: str, review_threshold: float, expiring_days: int) -> dict[str, Any]:
    warnings = normalize_list(raw.get("warnings"), 50)
    raw_type = clean_optional_text(raw.get("document_type"), 64)
    type_aliases = {
        "employee_certificate": "employee_certificate",
        "employee certificate": "employee_certificate",
        "员工证书": "employee_certificate",
        "人员证书": "employee_certificate",
        "个人证书": "employee_certificate",
        "unknown": "unknown",
        "其他": "unknown",
        "其他文档": "unknown",
        "未知": "unknown",
    }
    normalized_raw_type = raw_type.casefold().strip() if raw_type else ""
    document_type = type_aliases.get(normalized_raw_type, "unknown")
    if normalized_raw_type not in type_aliases:
        append_warning(warnings, f"文档类型不受支持，已归为 unknown：{raw_type or '空值'}")
    person_raw = raw.get("person") if isinstance(raw.get("person"), dict) else {}
    person = {field: clean_optional_text(person_raw.get(field), 256 if field == "id_number" else MAX_TEXT_LENGTH) for field, _ in PERSON_FIELDS}
    for key in ("birth_date",):
        person[key] = normalize_date(person.get(key), warnings, f"person.{key}")
    certificates: list[dict[str, Any]] = []
    raw_certificates = raw.get("certificates")
    if isinstance(raw_certificates, list):
        for index, item in enumerate(raw_certificates[:50]):
            if not isinstance(item, dict):
                append_warning(warnings, f"certificates[{index}] 不是对象，已忽略")
                continue
            issue = normalize_date(item.get("issue_date"), warnings, f"certificates[{index}].issue_date")
            start = normalize_date(item.get("valid_from"), warnings, f"certificates[{index}].valid_from")
            expiry = normalize_date(item.get("expiry_date"), warnings, f"certificates[{index}].expiry_date")
            cert = {
                "cert_name": clean_optional_text(item.get("cert_name")),
                "cert_no": clean_optional_text(item.get("cert_no"), 256),
                "qualification_type": clean_optional_text(item.get("qualification_type")),
                "level": clean_optional_text(item.get("level")),
                "profession": clean_optional_text(item.get("profession")),
                "additional_professions": normalize_list(item.get("additional_professions"), 20),
                "issuing_authority": clean_optional_text(item.get("issuing_authority")),
                "issue_date": issue, "valid_from": start, "expiry_date": expiry,
                "registration_status": clean_optional_text(item.get("registration_status")),
                "scope": clean_optional_text(item.get("scope")),
                "status": derive_status(expiry, expiring_days),
            }
            if any(value for key, value in cert.items() if key != "status"):
                certificates.append(cert)
    if document_type != "employee_certificate" and certificates:
        append_warning(warnings, "非员工证书返回了证书字段，本地校验已忽略")
        certificates = []
    unclassified = []
    for item in (raw.get("unclassified_fields") if isinstance(raw.get("unclassified_fields"), list) else [])[:100]:
        if isinstance(item, dict):
            label, value = clean_optional_text(item.get("label"), 256), clean_optional_text(item.get("value"))
            if label and value:
                unclassified.append({"label": label, "value": value})
    evidence = []
    for item in (raw.get("evidence") if isinstance(raw.get("evidence"), list) else [])[:200]:
        if isinstance(item, dict):
            field, text = clean_optional_text(item.get("field"), 256), clean_optional_text(item.get("text"))
            if field and text:
                evidence.append({"field": field, "text": text})
    confidence = normalize_confidence(raw.get("confidence"), warnings)
    match = company_match(person.get("company_name"), target_company)
    if match in {"UNKNOWN", "OTHER_COMPANY"} and target_company:
        append_warning(warnings, "持证人所属单位未能与目标企业明确匹配")
    if document_type == "employee_certificate" and not certificates:
        append_warning(warnings, "识别为员工证书，但未抽取到有效证书条目")
    if document_type == "employee_certificate" and not person.get("name"):
        append_warning(warnings, "员工证书未识别到持证人姓名")
    review = raw.get("review_required") is not False
    if document_type == "unknown" or confidence < review_threshold or warnings or match in {"UNKNOWN", "OTHER_COMPANY"}:
        review = True
    return {
        "source_file": source.name, "source_path": str(source.resolve()),
        "document_type": document_type, "document_title": clean_optional_text(raw.get("document_title")),
        "raw_text": clean_optional_text(raw.get("raw_text"), MAX_TEXT_LENGTH),
        "person": person, "company_match": match, "certificates": certificates,
        "unclassified_fields": unclassified, "evidence": evidence, "warnings": warnings,
        "confidence": confidence, "review_required": review,
    }


def iter_input_files(root: Path, recursive: bool) -> Iterator[Path]:
    if not recursive:
        yield from sorted((p for p in root.iterdir() if p.is_file()), key=lambda p: p.name.casefold())
        return
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort(key=str.casefold)
        for name in sorted(files, key=str.casefold):
            path = Path(current) / name
            if not path.is_symlink():
                yield path


def source_identity(path: Path, stat: os.stat_result) -> tuple[str, str]:
    key = os.path.normcase(str(path.resolve()))
    fingerprint = hashlib.sha256(f"{key}\0{stat.st_size}\0{stat.st_mtime_ns}".encode()).hexdigest()
    return key, fingerprint


def safe_stem(value: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z_\-.\u4e00-\u9fff]+", "_", value).strip("._")
    return (clean or "image")[:100]


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": 1, "records": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"状态文件损坏，未覆盖：{path}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("records"), dict):
        raise ValueError(f"状态文件结构无效：{path}")
    return state


def relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def managed_path(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = (root / value).resolve()
    try:
        relative = candidate.relative_to(root.resolve())
    except ValueError:
        return None
    allowed = {config["directory"] for config in DOCUMENT_CONFIG.values()}
    return candidate if relative.parts and relative.parts[0] in allowed else None


def cache_valid(entry: Any, fingerprint: str, root: Path) -> bool:
    if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
        return False
    record, document = managed_path(root, entry.get("record_path")), managed_path(root, entry.get("document_path"))
    return bool(record and record.is_file() and document and document.is_file())


def markdown_value(value: Any) -> str:
    if isinstance(value, list):
        value = "、".join(map(str, value))
    return clean_text(value).replace("|", "\\|") if value not in (None, "") else "—"


def render_single(record: dict[str, Any]) -> str:
    label = DOCUMENT_CONFIG[record["document_type"]]["label"]
    lines = [
        f"# {markdown_value(record.get('document_title')) if record.get('document_title') else label}识别结果", "",
        "> 本文件由视觉模型抽取并经本地规则归一化，仅作为待审核候选事实。", "",
        f"- 源文件：`{record['source_path']}`", f"- 文档类型：`{record['document_type']}`",
        f"- 模型：`{record['processing']['model']}`", f"- 置信度：{record['confidence']:.4f}",
        f"- 目标企业匹配：`{record['company_match']}`",
        f"- 需要人工复核：{'是' if record['review_required'] else '否'}", "",
        "## 人员信息", "", "| 字段 | 候选值 |", "|---|---|",
    ]
    lines += [f"| {label} | {markdown_value(record['person'].get(field))} |" for field, label in PERSON_FIELDS]
    if record["certificates"]:
        lines += ["", "## 证书信息", "", "| 证书 | 编号 | 类型 | 等级 | 专业 | 增项 | 发证机关 | 发证日期 | 有效起始 | 有效截止 | 状态 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for cert in record["certificates"]:
            lines.append("| " + " | ".join(markdown_value(cert.get(k)) for k in ("cert_name", "cert_no", "qualification_type", "level", "profession", "additional_professions", "issuing_authority", "issue_date", "valid_from", "expiry_date", "status")) + " |")
    if record["unclassified_fields"]:
        lines += ["", "## 其他可见字段", "", "| 字段 | 原文值 |", "|---|---|"]
        lines += [f"| {markdown_value(x['label'])} | {markdown_value(x['value'])} |" for x in record["unclassified_fields"]]
    if record["evidence"]:
        lines += ["", "## 字段证据", "", "| 字段 | 图片原文 |", "|---|---|"]
        lines += [f"| `{markdown_value(x['field'])}` | {markdown_value(x['text'])} |" for x in record["evidence"]]
    lines += ["", "## OCR 全文", "", record.get("raw_text") or "—", "", "## 警告与复核项", ""]
    lines += [f"- {markdown_value(x)}" for x in record["warnings"]] or ["- 无自动校验告警；关键姓名、编号和日期仍建议人工抽查。"]
    return "\n".join(lines) + "\n"


def load_records(state: dict[str, Any], root: Path) -> dict[str, list[dict[str, Any]]]:
    grouped = {key: [] for key in DOCUMENT_CONFIG}
    for entry in state["records"].values():
        path = managed_path(root, entry.get("record_path") if isinstance(entry, dict) else None)
        if not path or not path.is_file():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        grouped[record.get("document_type") if record.get("document_type") in grouped else "unknown"].append(record)
    for records in grouped.values():
        records.sort(key=lambda x: str(x.get("source_path", "")).casefold())
    return grouped


def duplicate_certificate_warnings(records: list[dict[str, Any]]) -> list[str]:
    by_number: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for record in records:
        for cert in record.get("certificates", []):
            number = cert.get("cert_no")
            if number:
                by_number[re.sub(r"\s+", "", str(number)).upper()].append((record.get("person", {}).get("name") or "姓名未知", record["source_file"]))
    warnings = []
    for number, values in by_number.items():
        if len(values) > 1:
            names = sorted({name for name, _ in values})
            warnings.append(f"证书编号 `{number}` 出现 {len(values)} 次，姓名：{'、'.join(names)}；来源：{'、'.join(file for _, file in values)}")
    return warnings


def render_summary(document_type: str, records: list[dict[str, Any]], generated: str) -> str:
    config = DOCUMENT_CONFIG[document_type]
    lines = [f"# {config['label']}汇总", "", "> 以下为图片识别候选事实，尚未人工审核，不应直接写入业务数据库。", "", f"- 生成时间：{generated}", f"- 文档数量：{len(records)}", f"- 待复核数量：{sum(bool(x.get('review_required')) for x in records)}", ""]
    if document_type == "employee_certificate":
        people = {x.get("person", {}).get("name") for x in records if x.get("person", {}).get("name")}
        statuses = Counter(cert.get("status", "UNKNOWN") for x in records for cert in x.get("certificates", []))
        lines += [f"- 按姓名去重的持证人：{len(people)}", f"- 证书状态：有效 {statuses['VALID']}、临期 {statuses['EXPIRING']}、过期 {statuses['EXPIRED']}、未知 {statuses['UNKNOWN']}", "", "## 员工证书候选值", "", "| 来源图片 | 姓名 | 单位 | 企业匹配 | 证书 | 编号 | 专业 | 等级 | 有效截止 | 状态 | 置信度 |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for record in records:
            certificates = record.get("certificates") or [{}]
            for cert in certificates:
                lines.append("| " + " | ".join([
                    markdown_value(record.get("source_file")), markdown_value(record.get("person", {}).get("name")),
                    markdown_value(record.get("person", {}).get("company_name")), markdown_value(record.get("company_match")),
                    markdown_value(cert.get("cert_name")), markdown_value(cert.get("cert_no")), markdown_value(cert.get("profession")),
                    markdown_value(cert.get("level")), markdown_value(cert.get("expiry_date")), markdown_value(cert.get("status")),
                    f"{float(record.get('confidence', 0)):.4f}",
                ]) + " |")
        duplicates = duplicate_certificate_warnings(records)
        lines += ["", "## 重复证书编号", ""] + ([f"- {x}" for x in duplicates] if duplicates else ["- 未发现重复的非空证书编号。"])
    else:
        lines += ["## 未分类文档", "", "| 来源图片 | 标题 | 置信度 | 待复核 |", "|---|---|---|---|"]
        lines += [f"| {markdown_value(x.get('source_file'))} | {markdown_value(x.get('document_title'))} | {float(x.get('confidence', 0)):.4f} | {'是' if x.get('review_required') else '否'} |" for x in records]
    lines += ["", "## 待人工复核", ""]
    review = [x for x in records if x.get("review_required")]
    lines += [f"- `{markdown_value(x['source_file'])}`：{markdown_value('；'.join(x.get('warnings') or ['模型标记需要复核']))}" for x in review] or ["- 无强制复核项；关键姓名、编号和日期仍建议抽查。"]
    return "\n".join(lines) + "\n"


def write_summaries(state: dict[str, Any], root: Path) -> None:
    grouped, generated = load_records(state, root), datetime.now(timezone.utc).isoformat()
    for document_type, records in grouped.items():
        config, directory = DOCUMENT_CONFIG[document_type], root / DOCUMENT_CONFIG[document_type]["directory"]
        if not records and not directory.exists():
            continue
        payload = {"schema_version": 1, "document_type": document_type, "generated_at": generated, "record_count": len(records), "review_required_count": sum(bool(x.get("review_required")) for x in records), "records": records}
        atomic_write_json(directory / f"{config['summary_stem']}.json", payload)
        atomic_write_text(directory / f"{config['summary_stem']}.md", render_summary(document_type, records, generated))


def write_errors(errors: list[dict[str, Any]], root: Path) -> None:
    directory, generated = root / "处理失败结果", datetime.now(timezone.utc).isoformat()
    atomic_write_json(directory / "errors.json", {"schema_version": 1, "generated_at": generated, "error_count": len(errors), "errors": errors})
    lines = ["# 图片处理失败汇总", "", f"- 生成时间：{generated}", f"- 失败数量：{len(errors)}", "", "| 源文件 | 错误类型 | 错误信息 |", "|---|---|---|"]
    lines += [f"| {markdown_value(x.get('source_path'))} | {markdown_value(x.get('error_type'))} | {markdown_value(x.get('message'))} |" for x in errors] or ["| — | — | 本次运行没有处理失败项 |"]
    atomic_write_text(directory / "errors.md", "\n".join(lines) + "\n")


def process(args: argparse.Namespace) -> int:
    args.output_root.mkdir(parents=True, exist_ok=True)
    state_path, state = args.output_root / STATE_FILE_NAME, load_state(args.output_root / STATE_FILE_NAME)
    client = ZhipuVisionClient(os.environ.get("ZHIPU_API_KEY", ""), os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"), args.model, args.timeout, args.max_retries, args.max_tokens)
    max_bytes = int(args.max_image_mb * 1024 * 1024)
    stats, errors, fatal, consecutive = {"found": 0, "processed": 0, "skipped": 0, "failed": 0, "review_required": 0}, [], False, 0
    for image in iter_input_files(args.input_dir, args.recursive):
        suffix = image.suffix.casefold()
        if suffix not in SUPPORTED_MIME_TYPES:
            continue
        if args.limit is not None and stats["found"] >= args.limit:
            break
        stats["found"] += 1
        try:
            stat = image.stat()
            if stat.st_size <= 0:
                raise ExtractionError("图片文件为空")
            if stat.st_size > max_bytes:
                raise ExtractionError(f"图片大小 {stat.st_size} 字节超过限制 {max_bytes} 字节")
            key, fingerprint = source_identity(image, stat)
            previous_entry = state["records"].get(key)
            if not args.overwrite and cache_valid(previous_entry, fingerprint, args.output_root):
                stats["skipped"] += 1
                print(f"跳过未变化图片：{image}")
                continue
            raw, metadata = client.extract(image, SUPPORTED_MIME_TYPES[suffix], args.company_name)
            consecutive = 0
            record = normalize_extraction(raw, image, args.company_name, args.review_confidence, args.expiring_days)
            record["schema_version"] = 1
            record["processing"] = {"processed_at": datetime.now(timezone.utc).isoformat(), "model": client.model, "source_fingerprint": fingerprint, "source_size_bytes": stat.st_size, "source_modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), "api_request_id": metadata.get("request_id"), "usage": metadata.get("usage")}
            config = DOCUMENT_CONFIG[record["document_type"]]
            stem = f"{safe_stem(image.stem)}__{fingerprint[:10]}"
            record_path = args.output_root / config["directory"] / "records" / f"{stem}.json"
            document_path = args.output_root / config["directory"] / "documents" / f"{stem}.md"
            atomic_write_json(record_path, record)
            atomic_write_text(document_path, render_single(record))
            # 覆盖重跑时，如果文档类型发生变化，删除旧分类目录中的同一条结果，
            # 避免一张证书同时残留在“员工证书结果”和“其他文档结果”。
            if args.overwrite and isinstance(previous_entry, dict):
                for field, replacement in (("record_path", record_path), ("document_path", document_path)):
                    old_path = managed_path(args.output_root, previous_entry.get(field))
                    if old_path and old_path != replacement.resolve() and old_path.is_file():
                        old_path.unlink()
            state["records"][key] = {"source_path": str(image.resolve()), "fingerprint": fingerprint, "document_type": record["document_type"], "record_path": relative_path(record_path, args.output_root), "document_path": relative_path(document_path, args.output_root), "updated_at": record["processing"]["processed_at"]}
            atomic_write_json(state_path, state)
            stats["processed"] += 1
            stats["review_required"] += int(record["review_required"])
            print(f"完成：{image} -> {config['directory']}")
        except (ExtractionError, OSError, ValueError) as exc:
            stats["failed"] += 1
            errors.append({"source_file": image.name, "source_path": str(image.resolve()), "error_type": type(exc).__name__, "message": clean_text(exc, 1000), "occurred_at": datetime.now(timezone.utc).isoformat()})
            print(f"失败：{image}：{exc}", file=sys.stderr)
            if isinstance(exc, ApiError) and exc.fatal:
                fatal = True
                break
            if isinstance(exc, ApiError):
                consecutive += 1
                if consecutive >= 3:
                    print("连续3张图片API请求失败，停止批次以避免重复费用。", file=sys.stderr)
                    fatal = True
                    break
    write_summaries(state, args.output_root)
    write_errors(errors, args.output_root)
    print(f"处理结束：发现 {stats['found']} 张，完成 {stats['processed']} 张，跳过 {stats['skipped']} 张，失败 {stats['failed']} 张，本次新增待复核 {stats['review_required']} 张。")
    return 2 if fatal or stats["failed"] else 0


def main() -> int:
    try:
        load_local_env(SCRIPT_DIR / ".env")
        args = parse_args()
        validate_args(args)
        return process(args)
    except (ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已由用户中断，已完成图片的结果和状态不会丢失。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
