"""Quality-aware ComfyUI execution packaging without creative promotion."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AdapterError
from .comfyui_execution_common import json_bytes, load_json, parse_sha, validate_token
from .comfyui_execution_package import (
    CANDIDATE_SIDECAR_KEYS,
    EXECUTION_PACKAGE_KEYS,
    FILE_ENTRY_KEYS,
    MANIFEST_FILE,
    PLAN_FILE,
    SIDECAR_VERSION,
    _expect_keys,
    _package_source,
    _source_file,
    candidate_files as _base_candidate_files,
    execution_manifest,
)
from ..quality import QualityGateError, packaged_quality_stage


def candidate_files(plan: dict[str, Any], outputs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Add the package-only quality stage to deterministic candidate sidecars."""

    candidates, generated = _base_candidate_files(plan, outputs)
    try:
        quality_stage = packaged_quality_stage(plan["request"])
    except QualityGateError as exc:
        raise AdapterError(exc.code, exc.message, exc.field) from exc
    for candidate in candidates:
        sidecar_path = str(candidate["sidecar_path"])
        sidecar = load_json_bytes(generated[sidecar_path], sidecar_path)
        sidecar["quality_stage"] = quality_stage
        generated[sidecar_path] = json_bytes(sidecar)
    return candidates, generated


def load_json_bytes(payload: bytes, field: str) -> dict[str, Any]:
    import json

    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("JSON_PARSE", "candidate sidecar is not valid UTF-8 JSON", field) from exc
    if not isinstance(value, dict):
        raise AdapterError("JSON_ROOT", "candidate sidecar must be an object", field)
    return value


def check_execution_package(manifest_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Offline, read-only verification with an exact quality-stage binding."""

    package_root, manifest = _package_source(manifest_path)
    _expect_keys(manifest, EXECUTION_PACKAGE_KEYS, "manifest")
    if manifest.get("kind") != "comfyui-execution-package" or manifest.get("schema_version") != "1.0":
        raise AdapterError("PACKAGE_SCHEMA", "invalid execution package schema", "manifest")
    validate_token(manifest.get("id"), "manifest.id")
    if manifest.get("status") != "executed":
        raise AdapterError("PACKAGE_STATUS", "execution package status must be executed", "manifest.status")
    if manifest.get("plan_ref") != plan["id"]:
        raise AdapterError("PACKAGE_BINDING", "execution package plan_ref mismatch", "manifest.plan_ref")
    if manifest.get("plan_sha256") != plan["plan_sha256"]:
        raise AdapterError("PACKAGE_BINDING", "execution package plan_sha256 mismatch", "manifest.plan_sha256")
    source_plan = _source_file(package_root, manifest.get("plan_path"), "manifest.plan_path")
    if source_plan.read_bytes() != json_bytes(plan):
        raise AdapterError("PLAN_COPY", "execution plan copy differs from the verified plan", "manifest.plan_path")
    if manifest.get("plan_path") != PLAN_FILE:
        raise AdapterError("PLAN_COPY", f"plan_path must be {PLAN_FILE}", "manifest.plan_path")
    if manifest.get("network_scope") != "loopback-only" or manifest.get("subprocess_started") is not False:
        raise AdapterError("PACKAGE_SAFETY", "execution package safety flags are invalid", "manifest")
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise AdapterError("PACKAGE_CANDIDATES", "execution package candidates must be a non-empty list", "manifest.candidates")
    outputs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for position, candidate in enumerate(candidates):
        field = f"manifest.candidates[{position}]"
        if not isinstance(candidate, dict):
            raise AdapterError("PACKAGE_CANDIDATE", "candidate entry must be an object", field)
        _expect_keys(candidate, CANDIDATE_SIDECAR_KEYS - {"quality_stage"}, field)
        candidate_id = validate_token(candidate.get("id"), f"{field}.id")
        if candidate_id in seen_ids:
            raise AdapterError("PACKAGE_CANDIDATE", "candidate ids must be unique", f"{field}.id")
        seen_ids.add(candidate_id)
        if candidate.get("request_ref") != plan["request"]["id"]:
            raise AdapterError("PACKAGE_BINDING", "candidate request_ref mismatch", f"{field}.request_ref")
        if candidate.get("status") != "technically_valid" or candidate.get("media_type") != "image/png":
            raise AdapterError("PACKAGE_CANDIDATE", "candidate status/media_type is invalid", field)
        if candidate.get("color_space") != "sRGB" or candidate.get("has_alpha") is not True:
            raise AdapterError("PACKAGE_CANDIDATE", "candidate color/alpha metadata is invalid", field)
        if candidate.get("width") != plan["execution_profile"]["expected_width"] or candidate.get("height") != plan["execution_profile"]["expected_height"]:
            raise AdapterError("PACKAGE_CANDIDATE", "candidate dimensions mismatch", field)
        output = candidate.get("provenance", {}).get("execution_output")
        if not isinstance(output, dict):
            raise AdapterError("PACKAGE_PROVENANCE", "candidate execution output provenance is missing", f"{field}.provenance")
        outputs.append({
            "node_id": output.get("node_id"),
            "output_index": output.get("output_index"),
            "filename": output.get("filename"),
            "subfolder": output.get("subfolder"),
            "type": output.get("type"),
            "sha256": candidate.get("sha256"),
            "width": candidate.get("width"),
            "height": candidate.get("height"),
            "has_alpha": candidate.get("has_alpha"),
            "has_srgb": True,
            "payload": _source_file(package_root, candidate.get("path"), f"{field}.path").read_bytes(),
        })
    expected_candidates, generated = candidate_files(plan, outputs)
    expected_manifest = execution_manifest(plan, expected_candidates, {PLAN_FILE: json_bytes(plan), **generated})
    if manifest != expected_manifest:
        raise AdapterError("PACKAGE_BINDING", "execution package manifest differs from its bound inputs", "manifest")
    expected_files = {PLAN_FILE: json_bytes(plan), **generated, MANIFEST_FILE: json_bytes(manifest)}
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise AdapterError("PACKAGE_FILES", "files must be a non-empty list", "manifest.files")
    expected_inventory = {path: (parse_sha(payload), len(payload)) for path, payload in expected_files.items() if path != MANIFEST_FILE}
    actual_inventory: dict[str, tuple[str, int]] = {}
    for position, entry in enumerate(entries):
        field = f"manifest.files[{position}]"
        if not isinstance(entry, dict):
            raise AdapterError("PACKAGE_FILE", "file entry must be an object", field)
        _expect_keys(entry, FILE_ENTRY_KEYS, field)
        path = entry.get("path")
        if not isinstance(path, str) or path in actual_inventory:
            raise AdapterError("PACKAGE_FILE", "file paths must be unique strings", f"{field}.path")
        sha = entry.get("sha256")
        size = entry.get("size")
        if not isinstance(sha, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise AdapterError("PACKAGE_FILE", "file hash/size is invalid", field)
        actual_inventory[path] = (sha, size)
    if actual_inventory != expected_inventory:
        raise AdapterError("FILE_INVENTORY", "file inventory differs from deterministic package contents", "manifest.files")
    for path, payload in expected_files.items():
        source = _source_file(package_root, path, path)
        if source.read_bytes() != payload:
            code = "SIDECAR_BINDING" if path.endswith(".json") and path not in {PLAN_FILE, MANIFEST_FILE} else "FILE_CONTENT"
            raise AdapterError(code, f"package file differs from deterministic content: {path}", path)
    return manifest
