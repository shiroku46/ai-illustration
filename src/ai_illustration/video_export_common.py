"""Shared bounded validation primitives for local video export."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .naming import canonical_json, safe_relative_path

VIDEO_EXPORT_PLAN = "video-export-plan.json"
VIDEO_EXPORT_MANIFEST = "video-export-manifest.json"
VIDEO_OUTPUT = "video.mp4"

PROFILE_FAMILY = "mp4-h264-aac-v1"
PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast",
    "medium", "slow", "slower", "veryslow",
)
MAX_FFMPEG_BYTES = 512 * 1024 * 1024
MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 2 * 1024 * 1024
MAX_FRAME_COUNT = 100_000
MAX_TIMEOUT_SECONDS = 24 * 60 * 60

@dataclass
class VideoExportError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path, maximum: int, field: str) -> tuple[str, int]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise VideoExportError("FILE_STAT", str(exc), field) from exc
    if size <= 0:
        raise VideoExportError("FILE_EMPTY", f"{field} must not be empty", field)
    if size > maximum:
        raise VideoExportError("FILE_TOO_LARGE", f"{field} exceeds {maximum} bytes", field)
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise VideoExportError("FILE_TOO_LARGE", f"{field} exceeds {maximum} bytes", field)
                digest.update(chunk)
    except VideoExportError:
        raise
    except OSError as exc:
        raise VideoExportError("FILE_READ", str(exc), field) from exc
    if total != size:
        raise VideoExportError("FILE_SIZE_CHANGED", f"{field} changed while being read", field)
    return digest.hexdigest(), total


def _load_object(path: Path, field: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise VideoExportError("DUPLICATE_KEY", f"duplicate JSON key: {key}", field)
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except VideoExportError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoExportError("LOAD_ERROR", str(exc), field) from exc
    if not isinstance(value, dict):
        raise VideoExportError("ROOT_TYPE", f"{field} JSON root must be an object", field)
    return value


def _canonical_object(path: Path, field: str) -> tuple[dict[str, Any], bytes]:
    value = _load_object(path, field)
    payload = path.read_bytes()
    if payload != _json_bytes(value):
        raise VideoExportError("NONCANONICAL_JSON", f"{field} must use canonical JSON", field)
    return value, payload


def _reject_native_lexical(path: Path, field: str) -> Path:
    raw = str(path)
    if "\x00" in raw:
        raise VideoExportError("UNSAFE_PATH", f"{field} contains a null byte", field)
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise VideoExportError("UNSAFE_PATH", f"{field} must not contain parent traversal", field)
    return expanded


def _reject_symlink_components(path: Path, field: str) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    for candidate in (lexical, *lexical.parents):
        try:
            if candidate.exists() and candidate.is_symlink():
                raise VideoExportError("PATH_SYMLINK", f"{field} contains a symlink component", field)
        except OSError as exc:
            raise VideoExportError("PATH_ERROR", str(exc), field) from exc


def _root(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = _reject_native_lexical(path, field)
    _reject_symlink_components(expanded, field)
    if must_exist and not expanded.is_dir():
        raise VideoExportError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise VideoExportError("ROOT_TYPE", f"{field} must be a directory", field)
    try:
        return expanded.resolve(strict=must_exist)
    except OSError as exc:
        raise VideoExportError("PATH_ERROR", str(exc), field) from exc


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_file(root: Path, relative: str, field: str) -> tuple[str, Path]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise VideoExportError("UNSAFE_PATH", str(exc), field) from exc
    candidate = root.joinpath(*safe.parts)
    current = root
    for part in safe.parts:
        current /= part
        if current.is_symlink():
            raise VideoExportError("PATH_SYMLINK", f"{field} contains a symlink component", field)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VideoExportError("FILE_MISSING", str(exc), field) from exc
    if not _within(root, resolved):
        raise VideoExportError("PATH_ESCAPE", f"{field} escapes configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise VideoExportError("FILE_TYPE", f"{field} must be a regular file", field)
    return safe.as_posix(), resolved


def _relative_file(path: Path, root: Path, field: str) -> tuple[str, Path]:
    expanded = _reject_native_lexical(path, field)
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        relative = lexical.relative_to(root).as_posix()
    except ValueError as exc:
        raise VideoExportError("PATH_ESCAPE", f"{field} must be beneath its configured root", field) from exc
    return _safe_file(root, relative, field)


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise VideoExportError("INTEGER_RANGE", f"{field} must be from {minimum} to {maximum}", field)
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise VideoExportError(
            "PROFILE_SCHEMA",
            f"{field} keys differ; missing={sorted(expected - actual)} extra={sorted(actual - expected)}",
            field,
        )
