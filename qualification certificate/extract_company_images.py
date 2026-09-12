"""使用智谱视觉模型抽取企业资质与施工许可图片并生成 Markdown/JSON。"""

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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from prompt import SYSTEM_PROMPT, build_user_prompt


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE_NAME = ".image_extraction_state.json"
SUPPORTED_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
DOCUMENT_CONFIG = {
    "qualification_certificate": {
        "directory": "资质证书结果",
        "label": "资质证书",
        "summary_stem": "资质证书汇总",
    },
    "construction_permit": {
        "directory": "施工许可证结果",
        "label": "施工许可证",
        "summary_stem": "施工许可证汇总",
    },
    "unknown": {
        "directory": "其他文档结果",
        "label": "其他文档",
        "summary_stem": "其他文档汇总",
    },
}
COMPANY_FIELDS = (
    ("name", "企业名称"),
    ("unified_social_credit_code", "统一社会信用代码"),
    ("legal_representative", "法定代表人"),
    ("address", "地址"),
    ("registered_capital", "注册资本"),
    ("economic_type", "经济性质/企业类型"),
)
PERMIT_FIELDS = (
    ("project_name", "项目名称"),
    ("engineering_name", "工程名称"),
    ("permit_no", "施工许可证编号"),
    ("provincial_permit_no", "省级施工许可证编号"),
    ("project_code", "项目代码/编号"),
    ("project_manager", "项目经理"),
    ("supervision_engineer", "总监理工程师"),
    ("contract_amount_wan", "合同金额（万元）"),
    ("area_square_meters", "面积（平方米）"),
)
NULL_TEXT_VALUES = {"", "null", "none", "未知", "未识别", "未显示", "--", "—"}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TEXT_LENGTH = 4000


class ExtractionError(RuntimeError):
    """单张图片可记录并继续处理的错误。"""


class ApiError(ExtractionError):
    """智谱接口错误。"""

    def __init__(self, message: str, *, retriable: bool = False, fatal: bool = False) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.fatal = fatal


def load_local_env(path: Path) -> None:
    """读取简单 KEY=VALUE 文件，不覆盖进程中已存在的环境变量。"""

    if not path.is_file():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            print(f"警告：忽略 .env 第 {line_number} 行（缺少等号）", file=sys.stderr)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            print(f"警告：忽略 .env 第 {line_number} 行（变量名无效）", file=sys.stderr)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc


def env_float(name: str, fallback: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数字") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="调用智谱视觉模型识别企业资质/施工许可图片，按类型生成 Markdown 和 JSON。"
    )
    parser.add_argument("--input-dir", required=True, type=Path, help="待处理图片目录（必填）")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=SCRIPT_DIR,
        help="输出根目录，默认是脚本所在的 qualification certificate 目录",
    )
    parser.add_argument("--recursive", action="store_true", help="递归扫描输入目录的子目录")
    parser.add_argument("--model", default=os.environ.get("ZHIPU_MODEL", "").strip(), help="智谱视觉模型")
    parser.add_argument(
        "--timeout",
        type=float,
        default=env_float("ZHIPU_TIMEOUT_SECONDS", 180.0),
        help="单次请求超时秒数",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=env_int("ZHIPU_MAX_RETRIES", 2),
        help="超时、429 和 5xx 的最大重试次数",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=env_int("ZHIPU_MAX_TOKENS", 3000),
        help="模型最大输出 token 数",
    )
    parser.add_argument(
        "--max-image-mb",
        type=float,
        default=env_float("ZHIPU_MAX_IMAGE_MB", 20.0),
        help="单张图片允许的最大 MiB",
    )
    parser.add_argument("--overwrite", action="store_true", help="重新处理已有成功缓存")
    parser.add_argument("--limit", type=int, help="最多处理前 N 张受支持图片")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    args.input_dir = args.input_dir.expanduser().resolve()
    args.output_root = args.output_root.expanduser().resolve()
    if not args.input_dir.is_dir():
        raise ValueError(f"输入目录不存在或不是目录：{args.input_dir}")
    if args.timeout <= 0:
        raise ValueError("--timeout 必须大于 0")
    if args.max_retries < 0:
        raise ValueError("--max-retries 不能小于 0")
    if args.max_tokens <= 0:
        raise ValueError("--max-tokens 必须大于 0")
    if args.max_image_mb <= 0:
        raise ValueError("--max-image-mb 必须大于 0")
    if args.limit is not None and args.limit <= 0:
        raise ValueError("--limit 必须大于 0")


def build_endpoint(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ZHIPU_BASE_URL 必须是有效的 HTTP/HTTPS 地址")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def extract_api_error_message(raw_body: bytes, fallback: str) -> str:
    """只提取服务端结构化错误消息，避免把请求图片内容写入日志。"""

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return clean_text(error["message"], 500) or fallback
    if isinstance(payload.get("message"), str):
        return clean_text(payload["message"], 500) or fallback
    return fallback


class ZhipuVisionClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float,
        max_retries: int,
        max_tokens: int,
    ) -> None:
        self.api_key = api_key.strip()
        self.endpoint = build_endpoint(base_url)
        self.model = model.strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_tokens = max_tokens

    def extract(self, image_path: Path, mime_type: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.api_key:
            raise ApiError("未配置 ZHIPU_API_KEY，请先填写脚本目录下的 .env", fatal=True)
        if not self.model:
            raise ApiError("未配置 ZHIPU_MODEL，请填写 .env 或使用 --model", fatal=True)

        image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": build_user_prompt(image_path.name)},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                        },
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        request_body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        del image_base64
        del payload

        last_error: ApiError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response_payload, response_headers = self._send(request_body)
                content = extract_message_content(response_payload)
                extracted = extract_json_object(content)
                api_metadata = {
                    "request_id": clean_optional_text(
                        response_payload.get("id") or response_headers.get("x-request-id"), 256
                    ),
                    "usage": normalize_usage(response_payload.get("usage")),
                }
                return extracted, api_metadata
            except ApiError as exc:
                last_error = exc
                if exc.fatal or not exc.retriable or attempt >= self.max_retries:
                    raise
                delay = min(2**attempt, 8)
                print(
                    f"请求失败，将在 {delay} 秒后重试（{attempt + 1}/{self.max_retries}）：{exc}",
                    file=sys.stderr,
                )
                time.sleep(delay)
        raise last_error or ApiError("智谱请求失败")

    def _send(self, request_body: bytes) -> tuple[dict[str, Any], Any]:
        request = urllib.request.Request(
            self.endpoint,
            data=request_body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "company-qualification-image-extractor/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_body) > MAX_RESPONSE_BYTES:
                    raise ApiError("智谱响应超过允许的大小限制")
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ApiError("智谱响应不是有效 JSON") from exc
                if not isinstance(payload, dict):
                    raise ApiError("智谱响应顶层不是 JSON 对象")
                return payload, response.headers
        except urllib.error.HTTPError as exc:
            raw_error = exc.read(64 * 1024)
            fallback = f"智谱请求失败（HTTP {exc.code}）"
            message = extract_api_error_message(raw_error, fallback)
            if exc.code in {401, 403}:
                raise ApiError(f"智谱认证失败（HTTP {exc.code}）：{message}", fatal=True) from exc
            if exc.code in {400, 404}:
                raise ApiError(f"智谱请求配置错误（HTTP {exc.code}）：{message}", fatal=True) from exc
            if exc.code == 413:
                raise ApiError(f"图片请求体过大（HTTP 413）：{message}") from exc
            if exc.code in {408, 409, 425, 429} or 500 <= exc.code <= 599:
                raise ApiError(fallback, retriable=True) from exc
            raise ApiError(f"{fallback}：{message}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            reason_text = clean_text(str(reason or exc), 300)
            raise ApiError(f"智谱网络请求异常：{reason_text}", retriable=True) from exc


def normalize_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = value.get(key)
        if isinstance(item, int) and item >= 0:
            result[key] = item
    return result or None


def extract_message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ApiError("智谱响应缺少 choices")
    first = choices[0]
    if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
        raise ApiError("智谱响应缺少 message")
    content = first["message"].get("content")
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        combined = "".join(parts).strip()
        if combined:
            return combined
    raise ApiError("智谱响应未包含可解析的文本内容")


def extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
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
            payload, _ = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ExtractionError("模型返回内容中没有合法 JSON 对象")


def clean_text(value: Any, max_length: int = MAX_TEXT_LENGTH) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:max_length]


def clean_optional_text(value: Any, max_length: int = MAX_TEXT_LENGTH) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = clean_text(value, max_length)
    if text.casefold() in NULL_TEXT_VALUES:
        return None
    return text or None


def append_warning(warnings: list[str], message: str) -> None:
    normalized = clean_text(message, 500)
    if normalized and normalized not in warnings:
        warnings.append(normalized)


def normalize_string_list(value: Any, *, limit: int, max_length: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        normalized = clean_optional_text(item, max_length)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


DATE_PATTERN = re.compile(r"^(\d{4})\s*(?:年|[-./])\s*(\d{1,2})\s*(?:月|[-./])\s*(\d{1,2})\s*日?$")


def normalize_date(value: Any, warnings: list[str], field_path: str) -> str | None:
    original = clean_optional_text(value, 64)
    if original is None:
        return None
    match = DATE_PATTERN.fullmatch(original)
    if not match:
        append_warning(warnings, f"{field_path} 无法可靠归一化，已保留原文：{original}")
        return original
    try:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        append_warning(warnings, f"{field_path} 不是有效日期，已保留原文：{original}")
        return original
    return parsed.isoformat()


def derive_certificate_status(expiry_date: str | None) -> str:
    if not expiry_date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry_date):
        return "UNKNOWN"
    try:
        expiry = date.fromisoformat(expiry_date)
    except ValueError:
        return "UNKNOWN"
    return "VALID" if expiry >= date.today() else "EXPIRED"


def normalize_confidence(value: Any, warnings: list[str]) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        append_warning(warnings, "模型未提供有效 confidence，已按 0 处理")
        return 0.0
    if not math.isfinite(confidence):
        append_warning(warnings, "模型 confidence 不是有限数字，已按 0 处理")
        return 0.0
    if 1 < confidence <= 100:
        append_warning(warnings, "模型 confidence 使用了百分制，已归一化到 0-1")
        confidence /= 100
    if confidence < 0 or confidence > 1:
        append_warning(warnings, "模型 confidence 超出 0-1，已截断")
    return round(min(1.0, max(0.0, confidence)), 4)


def normalize_extraction(raw: dict[str, Any], source_path: Path) -> dict[str, Any]:
    warnings = normalize_string_list(raw.get("warnings"), limit=50)
    raw_document_type = clean_optional_text(raw.get("document_type"), 64)
    document_type = raw_document_type if raw_document_type in DOCUMENT_CONFIG else "unknown"
    if raw_document_type not in DOCUMENT_CONFIG:
        append_warning(warnings, f"文档类型不受支持，已归类为 unknown：{raw_document_type or '空值'}")

    company_raw = raw.get("company") if isinstance(raw.get("company"), dict) else {}
    company = {
        field: clean_optional_text(company_raw.get(field))
        for field, _ in COMPANY_FIELDS
    }

    qualifications: list[dict[str, Any]] = []
    raw_qualifications = raw.get("qualifications")
    if isinstance(raw_qualifications, list):
        for index, item in enumerate(raw_qualifications[:50]):
            if not isinstance(item, dict):
                append_warning(warnings, f"qualifications[{index}] 不是对象，已忽略")
                continue
            issue_date = normalize_date(item.get("issue_date"), warnings, f"qualifications[{index}].issue_date")
            expiry_date = normalize_date(
                item.get("expiry_date"), warnings, f"qualifications[{index}].expiry_date"
            )
            qualification = {
                "cert_name": clean_optional_text(item.get("cert_name")),
                "cert_level": clean_optional_text(item.get("cert_level")),
                "cert_no": clean_optional_text(item.get("cert_no"), 256),
                "issue_date": issue_date,
                "expiry_date": expiry_date,
                "issuing_authority": clean_optional_text(item.get("issuing_authority")),
                "scope": clean_optional_text(item.get("scope")),
                "status": derive_certificate_status(expiry_date),
            }
            if any(value for key, value in qualification.items() if key != "status"):
                qualifications.append(qualification)
    if document_type != "qualification_certificate" and qualifications:
        append_warning(warnings, "非资质证书返回了资质字段，本地校验已忽略这些字段")
        qualifications = []

    permit_raw = raw.get("project_permit") if isinstance(raw.get("project_permit"), dict) else {}
    project_permit = {
        field: clean_optional_text(permit_raw.get(field))
        for field, _ in PERMIT_FIELDS
    }
    if document_type != "construction_permit" and any(project_permit.values()):
        append_warning(warnings, "非施工许可证返回了许可字段，本地校验已忽略这些字段")
        project_permit = {field: None for field, _ in PERMIT_FIELDS}

    unclassified_fields: list[dict[str, str]] = []
    raw_unclassified = raw.get("unclassified_fields")
    if isinstance(raw_unclassified, list):
        for item in raw_unclassified[:100]:
            if not isinstance(item, dict):
                continue
            label = clean_optional_text(item.get("label"), 256)
            value = clean_optional_text(item.get("value"))
            if label and value:
                unclassified_fields.append({"label": label, "value": value})

    evidence: list[dict[str, str]] = []
    raw_evidence = raw.get("evidence")
    if isinstance(raw_evidence, list):
        for item in raw_evidence[:200]:
            if not isinstance(item, dict):
                continue
            field = clean_optional_text(item.get("field"), 256)
            text = clean_optional_text(item.get("text"))
            if field and text:
                evidence.append({"field": field, "text": text})

    confidence = normalize_confidence(raw.get("confidence"), warnings)
    review_required = raw.get("review_required") is not False
    if document_type == "unknown" or confidence < 0.8 or warnings:
        review_required = True
    if document_type == "qualification_certificate" and not qualifications:
        append_warning(warnings, "识别为资质证书，但未抽取到有效资质条目")
        review_required = True
    if document_type == "construction_permit" and not any(project_permit.values()):
        append_warning(warnings, "识别为施工许可证，但未抽取到许可字段")
        review_required = True

    return {
        "source_file": source_path.name,
        "source_path": str(source_path.resolve()),
        "document_type": document_type,
        "document_title": clean_optional_text(raw.get("document_title")),
        "company": company,
        "qualifications": qualifications,
        "project_permit": project_permit,
        "unclassified_fields": unclassified_fields,
        "evidence": evidence,
        "warnings": warnings,
        "confidence": confidence,
        "review_required": review_required,
    }


def iter_input_files(input_dir: Path, recursive: bool) -> Iterator[Path]:
    if not recursive:
        with os.scandir(input_dir) as entries:
            files = sorted(
                (Path(entry.path) for entry in entries if entry.is_file(follow_symlinks=False)),
                key=lambda item: item.name.casefold(),
            )
        yield from files
        return

    for root, directories, files in os.walk(input_dir, followlinks=False):
        directories.sort(key=str.casefold)
        files.sort(key=str.casefold)
        root_path = Path(root)
        for filename in files:
            path = root_path / filename
            if not path.is_symlink():
                yield path


def source_identity(path: Path, file_stat: os.stat_result) -> tuple[str, str]:
    resolved = path.resolve()
    source_key = os.path.normcase(str(resolved))
    identity = f"{source_key}\0{file_stat.st_size}\0{file_stat.st_mtime_ns}"
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return source_key, fingerprint


def safe_output_stem(source_stem: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_\-.\u4e00-\u9fff]+", "_", source_stem).strip("._")
    return (normalized or "image")[:100]


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema_version": 1, "records": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"状态文件损坏，未覆盖原文件：{path}") from exc
    if not isinstance(state, dict) or not isinstance(state.get("records"), dict):
        raise ValueError(f"状态文件结构无效，未覆盖原文件：{path}")
    return state


def relative_output_path(path: Path, output_root: Path) -> str:
    return path.resolve().relative_to(output_root.resolve()).as_posix()


def resolve_managed_path(output_root: Path, relative_path: Any) -> Path | None:
    if not isinstance(relative_path, str) or not relative_path:
        return None
    candidate = (output_root / Path(relative_path)).resolve()
    try:
        relative = candidate.relative_to(output_root.resolve())
    except ValueError:
        return None
    if not relative.parts or relative.parts[0] not in {item["directory"] for item in DOCUMENT_CONFIG.values()}:
        return None
    return candidate


def cached_entry_is_valid(entry: Any, fingerprint: str, output_root: Path) -> bool:
    if not isinstance(entry, dict) or entry.get("fingerprint") != fingerprint:
        return False
    record_path = resolve_managed_path(output_root, entry.get("record_path"))
    document_path = resolve_managed_path(output_root, entry.get("document_path"))
    return bool(record_path and record_path.is_file() and document_path and document_path.is_file())


def remove_old_generated_file(output_root: Path, relative_path: Any, keep_path: Path) -> None:
    candidate = resolve_managed_path(output_root, relative_path)
    if candidate is None or candidate == keep_path.resolve() or not candidate.is_file():
        return
    if candidate.suffix.casefold() not in {".json", ".md"} or candidate.parent.name not in {"records", "documents"}:
        return
    candidate.unlink()


def markdown_value(value: Any) -> str:
    if value is None or value == "":
        return "—"
    text = clean_text(value).replace("|", "\\|")
    return text or "—"


def render_single_markdown(record: dict[str, Any]) -> str:
    config = DOCUMENT_CONFIG[record["document_type"]]
    lines = [
        f"# {markdown_value(record.get('document_title')) if record.get('document_title') else config['label']}识别结果",
        "",
        "> 本文件由视觉模型抽取并经本地规则归一化，仅作为待审核候选事实。",
        "",
        f"- 源文件：`{record['source_path']}`",
        f"- 文档类型：`{record['document_type']}`",
        f"- 模型：`{record['processing']['model']}`",
        f"- 置信度：{record['confidence']:.4f}",
        f"- 需要人工复核：{'是' if record['review_required'] else '否'}",
        f"- 处理时间：{record['processing']['processed_at']}",
        "",
        "## 企业信息候选值",
        "",
        "| 字段 | 候选值 |",
        "|---|---|",
    ]
    for field, label in COMPANY_FIELDS:
        lines.append(f"| {label} | {markdown_value(record['company'].get(field))} |")

    if record["qualifications"]:
        lines.extend(
            [
                "",
                "## 资质信息",
                "",
                "| 证书/资质名称 | 等级 | 编号 | 发证日期 | 有效期 | 发证机关 | 状态 | 范围 |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for item in record["qualifications"]:
            lines.append(
                "| {cert_name} | {cert_level} | {cert_no} | {issue_date} | {expiry_date} | "
                "{issuing_authority} | {status} | {scope} |".format(
                    **{key: markdown_value(value) for key, value in item.items()}
                )
            )

    if any(record["project_permit"].values()):
        lines.extend(["", "## 施工许可信息", "", "| 字段 | 候选值 |", "|---|---|"])
        for field, label in PERMIT_FIELDS:
            lines.append(f"| {label} | {markdown_value(record['project_permit'].get(field))} |")

    if record["unclassified_fields"]:
        lines.extend(["", "## 其他可见字段", "", "| 字段 | 原文值 |", "|---|---|"])
        for item in record["unclassified_fields"]:
            lines.append(f"| {markdown_value(item['label'])} | {markdown_value(item['value'])} |")

    if record["evidence"]:
        lines.extend(["", "## 字段证据", "", "| 字段 | 图片原文 |", "|---|---|"])
        for item in record["evidence"]:
            lines.append(f"| `{markdown_value(item['field'])}` | {markdown_value(item['text'])} |")

    lines.extend(["", "## 警告与复核项", ""])
    if record["warnings"]:
        lines.extend(f"- {markdown_value(item)}" for item in record["warnings"])
    else:
        lines.append("- 无模型或本地校验警告；仍建议核对关键编号与日期。")
    return "\n".join(lines) + "\n"


def collect_company_conflicts(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for field, label in COMPANY_FIELDS:
        sources_by_value: dict[str, list[str]] = {}
        for record in records:
            value = record.get("company", {}).get(field)
            if value:
                sources_by_value.setdefault(str(value), []).append(record["source_file"])
        if len(sources_by_value) > 1:
            conflicts.append({"field": label, "values": sources_by_value})
    return conflicts


def render_summary_markdown(
    document_type: str, records: list[dict[str, Any]], generated_at: str
) -> str:
    config = DOCUMENT_CONFIG[document_type]
    lines = [
        f"# {config['label']}汇总",
        "",
        "> 以下内容是图片识别得到的候选事实，尚未经过人工审核，不应直接写入业务数据库。",
        "",
        f"- 生成时间：{generated_at}",
        f"- 文档数量：{len(records)}",
        f"- 待复核数量：{sum(1 for item in records if item.get('review_required'))}",
        "",
        "## 企业信息候选值",
        "",
        "| 来源图片 | 企业名称 | 统一社会信用代码 | 法定代表人 | 地址 | 注册资本 | 企业类型 |",
        "|---|---|---|---|---|---|---|",
    ]
    for record in records:
        company = record.get("company", {})
        lines.append(
            "| {source} | {name} | {credit} | {legal} | {address} | {capital} | {economic} |".format(
                source=markdown_value(record.get("source_file")),
                name=markdown_value(company.get("name")),
                credit=markdown_value(company.get("unified_social_credit_code")),
                legal=markdown_value(company.get("legal_representative")),
                address=markdown_value(company.get("address")),
                capital=markdown_value(company.get("registered_capital")),
                economic=markdown_value(company.get("economic_type")),
            )
        )

    if document_type == "qualification_certificate":
        lines.extend(
            [
                "",
                "## 资质证书候选值",
                "",
                "| 来源图片 | 企业 | 证书/资质名称 | 等级 | 编号 | 发证日期 | 有效期 | 发证机关 | 状态 | 置信度 |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for record in records:
            qualifications = record.get("qualifications") or [{}]
            for item in qualifications:
                lines.append(
                    "| {source} | {company} | {name} | {level} | {number} | {issue} | {expiry} | "
                    "{authority} | {status} | {confidence:.4f} |".format(
                        source=markdown_value(record.get("source_file")),
                        company=markdown_value(record.get("company", {}).get("name")),
                        name=markdown_value(item.get("cert_name")),
                        level=markdown_value(item.get("cert_level")),
                        number=markdown_value(item.get("cert_no")),
                        issue=markdown_value(item.get("issue_date")),
                        expiry=markdown_value(item.get("expiry_date")),
                        authority=markdown_value(item.get("issuing_authority")),
                        status=markdown_value(item.get("status")),
                        confidence=float(record.get("confidence", 0)),
                    )
                )
    elif document_type == "construction_permit":
        lines.extend(
            [
                "",
                "## 施工许可候选值",
                "",
                "| 来源图片 | 项目名称 | 工程名称 | 许可证编号 | 省级编号 | 项目代码 | 项目经理 | 总监理工程师 | 合同金额（万元） | 面积（平方米） | 置信度 |",
                "|---|---|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for record in records:
            permit = record.get("project_permit", {})
            lines.append(
                "| {source} | {project} | {engineering} | {permit_no} | {provincial} | {code} | "
                "{manager} | {engineer} | {amount} | {area} | {confidence:.4f} |".format(
                    source=markdown_value(record.get("source_file")),
                    project=markdown_value(permit.get("project_name")),
                    engineering=markdown_value(permit.get("engineering_name")),
                    permit_no=markdown_value(permit.get("permit_no")),
                    provincial=markdown_value(permit.get("provincial_permit_no")),
                    code=markdown_value(permit.get("project_code")),
                    manager=markdown_value(permit.get("project_manager")),
                    engineer=markdown_value(permit.get("supervision_engineer")),
                    amount=markdown_value(permit.get("contract_amount_wan")),
                    area=markdown_value(permit.get("area_square_meters")),
                    confidence=float(record.get("confidence", 0)),
                )
            )
    else:
        lines.extend(
            [
                "",
                "## 未分类文档",
                "",
                "| 来源图片 | 文档标题 | 置信度 | 待复核 |",
                "|---|---|---|---|",
            ]
        )
        for record in records:
            lines.append(
                f"| {markdown_value(record.get('source_file'))} | {markdown_value(record.get('document_title'))} | "
                f"{float(record.get('confidence', 0)):.4f} | {'是' if record.get('review_required') else '否'} |"
            )

    conflicts = collect_company_conflicts(records)
    lines.extend(["", "## 字段冲突", ""])
    if conflicts:
        for conflict in conflicts:
            lines.append(f"### {conflict['field']}")
            lines.append("")
            for value, sources in conflict["values"].items():
                lines.append(f"- `{markdown_value(value)}`：{', '.join(markdown_value(item) for item in sources)}")
            lines.append("")
    else:
        lines.append("- 当前同类记录中未发现非空企业字段冲突。")

    lines.extend(["", "## 待人工复核", ""])
    review_records = [record for record in records if record.get("review_required")]
    if review_records:
        for record in review_records:
            warnings = "；".join(record.get("warnings") or ["模型标记需要复核"])
            lines.append(f"- `{markdown_value(record['source_file'])}`：{markdown_value(warnings)}")
    else:
        lines.append("- 无强制复核项；关键编号、企业名称和日期仍建议人工抽查。")
    return "\n".join(lines) + "\n"


def load_all_records(state: dict[str, Any], output_root: Path) -> dict[str, list[dict[str, Any]]]:
    grouped = {document_type: [] for document_type in DOCUMENT_CONFIG}
    for entry in state["records"].values():
        if not isinstance(entry, dict):
            continue
        record_path = resolve_managed_path(output_root, entry.get("record_path"))
        if record_path is None or not record_path.is_file():
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"警告：跳过损坏的记录文件：{record_path}", file=sys.stderr)
            continue
        if not isinstance(record, dict):
            continue
        document_type = record.get("document_type")
        if document_type not in DOCUMENT_CONFIG:
            document_type = "unknown"
        grouped[document_type].append(record)
    for records in grouped.values():
        records.sort(key=lambda item: str(item.get("source_path", "")).casefold())
    return grouped


def write_summaries(state: dict[str, Any], output_root: Path) -> None:
    grouped = load_all_records(state, output_root)
    generated_at = datetime.now(timezone.utc).isoformat()
    for document_type, records in grouped.items():
        config = DOCUMENT_CONFIG[document_type]
        result_dir = output_root / config["directory"]
        if not records and not result_dir.exists():
            continue
        summary_payload = {
            "schema_version": 1,
            "document_type": document_type,
            "generated_at": generated_at,
            "record_count": len(records),
            "review_required_count": sum(1 for item in records if item.get("review_required")),
            "records": records,
        }
        atomic_write_json(result_dir / f"{config['summary_stem']}.json", summary_payload)
        atomic_write_text(
            result_dir / f"{config['summary_stem']}.md",
            render_summary_markdown(document_type, records, generated_at),
        )


def write_errors(errors: list[dict[str, Any]], output_root: Path) -> None:
    result_dir = output_root / "处理失败结果"
    generated_at = datetime.now(timezone.utc).isoformat()
    atomic_write_json(
        result_dir / "errors.json",
        {"schema_version": 1, "generated_at": generated_at, "error_count": len(errors), "errors": errors},
    )
    lines = [
        "# 图片处理失败汇总",
        "",
        f"- 生成时间：{generated_at}",
        f"- 失败数量：{len(errors)}",
        "",
        "| 源文件 | 错误类型 | 错误信息 |",
        "|---|---|---|",
    ]
    for item in errors:
        lines.append(
            f"| {markdown_value(item.get('source_path'))} | {markdown_value(item.get('error_type'))} | "
            f"{markdown_value(item.get('message'))} |"
        )
    if not errors:
        lines.append("| — | — | 本次运行没有处理失败项 |")
    atomic_write_text(result_dir / "errors.md", "\n".join(lines) + "\n")


def make_error_record(path: Path, error: Exception) -> dict[str, Any]:
    return {
        "source_file": path.name,
        "source_path": str(path.resolve()),
        "error_type": type(error).__name__,
        "message": clean_text(str(error), 1000),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }


def process(args: argparse.Namespace) -> int:
    output_root: Path = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)
    state_path = output_root / STATE_FILE_NAME
    state = load_state(state_path)
    api_key = os.environ.get("ZHIPU_API_KEY", "")
    base_url = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    client = ZhipuVisionClient(
        api_key=api_key,
        base_url=base_url,
        model=args.model,
        timeout=args.timeout,
        max_retries=args.max_retries,
        max_tokens=args.max_tokens,
    )
    max_image_bytes = int(args.max_image_mb * 1024 * 1024)
    stats = {"found": 0, "processed": 0, "skipped": 0, "failed": 0, "review_required": 0}
    errors: list[dict[str, Any]] = []
    fatal_error = False
    consecutive_api_failures = 0

    for image_path in iter_input_files(args.input_dir, args.recursive):
        suffix = image_path.suffix.casefold()
        if suffix not in SUPPORTED_MIME_TYPES:
            continue
        if args.limit is not None and stats["found"] >= args.limit:
            break
        stats["found"] += 1
        try:
            file_stat = image_path.stat()
            if file_stat.st_size <= 0:
                raise ExtractionError("图片文件为空")
            if file_stat.st_size > max_image_bytes:
                raise ExtractionError(
                    f"图片大小 {file_stat.st_size} 字节超过限制 {max_image_bytes} 字节"
                )
            source_key, fingerprint = source_identity(image_path, file_stat)
            old_entry = state["records"].get(source_key)
            if not args.overwrite and cached_entry_is_valid(old_entry, fingerprint, output_root):
                stats["skipped"] += 1
                print(f"跳过未变化图片：{image_path}")
                continue

            raw_extraction, api_metadata = client.extract(image_path, SUPPORTED_MIME_TYPES[suffix])
            consecutive_api_failures = 0
            record = normalize_extraction(raw_extraction, image_path)
            record["schema_version"] = 1
            record["processing"] = {
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "model": client.model,
                "source_fingerprint": fingerprint,
                "source_size_bytes": file_stat.st_size,
                "source_modified_at": datetime.fromtimestamp(file_stat.st_mtime, timezone.utc).isoformat(),
                "api_request_id": api_metadata.get("request_id"),
                "usage": api_metadata.get("usage"),
            }

            config = DOCUMENT_CONFIG[record["document_type"]]
            result_dir = output_root / config["directory"]
            output_name = f"{safe_output_stem(image_path.stem)}__{fingerprint[:10]}"
            record_path = result_dir / "records" / f"{output_name}.json"
            document_path = result_dir / "documents" / f"{output_name}.md"
            atomic_write_json(record_path, record)
            atomic_write_text(document_path, render_single_markdown(record))

            state["records"][source_key] = {
                "source_path": str(image_path.resolve()),
                "fingerprint": fingerprint,
                "document_type": record["document_type"],
                "record_path": relative_output_path(record_path, output_root),
                "document_path": relative_output_path(document_path, output_root),
                "updated_at": record["processing"]["processed_at"],
            }
            atomic_write_json(state_path, state)
            if isinstance(old_entry, dict):
                for old_path, keep_path in (
                    (old_entry.get("record_path"), record_path),
                    (old_entry.get("document_path"), document_path),
                ):
                    try:
                        remove_old_generated_file(output_root, old_path, keep_path)
                    except OSError as cleanup_error:
                        print(f"警告：旧结果清理失败，不影响新结果：{cleanup_error}", file=sys.stderr)
            stats["processed"] += 1
            if record["review_required"]:
                stats["review_required"] += 1
            print(f"完成：{image_path} -> {config['directory']}")
        except (ExtractionError, OSError, ValueError) as exc:
            # 只把预期的输入/API/文件错误作为单图失败；编程错误直接终止，避免重复消耗 API。
            stats["failed"] += 1
            errors.append(make_error_record(image_path, exc))
            print(f"失败：{image_path}：{exc}", file=sys.stderr)
            if isinstance(exc, ApiError) and exc.fatal:
                fatal_error = True
                break
            if isinstance(exc, ApiError):
                consecutive_api_failures += 1
                if consecutive_api_failures >= 3:
                    print("连续 3 张图片的 API 请求失败，已停止批次以避免重复请求和费用。", file=sys.stderr)
                    fatal_error = True
                    break

    write_summaries(state, output_root)
    write_errors(errors, output_root)
    print(
        "处理结束："
        f"发现 {stats['found']} 张，完成 {stats['processed']} 张，跳过 {stats['skipped']} 张，"
        f"失败 {stats['failed']} 张，本次新增待复核 {stats['review_required']} 张。"
    )
    return 2 if fatal_error or stats["failed"] else 0


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
        print("已由用户中断。已完成图片的单图结果和状态不会丢失。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
