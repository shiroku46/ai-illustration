"""Validate benchmark results and render deterministic local SVG contact sheets."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
from typing import Any, Iterable, Sequence
import zlib

from .art_direction import load_document
from .model_benchmark import (
    canonical_sha256,
    expand_matrix,
    validate_dependencies,
    validate_plan,
)
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, canonical_json, safe_relative_path

RESULTS_KIND = "model-benchmark-results"
SCHEMA_VERSION = "1.0"
EXECUTION_STATES = frozenset({"succeeded", "failed"})
MAX_RESULT_IMAGE_BYTES = 128 * 1024 * 1024
MAX_DECODED_BYTES = 256 * 1024 * 1024
MAX_PNG_DIMENSION = 8192
MAX_PNG_PIXELS = 64 * 1024 * 1024
MAX_ERROR_MESSAGE = 2000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PACKAGE_MANIFEST = "contact-sheet-manifest.json"
SHEETS_DIR = "contact-sheets"

RESULTS_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "version",
        "plan_ref",
        "plan_version",
        "plan_sha256",
        "results",
        "notes",
    }
)
RESULTS_REQUIRED = RESULTS_FIELDS - {"notes"}
ENTRY_COMMON = frozenset(
    {
        "run_id",
        "state",
        "model_family",
        "model_profile_ref",
        "model_profile_sha256",
        "workflow_sha256",
        "seed",
        "prompt_case_id",
        "role_scope",
        "settings",
        "elapsed_ms",
        "peak_vram_mib",
    }
)
SUCCESS_FIELDS = ENTRY_COMMON | frozenset(
    {"image_path", "image_sha256", "width", "height"}
)
FAILURE_FIELDS = ENTRY_COMMON | frozenset({"error"})
ENTRY_REQUIRED_COMMON = ENTRY_COMMON - {"peak_vram_mib"}
SETTINGS_FIELDS = frozenset(
    {"width", "height", "sampler", "scheduler", "steps", "cfg", "prompt_format"}
)
ERROR_FIELDS = frozenset({"code", "message"})
FORBIDDEN_DECISION_TERMS = frozenset(
    {
        "aesthetic_score",
        "score",
        "rank",
        "ranking",
        "winner",
        "approved",
        "approval",
        "recommendation",
        "recommended",
        "selected",
        "selection",
    }
)


class BenchmarkResultsError(ValueError):
    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _diag(code: str, message: str, field: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "field": field}


def _sorted_diagnostics(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique = {
        (item.get("field", ""), item.get("code", ""), item.get("message", "")): {
            "code": item.get("code", ""),
            "message": item.get("message", ""),
            "field": item.get("field", ""),
        }
        for item in values
    }
    return [unique[key] for key in sorted(unique)]


def document_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def result_set_sha256(results: dict[str, Any]) -> str:
    return hashlib.sha256(document_bytes(results)).hexdigest()


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_fields(
    value: Any,
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    field: str,
) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return [_diag("OBJECT_REQUIRED", "must be an object", field)]
    diagnostics: list[dict[str, str]] = []
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        diagnostics.append(_diag("MISSING_FIELD", f"missing fields: {', '.join(missing)}", field))
    if unknown:
        diagnostics.append(_diag("UNKNOWN_FIELD", f"unknown fields: {', '.join(unknown)}", field))
    forbidden = sorted(set(value) & FORBIDDEN_DECISION_TERMS)
    if forbidden:
        diagnostics.append(
            _diag(
                "AUTOMATIC_SELECTION_FORBIDDEN",
                f"decision fields are forbidden: {', '.join(forbidden)}",
                field,
            )
        )
    return diagnostics


def _token(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        return [_diag("INVALID_TOKEN", "must be a lowercase ASCII token", field)]
    return []


def _checksum(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        return [_diag("CHECKSUM", "must be 64 lowercase hexadecimal characters", field)]
    return []


def _nonnegative_integer(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return [_diag("NONNEGATIVE_INTEGER", "must be a non-negative integer", field)]
    return []


def _positive_integer(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return [_diag("POSITIVE_INTEGER", "must be a positive integer", field)]
    return []


def _validate_settings(value: Any, field: str) -> list[dict[str, str]]:
    diagnostics = _check_fields(
        value,
        required=SETTINGS_FIELDS,
        allowed=SETTINGS_FIELDS,
        field=field,
    )
    if not isinstance(value, dict):
        return diagnostics
    for name in ("width", "height", "steps"):
        diagnostics.extend(_positive_integer(value.get(name), f"{field}.{name}"))
    for name in ("sampler", "scheduler"):
        diagnostics.extend(_token(value.get(name), f"{field}.{name}"))
    cfg = value.get("cfg")
    if not isinstance(cfg, (int, float)) or isinstance(cfg, bool) or cfg <= 0:
        diagnostics.append(_diag("POSITIVE_NUMBER", "must be a positive number", f"{field}.cfg"))
    if not _nonempty(value.get("prompt_format")):
        diagnostics.append(_diag("TEXT_REQUIRED", "must be non-empty", f"{field}.prompt_format"))
    return diagnostics


def _validate_entry(value: Any, index: int) -> list[dict[str, str]]:
    field = f"results[{index}]"
    if not isinstance(value, dict):
        return [_diag("OBJECT_REQUIRED", "result entry must be an object", field)]
    state = value.get("state")
    allowed = SUCCESS_FIELDS if state == "succeeded" else FAILURE_FIELDS if state == "failed" else ENTRY_COMMON
    required = (
        ENTRY_REQUIRED_COMMON | frozenset({"image_path", "image_sha256", "width", "height"})
        if state == "succeeded"
        else ENTRY_REQUIRED_COMMON | frozenset({"error"})
        if state == "failed"
        else ENTRY_REQUIRED_COMMON
    )
    diagnostics = _check_fields(value, required=required, allowed=allowed, field=field)
    if state not in EXECUTION_STATES:
        diagnostics.append(_diag("EXECUTION_STATE", "state must be succeeded or failed", f"{field}.state"))
    diagnostics.extend(_token(value.get("run_id"), f"{field}.run_id"))
    diagnostics.extend(_token(value.get("model_family"), f"{field}.model_family"))
    profile_ref = value.get("model_profile_ref")
    if not isinstance(profile_ref, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}", profile_ref):
        diagnostics.append(_diag("MODEL_REFERENCE", "must use id@vNNN", f"{field}.model_profile_ref"))
    diagnostics.extend(_checksum(value.get("model_profile_sha256"), f"{field}.model_profile_sha256"))
    diagnostics.extend(_checksum(value.get("workflow_sha256"), f"{field}.workflow_sha256"))
    diagnostics.extend(_nonnegative_integer(value.get("seed"), f"{field}.seed"))
    diagnostics.extend(_token(value.get("prompt_case_id"), f"{field}.prompt_case_id"))
    if value.get("role_scope") not in {"single-role", "two-character-secondary"}:
        diagnostics.append(_diag("ROLE_SCOPE", "invalid role_scope", f"{field}.role_scope"))
    diagnostics.extend(_validate_settings(value.get("settings"), f"{field}.settings"))
    diagnostics.extend(_nonnegative_integer(value.get("elapsed_ms"), f"{field}.elapsed_ms"))
    if "peak_vram_mib" in value:
        diagnostics.extend(_nonnegative_integer(value.get("peak_vram_mib"), f"{field}.peak_vram_mib"))

    if state == "succeeded":
        image_path = value.get("image_path")
        if not isinstance(image_path, str):
            diagnostics.append(_diag("UNSAFE_PATH", "image_path must be a POSIX relative path", f"{field}.image_path"))
        else:
            try:
                safe_relative_path(image_path)
            except ValueError as exc:
                diagnostics.append(_diag("UNSAFE_PATH", str(exc), f"{field}.image_path"))
        diagnostics.extend(_checksum(value.get("image_sha256"), f"{field}.image_sha256"))
        diagnostics.extend(_positive_integer(value.get("width"), f"{field}.width"))
        diagnostics.extend(_positive_integer(value.get("height"), f"{field}.height"))
    elif state == "failed":
        error = value.get("error")
        diagnostics.extend(_check_fields(error, required=ERROR_FIELDS, allowed=ERROR_FIELDS, field=f"{field}.error"))
        if isinstance(error, dict):
            diagnostics.extend(_token(error.get("code"), f"{field}.error.code"))
            message = error.get("message")
            if not _nonempty(message) or len(message) > MAX_ERROR_MESSAGE:
                diagnostics.append(
                    _diag(
                        "ERROR_MESSAGE",
                        f"message must be non-empty and at most {MAX_ERROR_MESSAGE} characters",
                        f"{field}.error.message",
                    )
                )
    return diagnostics


def validate_document(results: Any) -> list[dict[str, str]]:
    diagnostics = _check_fields(
        results,
        required=RESULTS_REQUIRED,
        allowed=RESULTS_FIELDS,
        field="results_document",
    )
    if not isinstance(results, dict):
        return diagnostics
    if results.get("kind") != RESULTS_KIND:
        diagnostics.append(_diag("KIND", f"kind must be {RESULTS_KIND}", "kind"))
    if results.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(_diag("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version"))
    diagnostics.extend(_token(results.get("id"), "id"))
    if not isinstance(results.get("version"), str) or not VERSION_RE.fullmatch(results.get("version", "")):
        diagnostics.append(_diag("VERSION", "version must use vNNN", "version"))
    diagnostics.extend(_token(results.get("plan_ref"), "plan_ref"))
    if not isinstance(results.get("plan_version"), str) or not VERSION_RE.fullmatch(results.get("plan_version", "")):
        diagnostics.append(_diag("VERSION", "plan_version must use vNNN", "plan_version"))
    diagnostics.extend(_checksum(results.get("plan_sha256"), "plan_sha256"))
    if "notes" in results and not _nonempty(results.get("notes")):
        diagnostics.append(_diag("TEXT_REQUIRED", "notes must be non-empty when present", "notes"))
    entries = results.get("results")
    if not isinstance(entries, list) or not entries:
        diagnostics.append(_diag("RESULTS", "results must be a non-empty list", "results"))
    else:
        for index, entry in enumerate(entries):
            diagnostics.extend(_validate_entry(entry, index))
        run_ids = [
            entry.get("run_id")
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("run_id"), str)
        ]
        if len(run_ids) != len(set(run_ids)):
            diagnostics.append(_diag("DUPLICATE_RUN", "run IDs must be unique", "results"))
    return _sorted_diagnostics(diagnostics)


def _root(path: Path, field: str) -> tuple[Path | None, list[dict[str, str]]]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        return None, [_diag("ROOT_SYMLINK", f"{field} must not be a symlink", field)]
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        return None, [_diag("ROOT_MISSING", str(exc), field)]
    if not resolved.is_dir():
        return None, [_diag("ROOT_TYPE", f"{field} must be a directory", field)]
    return resolved, []


def _has_symlink(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop:
            return False
        if current.parent == current:
            return True
        current = current.parent


def _read_image(root: Path, relative: str, field: str) -> tuple[bytes | None, list[dict[str, str]]]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        return None, [_diag("UNSAFE_PATH", str(exc), field)]
    lexical = root.joinpath(*safe.parts)
    if _has_symlink(lexical, root):
        return None, [_diag("PATH_SYMLINK", "path contains a symlink", field)]
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        return None, [_diag("IMAGE_MISSING", str(exc), field)]
    if not resolved.is_file() or resolved.is_symlink():
        return None, [_diag("IMAGE_TYPE", "image must be a regular file", field)]
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return None, [_diag("IMAGE_READ", str(exc), field)]
    if size <= 0 or size > MAX_RESULT_IMAGE_BYTES:
        return None, [_diag("IMAGE_SIZE", f"image size must be 1..{MAX_RESULT_IMAGE_BYTES} bytes", field)]
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        return None, [_diag("IMAGE_READ", str(exc), field)]
    if len(payload) != size:
        return None, [_diag("IMAGE_CHANGED", "image changed while being read", field)]
    return payload, []


def _parse_png(payload: bytes) -> tuple[int, int]:
    if not payload.startswith(PNG_SIGNATURE):
        raise BenchmarkResultsError("PNG_SIGNATURE", "invalid PNG signature", "image")
    offset = len(PNG_SIGNATURE)
    width = height = channels = -1
    seen_ihdr = seen_idat = seen_iend = False
    idat_closed = False
    compressed = bytearray()
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise BenchmarkResultsError("PNG_STRUCTURE", "truncated PNG chunk", "image")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(payload):
            raise BenchmarkResultsError("PNG_STRUCTURE", "chunk exceeds file length", "image")
        data = payload[offset + 8 : offset + 8 + length]
        stored_crc = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        if not re.fullmatch(rb"[A-Za-z]{4}", kind):
            raise BenchmarkResultsError("PNG_CHUNK", "invalid PNG chunk type", "image")
        if (binascii.crc32(kind + data) & 0xFFFFFFFF) != stored_crc:
            raise BenchmarkResultsError("PNG_CRC", f"invalid CRC for {kind!r}", "image")
        if not seen_ihdr and kind != b"IHDR":
            raise BenchmarkResultsError("PNG_STRUCTURE", "IHDR must be first", "image")
        if seen_iend:
            raise BenchmarkResultsError("PNG_TRAILING_DATA", "data follows IEND", "image")
        if seen_idat and kind not in {b"IDAT", b"IEND"}:
            idat_closed = True

        if kind == b"IHDR":
            if seen_ihdr or length != 13:
                raise BenchmarkResultsError("PNG_IHDR", "invalid or duplicate IHDR", "image")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if not 1 <= width <= MAX_PNG_DIMENSION or not 1 <= height <= MAX_PNG_DIMENSION:
                raise BenchmarkResultsError("PNG_DIMENSIONS", "PNG dimensions are out of range", "image")
            if width * height > MAX_PNG_PIXELS:
                raise BenchmarkResultsError("PNG_PIXELS", "PNG pixel count exceeds limit", "image")
            if bit_depth != 8 or color_type not in {2, 4, 6}:
                raise BenchmarkResultsError("PNG_FORMAT", "only 8-bit RGB, grayscale-alpha, or RGBA PNG is supported", "image")
            if (compression, filtering, interlace) != (0, 0, 0):
                raise BenchmarkResultsError("PNG_FORMAT", "unsupported compression, filtering, or interlace", "image")
            channels = {2: 3, 4: 2, 6: 4}[color_type]
            seen_ihdr = True
        elif kind == b"IDAT":
            if idat_closed:
                raise BenchmarkResultsError("PNG_STRUCTURE", "IDAT chunks must be consecutive", "image")
            seen_idat = True
            compressed.extend(data)
            if len(compressed) > MAX_RESULT_IMAGE_BYTES:
                raise BenchmarkResultsError("PNG_SIZE", "compressed stream exceeds limit", "image")
        elif kind == b"IEND":
            if length != 0 or not seen_idat:
                raise BenchmarkResultsError("PNG_IEND", "invalid IEND", "image")
            seen_iend = True
            offset = end
            break
        elif kind in {b"acTL", b"fcTL", b"fdAT"}:
            raise BenchmarkResultsError("PNG_ANIMATION", "animated PNG chunks are forbidden", "image")
        elif 65 <= kind[0] <= 90 and kind != b"PLTE":
            raise BenchmarkResultsError("PNG_CRITICAL_CHUNK", f"unsupported critical chunk {kind!r}", "image")
        offset = end

    if not (seen_ihdr and seen_idat and seen_iend):
        raise BenchmarkResultsError("PNG_STRUCTURE", "PNG requires IHDR, IDAT, and IEND", "image")
    if offset != len(payload):
        raise BenchmarkResultsError("PNG_TRAILING_DATA", "bytes follow IEND", "image")
    expected = height * (1 + width * channels)
    if expected > MAX_DECODED_BYTES:
        raise BenchmarkResultsError("PNG_DECODE_LIMIT", "decoded PNG exceeds limit", "image")
    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(bytes(compressed), expected + 1)
        if len(raw) > expected or decoder.unconsumed_tail:
            raise BenchmarkResultsError("PNG_DECODE_LIMIT", "decoded PNG exceeds IHDR-derived length", "image")
        remaining = expected - len(raw)
        raw += decoder.flush(max(1, remaining + 1))
    except BenchmarkResultsError:
        raise
    except zlib.error as exc:
        raise BenchmarkResultsError("PNG_ZLIB", str(exc), "image") from exc
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise BenchmarkResultsError("PNG_DECODE_LENGTH", "decoded PNG byte length is invalid", "image")
    stride = 1 + width * channels
    for row in range(height):
        if raw[row * stride] > 4:
            raise BenchmarkResultsError("PNG_FILTER", "invalid PNG filter type", "image")
    return width, height


def _expected_common(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "model_family": row["model_family"],
        "model_profile_ref": row["model_profile_ref"],
        "model_profile_sha256": row["model_profile_sha256"],
        "workflow_sha256": row["workflow_sha256"],
        "seed": row["seed"],
        "prompt_case_id": row["prompt_case_id"],
        "role_scope": row["role_scope"],
        "settings": row["settings"],
    }


def validate_results(
    results: Any,
    plan: Any,
    *,
    workspace_root: Path,
    reference_root: Path,
    result_root: Path,
) -> tuple[list[dict[str, str]], dict[str, bytes]]:
    diagnostics = validate_document(results)
    diagnostics.extend(validate_plan(plan))
    if diagnostics or not isinstance(results, dict) or not isinstance(plan, dict):
        return _sorted_diagnostics(diagnostics), {}
    diagnostics.extend(validate_dependencies(plan, workspace_root, reference_root))
    if results.get("plan_ref") != plan.get("id"):
        diagnostics.append(_diag("PLAN_BINDING", "plan_ref does not match", "plan_ref"))
    if results.get("plan_version") != plan.get("version"):
        diagnostics.append(_diag("PLAN_BINDING", "plan_version does not match", "plan_version"))
    expected_plan_sha = canonical_sha256(plan)
    if results.get("plan_sha256") != expected_plan_sha:
        diagnostics.append(_diag("PLAN_BINDING", "plan_sha256 does not match canonical plan", "plan_sha256"))

    expected_rows = {row["run_id"]: row for row in expand_matrix(plan)}
    entries = results.get("results", [])
    actual_entries = {
        entry["run_id"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("run_id"), str)
    }
    missing = sorted(set(expected_rows) - set(actual_entries))
    extra = sorted(set(actual_entries) - set(expected_rows))
    if missing:
        diagnostics.append(_diag("MISSING_RUNS", f"missing run IDs: {', '.join(missing)}", "results"))
    if extra:
        diagnostics.append(_diag("EXTRA_RUNS", f"unexpected run IDs: {', '.join(extra)}", "results"))

    root, root_diagnostics = _root(result_root, "result_root")
    diagnostics.extend(root_diagnostics)
    images: dict[str, bytes] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("run_id"), str):
            continue
        expected = expected_rows.get(entry["run_id"])
        if expected is None:
            continue
        for key, value in _expected_common(expected).items():
            if entry.get(key) != value:
                diagnostics.append(
                    _diag(
                        "RUN_BINDING",
                        f"{key} does not match benchmark matrix",
                        f"results[{index}].{key}",
                    )
                )
        if entry.get("state") != "succeeded" or root is None:
            continue
        if entry.get("image_path") != expected["image_path"]:
            diagnostics.append(
                _diag(
                    "IMAGE_PATH_BINDING",
                    f"expected {expected['image_path']}",
                    f"results[{index}].image_path",
                )
            )
            continue
        payload, values = _read_image(root, str(entry.get("image_path", "")), f"results[{index}].image_path")
        diagnostics.extend(values)
        if payload is None:
            continue
        if hashlib.sha256(payload).hexdigest() != entry.get("image_sha256"):
            diagnostics.append(_diag("IMAGE_CHECKSUM", "image SHA-256 does not match", f"results[{index}].image_sha256"))
        try:
            width, height = _parse_png(payload)
        except BenchmarkResultsError as exc:
            diagnostics.append(_diag(exc.code, exc.message, f"results[{index}].image_path"))
            continue
        if width != entry.get("width") or height != entry.get("height"):
            diagnostics.append(
                _diag(
                    "IMAGE_DIMENSIONS",
                    f"PNG dimensions are {width}x{height}",
                    f"results[{index}]",
                )
            )
        images[entry["run_id"]] = payload
    return _sorted_diagnostics(diagnostics), images


def _svg_text(x: int, y: int, text: str, *, size: int = 16, weight: str = "normal") -> str:
    return (
        f'<text x="{x}" y="{y}" font-family="monospace" font-size="{size}" '
        f'font-weight="{weight}" fill="#202020">{html.escape(text)}</text>'
    )


def _contact_sheet_svg(
    family: str,
    prompt_case: str,
    entries: list[dict[str, Any]],
    images: dict[str, bytes],
) -> bytes:
    columns = 4
    tile_width = 300
    tile_height = 350
    margin = 24
    header = 72
    rows = math.ceil(len(entries) / columns)
    width = margin * 2 + columns * tile_width
    height = header + margin + rows * tile_height
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#f5f1e8"/>',
        _svg_text(margin, 30, f"MODEL: {family}", size=20, weight="bold"),
        _svg_text(margin, 56, f"CASE: {prompt_case}", size=17),
    ]
    for index, entry in enumerate(sorted(entries, key=lambda item: item["seed"])):
        column = index % columns
        row = index // columns
        x = margin + column * tile_width
        y = header + row * tile_height
        parts.append(f'<rect x="{x + 4}" y="{y + 4}" width="{tile_width - 8}" height="{tile_height - 8}" rx="8" fill="#fffdf7" stroke="#27231f" stroke-width="2"/>')
        parts.append(_svg_text(x + 16, y + 28, f"SEED {entry['seed']}", size=16, weight="bold"))
        if entry["state"] == "succeeded":
            payload = images[entry["run_id"]]
            encoded = base64.b64encode(payload).decode("ascii")
            parts.append(
                f'<image x="{x + 16}" y="{y + 42}" width="{tile_width - 32}" height="{tile_width - 32}" '
                f'preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{encoded}"/>'
            )
            parts.append(_svg_text(x + 16, y + tile_width + 28, f"OK {entry['elapsed_ms']} MS", size=14, weight="bold"))
            if "peak_vram_mib" in entry:
                parts.append(_svg_text(x + 16, y + tile_width + 48, f"VRAM {entry['peak_vram_mib']} MIB", size=13))
        else:
            parts.append(f'<rect x="{x + 16}" y="{y + 42}" width="{tile_width - 32}" height="{tile_width - 32}" fill="#f4d8d8" stroke="#7a2020" stroke-width="2"/>')
            parts.append(_svg_text(x + 28, y + 90, "EXECUTION FAILED", size=18, weight="bold"))
            parts.append(_svg_text(x + 28, y + 122, f"CODE {entry['error']['code']}", size=14, weight="bold"))
            message = " ".join(entry["error"]["message"].split())[:64]
            parts.append(_svg_text(x + 28, y + 152, message, size=12))
            parts.append(_svg_text(x + 16, y + tile_width + 28, f"ERROR {entry['elapsed_ms']} MS", size=14, weight="bold"))
        parts.append(_svg_text(x + 16, y + tile_height - 18, entry["run_id"], size=11))
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def build_contact_sheet_package(
    results: dict[str, Any],
    images: dict[str, bytes],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in results["results"]:
        groups.setdefault((entry["model_family"], entry["prompt_case_id"]), []).append(entry)
    result_sha = result_set_sha256(results)
    files: dict[str, bytes] = {}
    sheets: list[dict[str, Any]] = []
    for family, prompt_case in sorted(groups):
        entries = sorted(groups[(family, prompt_case)], key=lambda item: item["seed"])
        svg = _contact_sheet_svg(family, prompt_case, entries, images)
        relative = f"{SHEETS_DIR}/{family}/{prompt_case}.svg"
        files[relative] = svg
        sheets.append(
            {
                "model_family": family,
                "prompt_case_id": prompt_case,
                "path": relative,
                "sha256": hashlib.sha256(svg).hexdigest(),
                "size": len(svg),
                "run_ids": [entry["run_id"] for entry in entries],
                "success_count": sum(entry["state"] == "succeeded" for entry in entries),
                "failure_count": sum(entry["state"] == "failed" for entry in entries),
            }
        )
    manifest_core = {
        "kind": "benchmark-contact-sheet-package",
        "schema_version": "1.0",
        "results_ref": results["id"],
        "results_version": results["version"],
        "results_sha256": result_sha,
        "plan_ref": results["plan_ref"],
        "plan_version": results["plan_version"],
        "plan_sha256": results["plan_sha256"],
        "selection_policy": "owner-only",
        "sheets": sheets,
    }
    manifest_id = f"contact-sheets-{hashlib.sha256(canonical_json(manifest_core)).hexdigest()[:16]}"
    manifest = {"id": manifest_id, **manifest_core}
    files[PACKAGE_MANIFEST] = document_bytes(manifest)
    return manifest, files


def _output_target(path: Path) -> tuple[Path, Path]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.exists() or lexical.is_symlink():
        raise BenchmarkResultsError("OUTPUT_EXISTS", "output directory must not already exist", "output_dir")
    parent = lexical.parent
    if parent.is_symlink() or not parent.is_dir():
        raise BenchmarkResultsError("OUTPUT_PARENT", "output parent must be an existing non-symlink directory", "output_dir")
    try:
        parent_resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise BenchmarkResultsError("OUTPUT_PARENT", str(exc), "output_dir") from exc
    return lexical, parent_resolved


def publish_package(output_dir: Path, files: dict[str, bytes]) -> None:
    target, parent = _output_target(output_dir)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=parent))
    published = False
    try:
        for relative, payload in sorted(files.items()):
            safe = safe_relative_path(relative)
            destination = stage.joinpath(*safe.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        if target.exists() or target.is_symlink():
            raise BenchmarkResultsError("OUTPUT_EXISTS", "output appeared during rendering", "output_dir")
        os.replace(stage, target)
        published = True
    except Exception:
        if not published and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _result(
    diagnostics: list[dict[str, str]],
    *,
    results: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {"ok": not diagnostics, "diagnostics": _sorted_diagnostics(diagnostics)}
    if results is not None:
        output["results_id"] = results.get("id")
        output["results_version"] = results.get("version")
        output["results_sha256"] = result_set_sha256(results)
        output["run_count"] = len(results.get("results", [])) if isinstance(results.get("results"), list) else 0
    if manifest is not None:
        output["package_id"] = manifest["id"]
        output["sheet_count"] = len(manifest["sheets"])
        output["manifest_sha256"] = hashlib.sha256(document_bytes(manifest)).hexdigest()
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate benchmark results and render local contact sheets")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("results-check", "render-contact-sheets"):
        command = sub.add_parser(name)
        command.add_argument("results", type=Path)
        command.add_argument("plan", type=Path)
        command.add_argument("--workspace-root", type=Path, required=True)
        command.add_argument("--reference-root", type=Path, required=True)
        command.add_argument("--result-root", type=Path, required=True)
        if name == "render-contact-sheets":
            command.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        results = load_document(args.results)
        plan = load_document(args.plan)
        diagnostics, images = validate_results(
            results,
            plan,
            workspace_root=args.workspace_root,
            reference_root=args.reference_root,
            result_root=args.result_root,
        )
        manifest = None
        if not diagnostics and args.command == "render-contact-sheets":
            manifest, files = build_contact_sheet_package(results, images)
            publish_package(args.output_dir, files)
        output = _result(diagnostics, results=results, manifest=manifest)
    except (BenchmarkResultsError, ValueError, OSError) as exc:
        diagnostic = exc.to_dict() if isinstance(exc, BenchmarkResultsError) else _diag("ERROR", str(exc))
        output = _result([diagnostic])
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
