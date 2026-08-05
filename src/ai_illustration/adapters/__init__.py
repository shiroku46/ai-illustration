"""Local generation adapter interfaces."""

from .base import AdapterError, ExecutionPlan, GenerationAdapter
from .comfyui import ComfyUIAdapter, load_json_object, sanitize_loopback_endpoint, validate_workflow
from . import comfyui_execution_quality as _comfyui_execution_quality
from .comfyui_execute import MANIFEST_FILE as COMFYUI_EXECUTION_MANIFEST
from .comfyui_execute import check_comfyui_execution, prepare_execution, run_comfyui_execution

__all__ = [
    "AdapterError",
    "COMFYUI_EXECUTION_MANIFEST",
    "ComfyUIAdapter",
    "ExecutionPlan",
    "GenerationAdapter",
    "check_comfyui_execution",
    "load_json_object",
    "prepare_execution",
    "run_comfyui_execution",
    "sanitize_loopback_endpoint",
    "validate_workflow",
]
