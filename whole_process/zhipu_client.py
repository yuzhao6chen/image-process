"""智谱 GLM-OCR、视觉复核和结构化文本抽取 HTTP 客户端。"""

from __future__ import annotations

import base64
import json
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from config import Settings
from io_utils import stable_hash
from normalizers import clean_text


class ZhipuApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable
        self.fatal = fatal


def _is_quota_exhausted(message: str) -> bool:
    normalized = message.casefold()
    return any(marker in normalized for marker in (
        "余额不足", "无可用资源包", "insufficient balance", "insufficient quota", "quota exhausted",
    ))


def _response_error_message(raw_body: bytes, fallback: str) -> str:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if isinstance(error, dict):
        return clean_text(error.get("message"), 500) or fallback
    return clean_text(payload.get("message"), 500) or fallback


def _message_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ZhipuApiError("智谱响应缺少 choices")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        value = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict)
        ).strip()
        if value:
            return value
    raise ZhipuApiError("智谱响应没有可解析文本")


def extract_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ZhipuApiError("模型响应中没有合法 JSON 对象")


class ZhipuClient:
    def __init__(self, settings: Settings) -> None:
        settings.validate_for_api()
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/")

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(self.settings.max_retries + 1):
            # 每次重试重建 Request，避免复用已被底层处理过的请求对象。
            request = urllib.request.Request(
                f"{self.base_url}/{path.lstrip('/')}",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.settings.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                    raw = response.read(self.settings.max_response_bytes + 1)
                if len(raw) > self.settings.max_response_bytes:
                    raise ZhipuApiError("智谱响应超过 ZHIPU_MAX_RESPONSE_MB 限制")
                try:
                    decoded = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ZhipuApiError("智谱响应不是合法 UTF-8 JSON") from exc
                if not isinstance(decoded, dict):
                    raise ZhipuApiError("智谱响应顶层必须是 JSON 对象")
                response_error = decoded.get("error")
                if isinstance(response_error, dict):
                    message = clean_text(response_error.get("message"), 500) or "智谱返回业务错误"
                    raise ZhipuApiError(message, fatal=_is_quota_exhausted(message))
                if decoded.get("code") not in {None, 0, "0", 200, "200"} and decoded.get("msg"):
                    message = clean_text(decoded.get("msg"), 500) or "智谱返回业务错误"
                    raise ZhipuApiError(message, fatal=_is_quota_exhausted(message))
                return decoded
            except urllib.error.HTTPError as exc:
                raw_error = exc.read(min(self.settings.max_response_bytes, 65536))
                status = int(exc.code)
                message = _response_error_message(raw_error, f"智谱请求失败（HTTP {status}）")
                quota_exhausted = _is_quota_exhausted(message)
                error = ZhipuApiError(
                    message,
                    status_code=status,
                    retriable=not quota_exhausted and (status == 429 or 500 <= status < 600),
                    fatal=status in {401, 403} or quota_exhausted,
                )
                last_error = error
                if error.fatal or not error.retriable or attempt >= self.settings.max_retries:
                    raise error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = ZhipuApiError("智谱请求超时或网络异常", retriable=True)
                if attempt >= self.settings.max_retries:
                    raise last_error from exc
            if attempt < self.settings.max_retries:
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25)
                time.sleep(delay)
        if last_error:
            raise last_error
        raise ZhipuApiError("智谱请求失败")

    def layout_parse(self, file_path: Path, *, request_id: str | None = None) -> dict[str, Any]:
        raw = file_path.read_bytes()
        file_limit = 10_000_000 if file_path.suffix.lower() in {".png", ".jpg", ".jpeg"} else self.settings.ocr_max_file_bytes
        if len(raw) > file_limit:
            raise ZhipuApiError(
                f"OCR 文件块超过限制：{len(raw)} bytes",
                status_code=413,
            )
        request_id = request_id or f"ocr_{stable_hash(file_path.name, len(raw), length=24)}"
        mime_type = {
            ".pdf": "application/pdf",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }.get(file_path.suffix.lower())
        if not mime_type:
            raise ZhipuApiError(f"不支持的 OCR 本地文件扩展名：{file_path.suffix or '<none>'}")
        payload = {
            "model": self.settings.ocr_model,
            # MaaS 对本地字节要求 data URI；裸 Base64 会被部分网关判定为未知文件类型。
            "file": f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}",
            "return_crop_images": False,
            "need_layout_visualization": False,
            "request_id": request_id,
        }
        result = self._post_json("layout_parsing", payload)
        if not isinstance(result.get("md_results"), str) and not isinstance(result.get("layout_details"), list):
            raise ZhipuApiError("GLM-OCR 响应缺少 md_results 和 layout_details")
        return result

    def chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        request_id: str | None = None,
        max_tokens: int | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        selected_model = model or self.settings.text_model
        request_id = request_id or f"txt_{stable_hash(selected_model, system_prompt, user_prompt, length=24)}"
        payload = {
            "model": selected_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "response_format": {"type": "json_object"},
            "request_id": request_id,
        }
        response = self._post_json("chat/completions", payload)
        content = _message_content(response)
        try:
            data = extract_json_object(content)
        except ZhipuApiError:
            repair_request_id = f"{request_id}_repair"
            repair_payload = {
                **payload,
                "messages": [
                    *payload["messages"],
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "上一条响应不是合法 JSON。只修复 JSON 语法和结构，不得增加、删除或猜测事实；仅输出一个 JSON 对象。",
                    },
                ],
                "request_id": repair_request_id,
            }
            repair_response = self._post_json("chat/completions", repair_payload)
            repaired_content = _message_content(repair_response)
            data = extract_json_object(repaired_content)
            return {
                "data": data,
                "rawContent": repaired_content,
                "requestId": repair_response.get("request_id") or repair_request_id,
                "model": repair_response.get("model") or selected_model,
                "usage": {
                    "primary": response.get("usage") if isinstance(response.get("usage"), dict) else {},
                    "repair": repair_response.get("usage") if isinstance(repair_response.get("usage"), dict) else {},
                },
                "repaired": True,
            }
        return {
            "data": data,
            "rawContent": content,
            "requestId": response.get("request_id") or request_id,
            "model": response.get("model") or selected_model,
            "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
        }

    def vision_json(
        self,
        image_path: Path,
        *,
        prompt: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        raw = image_path.read_bytes()
        if len(raw) > 10_000_000:
            raise ZhipuApiError("视觉复核图片超过 10 MB")
        request_id = request_id or f"vis_{stable_hash(image_path.name, len(raw), prompt, length=24)}"
        mime_type = "image/jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        payload = {
            "model": self.settings.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": self.settings.max_tokens,
            "response_format": {"type": "json_object"},
            "request_id": request_id,
        }
        response = self._post_json("chat/completions", payload)
        content = _message_content(response)
        return {
            "data": extract_json_object(content),
            "rawContent": content,
            "requestId": response.get("request_id") or request_id,
            "model": response.get("model") or self.settings.vision_model,
            "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
        }


__all__ = ["ZhipuApiError", "ZhipuClient", "extract_json_object"]
