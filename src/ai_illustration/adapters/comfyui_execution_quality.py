"""Quality-aware ComfyUI execution packaging without creative promotion."""

from __future__ import annotations

import json
from typing import Any

from . import comfyui_execution_package as _base
from .base import AdapterError
from .comfyui_execution_common import json_bytes
from ..quality import QualityGateError, packaged_quality_stage

_ORIGINAL_CANDIDATE_FILES = _base.candidate_files


def candidate_files(
    plan: dict[str, Any],
    prompt_id: str,
    descriptor: dict[str, Any],
    png: dict[str, Any],
    index: int,
) -> tuple[dict[str, Any], str, bytes, str]:
    """Add one explicit package-only quality stage to a candidate sidecar."""

    candidate, sidecar_path, sidecar_bytes, png_path = _ORIGINAL_CANDIDATE_FILES(
        plan,
        prompt_id,
        descriptor,
        png,
        index,
    )
    try:
        quality_stage = packaged_quality_stage(plan["request"])
    except QualityGateError as exc:
        raise AdapterError(exc.code, exc.message, exc.field) from exc
    try:
        sidecar = json.loads(sidecar_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError(
            "SIDECAR_JSON",
            "generated candidate sidecar is not valid UTF-8 JSON",
            sidecar_path,
        ) from exc
    if not isinstance(sidecar, dict):
        raise AdapterError("SIDECAR_JSON", "candidate sidecar must be an object", sidecar_path)
    sidecar["quality_stage"] = quality_stage
    return candidate, sidecar_path, json_bytes(sidecar), png_path


if not getattr(_base, "_quality_stage_patched", False):
    _base.candidate_files = candidate_files
    _base._quality_stage_patched = True

check_execution_package = _base.check_execution_package
