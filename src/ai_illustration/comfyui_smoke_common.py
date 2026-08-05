"""Workflow inspection primitives for local ComfyUI smoke tests."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .adapters.base import AdapterError
from .adapters.comfyui import _scan_for_secrets, validate_workflow
from .naming import canonical_json

WORKFLOW_FILE = "workflow-api.json"
REQUEST_FILE = "generation-request.json"
BINDINGS_FILE = "bindings.json"
TOOL_FILE = "tool-profile.json"
MODEL_FILE = "model-profile.json"
EXECUTION_FILE = "execution-profile.json"
MANIFEST_FILE = "smoke-bundle-manifest.json"
MAX_WORKFLOW_BYTES = 16 * 1024 * 1024
MAX_NODES = 4096
TOKEN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAMPLER_CLASSES = frozenset({"KSampler", "KSamplerAdvanced"})
OUTPUT_CLASSES = frozenset({"SaveImage"})
PROFILE_STATES = frozenset({"reviewing", "approved"})


@dataclass(frozen=True)
class SmokeError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _reject_symlinks(path: Path, field: str) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    for candidate in (lexical, *lexical.parents):
        try:
            if candidate.exists() and candidate.is_symlink():
                raise SmokeError("PATH_SYMLINK", f"{field} contains a symlink component", field)
        except OSError as exc:
            raise SmokeError("PATH_ERROR", str(exc), field) from exc


def _load_workflow(path: Path) -> tuple[dict[str, Any], bytes, Path, dict[str, Any]]:
    raw = str(path)
    expanded = path.expanduser()
    if "\x00" in raw or ".." in expanded.parts:
        raise SmokeError("UNSAFE_PATH", "workflow path is unsafe", "workflow")
    _reject_symlinks(expanded, "workflow")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise SmokeError("WORKFLOW_MISSING", str(exc), "workflow") from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise SmokeError("WORKFLOW_TYPE", "workflow must be a regular file", "workflow")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_WORKFLOW_BYTES:
        raise SmokeError("WORKFLOW_SIZE", "workflow exceeds the JSON size limit", "workflow")
    payload = resolved.read_bytes()
    if len(payload) != size:
        raise SmokeError("WORKFLOW_SIZE_CHANGED", "workflow changed during read", "workflow")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise SmokeError("DUPLICATE_JSON_KEY", f"duplicate JSON key: {key}", "workflow")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except SmokeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SmokeError("INVALID_JSON", str(exc), "workflow") from exc
    if not isinstance(value, dict):
        raise SmokeError("INVALID_JSON_ROOT", "workflow root must be an object", "workflow")
    if len(value) > MAX_NODES:
        raise SmokeError("NODE_LIMIT", f"workflow may contain at most {MAX_NODES} nodes", "workflow")
    try:
        _scan_for_secrets(value, "workflow")
        summary = validate_workflow(value)
    except AdapterError as exc:
        raise SmokeError(exc.code, exc.message, exc.field) from exc
    return value, payload, resolved, summary


def _node(workflow: Mapping[str, Any], node_id: str, field: str) -> Mapping[str, Any]:
    node = workflow.get(node_id)
    if not isinstance(node, Mapping):
        raise SmokeError("UNKNOWN_NODE", f"node {node_id!r} does not exist", field)
    return node


def _inputs(workflow: Mapping[str, Any], node_id: str) -> Mapping[str, Any]:
    value = _node(workflow, node_id, node_id).get("inputs")
    if not isinstance(value, Mapping):
        raise SmokeError("INVALID_INPUTS", f"node {node_id!r} inputs are invalid", node_id)
    return value


def _link_node(value: Any, workflow: Mapping[str, Any]) -> str | None:
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0] in workflow
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    ):
        return value[0]
    return None


def _upstream_nodes(workflow: Mapping[str, Any], value: Any) -> list[str]:
    start = _link_node(value, workflow)
    if start is None:
        return []
    pending = [start]
    seen: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        for item in _inputs(workflow, node_id).values():
            linked = _link_node(item, workflow)
            if linked is not None and linked not in seen:
                pending.append(linked)
    return sorted(seen)


def _matches(workflow: Mapping[str, Any], predicate: Any) -> list[str]:
    result: list[str] = []
    for node_id in sorted(workflow):
        node = _node(workflow, node_id, node_id)
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if isinstance(class_type, str) and isinstance(inputs, Mapping) and predicate(class_type, inputs):
            result.append(node_id)
    return result


def _choose(
    workflow: Mapping[str, Any],
    candidates: list[str],
    override: str | None,
    field: str,
    *,
    required: bool = True,
) -> str | None:
    if override is not None:
        _node(workflow, override, field)
        if override not in candidates:
            raise SmokeError("NODE_CLASS", f"node {override!r} is not valid for {field}", field)
        return override
    if len(candidates) == 1:
        return candidates[0]
    if not candidates and not required:
        return None
    if not candidates:
        raise SmokeError("NODE_MISSING", f"no node was found for {field}", field)
    raise SmokeError("NODE_AMBIGUOUS", f"multiple nodes were found for {field}: {candidates}", field)


def _branch_candidates(
    workflow: Mapping[str, Any],
    sampler_id: str,
    input_name: str,
    predicate: Any,
) -> list[str]:
    branch = _upstream_nodes(workflow, _inputs(workflow, sampler_id).get(input_name))
    return [
        node_id
        for node_id in branch
        if predicate(
            str(_node(workflow, node_id, node_id).get("class_type", "")),
            _inputs(workflow, node_id),
        )
    ]


def _scalar(value: Any) -> str | int | float | bool | None:
    if isinstance(value, (str, int, float, bool)) and value is not None:
        return value
    return None


def _tokenize(text: str, prefix: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not base:
        base = prefix
    suffix = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    value = f"{prefix}-{base}-{suffix}"
    return value[:128].rstrip("-")


def _validate_token(value: str, field: str) -> str:
    if not TOKEN_RE.fullmatch(value) or len(value) > 128:
        raise SmokeError("TOKEN", f"{field} must be lowercase ASCII words separated by hyphens", field)
    return value


def _node_class(workflow: Mapping[str, Any], node_id: str | None) -> str | None:
    if node_id is None:
        return None
    value = _node(workflow, node_id, node_id).get("class_type")
    return value if isinstance(value, str) else None


def inspect_workflow(
    workflow_path: Path,
    *,
    sampler_node: str | None = None,
    checkpoint_node: str | None = None,
    size_node: str | None = None,
    positive_node: str | None = None,
    negative_node: str | None = None,
    output_nodes: Sequence[str] = (),
    seed: int | None = None,
    steps: int | None = None,
    width: int | None = None,
    height: int | None = None,
    positive_prompt: str | None = None,
    negative_prompt: str | None = None,
) -> dict[str, Any]:
    workflow, payload, resolved, summary = _load_workflow(workflow_path)
    diagnostics: list[dict[str, str]] = []

    def capture(_field: str, operation: Any) -> Any:
        try:
            return operation()
        except SmokeError as exc:
            diagnostics.append(exc.to_dict())
            return None

    sampler_candidates = _matches(workflow, lambda class_type, _inputs: class_type in SAMPLER_CLASSES)
    sampler_id = capture(
        "sampler_node",
        lambda: _choose(workflow, sampler_candidates, sampler_node, "sampler_node"),
    )

    checkpoint_id: str | None = None
    size_id: str | None = None
    positive_id: str | None = None
    negative_id: str | None = None
    if isinstance(sampler_id, str):
        checkpoint_candidates = _branch_candidates(
            workflow,
            sampler_id,
            "model",
            lambda class_type, inputs: "CheckpointLoader" in class_type and isinstance(inputs.get("ckpt_name"), str),
        )
        if not checkpoint_candidates:
            checkpoint_candidates = _matches(
                workflow,
                lambda class_type, inputs: "CheckpointLoader" in class_type and isinstance(inputs.get("ckpt_name"), str),
            )
        checkpoint_id = capture(
            "checkpoint_node",
            lambda: _choose(workflow, checkpoint_candidates, checkpoint_node, "checkpoint_node"),
        )

        size_candidates = _branch_candidates(
            workflow,
            sampler_id,
            "latent_image",
            lambda class_type, inputs: (
                "Latent" in class_type
                and isinstance(inputs.get("width"), int)
                and not isinstance(inputs.get("width"), bool)
                and isinstance(inputs.get("height"), int)
                and not isinstance(inputs.get("height"), bool)
            ),
        )
        if not size_candidates:
            size_candidates = _matches(
                workflow,
                lambda class_type, inputs: (
                    "Latent" in class_type
                    and isinstance(inputs.get("width"), int)
                    and not isinstance(inputs.get("width"), bool)
                    and isinstance(inputs.get("height"), int)
                    and not isinstance(inputs.get("height"), bool)
                ),
            )
        size_id = capture(
            "size_node",
            lambda: _choose(workflow, size_candidates, size_node, "size_node", required=False),
        )

        text_predicate = lambda class_type, inputs: (
            "TextEncode" in class_type and isinstance(inputs.get("text"), str)
        )
        positive_candidates = _branch_candidates(workflow, sampler_id, "positive", text_predicate)
        negative_candidates = _branch_candidates(workflow, sampler_id, "negative", text_predicate)
        positive_id = capture(
            "positive_node",
            lambda: _choose(workflow, positive_candidates, positive_node, "positive_node", required=False),
        )
        negative_id = capture(
            "negative_node",
            lambda: _choose(workflow, negative_candidates, negative_node, "negative_node", required=False),
        )

    output_candidates = _matches(workflow, lambda class_type, _inputs: class_type in OUTPUT_CLASSES)
    if output_nodes:
        selected_outputs: list[str] = []
        for node_id in sorted(set(output_nodes)):
            chosen = capture(
                "output_nodes",
                lambda node_id=node_id: _choose(workflow, output_candidates, node_id, "output_nodes"),
            )
            if isinstance(chosen, str):
                selected_outputs.append(chosen)
    else:
        selected_outputs = output_candidates
        if not selected_outputs:
            diagnostics.append(
                SmokeError("NODE_MISSING", "no SaveImage output node was found", "output_nodes").to_dict()
            )

    sampler_inputs = _inputs(workflow, sampler_id) if isinstance(sampler_id, str) else {}
    checkpoint_inputs = _inputs(workflow, checkpoint_id) if isinstance(checkpoint_id, str) else {}
    size_inputs = _inputs(workflow, size_id) if isinstance(size_id, str) else {}
    positive_inputs = _inputs(workflow, positive_id) if isinstance(positive_id, str) else {}
    negative_inputs = _inputs(workflow, negative_id) if isinstance(negative_id, str) else {}

    seed_input = "noise_seed" if "noise_seed" in sampler_inputs else "seed" if "seed" in sampler_inputs else None
    detected_seed = _scalar(sampler_inputs.get(seed_input)) if seed_input else None
    detected_steps = _scalar(sampler_inputs.get("steps"))
    detected_width = _scalar(size_inputs.get("width"))
    detected_height = _scalar(size_inputs.get("height"))
    final_seed = seed if seed is not None else detected_seed
    final_steps = steps if steps is not None else detected_steps
    final_width = width if width is not None else detected_width
    final_height = height if height is not None else detected_height
    final_positive = positive_prompt if positive_prompt is not None else _scalar(positive_inputs.get("text"))
    final_negative = negative_prompt if negative_prompt is not None else _scalar(negative_inputs.get("text"))

    for value, field, minimum, maximum in (
        (final_seed, "seed", 0, 2**63 - 1),
        (final_steps, "steps", 1, 10_000),
        (final_width, "width", 1, 8192),
        (final_height, "height", 1, 8192),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            diagnostics.append(
                SmokeError("VALUE_REQUIRED", f"{field} must be an integer from {minimum} to {maximum}", field).to_dict()
            )

    checkpoint_name = _scalar(checkpoint_inputs.get("ckpt_name"))
    if not isinstance(checkpoint_name, str) or not checkpoint_name:
        diagnostics.append(
            SmokeError("VALUE_REQUIRED", "checkpoint filename could not be detected", "checkpoint_name").to_dict()
        )

    bindings: dict[str, dict[str, str]] = {}
    config: dict[str, Any] = {}
    if isinstance(checkpoint_id, str) and isinstance(checkpoint_name, str):
        bindings["checkpoint_name"] = {
            "node_id": checkpoint_id,
            "input": "ckpt_name",
            "source": "config.checkpoint_name",
        }
        config["checkpoint_name"] = checkpoint_name
    if isinstance(sampler_id, str) and seed_input is not None and isinstance(final_seed, int) and not isinstance(final_seed, bool):
        bindings["seed"] = {"node_id": sampler_id, "input": seed_input, "source": "seed"}
    if isinstance(sampler_id, str) and "steps" in sampler_inputs and isinstance(final_steps, int) and not isinstance(final_steps, bool):
        bindings["steps"] = {"node_id": sampler_id, "input": "steps", "source": "config.steps"}
        config["steps"] = final_steps
    for name in ("cfg", "sampler_name", "scheduler", "denoise"):
        value = _scalar(sampler_inputs.get(name))
        if value is not None:
            bindings[name] = {"node_id": sampler_id, "input": name, "source": f"config.{name}"}
            config[name] = value
    if isinstance(size_id, str):
        if isinstance(final_width, int) and not isinstance(final_width, bool):
            bindings["width"] = {"node_id": size_id, "input": "width", "source": "config.width"}
            config["width"] = final_width
        if isinstance(final_height, int) and not isinstance(final_height, bool):
            bindings["height"] = {"node_id": size_id, "input": "height", "source": "config.height"}
            config["height"] = final_height
    if isinstance(positive_id, str) and isinstance(final_positive, str):
        bindings["positive_prompt"] = {
            "node_id": positive_id,
            "input": "text",
            "source": "config.positive_prompt",
        }
        config["positive_prompt"] = final_positive
    if isinstance(negative_id, str) and isinstance(final_negative, str):
        bindings["negative_prompt"] = {
            "node_id": negative_id,
            "input": "text",
            "source": "config.negative_prompt",
        }
        config["negative_prompt"] = final_negative
    for node_id in selected_outputs:
        output_inputs = _inputs(workflow, node_id)
        if "filename_prefix" in output_inputs:
            name = f"output_prefix_{node_id}"
            bindings[name] = {
                "node_id": node_id,
                "input": "filename_prefix",
                "source": "config.output_prefix",
            }
            config["output_prefix"] = "ai-illustration-smoke"

    diagnostics = sorted(
        diagnostics,
        key=lambda item: (item["code"], item["field"], item["message"]),
    )
    selection = {
        "sampler_node_id": sampler_id,
        "sampler_class": _node_class(workflow, sampler_id),
        "checkpoint_node_id": checkpoint_id,
        "checkpoint_class": _node_class(workflow, checkpoint_id),
        "size_node_id": size_id,
        "size_class": _node_class(workflow, size_id),
        "positive_node_id": positive_id,
        "negative_node_id": negative_id,
        "output_node_ids": sorted(set(selected_outputs)),
    }
    values = {
        "checkpoint_name": checkpoint_name,
        "seed": final_seed,
        "steps": final_steps,
        "width": final_width,
        "height": final_height,
        "positive_prompt": final_positive,
        "negative_prompt": final_negative,
    }
    return {
        "ok": not diagnostics,
        "workflow": {
            "name": resolved.name,
            "raw_sha256": _sha(payload),
            "canonical_sha256": summary["workflow_sha256"],
            "node_count": summary["node_count"],
            "class_types": summary["class_types"],
        },
        "selection": selection,
        "values": values,
        "config": dict(sorted(config.items())),
        "bindings": dict(sorted(bindings.items())),
        "diagnostics": diagnostics,
        "filesystem_mutated": False,
        "network_contacted": False,
        "external_process_started": False,
    }
