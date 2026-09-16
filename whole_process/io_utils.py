"""原子文件写入、哈希和断点状态。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(*parts: object, length: int | None = None) -> str:
    payload = "\x1f".join(json.dumps(part, ensure_ascii=False, sort_keys=True, default=str) for part in parts)
    value = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return value[:length] if length else value


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    # Path.write_text gained the newline argument in Python 3.10.  Keep the
    # project runnable on Python 3.9, which is still used by the PDF parser
    # environment declared for this repository.
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def read_json(path: Path, fallback: Any = None) -> Any:
    if not path.is_file():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def batched(values: list[Any], size: int) -> Iterable[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


class StateStore:
    def __init__(self, path: Path, *, source_hash: str, schema_version: str) -> None:
        self.path = path
        existing = read_json(path, {})
        if existing and (
            existing.get("sourceHash") != source_hash
            or existing.get("schemaVersion") != schema_version
        ):
            raise ValueError("状态文件与当前源文件或 Schema 版本不匹配；请使用新的输出目录或 --overwrite")
        self.data: dict[str, Any] = existing or {
            "sourceHash": source_hash,
            "schemaVersion": schema_version,
            "status": "PENDING",
            "stages": {},
            "createdAt": utc_now_iso(),
            "updatedAt": utc_now_iso(),
        }

    def stage(self, name: str, status: str, **metadata: Any) -> None:
        stages = self.data.setdefault("stages", {})
        stages[name] = {"status": status, "updatedAt": utc_now_iso(), **metadata}
        self.data["status"] = status
        self.data["updatedAt"] = utc_now_iso()
        atomic_write_json(self.path, self.data)

    def finish(self, status: str, **metadata: Any) -> None:
        self.data.update({"status": status, "updatedAt": utc_now_iso(), **metadata})
        atomic_write_json(self.path, self.data)


__all__ = [
    "StateStore",
    "atomic_write_json",
    "atomic_write_text",
    "batched",
    "read_json",
    "sha256_file",
    "stable_hash",
    "utc_now_iso",
]
