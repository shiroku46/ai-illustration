"""Strict read-only validation for committed ComfyUI API benchmark workflows."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .naming import SHA256_RE

MAX_WORKFLOW_BYTES = 4 * 1024 * 1024
REQUIRED_COMMON_CLASSES = frozenset(
    {"KSampler", "EmptyLatentImage", "CLIPTextEncode", "VAEDecode", "SaveImage"}
)
SECRET_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "cookie", "password", "secret", "token"}
)


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


def _scan_secrets(value: Any, field: str = "workflow") -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_field = f"{field}.{key}"
            if isinstance(key, str) and key.lower() in SECRET_KEYS:
                diagnostics.append(_diag("WORKFLOW_SECRET", "credential-like key is forbidden", child_field))
            diagnostics.extend(_scan_secrets(child, child_field))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            diagnostics.extend(_scan_secrets(child, f"{field}[{index}]"))
    elif isinstance(value, str):
        lowered = value.lower()
        if "bearer " in lowered or "hf_" in lowered or "sk-" in lowered:
            diagnostics.append(_diag("WORKFLOW_SECRET", "credential-like value is forbidden", field))
    return diagnostics


def _nodes_by_class(workflow: dict[str, Any], class_type: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (node_id, node)
        for node_id, node in sorted(workflow.items())
        if isinstance(node, dict) and node.get("class_type") == class_type
    ]


def _one(workflow: dict[str, Any], class_type: str, field: str) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    nodes = _nodes_by_class(workflow, class_type)
    if len(nodes) != 1:
        return None, [_diag("WORKFLOW_NODE_COUNT", f"exactly one {class_type} node is required", field)]
    return nodes[0][1], []


def _inputs(node: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
        return {}
    return node["inputs"]


def _link(value: Any, workflow: dict[str, Any], field: str) -> list[dict[str, str]]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or value[0] not in workflow
        or not isinstance(value[1], int)
        or isinstance(value[1], bool)
        or value[1] < 0
    ):
        return [_diag("WORKFLOW_LINK", "must reference an existing node output", field)]
    return []


def _artifact_filenames(model: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for artifact in model.get("artifacts", []):
        if isinstance(artifact, dict) and isinstance(artifact.get("component"), str):
            result[artifact["component"]] = str(artifact.get("filename", ""))
    return result


def validate_workflow_bytes(
    payload: bytes,
    model: dict[str, Any],
    *,
    field: str = "workflow",
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    diagnostics: list[dict[str, str]] = []
    if not payload or len(payload) > MAX_WORKFLOW_BYTES:
        diagnostics.append(_diag("WORKFLOW_SIZE", f"workflow must be 1..{MAX_WORKFLOW_BYTES} bytes", field))
        return diagnostics, {}
    try:
        workflow = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        return [_diag("WORKFLOW_JSON", str(exc), field)], {}
    if not isinstance(workflow, dict) or not workflow:
        return [_diag("WORKFLOW_OBJECT", "workflow root must be a non-empty object", field)], {}

    classes: set[str] = set()
    for node_id, node in workflow.items():
        node_field = f"{field}.{node_id}"
        if not isinstance(node_id, str) or not node_id.isdigit():
            diagnostics.append(_diag("WORKFLOW_NODE_ID", "node IDs must be decimal strings", node_field))
        if not isinstance(node, dict):
            diagnostics.append(_diag("WORKFLOW_NODE", "node must be an object", node_field))
            continue
        if set(node) - {"inputs", "class_type", "_meta"}:
            diagnostics.append(_diag("WORKFLOW_NODE_FIELD", "only inputs, class_type, and _meta are allowed", node_field))
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type:
            diagnostics.append(_diag("WORKFLOW_CLASS", "class_type must be non-empty", f"{node_field}.class_type"))
        else:
            classes.add(class_type)
        if not isinstance(node.get("inputs"), dict):
            diagnostics.append(_diag("WORKFLOW_INPUTS", "inputs must be an object", f"{node_field}.inputs"))

    diagnostics.extend(_scan_secrets(workflow, field))
    missing_classes = sorted(REQUIRED_COMMON_CLASSES - classes)
    if missing_classes:
        diagnostics.append(_diag("WORKFLOW_CLASS_MISSING", f"missing classes: {', '.join(missing_classes)}", field))

    sampler, values = _one(workflow, "KSampler", f"{field}.sampler")
    diagnostics.extend(values)
    latent, values = _one(workflow, "EmptyLatentImage", f"{field}.latent")
    diagnostics.extend(values)
    decode, values = _one(workflow, "VAEDecode", f"{field}.decode")
    diagnostics.extend(values)
    output, values = _one(workflow, "SaveImage", f"{field}.output")
    diagnostics.extend(values)
    text_nodes = _nodes_by_class(workflow, "CLIPTextEncode")
    if len(text_nodes) != 2:
        diagnostics.append(_diag("WORKFLOW_NODE_COUNT", "exactly two CLIPTextEncode nodes are required", f"{field}.prompts"))

    settings = model.get("benchmark_settings") if isinstance(model.get("benchmark_settings"), dict) else {}
    sampler_inputs = _inputs(sampler)
    for name, expected in (
        ("seed", 101),
        ("steps", settings.get("steps")),
        ("cfg", settings.get("cfg")),
        ("sampler_name", settings.get("sampler")),
        ("scheduler", settings.get("scheduler")),
        ("denoise", 1.0),
    ):
        if sampler_inputs.get(name) != expected:
            diagnostics.append(_diag("WORKFLOW_SETTING", f"{name} must equal manifest value {expected!r}", f"{field}.sampler.{name}"))
    for name in ("model", "positive", "negative", "latent_image"):
        diagnostics.extend(_link(sampler_inputs.get(name), workflow, f"{field}.sampler.{name}"))

    latent_inputs = _inputs(latent)
    for name in ("width", "height"):
        if latent_inputs.get(name) != settings.get(name):
            diagnostics.append(_diag("WORKFLOW_SETTING", f"{name} must equal manifest value", f"{field}.latent.{name}"))
    if latent_inputs.get("batch_size") != 1:
        diagnostics.append(_diag("WORKFLOW_BATCH", "template batch_size must be 1", f"{field}.latent.batch_size"))

    prompt_texts: list[str] = []
    for node_id, node in text_nodes:
        inputs = _inputs(node)
        text = inputs.get("text")
        if not isinstance(text, str) or not text.strip():
            diagnostics.append(_diag("WORKFLOW_PROMPT", "prompt must be non-empty", f"{field}.{node_id}.text"))
        else:
            prompt_texts.append(text)
        diagnostics.extend(_link(inputs.get("clip"), workflow, f"{field}.{node_id}.clip"))
    if len(prompt_texts) == 2 and prompt_texts[0] == prompt_texts[1]:
        diagnostics.append(_diag("WORKFLOW_PROMPT", "positive and negative prompts must differ", f"{field}.prompts"))

    decode_inputs = _inputs(decode)
    diagnostics.extend(_link(decode_inputs.get("samples"), workflow, f"{field}.decode.samples"))
    diagnostics.extend(_link(decode_inputs.get("vae"), workflow, f"{field}.decode.vae"))
    output_inputs = _inputs(output)
    diagnostics.extend(_link(output_inputs.get("images"), workflow, f"{field}.output.images"))
    prefix = output_inputs.get("filename_prefix")
    expected_prefix = f"ai-illustration-benchmark/{model.get('family')}/"
    if not isinstance(prefix, str) or not prefix.startswith(expected_prefix):
        diagnostics.append(_diag("WORKFLOW_OUTPUT", f"filename_prefix must start with {expected_prefix}", f"{field}.output.filename_prefix"))

    filenames = _artifact_filenames(model)
    if "checkpoint" in filenames:
        loader, values = _one(workflow, "CheckpointLoaderSimple", f"{field}.checkpoint")
        diagnostics.extend(values)
        if _inputs(loader).get("ckpt_name") != filenames["checkpoint"]:
            diagnostics.append(_diag("WORKFLOW_MODEL", "checkpoint filename does not match manifest", f"{field}.checkpoint.ckpt_name"))
        for forbidden in ("UNETLoader", "CLIPLoader", "VAELoader", "ModelSamplingAuraFlow"):
            if _nodes_by_class(workflow, forbidden):
                diagnostics.append(_diag("WORKFLOW_ARCHITECTURE", f"SDXL template must not contain {forbidden}", field))
    else:
        unet, values = _one(workflow, "UNETLoader", f"{field}.unet")
        diagnostics.extend(values)
        clip, values = _one(workflow, "CLIPLoader", f"{field}.clip")
        diagnostics.extend(values)
        vae, values = _one(workflow, "VAELoader", f"{field}.vae")
        diagnostics.extend(values)
        sampling, values = _one(workflow, "ModelSamplingAuraFlow", f"{field}.sampling")
        diagnostics.extend(values)
        if _inputs(unet).get("unet_name") != filenames.get("diffusion-model"):
            diagnostics.append(_diag("WORKFLOW_MODEL", "diffusion model filename does not match manifest", f"{field}.unet.unet_name"))
        if _inputs(clip).get("clip_name") != filenames.get("text-encoder"):
            diagnostics.append(_diag("WORKFLOW_MODEL", "text encoder filename does not match manifest", f"{field}.clip.clip_name"))
        if _inputs(clip).get("type") != "stable_diffusion" or _inputs(clip).get("device") != "default":
            diagnostics.append(_diag("WORKFLOW_MODEL", "Anima CLIPLoader type/device mismatch", f"{field}.clip"))
        if _inputs(vae).get("vae_name") != filenames.get("vae"):
            diagnostics.append(_diag("WORKFLOW_MODEL", "VAE filename does not match manifest", f"{field}.vae.vae_name"))
        if _inputs(sampling).get("shift") != 3.0:
            diagnostics.append(_diag("WORKFLOW_SETTING", "Anima sampling shift must be 3.0", f"{field}.sampling.shift"))
        diagnostics.extend(_link(_inputs(sampling).get("model"), workflow, f"{field}.sampling.model"))
        if _nodes_by_class(workflow, "CheckpointLoaderSimple"):
            diagnostics.append(_diag("WORKFLOW_ARCHITECTURE", "Anima template must not contain CheckpointLoaderSimple", field))

    checksum = hashlib.sha256(payload).hexdigest()
    summary = {
        "sha256": checksum,
        "size_bytes": len(payload),
        "node_count": len(workflow),
        "class_types": sorted(classes),
        "seed": sampler_inputs.get("seed"),
        "steps": sampler_inputs.get("steps"),
        "cfg": sampler_inputs.get("cfg"),
        "sampler": sampler_inputs.get("sampler_name"),
        "scheduler": sampler_inputs.get("scheduler"),
        "width": latent_inputs.get("width"),
        "height": latent_inputs.get("height"),
    }
    if not SHA256_RE.fullmatch(checksum):
        diagnostics.append(_diag("WORKFLOW_CHECKSUM", "internal checksum failure", field))
    return _sorted(diagnostics), summary
