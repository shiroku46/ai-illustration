"""Explicit, bounded loopback ComfyUI execution and offline checking."""

from __future__ import annotations

from pathlib import Path
import shutil
import time
from typing import Any, Callable

from .base import AdapterError
from .comfyui_execution_common import MANIFEST_FILE, PLAN_FILE, json_bytes, output_root
from .comfyui_execution_package import (
    candidate_files,
    check_execution_package,
    execution_manifest,
    history_outputs,
)
from .comfyui_execution_plan import prepare_execution
from .comfyui_http import ComfyUIHttpClient, HttpLimits
from ..naming import safe_relative_path


def _read_existing(destination: Path, plan: dict[str, Any]) -> dict[str, Any] | None:
    if not destination.exists():
        return None
    return check_execution_package(destination / MANIFEST_FILE, plan)


def run_comfyui_execution(
    request_path: Path,
    workflow_path: Path,
    bindings_path: Path,
    tool_profile_path: Path,
    model_profile_path: Path,
    execution_profile_path: Path,
    output_root_path: Path,
    *,
    endpoint: str,
    execute: bool,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    client_factory: Callable[[str, HttpLimits], ComfyUIHttpClient] = ComfyUIHttpClient,
) -> dict[str, Any]:
    if execute is not True:
        raise AdapterError("EXECUTE_ACKNOWLEDGEMENT", "adapter-run requires --execute", "execute")
    plan, bound_workflow, execution, sources = prepare_execution(
        request_path,
        workflow_path,
        bindings_path,
        tool_profile_path,
        model_profile_path,
        execution_profile_path,
        endpoint=endpoint,
    )
    plan_bytes = json_bytes(plan)
    root = output_root(output_root_path, sources)
    destination = root / plan["id"]
    existing = _read_existing(destination, plan)
    if existing is not None:
        return {
            "ok": True,
            "written": False,
            "reused": True,
            "package_path": plan["id"],
            "execution": existing,
        }

    staging = root / f".{plan['id']}.tmp"
    if staging.exists() or staging.is_symlink():
        raise AdapterError("STAGING_CONFLICT", "staging path already exists", "output_root")
    root.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    try:
        (staging / PLAN_FILE).write_bytes(plan_bytes)
        limits = execution["limits"]
        client = client_factory(
            plan["endpoint"],
            HttpLimits(
                queue_response_bytes=limits["max_queue_response_bytes"],
                history_response_bytes=limits["max_history_response_bytes"],
                png_bytes=limits["max_png_bytes"],
                request_timeout_seconds=limits["request_timeout_seconds"],
            ),
        )
        deadline = clock() + limits["overall_timeout_seconds"]

        def remaining_timeout() -> float:
            remaining = deadline - clock()
            if remaining <= 0:
                raise AdapterError(
                    "OVERALL_TIMEOUT",
                    "ComfyUI execution exceeded the overall timeout",
                    "overall_timeout_seconds",
                )
            return min(float(limits["request_timeout_seconds"]), remaining)

        prompt_id = client.queue_prompt(bound_workflow, timeout_seconds=remaining_timeout())
        descriptors = None
        while descriptors is None:
            if clock() >= deadline:
                raise AdapterError(
                    "OVERALL_TIMEOUT",
                    "ComfyUI execution exceeded the overall timeout",
                    "overall_timeout_seconds",
                )
            history = client.history(prompt_id, timeout_seconds=remaining_timeout())
            descriptors = history_outputs(
                history,
                prompt_id,
                plan["output_node_ids"],
                limits["max_images"],
            )
            if descriptors is None:
                delay = min(limits["poll_interval_ms"] / 1000, max(0.0, deadline - clock()))
                if delay <= 0:
                    raise AdapterError(
                        "OVERALL_TIMEOUT",
                        "ComfyUI execution exceeded the overall timeout",
                        "overall_timeout_seconds",
                    )
                sleeper(delay)

        generated: dict[str, bytes] = {PLAN_FILE: plan_bytes}
        candidates: list[dict[str, Any]] = []
        total_png = 0
        for index, descriptor in enumerate(descriptors):
            png = client.image(
                descriptor["filename"],
                descriptor["subfolder"],
                timeout_seconds=remaining_timeout(),
            )
            total_png += len(png)
            if total_png > limits["max_total_png_bytes"]:
                raise AdapterError(
                    "TOTAL_IMAGE_BYTES",
                    "downloaded PNG bytes exceed configured total",
                    "max_total_png_bytes",
                )
            sidecar, sidecar_bytes, png_path, png_bytes = candidate_files(
                plan,
                prompt_id,
                descriptor,
                png,
                index,
            )
            sidecar_path = f"candidates/{sidecar['id']}.json"
            generated[png_path] = png_bytes
            generated[sidecar_path] = sidecar_bytes
            candidates.append(
                {
                    "id": sidecar["id"],
                    "path": png_path,
                    "sidecar_path": sidecar_path,
                    "sha256": sidecar["sha256"],
                    "size": len(png_bytes),
                    "width": sidecar["width"],
                    "height": sidecar["height"],
                    "output_node_id": descriptor["node_id"],
                    "server_filename": descriptor["filename"],
                    "server_subfolder": descriptor["subfolder"],
                    "index": index,
                }
            )

        manifest = execution_manifest(plan, plan_bytes, prompt_id, candidates, generated)
        generated[MANIFEST_FILE] = json_bytes(manifest)
        for relative, payload in generated.items():
            target = staging.joinpath(*safe_relative_path(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
                    raise AdapterError("STAGING_CONFLICT", f"staging file differs: {relative}", relative)
            else:
                target.write_bytes(payload)

        expected_files = set(generated)
        actual_files: set[str] = set()
        for path in staging.rglob("*"):
            if path.is_symlink():
                raise AdapterError("STAGING_SYMLINK", "staging package contains a symlink", str(path))
            if path.is_file():
                actual_files.add(path.relative_to(staging).as_posix())
        if actual_files != expected_files:
            raise AdapterError(
                "STAGING_FILE_SET",
                f"missing={sorted(expected_files-actual_files)} extra={sorted(actual_files-expected_files)}",
                str(staging),
            )
        staging.replace(destination)
        return {
            "ok": True,
            "written": True,
            "reused": False,
            "package_path": plan["id"],
            "execution": manifest,
        }
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise


def check_comfyui_execution(
    manifest_path: Path,
    output_root_path: Path,
    request_path: Path,
    workflow_path: Path,
    bindings_path: Path,
    tool_profile_path: Path,
    model_profile_path: Path,
    execution_profile_path: Path,
    *,
    endpoint: str,
) -> dict[str, Any]:
    plan, _bound_workflow, _execution, sources = prepare_execution(
        request_path,
        workflow_path,
        bindings_path,
        tool_profile_path,
        model_profile_path,
        execution_profile_path,
        endpoint=endpoint,
    )
    root = output_root(output_root_path, sources)
    try:
        resolved = manifest_path.expanduser().resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AdapterError(
            "MANIFEST_LOCATION",
            "execution manifest must be beneath output_root",
            str(manifest_path),
        ) from exc
    manifest = check_execution_package(resolved, plan)
    return {
        "ok": True,
        "execution": manifest,
        "candidate_count": manifest["candidate_count"],
        "network_contacted": False,
    }
