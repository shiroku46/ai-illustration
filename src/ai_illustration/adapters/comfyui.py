"""Fixture-only ComfyUI API workflow adapter.

This module validates and plans localhost work. It deliberately contains no
socket, HTTP client, subprocess, model-loading, or image-generation code.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .base import AdapterError, ExecutionPlan

ADAPTER_ID = "comfyui-local-api"
ADAPTER_VERSION = "v001"
SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(api[_-]?key|authorization|bearer|cookie|credential|password|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(r"^(?:bearer\s+|sk-[A-Za-z0-9]|gh[opusr]_[A-Za-z0-9])", re.IGNORECASE)
NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise AdapterError("INVALID_JSON_ROOT", "JSON root must be an object", str(path))
    return value


def sanitize_loopback_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise AdapterError("UNSAFE_ENDPOINT", "endpoint must be a non-empty string", "endpoint")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http":
        raise AdapterError("UNSAFE_ENDPOINT", "only the http scheme is allowed", "endpoint")
    if parsed.username is not None or parsed.password is not None:
        raise AdapterError("UNSAFE_ENDPOINT", "credentials are forbidden", "endpoint")
    if parsed.query or parsed.fragment:
        raise AdapterError("UNSAFE_ENDPOINT", "query strings and fragments are forbidden", "endpoint")
    if parsed.path not in {"", "/"}:
        if ".." in PurePosixPath(parsed.path).parts:
            raise AdapterError("UNSAFE_ENDPOINT", "path traversal is forbidden", "endpoint")
        raise AdapterError("UNSAFE_ENDPOINT", "endpoint paths are not supported", "endpoint")
    host = parsed.hostname
    if host is None:
        raise AdapterError("UNSAFE_ENDPOINT", "endpoint host is required", "endpoint")
    allowed = host.lower() == "localhost"
    if not allowed:
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
    if not allowed:
        raise AdapterError("UNSAFE_ENDPOINT", "endpoint must use a loopback host", "endpoint")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AdapterError("UNSAFE_ENDPOINT", "endpoint port is invalid", "endpoint") from exc
    if port is not None and not 1 <= port <= 65535:
        raise AdapterError("UNSAFE_ENDPOINT", "endpoint port is invalid", "endpoint")
    hostname = "localhost" if host.lower() == "localhost" else str(ipaddress.ip_address(host))
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc += f":{port}"
    return urlunsplit(("http", netloc, "", "", ""))


def _scan_for_secrets(value: Any, field: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            child = f"{field}.{key_text}" if field else key_text
            if SECRET_KEY_RE.search(key_text):
                raise AdapterError("SECRET_LIKE_DATA", "secret-like keys are forbidden", child)
            _scan_for_secrets(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_for_secrets(item, f"{field}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value.strip()):
        raise AdapterError("SECRET_LIKE_DATA", "secret-like values are forbidden", field)


def validate_workflow(workflow: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(workflow, Mapping) or not workflow:
        raise AdapterError("INVALID_WORKFLOW", "workflow must be a non-empty object", "workflow")
    classes: dict[str, int] = {}
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
            raise AdapterError("INVALID_NODE_ID", "node IDs must be stable ASCII tokens", str(node_id))
        if not isinstance(node, Mapping):
            raise AdapterError("INVALID_NODE", "each workflow node must be an object", node_id)
        if set(node) - {"class_type", "inputs", "_meta"}:
            raise AdapterError("UNKNOWN_WORKFLOW_FIELD", "unsupported workflow node field", node_id)
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(class_type, str) or not class_type.strip():
            raise AdapterError("INVALID_CLASS_TYPE", "class_type is required", f"{node_id}.class_type")
        if not isinstance(inputs, Mapping):
            raise AdapterError("INVALID_INPUTS", "inputs must be an object", f"{node_id}.inputs")
        _scan_for_secrets(inputs, f"{node_id}.inputs")
        classes[class_type] = classes.get(class_type, 0) + 1
    return {
        "node_count": len(workflow),
        "class_types": dict(sorted(classes.items())),
        "workflow_sha256": hashlib.sha256(canonical_json_bytes(workflow)).hexdigest(),
    }


def _source_value(request: Mapping[str, Any], source: str) -> Any:
    if not SOURCE_RE.fullmatch(source):
        raise AdapterError("INVALID_BINDING_SOURCE", "binding source path is invalid", source)
    value: Any = request
    for part in source.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise AdapterError("MISSING_BINDING_SOURCE", f"request field {source!r} is missing", source)
        value = value[part]
    return value


def _validated_bindings(
    request: Mapping[str, Any], workflow: Mapping[str, Any], bindings: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(bindings, Mapping) or not bindings:
        raise AdapterError("INVALID_BINDINGS", "bindings must be a non-empty object", "bindings")
    _scan_for_secrets(bindings, "bindings")
    bound_values: dict[str, Any] = {}
    workflow_copy = copy.deepcopy(dict(workflow))
    for name in sorted(bindings):
        spec = bindings[name]
        if not isinstance(name, str) or not NODE_ID_RE.fullmatch(name):
            raise AdapterError("INVALID_BINDING_NAME", "binding names must be stable ASCII tokens", str(name))
        if not isinstance(spec, Mapping) or set(spec) != {"node_id", "input", "source"}:
            raise AdapterError("INVALID_BINDING", "binding requires node_id, input, and source only", name)
        node_id = spec["node_id"]
        input_name = spec["input"]
        source = spec["source"]
        if not isinstance(node_id, str) or node_id not in workflow_copy:
            raise AdapterError("UNKNOWN_BINDING_NODE", "binding node does not exist", f"{name}.node_id")
        if not isinstance(input_name, str) or not NODE_ID_RE.fullmatch(input_name):
            raise AdapterError("INVALID_BINDING_INPUT", "binding input name is invalid", f"{name}.input")
        node_inputs = workflow_copy[node_id].get("inputs")
        if not isinstance(node_inputs, dict) or input_name not in node_inputs:
            raise AdapterError("UNKNOWN_BINDING_INPUT", "binding input does not exist", f"{name}.input")
        if not isinstance(source, str):
            raise AdapterError("INVALID_BINDING_SOURCE", "source must be a string", f"{name}.source")
        value = _source_value(request, source)
        _scan_for_secrets(value, source)
        if not isinstance(value, (str, int, float, bool)) or value is None:
            raise AdapterError("UNSUPPORTED_BINDING_VALUE", "binding values must be JSON scalars", source)
        node_inputs[input_name] = value
        bound_values[name] = value
    return workflow_copy, bound_values


class ComfyUIAdapter:
    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION

    def check_workflow(self, workflow: Mapping[str, Any]) -> dict[str, Any]:
        return validate_workflow(workflow)

    def plan(
        self,
        request: Mapping[str, Any],
        workflow: Mapping[str, Any],
        bindings: Mapping[str, Any],
        *,
        endpoint: str = "http://127.0.0.1:8188",
    ) -> ExecutionPlan:
        if request.get("kind") != "generation-request":
            raise AdapterError("INVALID_REQUEST", "request must be a generation-request manifest", "kind")
        if request.get("tool_id") not in {"fixture-tool", self.adapter_id}:
            raise AdapterError("UNSUPPORTED_ADAPTER", "request tool_id is not supported", "tool_id")
        request_id = request.get("id")
        if not isinstance(request_id, str) or not NODE_ID_RE.fullmatch(request_id):
            raise AdapterError("INVALID_REQUEST_ID", "request id is invalid", "id")
        _scan_for_secrets(request, "request")
        endpoint_value = sanitize_loopback_endpoint(endpoint)
        workflow_summary = validate_workflow(workflow)
        bound_workflow, bound_values = _validated_bindings(request, workflow, bindings)
        bound_sha = hashlib.sha256(canonical_json_bytes(bound_workflow)).hexdigest()
        reasons: list[str] = []
        model_id = request.get("model_id")
        if not isinstance(model_id, str) or model_id in {"", "unknown", "unresolved", "none"}:
            reasons.append("model-identifier-unresolved")
        if request.get("license_status") != "approved":
            reasons.append("model-license-not-approved")
        return ExecutionPlan(
            adapter_id=self.adapter_id,
            adapter_version=self.adapter_version,
            endpoint=endpoint_value,
            workflow_sha256=workflow_summary["workflow_sha256"],
            request_id=request_id,
            output_directory=f"outputs/{request_id}/{bound_sha[:12]}",
            bindings=bound_values,
            payload_summary={
                "binding_names": sorted(bound_values),
                "bound_workflow_sha256": bound_sha,
                "node_count": workflow_summary["node_count"],
            },
            dry_run=True,
            executable_ready=not reasons,
            readiness_reasons=tuple(sorted(reasons)),
        )

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        raise AdapterError("EXECUTION_DISABLED", "adapter execution is not authorized in this phase")
