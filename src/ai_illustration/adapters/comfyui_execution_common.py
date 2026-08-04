"""Shared fail-closed validation primitives for ComfyUI execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .base import AdapterError
from .comfyui import _scan_for_secrets as scan_for_secrets
from ..naming import canonical_json, content_identifier

ADAPTER_ID = "comfyui-local-api"
ADAPTER_VERSION = "v001"
PLAN_FILE = "execution-plan.json"
MANIFEST_FILE = "execution-manifest.json"
MAX_SOURCE_JSON_BYTES = 16 * 1024 * 1024
MAX_PROFILE_IMAGES = 32
MAX_TOTAL_PNG_BYTES = 512 * 1024 * 1024
TOKEN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def reject_symlinks(path: Path, field: str) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    for item in (lexical, *lexical.parents):
        if item.exists() and item.is_symlink():
            raise AdapterError("PATH_SYMLINK", f"{field} contains a symlink component", field)


def source_file(path: Path, field: str, *, canonical_required: bool) -> tuple[dict[str, Any], bytes, Path]:
    raw = str(path)
    if "\x00" in raw or ".." in path.expanduser().parts:
        raise AdapterError("UNSAFE_PATH", f"{field} path is unsafe", field)
    reject_symlinks(path.expanduser(), field)
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise AdapterError("FILE_TYPE", f"{field} must be a regular file", field)
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_SOURCE_JSON_BYTES:
        raise AdapterError("SOURCE_SIZE", f"{field} exceeds the JSON size limit", field)
    with resolved.open("rb") as handle:
        payload = handle.read(MAX_SOURCE_JSON_BYTES + 1)
    if len(payload) != size or len(payload) > MAX_SOURCE_JSON_BYTES:
        raise AdapterError("SOURCE_SIZE_CHANGED", f"{field} changed size during bounded read", field)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise AdapterError("DUPLICATE_JSON_KEY", f"duplicate JSON key: {key}", field)
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except AdapterError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError("INVALID_JSON", str(exc), field) from exc
    if not isinstance(value, dict):
        raise AdapterError("INVALID_JSON_ROOT", f"{field} root must be an object", field)
    if canonical_required and payload != json_bytes(value):
        raise AdapterError("NONCANONICAL_JSON", f"{field} must use canonical JSON plus newline", field)
    scan_for_secrets(value, field)
    return value, payload, resolved


def token(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or any(ch not in TOKEN_CHARS for ch in value):
        raise AdapterError("TOKEN", f"{field} must be a bounded ASCII token", field)
    return value


def integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AdapterError("INTEGER_RANGE", f"{field} must be from {minimum} to {maximum}", field)
    return value


def validate_catalog_profile(value: dict[str, Any], *, expected_id: str, profile_type: str, field: str) -> None:
    required = {
        "kind", "schema_version", "id", "version", "profile_type", "adapter_type", "runtime_type",
        "offline_capability", "deterministic_seed_support", "control_capabilities", "minimum_vram_gb",
        "minimum_ram_gb", "supported_operating_systems", "install_state", "evidence_references",
        "license_evidence_state", "commercial_use_review_state", "decision_state",
    }
    if set(value) != required:
        raise AdapterError("PROFILE_SCHEMA", f"{field} fields are not canonical", field)
    if value.get("kind") != "tool-profile" or value.get("schema_version") != "1.0":
        raise AdapterError("PROFILE_SCHEMA", f"{field} kind/version is invalid", field)
    if value.get("id") != expected_id or value.get("profile_type") != profile_type:
        raise AdapterError("PROFILE_BINDING", f"{field} does not match the request", field)
    if value.get("adapter_type") != ADAPTER_ID:
        raise AdapterError("PROFILE_ADAPTER", f"{field} must use {ADAPTER_ID}", field)
    if value.get("offline_capability") != "yes" or value.get("install_state") != "installed":
        raise AdapterError("PROFILE_INSTALLATION", f"{field} must be installed and offline-capable", field)
    if value.get("deterministic_seed_support") is not True:
        raise AdapterError("PROFILE_SEED", f"{field} must declare deterministic seed support", field)
    capabilities = value.get("control_capabilities")
    if not isinstance(capabilities, list) or not {"seed", "workflow"}.issubset(set(capabilities)):
        raise AdapterError("PROFILE_CAPABILITY", f"{field} lacks required capabilities", field)
    for state in ("license_evidence_state", "commercial_use_review_state", "decision_state"):
        if value.get(state) != "approved":
            raise AdapterError("PROFILE_APPROVAL", f"{field}.{state} must be approved", f"{field}.{state}")


def validate_execution_profile(value: dict[str, Any], *, workflow_sha: str, tool_id: str, model_id: str) -> None:
    required = {
        "id", "kind", "schema_version", "workflow_sha256", "tool_profile_ref", "model_profile_ref",
        "output_node_ids", "expected_width", "expected_height", "limits",
    }
    if set(value) != required or value.get("kind") != "comfyui-execution-profile" or value.get("schema_version") != "1.0":
        raise AdapterError("EXECUTION_PROFILE_SCHEMA", "execution profile fields are invalid", "execution_profile")
    core = {key: value[key] for key in sorted(value) if key != "id"}
    if value.get("id") != content_identifier("comfyui-execution-profile", core, 20):
        raise AdapterError("EXECUTION_PROFILE_ID", "execution profile ID is not content-derived", "execution_profile.id")
    if value.get("workflow_sha256") != workflow_sha:
        raise AdapterError("WORKFLOW_BINDING", "execution profile workflow checksum changed", "workflow_sha256")
    if value.get("tool_profile_ref") != tool_id or value.get("model_profile_ref") != model_id:
        raise AdapterError("PROFILE_BINDING", "execution profile profile references changed", "execution_profile")
    nodes = value.get("output_node_ids")
    if not isinstance(nodes, list) or not nodes or len(nodes) > MAX_PROFILE_IMAGES:
        raise AdapterError("OUTPUT_NODES", "output_node_ids must be a bounded non-empty list", "output_node_ids")
    normalized = [token(node, f"output_node_ids[{index}]") for index, node in enumerate(nodes)]
    if normalized != sorted(set(normalized)):
        raise AdapterError("OUTPUT_NODES", "output_node_ids must be sorted and unique", "output_node_ids")
    integer(value.get("expected_width"), "expected_width", 1, 8192)
    integer(value.get("expected_height"), "expected_height", 1, 8192)
    limits = value.get("limits")
    limit_fields = {
        "max_images", "max_queue_response_bytes", "max_history_response_bytes", "max_png_bytes",
        "max_total_png_bytes", "request_timeout_seconds", "poll_interval_ms", "overall_timeout_seconds",
    }
    if not isinstance(limits, dict) or set(limits) != limit_fields:
        raise AdapterError("LIMIT_SCHEMA", "execution limits are invalid", "limits")
    integer(limits["max_images"], "limits.max_images", len(nodes), MAX_PROFILE_IMAGES)
    integer(limits["max_queue_response_bytes"], "limits.max_queue_response_bytes", 128, 1024 * 1024)
    integer(limits["max_history_response_bytes"], "limits.max_history_response_bytes", 128, 16 * 1024 * 1024)
    max_png = integer(limits["max_png_bytes"], "limits.max_png_bytes", 64, 128 * 1024 * 1024)
    max_total = integer(limits["max_total_png_bytes"], "limits.max_total_png_bytes", max_png, MAX_TOTAL_PNG_BYTES)
    if max_total < max_png:
        raise AdapterError("LIMIT_RANGE", "total PNG limit must cover one PNG", "limits.max_total_png_bytes")
    integer(limits["request_timeout_seconds"], "limits.request_timeout_seconds", 1, 300)
    integer(limits["poll_interval_ms"], "limits.poll_interval_ms", 50, 10_000)
    integer(limits["overall_timeout_seconds"], "limits.overall_timeout_seconds", 1, 86_400)


def source_binding(identifier: str, path: Path, payload: bytes) -> dict[str, Any]:
    return {"id": identifier, "name": path.name, "sha256": sha256(payload)}


def output_root(path: Path, sources: set[Path]) -> Path:
    raw = str(path)
    if "\x00" in raw or ".." in path.expanduser().parts:
        raise AdapterError("UNSAFE_PATH", "output_root path is unsafe", "output_root")
    reject_symlinks(path.expanduser(), "output_root")
    candidate = path.expanduser().resolve(strict=False)
    if candidate.exists() and not candidate.is_dir():
        raise AdapterError("ROOT_TYPE", "output_root must be a directory", "output_root")
    for source in sources:
        resolved = source.resolve(strict=False)
        try:
            resolved.relative_to(candidate)
        except ValueError:
            continue
        raise AdapterError("OUTPUT_OVERLAP", "output_root contains a source input", "output_root")
    return candidate
