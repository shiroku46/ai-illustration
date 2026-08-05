"""Candidate packaging and offline verification for ComfyUI execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import AdapterError
from .comfyui_execution_common import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    MANIFEST_FILE,
    PLAN_FILE,
    integer,
    json_bytes,
    reject_symlinks,
    sha256,
    source_file,
    token,
)
from .comfyui_png import decode_comfyui_png
from ..naming import content_identifier, safe_relative_path
from ..quality import QualityGateError, packaged_quality_stage


def safe_descriptor(value: Any, field: str, *, allow_empty: bool) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value or value.startswith("/"):
        raise AdapterError("OUTPUT_DESCRIPTOR", f"{field} is unsafe", field)
    if not value and allow_empty:
        return ""
    try:
        path = safe_relative_path(value)
    except (TypeError, ValueError) as exc:
        raise AdapterError("OUTPUT_DESCRIPTOR", f"{field} is unsafe", field) from exc
    if not allow_empty and len(path.parts) != 1:
        raise AdapterError("OUTPUT_DESCRIPTOR", f"{field} must be one file name", field)
    for part in path.parts:
        if len(part) > 255 or any(not (ch.isalnum() or ch in "._- ") for ch in part):
            raise AdapterError("OUTPUT_DESCRIPTOR", f"{field} contains unsupported characters", field)
    return path.as_posix()


def history_outputs(history: dict[str, Any], prompt_id: str, nodes: list[str], max_images: int) -> list[dict[str, str]] | None:
    if not history:
        return None
    if set(history) != {prompt_id}:
        raise AdapterError("HISTORY_PROMPT_ID", "history response contains an unexpected prompt ID", "history_response")
    entry = history[prompt_id]
    if not isinstance(entry, dict):
        raise AdapterError("HISTORY_SCHEMA", "history entry must be an object", "history_response")
    status = entry.get("status")
    if isinstance(status, dict):
        if status.get("status_str") in {"error", "failed"}:
            raise AdapterError("EXECUTION_ERROR", "ComfyUI reported execution failure", "history_response.status")
        messages = status.get("messages")
        if isinstance(messages, list) and any(isinstance(item, list) and item and item[0] == "execution_error" for item in messages):
            raise AdapterError("EXECUTION_ERROR", "ComfyUI reported execution failure", "history_response.status")
    outputs = entry.get("outputs")
    if outputs is None:
        return None
    if not isinstance(outputs, dict) or set(outputs) != set(nodes):
        raise AdapterError("OUTPUT_NODES", "history output nodes differ from the execution profile", "history_response.outputs")
    descriptors: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for node_id in nodes:
        node = outputs.get(node_id)
        if not isinstance(node, dict) or set(node) != {"images"} or not isinstance(node.get("images"), list):
            raise AdapterError("OUTPUT_SCHEMA", "output node must contain only an images list", f"outputs.{node_id}")
        for index, item in enumerate(node["images"]):
            if not isinstance(item, dict) or set(item) != {"filename", "subfolder", "type"}:
                raise AdapterError("OUTPUT_SCHEMA", "image descriptor fields are invalid", f"outputs.{node_id}.images[{index}]")
            if item.get("type") != "output":
                raise AdapterError("OUTPUT_TYPE", "only type=output images are authorized", f"outputs.{node_id}.images[{index}].type")
            filename = safe_descriptor(item.get("filename"), f"outputs.{node_id}.images[{index}].filename", allow_empty=False)
            subfolder = safe_descriptor(item.get("subfolder"), f"outputs.{node_id}.images[{index}].subfolder", allow_empty=True)
            key = (node_id, subfolder, filename)
            if key in seen:
                raise AdapterError("DUPLICATE_OUTPUT", "duplicate output descriptor", f"outputs.{node_id}.images[{index}]")
            seen.add(key)
            descriptors.append({"node_id": node_id, "filename": filename, "subfolder": subfolder, "type": "output"})
            if len(descriptors) > max_images:
                raise AdapterError("IMAGE_COUNT", "output image count exceeds configured limit", "history_response.outputs")
    if not descriptors:
        raise AdapterError("IMAGE_COUNT", "execution produced no authorized images", "history_response.outputs")
    return sorted(descriptors, key=lambda item: (item["node_id"], item["subfolder"], item["filename"]))


def candidate_files(plan: dict[str, Any], prompt_id: str, descriptor: dict[str, str], png: bytes, index: int) -> tuple[dict[str, Any], bytes, str, bytes]:
    try:
        image = decode_comfyui_png(png, expected_width=plan["expected_width"], expected_height=plan["expected_height"])
    except Exception as exc:
        raise AdapterError("PNG_INVALID", str(exc), descriptor["filename"]) from exc
    try:
        quality_stage = packaged_quality_stage(plan["request"])
    except QualityGateError as exc:
        raise AdapterError(exc.code, exc.message, exc.field) from exc
    png_sha = sha256(png)
    identity = {
        "plan_ref": plan["id"],
        "node_id": descriptor["node_id"],
        "subfolder": descriptor["subfolder"],
        "filename": descriptor["filename"],
        "png_sha256": png_sha,
        "index": index,
    }
    candidate_id = content_identifier("candidate", identity, 20)
    png_path = f"candidates/{candidate_id}.png"
    sidecar = {
        "id": candidate_id,
        "kind": "candidate-asset",
        "schema_version": "1.0",
        "request_ref": plan["request"]["id"],
        "path": png_path,
        "sha256": png_sha,
        "width": image.width,
        "height": image.height,
        "color_space": "sRGB",
        "has_alpha": image.has_alpha,
        "media_type": "image/png",
        "status": "technically_valid",
        "quality_stage": quality_stage,
        "provenance": {
            "source": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "execution_plan_ref": plan["id"],
            "prompt_id": prompt_id,
            "output_node_id": descriptor["node_id"],
            "server_filename": descriptor["filename"],
            "server_subfolder": descriptor["subfolder"],
            "server_type": "output",
            "workflow_sha256": plan["workflow"]["sha256"],
            "bound_workflow_sha256": plan["workflow"]["bound_sha256"],
            "tool_profile_ref": plan["tool_profile"]["id"],
            "model_profile_ref": plan["model_profile"]["id"],
        },
    }
    return sidecar, json_bytes(sidecar), png_path, png


def execution_manifest(plan: dict[str, Any], plan_bytes: bytes, prompt_id: str, candidates: list[dict[str, Any]], generated: dict[str, bytes]) -> dict[str, Any]:
    files = [{"path": path, "sha256": sha256(payload), "size": len(payload)} for path, payload in sorted(generated.items())]
    core = {
        "kind": "comfyui-execution-package",
        "schema_version": "1.0",
        "execution_plan": {"id": plan["id"], "path": PLAN_FILE, "sha256": sha256(plan_bytes)},
        "request_ref": plan["request"]["id"],
        "endpoint": plan["endpoint"],
        "prompt_id": prompt_id,
        "workflow": plan["workflow"],
        "tool_profile_ref": plan["tool_profile"]["id"],
        "model_profile_ref": plan["model_profile"]["id"],
        "execution_profile_ref": plan["execution_profile"]["id"],
        "candidate_count": len(candidates),
        "candidates": candidates,
        "review_state": "unreviewed",
        "production_approved": False,
        "commercial_use_approved": False,
        "network_effect": {"queued": True, "loopback_only": True},
        "files": files,
    }
    return {"id": content_identifier("comfyui-execution-package", core, 20), **core}


def check_execution_package(manifest_path: Path, expected_plan: dict[str, Any]) -> dict[str, Any]:
    plan_id = expected_plan["id"]
    destination = manifest_path.parent.resolve()
    if destination.name != plan_id or manifest_path.resolve() != destination / MANIFEST_FILE:
        raise AdapterError("MANIFEST_LOCATION", "execution manifest location is not canonical", str(manifest_path))
    if destination.is_symlink() or not destination.is_dir():
        raise AdapterError("PACKAGE_TYPE", "execution package must be a regular directory", str(destination))
    manifest, manifest_bytes, _ = source_file(manifest_path, "execution_manifest", canonical_required=True)
    plan, plan_bytes, _ = source_file(destination / PLAN_FILE, "execution_plan", canonical_required=True)
    if plan != expected_plan:
        raise AdapterError("PLAN_BINDING", "stored execution plan is stale", PLAN_FILE)
    required_manifest = {
        "id", "kind", "schema_version", "execution_plan", "request_ref", "endpoint", "prompt_id", "workflow",
        "tool_profile_ref", "model_profile_ref", "execution_profile_ref", "candidate_count", "candidates", "review_state",
        "production_approved", "commercial_use_approved", "network_effect", "files",
    }
    if set(manifest) != required_manifest or manifest.get("kind") != "comfyui-execution-package" or manifest.get("schema_version") != "1.0":
        raise AdapterError("MANIFEST_SCHEMA", "execution manifest fields are invalid", MANIFEST_FILE)
    if manifest.get("execution_plan") != {"id": plan_id, "path": PLAN_FILE, "sha256": sha256(plan_bytes)}:
        raise AdapterError("PLAN_BINDING", "execution plan checksum binding changed", MANIFEST_FILE)
    if manifest.get("request_ref") != plan["request"]["id"] or manifest.get("endpoint") != plan["endpoint"]:
        raise AdapterError("SOURCE_BINDING", "execution source binding changed", MANIFEST_FILE)
    if manifest.get("workflow") != plan["workflow"]:
        raise AdapterError("SOURCE_BINDING", "workflow binding changed", "workflow")
    if manifest.get("tool_profile_ref") != plan["tool_profile"]["id"] or manifest.get("model_profile_ref") != plan["model_profile"]["id"] or manifest.get("execution_profile_ref") != plan["execution_profile"]["id"]:
        raise AdapterError("SOURCE_BINDING", "profile binding changed", MANIFEST_FILE)
    token(manifest.get("prompt_id"), "prompt_id")
    if manifest.get("network_effect") != {"queued": True, "loopback_only": True}:
        raise AdapterError("NETWORK_EFFECT", "network effect declaration changed", "network_effect")
    if manifest.get("review_state") != "unreviewed" or manifest.get("production_approved") is not False or manifest.get("commercial_use_approved") is not False:
        raise AdapterError("AUTOMATIC_APPROVAL", "execution package must remain unreviewed", MANIFEST_FILE)
    candidates = manifest.get("candidates")
    if not isinstance(candidates, list) or manifest.get("candidate_count") != len(candidates) or not candidates:
        raise AdapterError("CANDIDATE_SCHEMA", "candidate inventory is invalid", "candidates")
    expected_files = {PLAN_FILE, MANIFEST_FILE}
    reconstructed_files: list[dict[str, Any]] = [{"path": PLAN_FILE, "sha256": sha256(plan_bytes), "size": len(plan_bytes)}]
    previous_key = None
    for index, item in enumerate(candidates):
        required = {"id", "path", "sidecar_path", "sha256", "size", "width", "height", "output_node_id", "server_filename", "server_subfolder", "index"}
        if not isinstance(item, dict) or set(item) != required or item.get("index") != index:
            raise AdapterError("CANDIDATE_SCHEMA", "candidate inventory entry is invalid", f"candidates[{index}]")
        token(item.get("id"), f"candidates[{index}].id")
        if not isinstance(item.get("path"), str) or not isinstance(item.get("sidecar_path"), str):
            raise AdapterError("CANDIDATE_SCHEMA", "candidate paths are invalid", f"candidates[{index}]")
        if not isinstance(item.get("sha256"), str) or len(item["sha256"]) != 64 or any(ch not in "0123456789abcdef" for ch in item["sha256"]):
            raise AdapterError("CANDIDATE_SCHEMA", "candidate checksum is invalid", f"candidates[{index}].sha256")
        integer(item.get("size"), f"candidates[{index}].size", 1, plan["limits"]["max_png_bytes"])
        if item.get("width") != plan["expected_width"] or item.get("height") != plan["expected_height"]:
            raise AdapterError("CANDIDATE_SCHEMA", "candidate dimensions changed", f"candidates[{index}]")
        if item.get("output_node_id") not in plan["output_node_ids"]:
            raise AdapterError("CANDIDATE_SCHEMA", "candidate output node changed", f"candidates[{index}].output_node_id")
        safe_descriptor(item.get("server_filename"), f"candidates[{index}].server_filename", allow_empty=False)
        safe_descriptor(item.get("server_subfolder"), f"candidates[{index}].server_subfolder", allow_empty=True)
        key = (item["output_node_id"], item["server_subfolder"], item["server_filename"])
        if previous_key is not None and key < previous_key:
            raise AdapterError("CANDIDATE_ORDER", "candidate inventory is not deterministic", f"candidates[{index}]")
        previous_key = key
        png_rel = safe_relative_path(item["path"]).as_posix()
        side_rel = safe_relative_path(item["sidecar_path"]).as_posix()
        if png_rel != f"candidates/{item['id']}.png" or side_rel != f"candidates/{item['id']}.json":
            raise AdapterError("CANDIDATE_PATH", "candidate path is not canonical", f"candidates[{index}]")
        png_path, side_path = destination / png_rel, destination / side_rel
        for path in (png_path, side_path):
            reject_symlinks(path, str(path))
            if not path.is_file() or path.is_symlink():
                raise AdapterError("FILE_MISSING", "candidate file is missing", str(path))
        declared_size = item["size"]
        try:
            observed_size = png_path.stat().st_size
        except OSError as exc:
            raise AdapterError("CANDIDATE_BYTES", str(exc), png_rel) from exc
        if observed_size != declared_size or observed_size > plan["limits"]["max_png_bytes"]:
            raise AdapterError("CANDIDATE_BYTES", "candidate PNG checksum or size changed", png_rel)
        with png_path.open("rb") as handle:
            png = handle.read(plan["limits"]["max_png_bytes"] + 1)
        sidecar, side_bytes, _ = source_file(side_path, f"candidate_sidecar[{index}]", canonical_required=True)
        if sha256(png) != item["sha256"] or len(png) != declared_size:
            raise AdapterError("CANDIDATE_BYTES", "candidate PNG checksum or size changed", png_rel)
        try:
            decode_comfyui_png(png, expected_width=plan["expected_width"], expected_height=plan["expected_height"])
        except Exception as exc:
            raise AdapterError("PNG_INVALID", str(exc), png_rel) from exc
        expected_sidecar, expected_sidecar_bytes, _, _ = candidate_files(
            plan,
            manifest["prompt_id"],
            {"node_id": item["output_node_id"], "filename": item["server_filename"], "subfolder": item["server_subfolder"], "type": "output"},
            png,
            index,
        )
        if sidecar != expected_sidecar or side_bytes != expected_sidecar_bytes or item["id"] != sidecar["id"]:
            raise AdapterError("SIDECAR_BINDING", "candidate sidecar changed", side_rel)
        expected_files.update({png_rel, side_rel})
        reconstructed_files.extend([
            {"path": png_rel, "sha256": sha256(png), "size": len(png)},
            {"path": side_rel, "sha256": sha256(side_bytes), "size": len(side_bytes)},
        ])
    actual_files: set[str] = set()
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise AdapterError("PACKAGE_SYMLINK", "package contains a symlink", str(path))
        if path.is_file():
            actual_files.add(path.relative_to(destination).as_posix())
    if actual_files != expected_files:
        raise AdapterError("FILE_SET", f"missing={sorted(expected_files-actual_files)} extra={sorted(actual_files-expected_files)}", str(destination))
    if manifest.get("files") != sorted(reconstructed_files, key=lambda item: item["path"]):
        raise AdapterError("FILE_INVENTORY", "manifest file inventory changed", "files")
    core = {key: manifest[key] for key in manifest if key != "id"}
    if manifest.get("id") != content_identifier("comfyui-execution-package", core, 20):
        raise AdapterError("MANIFEST_ID", "execution manifest ID is not content-derived", "id")
    if manifest_bytes != json_bytes(manifest):
        raise AdapterError("NONCANONICAL_JSON", "execution manifest is not canonical", MANIFEST_FILE)
    return manifest
