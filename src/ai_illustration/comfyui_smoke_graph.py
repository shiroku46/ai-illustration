"""Extended graph inspection for current ComfyUI sampler layouts."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import comfyui_smoke_common as _common

_BASE_INSPECT = _common.inspect_workflow
SAMPLER_CLASSES = frozenset({"KSampler", "KSamplerAdvanced", "SamplerCustom"})


def _class(workflow: Mapping[str, Any], node_id: str | None) -> str | None:
    return _common._node_class(workflow, node_id)


def _choose_optional(
    workflow: Mapping[str, Any],
    candidates: list[str],
    field: str,
    capture: Any,
) -> str | None:
    return capture(
        field,
        lambda: _common._choose(
            workflow, candidates, None, field, required=False
        ),
    )


def inspect_workflow(
    workflow_path: Any,
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
    workflow, payload, resolved, summary = _common._load_workflow(workflow_path)
    diagnostics: list[dict[str, str]] = []

    def capture(_field: str, operation: Any) -> Any:
        try:
            return operation()
        except _common.SmokeError as exc:
            diagnostics.append(exc.to_dict())
            return None

    sampler_candidates = _common._matches(
        workflow, lambda class_type, _inputs: class_type in SAMPLER_CLASSES
    )
    sampler_id = capture(
        "sampler_node",
        lambda: _common._choose(
            workflow, sampler_candidates, sampler_node, "sampler_node"
        ),
    )

    checkpoint_id: str | None = None
    size_id: str | None = None
    positive_id: str | None = None
    negative_id: str | None = None
    scheduler_id: str | None = None
    sampler_select_id: str | None = None
    if isinstance(sampler_id, str):
        checkpoint_candidates = _common._branch_candidates(
            workflow,
            sampler_id,
            "model",
            lambda class_type, inputs: (
                "CheckpointLoader" in class_type
                and isinstance(inputs.get("ckpt_name"), str)
            ),
        )
        if not checkpoint_candidates:
            checkpoint_candidates = _common._matches(
                workflow,
                lambda class_type, inputs: (
                    "CheckpointLoader" in class_type
                    and isinstance(inputs.get("ckpt_name"), str)
                ),
            )
        checkpoint_id = capture(
            "checkpoint_node",
            lambda: _common._choose(
                workflow,
                checkpoint_candidates,
                checkpoint_node,
                "checkpoint_node",
            ),
        )

        size_candidates = _common._branch_candidates(
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
            size_candidates = _common._matches(
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
            lambda: _common._choose(
                workflow,
                size_candidates,
                size_node,
                "size_node",
                required=False,
            ),
        )

        text_predicate = lambda class_type, inputs: (
            "TextEncode" in class_type and isinstance(inputs.get("text"), str)
        )
        positive_candidates = _common._branch_candidates(
            workflow, sampler_id, "positive", text_predicate
        )
        negative_candidates = _common._branch_candidates(
            workflow, sampler_id, "negative", text_predicate
        )
        positive_id = capture(
            "positive_node",
            lambda: _common._choose(
                workflow,
                positive_candidates,
                positive_node,
                "positive_node",
                required=False,
            ),
        )
        negative_id = capture(
            "negative_node",
            lambda: _common._choose(
                workflow,
                negative_candidates,
                negative_node,
                "negative_node",
                required=False,
            ),
        )

        scheduler_candidates = _common._branch_candidates(
            workflow,
            sampler_id,
            "sigmas",
            lambda _class_type, inputs: (
                isinstance(inputs.get("steps"), int)
                and not isinstance(inputs.get("steps"), bool)
            ),
        )
        scheduler_id = _choose_optional(
            workflow, scheduler_candidates, "scheduler_node", capture
        )
        sampler_select_candidates = _common._branch_candidates(
            workflow,
            sampler_id,
            "sampler",
            lambda _class_type, inputs: isinstance(
                inputs.get("sampler_name"), str
            ),
        )
        sampler_select_id = _choose_optional(
            workflow,
            sampler_select_candidates,
            "sampler_select_node",
            capture,
        )

    output_candidates = _common._matches(
        workflow,
        lambda class_type, _inputs: class_type in _common.OUTPUT_CLASSES,
    )
    if output_nodes:
        selected_outputs: list[str] = []
        for node_id in sorted(set(output_nodes)):
            chosen = capture(
                "output_nodes",
                lambda node_id=node_id: _common._choose(
                    workflow,
                    output_candidates,
                    node_id,
                    "output_nodes",
                ),
            )
            if isinstance(chosen, str):
                selected_outputs.append(chosen)
    else:
        selected_outputs = output_candidates
        if not selected_outputs:
            diagnostics.append(
                _common.SmokeError(
                    "NODE_MISSING",
                    "no SaveImage output node was found",
                    "output_nodes",
                ).to_dict()
            )

    sampler_inputs = (
        _common._inputs(workflow, sampler_id)
        if isinstance(sampler_id, str)
        else {}
    )
    checkpoint_inputs = (
        _common._inputs(workflow, checkpoint_id)
        if isinstance(checkpoint_id, str)
        else {}
    )
    size_inputs = (
        _common._inputs(workflow, size_id)
        if isinstance(size_id, str)
        else {}
    )
    positive_inputs = (
        _common._inputs(workflow, positive_id)
        if isinstance(positive_id, str)
        else {}
    )
    negative_inputs = (
        _common._inputs(workflow, negative_id)
        if isinstance(negative_id, str)
        else {}
    )
    scheduler_inputs = (
        _common._inputs(workflow, scheduler_id)
        if isinstance(scheduler_id, str)
        else {}
    )
    sampler_select_inputs = (
        _common._inputs(workflow, sampler_select_id)
        if isinstance(sampler_select_id, str)
        else {}
    )

    seed_input = (
        "noise_seed"
        if "noise_seed" in sampler_inputs
        else "seed"
        if "seed" in sampler_inputs
        else None
    )
    detected_seed = (
        _common._scalar(sampler_inputs.get(seed_input)) if seed_input else None
    )
    steps_node = sampler_id if "steps" in sampler_inputs else scheduler_id
    steps_inputs = sampler_inputs if "steps" in sampler_inputs else scheduler_inputs
    detected_steps = _common._scalar(steps_inputs.get("steps"))
    detected_width = _common._scalar(size_inputs.get("width"))
    detected_height = _common._scalar(size_inputs.get("height"))
    final_seed = seed if seed is not None else detected_seed
    final_steps = steps if steps is not None else detected_steps
    final_width = width if width is not None else detected_width
    final_height = height if height is not None else detected_height
    final_positive = (
        positive_prompt
        if positive_prompt is not None
        else _common._scalar(positive_inputs.get("text"))
    )
    final_negative = (
        negative_prompt
        if negative_prompt is not None
        else _common._scalar(negative_inputs.get("text"))
    )

    for value, field, minimum, maximum in (
        (final_seed, "seed", 0, 2**63 - 1),
        (final_steps, "steps", 1, 10_000),
        (final_width, "width", 1, 8192),
        (final_height, "height", 1, 8192),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            diagnostics.append(
                _common.SmokeError(
                    "VALUE_REQUIRED",
                    f"{field} must be an integer from {minimum} to {maximum}",
                    field,
                ).to_dict()
            )

    checkpoint_name = _common._scalar(checkpoint_inputs.get("ckpt_name"))
    if not isinstance(checkpoint_name, str) or not checkpoint_name:
        diagnostics.append(
            _common.SmokeError(
                "VALUE_REQUIRED",
                "checkpoint filename could not be detected",
                "checkpoint_name",
            ).to_dict()
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
    if (
        isinstance(sampler_id, str)
        and seed_input is not None
        and isinstance(final_seed, int)
        and not isinstance(final_seed, bool)
    ):
        bindings["seed"] = {
            "node_id": sampler_id,
            "input": seed_input,
            "source": "seed",
        }
    if (
        isinstance(steps_node, str)
        and "steps" in steps_inputs
        and isinstance(final_steps, int)
        and not isinstance(final_steps, bool)
    ):
        bindings["steps"] = {
            "node_id": steps_node,
            "input": "steps",
            "source": "config.steps",
        }
        config["steps"] = final_steps

    scalar_sources = {
        "cfg": (sampler_id, sampler_inputs),
        "scheduler": (sampler_id, sampler_inputs),
        "sampler_name": (
            sampler_id
            if "sampler_name" in sampler_inputs
            else sampler_select_id,
            sampler_inputs
            if "sampler_name" in sampler_inputs
            else sampler_select_inputs,
        ),
        "denoise": (
            sampler_id if "denoise" in sampler_inputs else scheduler_id,
            sampler_inputs if "denoise" in sampler_inputs else scheduler_inputs,
        ),
    }
    for name, (node_id, inputs) in scalar_sources.items():
        value = _common._scalar(inputs.get(name))
        if isinstance(node_id, str) and value is not None:
            bindings[name] = {
                "node_id": node_id,
                "input": name,
                "source": f"config.{name}",
            }
            config[name] = value
    if isinstance(size_id, str):
        if isinstance(final_width, int) and not isinstance(final_width, bool):
            bindings["width"] = {
                "node_id": size_id,
                "input": "width",
                "source": "config.width",
            }
            config["width"] = final_width
        if isinstance(final_height, int) and not isinstance(final_height, bool):
            bindings["height"] = {
                "node_id": size_id,
                "input": "height",
                "source": "config.height",
            }
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
        output_inputs = _common._inputs(workflow, node_id)
        if "filename_prefix" in output_inputs:
            bindings[f"output_prefix_{node_id}"] = {
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
        "sampler_class": _class(workflow, sampler_id),
        "scheduler_node_id": scheduler_id,
        "scheduler_class": _class(workflow, scheduler_id),
        "sampler_select_node_id": sampler_select_id,
        "sampler_select_class": _class(workflow, sampler_select_id),
        "checkpoint_node_id": checkpoint_id,
        "checkpoint_class": _class(workflow, checkpoint_id),
        "size_node_id": size_id,
        "size_class": _class(workflow, size_id),
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
            "raw_sha256": _common._sha(payload),
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


def install() -> None:
    """Install the extended inspector before internal bundle modules import it."""

    _common.inspect_workflow = inspect_workflow
