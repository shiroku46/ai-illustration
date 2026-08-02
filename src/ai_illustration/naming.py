"""Deterministic identifier and export-path helpers."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any

TOKEN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^v[0-9]{3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_identifier(prefix: str, value: Any, length: int = 16) -> str:
    if not TOKEN_RE.fullmatch(prefix):
        raise ValueError("prefix must be a lowercase ASCII token")
    return f"{prefix}-{hashlib.sha256(canonical_json(value)).hexdigest()[:length]}"


def ensure_token(value: str, field: str = "value") -> str:
    if not TOKEN_RE.fullmatch(value):
        raise ValueError(f"{field} must be lowercase ASCII with optional hyphens")
    return value


def safe_relative_path(value: str) -> PurePosixPath:
    if not value or "\\" in value or value.startswith("/"):
        raise ValueError("path must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path traversal or empty path component is forbidden")
    return path


def export_paths(*, character_id: str, crop: str, facing: str, pose: str, expression: str, version: str, sha256: str) -> tuple[str, str]:
    for field, value in (("character_id", character_id), ("crop", crop), ("facing", facing), ("pose", pose), ("expression", expression)):
        ensure_token(value, field)
    if not VERSION_RE.fullmatch(version):
        raise ValueError("version must use vNNN form")
    if not SHA256_RE.fullmatch(sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    stem = f"{character_id}__{crop}__{facing}__{pose}__{expression}__{version}__{sha256[:12]}"
    directory = PurePosixPath("exports", "v1", character_id, crop, facing, f"{pose}-{expression}")
    return str(directory / f"{stem}.png"), str(directory / f"{stem}.json")
