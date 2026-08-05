"""Strict non-generating readiness preflight for approved ComfyUI smoke bundles."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from .adapters.base import AdapterError
from .adapters.comfyui import sanitize_loopback_endpoint
from .comfyui_preflight_http import (
    ComfyUIPreflightHttpClient,
    PreflightHttpLimits,
    encode_node_class,
)
from .comfyui_smoke_check import _load_canonical, check_bundle
from .comfyui_smoke_common import REQUEST_FILE, WORKFLOW_FILE, SmokeError, _json_bytes, _load_workflow


MAX_NODE_CLASSES = 512
MAX_CHECKPOINTS = 10_000
MAX_CHECKPOINT_CHARS = 1024
MAX_DEVICES = 32
MAX_TEXT_CHARS = 4096
ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|tmp|var|opt|mnt)/)")


def _diagnostic(exc: Exception) -> dict[str, str]:
    return {
        "code": str(getattr(exc, "code", exc.__class__.__name__.upper())),
        "message": str(getattr(exc, "message", str(exc))),
        "field": str(getattr(exc, "field", "")),
    }


def _failed(
    exc: Exception,
    *,
    network_contacted: bool,
    requested_routes: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "ok": False,
        "ready": False,
        "diagnostics": [_diagnostic(exc)],
        "requested_routes": list(requested_routes),
        "network_contacted": network_contacted,
        "external_process_started": False,
        "filesystem_mutated": False,
        "prompt_queued": False,
    }


def _clean_text(value: Any, field: str, *, maximum: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise AdapterError("SYSTEM_STATS_SCHEMA", f"{field} must be a string", field)
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > maximum
        or "\x00" in normalized
        or ABSOLUTE_PATH_RE.search(normalized)
    ):
        raise AdapterError(
            "SYSTEM_STATS_SCHEMA",
            f"{field} contains unsafe or excessive text",
            field,
        )
    return normalized


def _bounded_integer(value: Any, field: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
        raise AdapterError("SYSTEM_STATS_SCHEMA", f"{field} must be a non-negative integer", field)
    return value


def _system_summary(value: dict[str, Any]) -> dict[str, Any]:
    system = value.get("system")
    devices = value.get("devices")
    if not isinstance(system, dict):
        raise AdapterError("SYSTEM_STATS_SCHEMA", "system must be an object", "system_stats.system")
    if not isinstance(devices, list) or not devices or len(devices) > MAX_DEVICES:
        raise AdapterError(
            "SYSTEM_STATS_SCHEMA",
            f"devices must contain from 1 to {MAX_DEVICES} entries",
            "system_stats.devices",
        )
    result_devices: list[dict[str, Any]] = []
    for index, item in enumerate(devices):
        field = f"system_stats.devices[{index}]"
        if not isinstance(item, dict):
            raise AdapterError("SYSTEM_STATS_SCHEMA", "device entry must be an object", field)
        result_devices.append(
            {
                "name": _clean_text(item.get("name"), f"{field}.name", maximum=512),
                "type": _clean_text(item.get("type"), f"{field}.type", maximum=64),
                "index": _bounded_integer(item.get("index"), f"{field}.index", nullable=True),
                "vram_total": _bounded_integer(item.get("vram_total"), f"{field}.vram_total"),
                "vram_free": _bounded_integer(item.get("vram_free"), f"{field}.vram_free"),
            }
        )
    return {
        "comfyui_version": _clean_text(
            system.get("comfyui_version"),
            "system_stats.system.comfyui_version",
            maximum=256,
        ),
        "python_version": _clean_text(
            system.get("python_version"),
            "system_stats.system.python_version",
            maximum=1024,
        ),
        "pytorch_version": _clean_text(
            system.get("pytorch_version"),
            "system_stats.system.pytorch_version",
            maximum=256,
        ),
        "devices": result_devices,
    }


def _checkpoint_set(value: list[Any]) -> set[str]:
    if len(value) > MAX_CHECKPOINTS:
        raise AdapterError(
            "CHECKPOINTS_SCHEMA",
            f"checkpoint list exceeds {MAX_CHECKPOINTS} entries",
            "checkpoints",
        )
    result: set[str] = set()
    for index, item in enumerate(value):
        field = f"checkpoints[{index}]"
        if (
            not isinstance(item, str)
            or not item
            or len(item) > MAX_CHECKPOINT_CHARS
            or item != item.strip()
            or not item.isprintable()
            or "\x00" in item
        ):
            raise AdapterError(
                "CHECKPOINTS_SCHEMA",
                "checkpoint name is invalid",
                field,
            )
        if item in result:
            raise AdapterError(
                "CHECKPOINTS_SCHEMA",
                "checkpoint names must be unique",
                field,
            )
        result.add(item)
    return result


def _workflow_classes(workflow: dict[str, Any]) -> list[str]:
    classes = sorted({str(node["class_type"]) for node in workflow.values()})
    if not classes or len(classes) > MAX_NODE_CLASSES:
        raise AdapterError(
            "NODE_CLASS_LIMIT",
            f"workflow must use from 1 to {MAX_NODE_CLASSES} unique node classes",
            "workflow.class_types",
        )
    for node_class in classes:
        encode_node_class(node_class)
    return classes


def run_preflight(
    manifest_path: Path,
    bundle_root: Path,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    try:
        checked = check_bundle(manifest_path, bundle_root)
        if checked.get("execution_ready") is not True:
            raise AdapterError(
                "BUNDLE_NOT_APPROVED",
                "preflight requires an approved execution-ready smoke bundle",
                "approval_state",
            )
        bundle = checked["bundle"]
        endpoint = sanitize_loopback_endpoint(bundle["endpoint"])
        package = manifest_path.expanduser().resolve(strict=True).parent
        request, _request_bytes = _load_canonical(package / REQUEST_FILE, REQUEST_FILE)
        workflow, _workflow_bytes, _workflow_path, _summary = _load_workflow(package / WORKFLOW_FILE)
        config = request.get("config")
        checkpoint_name = config.get("checkpoint_name") if isinstance(config, dict) else None
        if (
            not isinstance(checkpoint_name, str)
            or not checkpoint_name
            or len(checkpoint_name) > MAX_CHECKPOINT_CHARS
            or checkpoint_name != checkpoint_name.strip()
            or not checkpoint_name.isprintable()
        ):
            raise AdapterError(
                "CHECKPOINT_BINDING",
                "approved request has no safe checkpoint_name binding",
                "config.checkpoint_name",
            )
        class_types = _workflow_classes(workflow)
        limits = PreflightHttpLimits(request_timeout_seconds=timeout_seconds)
    except (AdapterError, SmokeError, OSError) as exc:
        return _failed(exc, network_contacted=False)

    client = ComfyUIPreflightHttpClient(endpoint, limits)
    try:
        system = _system_summary(client.system_stats())
        checkpoints = _checkpoint_set(client.checkpoints())
        available_classes: list[str] = []
        missing_classes: list[str] = []
        for node_class in class_types:
            response = client.object_info(node_class)
            if not response:
                missing_classes.append(node_class)
                continue
            if set(response) != {node_class} or not isinstance(response.get(node_class), dict):
                raise AdapterError(
                    "OBJECT_INFO_SCHEMA",
                    "object-info response must contain exactly the requested node class",
                    node_class,
                )
            available_classes.append(node_class)
    except AdapterError as exc:
        return _failed(
            exc,
            network_contacted=bool(client.requested_routes),
            requested_routes=client.requested_routes,
        )

    diagnostics: list[dict[str, str]] = []
    checkpoint_available = checkpoint_name in checkpoints
    if not checkpoint_available:
        diagnostics.append(
            {
                "code": "CHECKPOINT_MISSING",
                "message": f"required checkpoint is not installed: {checkpoint_name}",
                "field": "config.checkpoint_name",
            }
        )
    if missing_classes:
        diagnostics.append(
            {
                "code": "NODE_CLASSES_MISSING",
                "message": "required node classes are unavailable: " + ", ".join(missing_classes),
                "field": "workflow.class_types",
            }
        )
    diagnostics.sort(key=lambda item: (item["code"], item["field"], item["message"]))
    ready = not diagnostics
    return {
        "ok": ready,
        "ready": ready,
        "bundle_id": bundle["id"],
        "endpoint": endpoint,
        "system": system,
        "checkpoint": {
            "required": checkpoint_name,
            "available": checkpoint_available,
            "installed_count": len(checkpoints),
        },
        "workflow": {
            "required_node_classes": class_types,
            "available_node_classes": available_classes,
            "missing_node_classes": missing_classes,
        },
        "diagnostics": diagnostics,
        "requested_routes": list(client.requested_routes),
        "network_contacted": True,
        "external_process_started": False,
        "filesystem_mutated": False,
        "prompt_queued": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ai_illustration.comfyui_preflight")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("manifest", type=Path)
    run.add_argument("--bundle-root", type=Path, required=True)
    run.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run_preflight(
        args.manifest,
        args.bundle_root,
        timeout_seconds=args.timeout_seconds,
    )
    sys.stdout.buffer.write(_json_bytes(result))
    print(
        f"ComfyUI preflight: {'ready' if result.get('ready') else 'failed'}",
        file=sys.stderr,
    )
    return 0 if result.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
