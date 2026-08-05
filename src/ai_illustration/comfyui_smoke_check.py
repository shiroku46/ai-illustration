"""Offline verification for prepared ComfyUI smoke bundles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapters.base import AdapterError
from .adapters.comfyui import _validated_bindings, sanitize_loopback_endpoint
from .adapters.comfyui_execution_common import validate_catalog_profile, validate_execution_profile
from .adapters.comfyui_execution_plan import prepare_execution
from .models import Manifest
from .naming import content_identifier, safe_relative_path
from .validation import validate_document
from .comfyui_smoke_common import (
    BINDINGS_FILE, EXECUTION_FILE, MANIFEST_FILE, MODEL_FILE, PROFILE_STATES, REQUEST_FILE,
    TOOL_FILE, WORKFLOW_FILE, SmokeError, _json_bytes, _load_workflow, _reject_symlinks, _sha,
    inspect_workflow,
)
from .comfyui_smoke_bundle import _bundle_objects, _root


def _load_canonical(path: Path, field: str) -> tuple[dict[str, Any], bytes]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise SmokeError("DUPLICATE_JSON_KEY", f"duplicate JSON key: {key}", field)
            result[key] = value
        return result

    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except SmokeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeError("LOAD_ERROR", str(exc), field) from exc
    if not isinstance(value, dict) or payload != _json_bytes(value):
        raise SmokeError("NONCANONICAL_JSON", f"{field} must be canonical JSON plus newline", field)
    return value, payload


def check_bundle(manifest_path: Path, output_root: Path) -> dict[str, Any]:
    root = _root(output_root, "output_root", must_exist=True)
    expanded = manifest_path.expanduser()
    _reject_symlinks(expanded, "manifest")
    try:
        resolved = expanded.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise SmokeError("MANIFEST_LOCATION", "manifest must be beneath output_root", "manifest") from exc
    if relative.name != MANIFEST_FILE or len(relative.parts) != 2:
        raise SmokeError("MANIFEST_LOCATION", "manifest location is not canonical", "manifest")
    manifest, manifest_bytes = _load_canonical(resolved, "manifest")
    required = {
        "id", "kind", "schema_version", "approval_state", "execution_ready", "endpoint", "workflow",
        "selection", "request_ref", "tool_profile_ref", "model_profile_ref", "execution_profile_ref",
        "expected_width", "expected_height", "files",
    }
    if set(manifest) != required or manifest.get("kind") != "comfyui-smoke-bundle" or manifest.get("schema_version") != "1.0":
        raise SmokeError("MANIFEST_SCHEMA", "smoke bundle manifest fields are invalid", "manifest")
    core = {key: manifest[key] for key in manifest if key != "id"}
    if manifest.get("id") != content_identifier("comfyui-smoke-bundle", core, 20):
        raise SmokeError("MANIFEST_ID", "smoke bundle ID is not content-derived", "id")
    if relative.parts[0] != manifest["id"]:
        raise SmokeError("MANIFEST_LOCATION", "bundle directory does not match its ID", "manifest")
    state = manifest.get("approval_state")
    if state not in PROFILE_STATES or manifest.get("execution_ready") is not (state == "approved"):
        raise SmokeError("APPROVAL_STATE", "bundle approval state is inconsistent", "approval_state")
    try:
        endpoint = sanitize_loopback_endpoint(manifest.get("endpoint"))
    except AdapterError as exc:
        raise SmokeError(exc.code, exc.message, exc.field) from exc

    package = resolved.parent
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 6:
        raise SmokeError("FILE_INVENTORY", "bundle must contain six source files", "files")
    expected_names = {MANIFEST_FILE}
    for index, item in enumerate(files):
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise SmokeError("FILE_INVENTORY", "file inventory entry is invalid", f"files[{index}]")
        try:
            relative_path = safe_relative_path(item["path"])
        except (TypeError, ValueError) as exc:
            raise SmokeError("UNSAFE_PATH", str(exc), f"files[{index}].path") from exc
        if len(relative_path.parts) != 1:
            raise SmokeError("FILE_INVENTORY", "bundle files must be flat", f"files[{index}].path")
        expected_names.add(relative_path.as_posix())
        candidate = package / relative_path
        if candidate.is_symlink() or not candidate.is_file():
            raise SmokeError("FILE_TYPE", f"bundle file is missing or unsafe: {relative_path}", str(relative_path))
        payload = candidate.read_bytes()
        if item.get("size") != len(payload) or item.get("sha256") != _sha(payload):
            raise SmokeError("FILE_MISMATCH", f"bundle file changed: {relative_path}", str(relative_path))
    actual_names: set[str] = set()
    for candidate in package.rglob("*"):
        if candidate.is_symlink():
            raise SmokeError("PACKAGE_SYMLINK", "bundle contains a symlink", str(candidate))
        if candidate.is_file():
            actual_names.add(candidate.relative_to(package).as_posix())
        elif candidate.is_dir():
            raise SmokeError("FILE_SET_MISMATCH", "bundle contains an unexpected directory", str(candidate))
    if actual_names != expected_names:
        raise SmokeError(
            "FILE_SET_MISMATCH",
            f"missing={sorted(expected_names - actual_names)}; extra={sorted(actual_names - expected_names)}",
            str(package),
        )

    workflow, workflow_bytes, _workflow_path, summary = _load_workflow(package / WORKFLOW_FILE)
    request, _ = _load_canonical(package / REQUEST_FILE, REQUEST_FILE)
    bindings, _ = _load_canonical(package / BINDINGS_FILE, BINDINGS_FILE)
    tool, _ = _load_canonical(package / TOOL_FILE, TOOL_FILE)
    model, _ = _load_canonical(package / MODEL_FILE, MODEL_FILE)
    execution, _ = _load_canonical(package / EXECUTION_FILE, EXECUTION_FILE)
    if manifest["workflow"] != {
        "name": WORKFLOW_FILE,
        "raw_sha256": _sha(workflow_bytes),
        "canonical_sha256": summary["workflow_sha256"],
        "node_count": summary["node_count"],
        "class_types": summary["class_types"],
    }:
        raise SmokeError("WORKFLOW_BINDING", "bundle workflow summary is stale", "workflow")
    request_diagnostics = validate_document(Manifest(package / REQUEST_FILE, request))
    if request_diagnostics:
        first = request_diagnostics[0]
        raise SmokeError("REQUEST_VALIDATION", f"{first.code}: {first.message}", first.field)
    for value, filename, expected_id, profile_type in (
        (tool, TOOL_FILE, manifest["tool_profile_ref"], "tool"),
        (model, MODEL_FILE, manifest["model_profile_ref"], "model-configuration"),
    ):
        try:
            validate_catalog_profile(
                value,
                source_path=package / filename,
                expected_id=expected_id,
                profile_type=profile_type,
                field=profile_type,
            )
        except AdapterError as exc:
            if not (state == "reviewing" and exc.code == "PROFILE_APPROVAL"):
                raise SmokeError(exc.code, exc.message, exc.field) from exc
    try:
        validate_execution_profile(
            execution,
            workflow_sha=_sha(workflow_bytes),
            tool_id=manifest["tool_profile_ref"],
            model_id=manifest["model_profile_ref"],
        )
        _validated_bindings(request, workflow, bindings)
    except AdapterError as exc:
        raise SmokeError(exc.code, exc.message, exc.field) from exc
    if request.get("license_status") != state:
        raise SmokeError("APPROVAL_STATE", "request license state differs from the bundle", "license_status")
    for profile, field in ((tool, "tool_profile"), (model, "model_profile")):
        for state_field in ("license_evidence_state", "commercial_use_review_state", "decision_state"):
            if profile.get(state_field) != state:
                raise SmokeError("APPROVAL_STATE", f"{field}.{state_field} differs from the bundle", f"{field}.{state_field}")
    if (
        request.get("id") != manifest["request_ref"]
        or request.get("tool_id") != manifest["tool_profile_ref"]
        or request.get("model_id") != manifest["model_profile_ref"]
        or execution.get("id") != manifest["execution_profile_ref"]
        or execution.get("output_node_ids") != manifest["selection"].get("output_node_ids")
    ):
        raise SmokeError("PROFILE_BINDING", "bundle source references are stale", "manifest")
    if execution.get("expected_width") != manifest["expected_width"] or execution.get("expected_height") != manifest["expected_height"]:
        raise SmokeError("DIMENSION_BINDING", "bundle dimensions are stale", "manifest")
    config = request.get("config")
    if not isinstance(config, dict):
        raise SmokeError("REQUEST_VALIDATION", "request config is invalid", "config")
    selection = manifest.get("selection")
    if not isinstance(selection, dict):
        raise SmokeError("MANIFEST_SCHEMA", "selection is invalid", "selection")
    report = inspect_workflow(
        package / WORKFLOW_FILE,
        sampler_node=selection.get("sampler_node_id"),
        checkpoint_node=selection.get("checkpoint_node_id"),
        size_node=selection.get("size_node_id"),
        positive_node=selection.get("positive_node_id"),
        negative_node=selection.get("negative_node_id"),
        output_nodes=selection.get("output_node_ids", []),
        seed=request.get("seed"),
        steps=config.get("steps"),
        width=manifest.get("expected_width"),
        height=manifest.get("expected_height"),
        positive_prompt=config.get("positive_prompt"),
        negative_prompt=config.get("negative_prompt"),
    )
    if not report.get("ok"):
        raise SmokeError("INSPECTION_FAILED", "stored workflow selection is no longer valid", "selection")
    if report.get("selection") != selection or report.get("bindings") != bindings or report.get("config") != config:
        raise SmokeError("BUNDLE_BINDING_MISMATCH", "workflow selection or scalar bindings changed", "selection")
    for profile, field in ((tool, "tool_profile"), (model, "model_profile")):
        evidence = profile.get("evidence_references")
        if not isinstance(evidence, list) or len(evidence) != 1 or not isinstance(evidence[0], dict):
            raise SmokeError("EVIDENCE", f"{field} evidence is invalid", field)
    expected_manifest, expected_generated = _bundle_objects(
        package / WORKFLOW_FILE,
        report,
        profile_state=state,
        review_date=tool["evidence_references"][0]["retrieved_at"],
        tool_evidence_url=tool["evidence_references"][0]["source_url"],
        model_evidence_url=model["evidence_references"][0]["source_url"],
        tool_id=manifest["tool_profile_ref"],
        model_id=manifest["model_profile_ref"],
        request_id=manifest["request_ref"],
        endpoint=endpoint,
        minimum_vram_gb=tool.get("minimum_vram_gb"),
        minimum_ram_gb=tool.get("minimum_ram_gb"),
    )
    if manifest != expected_manifest:
        raise SmokeError("MANIFEST_BINDING_MISMATCH", "bundle manifest is stale", "manifest")
    for filename, expected_payload in expected_generated.items():
        if (package / filename).read_bytes() != expected_payload:
            raise SmokeError("FILE_MISMATCH", f"bundle file differs from reconstructed bytes: {filename}", filename)

    if state == "approved":
        try:
            prepare_execution(
                package / REQUEST_FILE,
                package / WORKFLOW_FILE,
                package / BINDINGS_FILE,
                package / TOOL_FILE,
                package / MODEL_FILE,
                package / EXECUTION_FILE,
                endpoint=endpoint,
            )
        except AdapterError as exc:
            raise SmokeError(exc.code, exc.message, exc.field) from exc
    return {
        "ok": True,
        "bundle": manifest,
        "file_count": len(actual_names),
        "execution_ready": state == "approved",
        "network_contacted": False,
        "external_process_started": False,
        "manifest_sha256": _sha(manifest_bytes),
    }
