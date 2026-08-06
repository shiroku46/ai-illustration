"""Exact local-artifact and non-generating ComfyUI benchmark readiness checks."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable, Sequence

from .adapters.base import AdapterError
from .adapters.comfyui import sanitize_loopback_endpoint
from .comfyui_preflight_http import (
    ComfyUIPreflightHttpClient,
    PreflightHttpLimits,
)
from .model_install_manifest import load_manifest, validate_manifest
from .naming import safe_relative_path

MAX_ARTIFACTS = 64
MAX_NODE_CLASSES = 128
MAX_CHOICES = 20_000
MAX_CHOICE_CHARS = 1024
HASH_CHUNK_BYTES = 8 * 1024 * 1024
SAFE_TEXT_RE = re.compile(r"^[^\x00\r\n]{1,1024}$")
LOADER_BINDINGS = {
    "checkpoint": ("CheckpointLoaderSimple", "ckpt_name"),
    "diffusion-model": ("UNETLoader", "unet_name"),
    "text-encoder": ("CLIPLoader", "clip_name"),
    "vae": ("VAELoader", "vae_name"),
}


def _diag(code: str, message: str, field: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "field": field}


def _sorted(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique = {
        (item.get("field", ""), item.get("code", ""), item.get("message", "")): {
            "code": item.get("code", ""),
            "message": item.get("message", ""),
            "field": item.get("field", ""),
        }
        for item in values
    }
    return [unique[key] for key in sorted(unique)]


def _resolve_directory(path: Path, field: str) -> tuple[Path | None, list[dict[str, str]]]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        return None, [_diag("ROOT_SYMLINK", "root must not be a symlink", field)]
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        return None, [_diag("ROOT_MISSING", str(exc), field)]
    if not resolved.is_dir():
        return None, [_diag("ROOT_TYPE", "root must be a directory", field)]
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_local_artifacts(
    manifest: dict[str, Any],
    *,
    comfyui_root: Path,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    root, diagnostics = _resolve_directory(comfyui_root, "comfyui_root")
    if root is None:
        return diagnostics, []
    models = manifest.get("models")
    if not isinstance(models, list):
        return [_diag("MODELS", "manifest models must be a list", "models")], []
    artifacts = [
        (str(model.get("family", "")), artifact)
        for model in models
        if isinstance(model, dict)
        for artifact in model.get("artifacts", [])
        if isinstance(artifact, dict)
    ]
    if not artifacts or len(artifacts) > MAX_ARTIFACTS:
        return [
            _diag(
                "ARTIFACT_COUNT",
                f"manifest must contain 1..{MAX_ARTIFACTS} artifacts",
                "models",
            )
        ], []

    results: list[dict[str, Any]] = []
    for family, artifact in artifacts:
        artifact_id = str(artifact.get("id", ""))
        field = f"artifacts.{artifact_id or 'unknown'}"
        destination = artifact.get("destination")
        filename = artifact.get("filename")
        expected_size = artifact.get("size_bytes")
        expected_sha = artifact.get("sha256")
        try:
            relative_dir = safe_relative_path(str(destination))
            if not relative_dir.as_posix().startswith("models/"):
                raise ValueError("destination must be under models/")
            if (
                not isinstance(filename, str)
                or not filename
                or Path(filename).name != filename
                or "/" in filename
                or "\\" in filename
            ):
                raise ValueError("filename must be one safe basename")
            lexical = root.joinpath(*relative_dir.parts, filename)
            if _has_symlink(lexical, root):
                raise ValueError("artifact path contains a symlink")
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file() or resolved.is_symlink():
                raise ValueError("artifact must be a regular non-symlink file")
            before = resolved.stat()
            actual_sha = _sha256(resolved)
            after = resolved.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise ValueError("artifact changed while being verified")
            actual_size = after.st_size
        except (OSError, ValueError) as exc:
            diagnostics.append(_diag("ARTIFACT_UNAVAILABLE", str(exc), field))
            results.append(
                {
                    "family": family,
                    "artifact_id": artifact_id,
                    "path": str(
                        root.joinpath(
                            *Path(str(destination)).parts,
                            str(filename),
                        )
                    ),
                    "available": False,
                    "size_ok": False,
                    "sha256_ok": False,
                }
            )
            continue
        size_ok = (
            isinstance(expected_size, int)
            and not isinstance(expected_size, bool)
            and actual_size == expected_size
        )
        sha_ok = isinstance(expected_sha, str) and actual_sha == expected_sha
        if not size_ok:
            diagnostics.append(
                _diag(
                    "ARTIFACT_SIZE",
                    f"expected {expected_size}, found {actual_size}",
                    field,
                )
            )
        if not sha_ok:
            diagnostics.append(
                _diag(
                    "ARTIFACT_SHA256",
                    f"expected {expected_sha}, found {actual_sha}",
                    field,
                )
            )
        results.append(
            {
                "family": family,
                "artifact_id": artifact_id,
                "path": str(resolved),
                "available": True,
                "size_bytes": actual_size,
                "sha256": actual_sha,
                "size_ok": size_ok,
                "sha256_ok": sha_ok,
            }
        )
    return _sorted(diagnostics), sorted(
        results, key=lambda item: (item["family"], item["artifact_id"])
    )


def required_runtime_contract(
    manifest: dict[str, Any],
    model_summaries: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], list[str], list[dict[str, str]]]:
    diagnostics: list[dict[str, str]] = []
    classes: set[str] = set()
    for summary in model_summaries:
        workflow = summary.get("workflow_summary")
        if not isinstance(workflow, dict):
            diagnostics.append(
                _diag(
                    "WORKFLOW_SUMMARY",
                    "validated workflow summary is missing",
                    str(summary.get("family", "")),
                )
            )
            continue
        class_types = workflow.get("class_types")
        if not isinstance(class_types, list):
            diagnostics.append(
                _diag(
                    "WORKFLOW_CLASSES",
                    "workflow class list is missing",
                    str(summary.get("family", "")),
                )
            )
            continue
        classes.update(str(item) for item in class_types)

    requirements: list[dict[str, str]] = []
    models = manifest.get("models")
    if isinstance(models, list):
        for model in models:
            if not isinstance(model, dict):
                continue
            family = str(model.get("family", ""))
            for artifact in model.get("artifacts", []):
                if not isinstance(artifact, dict):
                    continue
                component = artifact.get("component")
                binding = LOADER_BINDINGS.get(str(component))
                if binding is None:
                    diagnostics.append(
                        _diag(
                            "LOADER_BINDING",
                            f"unsupported component: {component}",
                            family,
                        )
                    )
                    continue
                node_class, input_name = binding
                requirements.append(
                    {
                        "family": family,
                        "component": str(component),
                        "node_class": node_class,
                        "input_name": input_name,
                        "filename": str(artifact.get("filename", "")),
                    }
                )
                classes.add(node_class)

    if not classes or len(classes) > MAX_NODE_CLASSES:
        diagnostics.append(
            _diag(
                "NODE_CLASS_COUNT",
                f"required node classes must contain 1..{MAX_NODE_CLASSES} values",
                "workflow.class_types",
            )
        )
    return (
        _sorted(diagnostics),
        sorted(classes),
        sorted(
            requirements,
            key=lambda item: (
                item["family"],
                item["component"],
                item["filename"],
            ),
        ),
    )


def _safe_text(value: Any, field: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or not SAFE_TEXT_RE.fullmatch(value)
    ):
        raise AdapterError("SYSTEM_STATS_SCHEMA", "unsafe text value", field)
    return " ".join(value.split())


def summarize_system(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdapterError(
            "SYSTEM_STATS_SCHEMA",
            "system_stats root must be an object",
            "system_stats",
        )
    system = value.get("system")
    devices = value.get("devices")
    if not isinstance(system, dict):
        raise AdapterError(
            "SYSTEM_STATS_SCHEMA",
            "system must be an object",
            "system_stats.system",
        )
    if not isinstance(devices, list) or not 1 <= len(devices) <= 32:
        raise AdapterError(
            "SYSTEM_STATS_SCHEMA",
            "devices must contain 1..32 entries",
            "system_stats.devices",
        )
    result_devices: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        field = f"system_stats.devices[{index}]"
        if not isinstance(device, dict):
            raise AdapterError(
                "SYSTEM_STATS_SCHEMA",
                "device must be an object",
                field,
            )
        total = device.get("vram_total")
        free = device.get("vram_free")
        for name, amount in (("vram_total", total), ("vram_free", free)):
            if (
                isinstance(amount, bool)
                or not isinstance(amount, int)
                or not 0 <= amount <= 2**63 - 1
            ):
                raise AdapterError(
                    "SYSTEM_STATS_SCHEMA",
                    f"{name} must be a non-negative integer",
                    f"{field}.{name}",
                )
        result_devices.append(
            {
                "name": _safe_text(device.get("name"), f"{field}.name", 512),
                "type": _safe_text(device.get("type"), f"{field}.type", 64),
                "vram_total": total,
                "vram_free": free,
            }
        )
    return {
        "comfyui_version": _safe_text(
            system.get("comfyui_version"),
            "system_stats.system.comfyui_version",
            256,
        ),
        "python_version": _safe_text(
            system.get("python_version"),
            "system_stats.system.python_version",
        ),
        "pytorch_version": _safe_text(
            system.get("pytorch_version"),
            "system_stats.system.pytorch_version",
            256,
        ),
        "devices": result_devices,
    }


def _choice_values(node_info: Any, input_name: str, field: str) -> set[str]:
    if not isinstance(node_info, dict):
        raise AdapterError(
            "OBJECT_INFO_SCHEMA",
            "node info must be an object",
            field,
        )
    inputs = node_info.get("input")
    required = inputs.get("required") if isinstance(inputs, dict) else None
    spec = required.get(input_name) if isinstance(required, dict) else None
    choices = spec[0] if isinstance(spec, list) and spec else None
    if (
        not isinstance(choices, list)
        or len(choices) > MAX_CHOICES
        or any(
            not isinstance(item, str)
            or not item
            or len(item) > MAX_CHOICE_CHARS
            or item != item.strip()
            or not item.isprintable()
            for item in choices
        )
    ):
        raise AdapterError(
            "OBJECT_INFO_CHOICES",
            f"{input_name} choices are missing or invalid",
            field,
        )
    if len(choices) != len(set(choices)):
        raise AdapterError(
            "OBJECT_INFO_CHOICES",
            f"{input_name} choices must be unique",
            field,
        )
    return set(choices)


def evaluate_runtime(
    client: Any,
    *,
    required_classes: list[str],
    loader_requirements: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    diagnostics: list[dict[str, str]] = []
    system = summarize_system(client.system_stats())
    node_info: dict[str, dict[str, Any]] = {}
    available_classes: list[str] = []
    missing_classes: list[str] = []
    for node_class in required_classes:
        response = client.object_info(node_class)
        if not response:
            missing_classes.append(node_class)
            continue
        if (
            not isinstance(response, dict)
            or set(response) != {node_class}
            or not isinstance(response.get(node_class), dict)
        ):
            raise AdapterError(
                "OBJECT_INFO_SCHEMA",
                "response must contain exactly the requested node class",
                node_class,
            )
        node_info[node_class] = response[node_class]
        available_classes.append(node_class)
    if missing_classes:
        diagnostics.append(
            _diag(
                "NODE_CLASSES_MISSING",
                ", ".join(missing_classes),
                "workflow.class_types",
            )
        )

    loader_results: list[dict[str, Any]] = []
    for requirement in loader_requirements:
        node_class = requirement["node_class"]
        input_name = requirement["input_name"]
        filename = requirement["filename"]
        available = False
        choices_count = 0
        if node_class in node_info:
            choices = _choice_values(
                node_info[node_class],
                input_name,
                f"{node_class}.{input_name}",
            )
            choices_count = len(choices)
            available = filename in choices
        if not available:
            diagnostics.append(
                _diag(
                    "MODEL_CHOICE_MISSING",
                    f"{filename} is not available in {node_class}.{input_name}",
                    requirement["family"],
                )
            )
        loader_results.append(
            {
                **requirement,
                "available": available,
                "choices_count": choices_count,
            }
        )
    return _sorted(diagnostics), {
        "system": system,
        "required_node_classes": required_classes,
        "available_node_classes": available_classes,
        "missing_node_classes": missing_classes,
        "loader_requirements": loader_results,
    }


def _base_result(
    *,
    ready: bool,
    diagnostics: list[dict[str, str]],
    manifest: dict[str, Any] | None = None,
    models: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    runtime: dict[str, Any] | None = None,
    requested_routes: Sequence[str] = (),
    network_contacted: bool = False,
) -> dict[str, Any]:
    return {
        "ok": ready,
        "ready": ready,
        "manifest_id": manifest.get("id") if isinstance(manifest, dict) else None,
        "manifest_version": (
            manifest.get("version") if isinstance(manifest, dict) else None
        ),
        "models": models or [],
        "artifacts": artifacts or [],
        "runtime": runtime,
        "diagnostics": _sorted(diagnostics),
        "requested_routes": list(requested_routes),
        "network_contacted": network_contacted,
        "filesystem_mutated": False,
        "external_process_started": False,
        "prompt_queued": False,
    }


def run_offline_preflight(
    manifest_path: Path,
    *,
    workspace_root: Path,
    comfyui_root: Path,
) -> dict[str, Any]:
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return _base_result(
            ready=False,
            diagnostics=[_diag("MANIFEST_READ", str(exc), "manifest")],
        )
    manifest_diagnostics, models = validate_manifest(
        manifest,
        workspace_root=workspace_root,
    )
    if manifest_diagnostics:
        return _base_result(
            ready=False,
            diagnostics=manifest_diagnostics,
            manifest=manifest,
            models=models,
        )
    artifact_diagnostics, artifacts = verify_local_artifacts(
        manifest,
        comfyui_root=comfyui_root,
    )
    return _base_result(
        ready=not artifact_diagnostics,
        diagnostics=artifact_diagnostics,
        manifest=manifest,
        models=models,
        artifacts=artifacts,
    )


def run_runtime_preflight(
    manifest_path: Path,
    *,
    workspace_root: Path,
    comfyui_root: Path,
    endpoint: str,
    timeout_seconds: float = 10.0,
    client_factory: Callable[..., Any] = ComfyUIPreflightHttpClient,
) -> dict[str, Any]:
    offline = run_offline_preflight(
        manifest_path,
        workspace_root=workspace_root,
        comfyui_root=comfyui_root,
    )
    if not offline["ready"]:
        return offline
    manifest = load_manifest(manifest_path)
    contract_diagnostics, classes, requirements = required_runtime_contract(
        manifest,
        offline["models"],
    )
    if contract_diagnostics:
        return _base_result(
            ready=False,
            diagnostics=contract_diagnostics,
            manifest=manifest,
            models=offline["models"],
            artifacts=offline["artifacts"],
        )
    try:
        sanitized = sanitize_loopback_endpoint(endpoint)
        client = client_factory(
            sanitized,
            PreflightHttpLimits(request_timeout_seconds=timeout_seconds),
        )
        runtime_diagnostics, runtime = evaluate_runtime(
            client,
            required_classes=classes,
            loader_requirements=requirements,
        )
    except (AdapterError, OSError, ValueError) as exc:
        routes = list(getattr(locals().get("client"), "requested_routes", []))
        return _base_result(
            ready=False,
            diagnostics=[
                _diag(
                    str(getattr(exc, "code", exc.__class__.__name__.upper())),
                    str(getattr(exc, "message", str(exc))),
                    str(getattr(exc, "field", "")),
                )
            ],
            manifest=manifest,
            models=offline["models"],
            artifacts=offline["artifacts"],
            requested_routes=routes,
            network_contacted=bool(routes),
        )
    routes = list(getattr(client, "requested_routes", []))
    return _base_result(
        ready=not runtime_diagnostics,
        diagnostics=runtime_diagnostics,
        manifest=manifest,
        models=offline["models"],
        artifacts=offline["artifacts"],
        runtime={"endpoint": sanitized, **runtime},
        requested_routes=routes,
        network_contacted=bool(routes),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_illustration.benchmark_readiness"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("offline-check")
    runtime = sub.add_parser("runtime-check")
    for command in (offline, runtime):
        command.add_argument("manifest", type=Path)
        command.add_argument("--workspace-root", type=Path, required=True)
        command.add_argument("--comfyui-root", type=Path, required=True)
    runtime.add_argument("--endpoint", required=True)
    runtime.add_argument("--timeout-seconds", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "offline-check":
        result = run_offline_preflight(
            args.manifest,
            workspace_root=args.workspace_root,
            comfyui_root=args.comfyui_root,
        )
    else:
        result = run_runtime_preflight(
            args.manifest,
            workspace_root=args.workspace_root,
            comfyui_root=args.comfyui_root,
            endpoint=args.endpoint,
            timeout_seconds=args.timeout_seconds,
        )
    sys.stdout.write(
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
