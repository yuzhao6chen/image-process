"""运行配置：只从环境变量和 CLI 读取，不包含任何凭据默认值。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit


SCRIPT_DIR = Path(__file__).resolve().parent


def load_local_env(path: Path) -> None:
    """读取简单 KEY=VALUE 文件，不覆盖进程已经设置的环境变量。"""

    if not path.is_file():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{path} 第 {line_number} 行缺少等号")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{path} 第 {line_number} 行变量名无效")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env_int(name: str, fallback: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"环境变量 {name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _env_float(name: str, fallback: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"环境变量 {name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _env_bool(name: str, fallback: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return fallback
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"环境变量 {name} 必须是 true/false")


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    ocr_model: str
    vision_model: str
    classifier_model: str
    text_model: str
    timeout_seconds: float
    max_retries: int
    max_tokens: int
    max_response_bytes: int
    ocr_chunk_pages: int
    ocr_max_file_bytes: int
    max_concurrency: int
    classifier_batch_pages: int
    domain_max_pages: int
    domain_max_chars: int
    render_dpi: int
    include_person_names: bool

    @classmethod
    def from_env(cls) -> "Settings":
        max_response_mb = _env_float("ZHIPU_MAX_RESPONSE_MB", 8.0, 0.25, 64.0)
        ocr_max_file_mb = _env_float("ZHIPU_OCR_MAX_FILE_MB", 49.0, 1.0, 50.0)
        return cls(
            api_key=os.environ.get("ZHIPU_API_KEY", "").strip(),
            base_url=os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").strip(),
            ocr_model=os.environ.get("ZHIPU_OCR_MODEL", "glm-ocr").strip(),
            vision_model=os.environ.get("ZHIPU_VISION_MODEL", "glm-5v-turbo").strip(),
            classifier_model=os.environ.get("ZHIPU_CLASSIFIER_MODEL", "glm-4.7-flash").strip(),
            text_model=os.environ.get("ZHIPU_TEXT_MODEL", "glm-5-turbo").strip(),
            timeout_seconds=_env_float("ZHIPU_TIMEOUT_SECONDS", 180.0, 1.0, 600.0),
            max_retries=_env_int("ZHIPU_MAX_RETRIES", 2, 0, 10),
            max_tokens=_env_int("ZHIPU_MAX_TOKENS", 8000, 256, 128000),
            max_response_bytes=int(max_response_mb * 1024 * 1024),
            ocr_chunk_pages=_env_int("ZHIPU_OCR_CHUNK_PAGES", 40, 1, 100),
            ocr_max_file_bytes=int(ocr_max_file_mb * 1_000_000),
            max_concurrency=_env_int("ZHIPU_MAX_CONCURRENCY", 2, 1, 8),
            classifier_batch_pages=_env_int("ZHIPU_CLASSIFIER_BATCH_PAGES", 12, 1, 30),
            domain_max_pages=_env_int("ZHIPU_DOMAIN_MAX_PAGES", 20, 1, 50),
            domain_max_chars=_env_int("ZHIPU_DOMAIN_MAX_CHARS", 90000, 5000, 180000),
            render_dpi=_env_int("PDF_RENDER_DPI", 180, 72, 400),
            include_person_names=_env_bool("OUTPUT_INCLUDE_PERSON_NAMES", True),
        )

    def with_cli_overrides(
        self,
        *,
        max_concurrency: int | None = None,
        include_person_names: bool | None = None,
    ) -> "Settings":
        updates: dict[str, object] = {}
        if max_concurrency is not None:
            if not 1 <= max_concurrency <= 8:
                raise ValueError("--max-concurrency 必须在 1 到 8 之间")
            updates["max_concurrency"] = max_concurrency
        if include_person_names is not None:
            updates["include_person_names"] = include_person_names
        return replace(self, **updates) if updates else self

    def validate_for_api(self) -> None:
        if not self.api_key:
            raise ValueError("未配置 ZHIPU_API_KEY")
        if not self.ocr_model:
            raise ValueError("未配置 ZHIPU_OCR_MODEL")
        if not self.text_model:
            raise ValueError("未配置 ZHIPU_TEXT_MODEL；请显式选择当前账号可用的文本模型")
        if not self.classifier_model:
            raise ValueError("未配置 ZHIPU_CLASSIFIER_MODEL")
        parsed = urlsplit(self.base_url.rstrip("/"))
        if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("ZHIPU_BASE_URL 格式无效，且不得包含凭据、查询参数或片段")
        is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
            raise ValueError("ZHIPU_BASE_URL 必须使用 HTTPS；仅本地回环调试允许 HTTP")


__all__ = ["SCRIPT_DIR", "Settings", "load_local_env"]
