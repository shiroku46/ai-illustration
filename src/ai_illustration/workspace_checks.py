"""Deterministic dispatch from workspace checks to existing pipeline integrity checkers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .adapters.comfyui_execute import check_comfyui_execution
from .audio_preview import check_audio_preview_package
from .composition import check_composition_job_package
from .exporter import check_export_package
from .frame_preview import check_frame_preview_package
from .frame_renderer import check_frame_render_package
from .paper_theater import check_scene_plan
from .preview import check_preview_package
from .render_plan import check_render_plan_package
from .validation import validate_path
from .variants import check_variant_set
from .video_export import check_video_export_package
from .workspace_common import WorkspaceError


def _manifest_set(arguments: dict[str, str | Path]) -> dict[str, Any]:
    path = arguments["path"]
    assert isinstance(path, Path)
    diagnostics = validate_path(path)
    if diagnostics:
        first = diagnostics[0]
        raise WorkspaceError("MANIFEST_INVALID", f"{first.code}: {first.message}", first.field)
    return {"ok": True, "validated_path_count": 1}


def _comfyui_execution(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_comfyui_execution(
        arguments["manifest"],
        arguments["output_root"],
        arguments["request"],
        arguments["workflow"],
        arguments["bindings"],
        arguments["tool_profile"],
        arguments["model_profile"],
        arguments["execution_profile"],
        endpoint=str(arguments["endpoint"]),
    )


def _variant_set(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return {"ok": True, "variant_set": check_variant_set(arguments["variant_set"], arguments["manifest_root"])}


def _export_package(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_export_package(arguments["package_manifest"], arguments["output_root"])


def _scene_plan(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_scene_plan(arguments["scene_plan"], arguments["package_root"])


def _preview_package(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_preview_package(arguments["preview_manifest"], arguments["output_root"], arguments["package_root"])


def _audio_preview(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_audio_preview_package(
        arguments["audio_preview_manifest"],
        arguments["output_root"],
        arguments["preview_root"],
        arguments["package_root"],
        arguments["audio_root"],
    )


def _render_plan(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_render_plan_package(
        arguments["render_plan_manifest"],
        arguments["output_root"],
        arguments["audio_preview_root"],
        arguments["preview_root"],
        arguments["package_root"],
        arguments["audio_root"],
    )


def _renderer_job(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_composition_job_package(
        arguments["renderer_job_manifest"],
        arguments["output_root"],
        arguments["render_plan_root"],
        arguments["audio_preview_root"],
        arguments["preview_root"],
        arguments["package_root"],
        arguments["audio_root"],
    )


def _frame_render(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_frame_render_package(
        arguments["frame_render_manifest"],
        arguments["output_root"],
        arguments["renderer_job_root"],
        arguments["render_plan_root"],
        arguments["audio_preview_root"],
        arguments["preview_root"],
        arguments["package_root"],
        arguments["audio_root"],
    )


def _frame_preview(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_frame_preview_package(
        arguments["frame_preview_manifest"],
        arguments["output_root"],
        arguments["frame_render_root"],
        arguments["renderer_job_root"],
        arguments["render_plan_root"],
        arguments["audio_preview_root"],
        arguments["preview_root"],
        arguments["package_root"],
        arguments["audio_root"],
    )


def _video_export(arguments: dict[str, str | Path]) -> dict[str, Any]:
    return check_video_export_package(
        arguments["video_export_manifest"],
        arguments["profile"],
        arguments["ffmpeg"],
        arguments["output_root"],
        arguments["frame_preview_root"],
        arguments["frame_render_root"],
        arguments["renderer_job_root"],
        arguments["render_plan_root"],
        arguments["audio_preview_root"],
        arguments["preview_root"],
        arguments["package_root"],
        arguments["audio_root"],
        arguments["profile_root"],
    )


CHECKERS: dict[str, Callable[[dict[str, str | Path]], dict[str, Any]]] = {
    "manifest-set": _manifest_set,
    "comfyui-execution": _comfyui_execution,
    "variant-set": _variant_set,
    "export-package": _export_package,
    "scene-plan": _scene_plan,
    "preview-package": _preview_package,
    "audio-preview": _audio_preview,
    "render-plan": _render_plan,
    "renderer-job": _renderer_job,
    "frame-render": _frame_render,
    "frame-preview": _frame_preview,
    "video-export": _video_export,
}


def _stable_result_id(result: dict[str, Any]) -> str | None:
    for key in (
        "execution",
        "variant_set",
        "package",
        "scene_plan",
        "preview",
        "audio_preview",
        "render_plan",
        "renderer_job",
        "frame_render",
        "frame_preview",
        "video_export",
    ):
        value = result.get(key)
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            return value["id"]
    return None


def _diagnostic(exc: Exception, workspace_root: Path) -> dict[str, str]:
    code = getattr(exc, "code", exc.__class__.__name__.upper())
    field = str(getattr(exc, "field", ""))
    message = str(getattr(exc, "message", str(exc)))
    root_text = str(workspace_root)
    message = message.replace(root_text, ".").replace(root_text.replace("/", "\\"), ".")
    field = field.replace(root_text, ".").replace(root_text.replace("/", "\\"), ".")
    return {"code": str(code), "message": message, "field": field}


def evaluate_checks(resolved_checks: list[dict[str, Any]], workspace_root: Path) -> dict[str, Any]:
    statuses: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    complete_count = invalid_count = blocked_count = not_started_count = 0
    first_actionable: dict[str, Any] | None = None

    for check in resolved_checks:
        dependencies = check["depends_on"]
        unmet = [identifier for identifier in dependencies if statuses.get(identifier) != "complete"]
        entry: dict[str, Any] = {
            "id": check["id"],
            "kind": check["kind"],
            "depends_on": dependencies,
            "status": "",
            "artifact": check["arguments"][check["primary"]],
            "result_id": None,
            "diagnostics": [],
            "action": check["action"],
        }
        if unmet:
            entry["status"] = "blocked"
            entry["diagnostics"] = [
                {
                    "code": "DEPENDENCY_INCOMPLETE",
                    "message": f"waiting for: {', '.join(unmet)}",
                    "field": "depends_on",
                }
            ]
            blocked_count += 1
        else:
            primary = check["resolved_arguments"][check["primary"]]
            assert isinstance(primary, Path)
            if not primary.exists():
                entry["status"] = "not-started"
                not_started_count += 1
            else:
                try:
                    result = CHECKERS[check["kind"]](check["resolved_arguments"])
                except Exception as exc:
                    entry["status"] = "invalid"
                    entry["diagnostics"] = [_diagnostic(exc, workspace_root)]
                    invalid_count += 1
                else:
                    entry["status"] = "complete"
                    entry["result_id"] = _stable_result_id(result)
                    complete_count += 1
        statuses[check["id"]] = entry["status"]
        if first_actionable is None and entry["status"] in {"not-started", "invalid"}:
            first_actionable = {
                "check_id": check["id"],
                "status": entry["status"],
                "action": check["action"],
            }
        entries.append(entry)

    total = len(entries)
    return {
        "checks": entries,
        "counts": {
            "total": total,
            "complete": complete_count,
            "not_started": not_started_count,
            "blocked": blocked_count,
            "invalid": invalid_count,
        },
        "complete": complete_count == total,
        "next": first_actionable,
    }
