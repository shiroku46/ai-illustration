"""Deterministic ComfyUI execution-plan construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AdapterError
from .comfyui import _validated_bindings as bind_workflow, canonical_json_bytes, sanitize_loopback_endpoint, validate_workflow
from .comfyui_execution_common import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    integer,
    sha256,
    source_binding,
    source_file,
    token,
    validate_catalog_profile,
    validate_execution_profile,
)
from ..models import Manifest
from ..naming import content_identifier
from ..validation import validate_document


def prepare_execution(
    request_path: Path,
    workflow_path: Path,
    bindings_path: Path,
    tool_profile_path: Path,
    model_profile_path: Path,
    execution_profile_path: Path,
    *,
    endpoint: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], set[Path]]:
    request, request_bytes, request_file = source_file(request_path, "request", canonical_required=True)
    workflow, workflow_bytes, workflow_file = source_file(workflow_path, "workflow", canonical_required=False)
    bindings, bindings_bytes, bindings_file = source_file(bindings_path, "bindings", canonical_required=False)
    tool, tool_bytes, tool_file = source_file(tool_profile_path, "tool_profile", canonical_required=True)
    model, model_bytes, model_file = source_file(model_profile_path, "model_profile", canonical_required=True)
    execution, execution_bytes, execution_file = source_file(execution_profile_path, "execution_profile", canonical_required=True)
    if request.get("kind") != "generation-request" or request.get("schema_version") != "1.0":
        raise AdapterError("INVALID_REQUEST", "request must be a canonical generation-request", "request")
    request_diagnostics = validate_document(Manifest(request_file, request))
    if request_diagnostics:
        first = request_diagnostics[0]
        raise AdapterError("REQUEST_VALIDATION", f"{first.code}: {first.message}", first.field or "request")
    request_id = token(request.get("id"), "request.id")
    tool_id = token(request.get("tool_id"), "request.tool_id")
    model_id = token(request.get("model_id"), "request.model_id")
    integer(request.get("seed"), "request.seed", 0, 2**63 - 1)
    if request.get("license_status") != "approved":
        raise AdapterError("REQUEST_LICENSE", "request license_status must be approved", "request.license_status")
    validate_catalog_profile(tool, source_path=tool_file, expected_id=tool_id, profile_type="tool", field="tool_profile")
    validate_catalog_profile(model, source_path=model_file, expected_id=model_id, profile_type="model-configuration", field="model_profile")
    workflow_summary = validate_workflow(workflow)
    validate_execution_profile(execution, workflow_sha=sha256(workflow_bytes), tool_id=tool_id, model_id=model_id)
    bound_workflow, bound_values = bind_workflow(request, workflow, bindings)
    bound_sha = sha256(canonical_json_bytes(bound_workflow))
    endpoint_value = sanitize_loopback_endpoint(endpoint)
    core = {
        "kind": "comfyui-execution-plan",
        "schema_version": "1.0",
        "adapter_id": ADAPTER_ID,
        "adapter_version": ADAPTER_VERSION,
        "endpoint": endpoint_value,
        "request": source_binding(request_id, request_file, request_bytes),
        "workflow": {
            "name": workflow_file.name,
            "sha256": sha256(workflow_bytes),
            "canonical_sha256": workflow_summary["workflow_sha256"],
            "bound_sha256": bound_sha,
        },
        "bindings": {"name": bindings_file.name, "sha256": sha256(bindings_bytes)},
        "tool_profile": source_binding(tool_id, tool_file, tool_bytes),
        "model_profile": source_binding(model_id, model_file, model_bytes),
        "execution_profile": source_binding(execution["id"], execution_file, execution_bytes),
        "seed": request["seed"],
        "output_node_ids": execution["output_node_ids"],
        "expected_width": execution["expected_width"],
        "expected_height": execution["expected_height"],
        "limits": execution["limits"],
        "bound_values": dict(sorted(bound_values.items())),
        "network_policy": {
            "allowed_methods": ["GET", "POST"],
            "allowed_paths": ["/history/{prompt_id}", "/prompt", "/view"],
            "loopback_only": True,
            "proxies": False,
            "redirects": False,
            "credentials": False,
            "cookies": False,
        },
    }
    plan = {"id": content_identifier("comfyui-execution-plan", core, 20), **core}
    return plan, bound_workflow, execution, {request_file, workflow_file, bindings_file, tool_file, model_file, execution_file}
