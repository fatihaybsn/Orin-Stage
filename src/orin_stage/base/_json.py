from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


class JsonRecordError(RuntimeError):
    """Raised when a persisted Orin Stage JSON record cannot be read or written."""


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def json_digest(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, value: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(value, indent=2, sort_keys=True) + "\n"

    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_json_object(path: Path, *, expected_schema_version: int = 1) -> Mapping[str, object]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise JsonRecordError(f"cannot load JSON record: {path}") from exc

    if not isinstance(data, dict) or data.get("schema_version") != expected_schema_version:
        raise JsonRecordError(f"unsupported or malformed JSON record: {path}")
    return data
