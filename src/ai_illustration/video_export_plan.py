"""Deterministic video-export plan construction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .naming import content_identifier
from .video_export_bindings import _executable_reference, _profile_reference
from .video_export_common import (
    MAX_DIAGNOSTIC_BYTES, MAX_TIMEOUT_SECONDS, MAX_VIDEO_BYTES, VIDEO_OUTPUT,
    VideoExportError, _bounded_int, _json_bytes, _reject_native_lexical,
    _reject_symlink_components, _root, _sha, _within,
)
from .video_export_source import _audio_filter, _command_template, _source_reference

def _build_plan(
    frame_preview_manifest: Path,
    profile_path: Path,
    ffmpeg_path: Path,
    frame_preview_root: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
    profile_root: Path,
) -> tuple[dict[str, Any], bytes, Path, Path, dict[str, Path], set[Path]]:
    source, source_relative, source_path, source_bytes, source_package, source_paths = _source_reference(
        frame_preview_manifest,
        frame_preview_root,
        frame_render_root,
        renderer_job_root,
        render_plan_root,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
    )
    profile, profile_relative, profile_resolved, profile_bytes = _profile_reference(profile_path, profile_root)
    executable, executable_path = _executable_reference(ffmpeg_path)
    audio = source.get("audio")
    placement = source.get("audio_placement")
    if not isinstance(audio, dict) or not isinstance(audio.get("path"), str) or not isinstance(placement, dict):
        raise VideoExportError("SOURCE_SCHEMA", "source audio or placement is missing", "frame_preview")
    offset = _bounded_int(placement.get("offset_ms"), "audio_placement.offset_ms", -24 * 60 * 60 * 1000, 24 * 60 * 60 * 1000)
    duration = _bounded_int(source.get("scene_duration_ms"), "scene_duration_ms", 1, 24 * 60 * 60 * 1000)
    filter_value = _audio_filter(offset, duration)
    command = _command_template(source, profile, audio["path"], filter_value)
    core = {
        "kind": "paper-theater-video-export-plan",
        "schema_version": "1.0",
        "source_frame_preview": {
            "id": source["id"],
            "path": source_relative,
            "sha256": _sha(source_bytes),
        },
        "profile": {
            "id": profile["id"],
            "path": profile_relative,
            "sha256": _sha(profile_bytes),
        },
        "ffmpeg": executable,
        "intent": source.get("intent"),
        "audio_license_status": source.get("audio_license_status"),
        "canvas": source.get("canvas"),
        "fps_num": source.get("fps_num"),
        "fps_den": source.get("fps_den"),
        "frame_count": source.get("frame_count"),
        "scene_duration_ms": duration,
        "audio_placement": placement,
        "input": {
            "working_directory": "isolated-byte-identical-source-copy",
            "frame_pattern": "frames/%08d.png",
            "audio_path": audio["path"],
        },
        "audio_filter": filter_value,
        "output": {
            "path": VIDEO_OUTPUT,
            "container": profile["container"],
            "extension": profile["extension"],
        },
        "command_template": command,
        "safety": {
            "shell": False,
            "stdin": False,
            "network_inputs": False,
            "source_copy_isolated": True,
            "max_output_bytes": MAX_VIDEO_BYTES,
            "max_diagnostic_bytes": MAX_DIAGNOSTIC_BYTES,
            "max_timeout_seconds": MAX_TIMEOUT_SECONDS,
        },
        "reproducibility_scope": "exact-source-profile-executable-and-recorded-output-bytes",
    }
    plan_id = content_identifier("paper-theater-video-export-plan", core, 20)
    plan = {"id": plan_id, **core}
    plan_bytes = _json_bytes(plan)
    sources = {
        source_package,
        _root(frame_preview_root, must_exist=True, field="frame_preview_root"),
        _root(frame_render_root, must_exist=True, field="frame_render_root"),
        _root(renderer_job_root, must_exist=True, field="renderer_job_root"),
        _root(render_plan_root, must_exist=True, field="render_plan_root"),
        _root(audio_preview_root, must_exist=True, field="audio_preview_root"),
        _root(preview_root, must_exist=True, field="preview_root"),
        _root(package_root, must_exist=True, field="package_root"),
        _root(audio_root, must_exist=True, field="audio_root"),
        _root(profile_root, must_exist=True, field="profile_root"),
        profile_resolved,
        executable_path,
    }
    return plan, plan_bytes, source_package, executable_path, source_paths, sources


def _output_candidate(output_root: Path) -> Path:
    expanded = _reject_native_lexical(output_root, "output_root")
    _reject_symlink_components(expanded, "output_root")
    if expanded.exists() and not expanded.is_dir():
        raise VideoExportError("ROOT_TYPE", "output_root must be a directory", "output_root")
    return expanded.resolve(strict=False)


def _reject_output_overlap(output_root: Path, sources: set[Path]) -> None:
    candidate = _output_candidate(output_root)
    for source in sorted(sources, key=str):
        resolved = source.resolve(strict=False)
        if candidate == resolved or _within(resolved, candidate) or _within(candidate, resolved):
            raise VideoExportError("OUTPUT_OVERLAPS_SOURCE", f"output_root overlaps source {resolved}", "output_root")


def build_video_export_plan(
    frame_preview_manifest: Path,
    profile_path: Path,
    ffmpeg_path: Path,
    frame_preview_root: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
    profile_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    plan, _plan_bytes, _source_package, _executable_path, _source_paths, sources = _build_plan(
        frame_preview_manifest,
        profile_path,
        ffmpeg_path,
        frame_preview_root,
        frame_render_root,
        renderer_job_root,
        render_plan_root,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
        profile_root,
    )
    _reject_output_overlap(output_root, sources)
    package_id = content_identifier("paper-theater-video-export-package", {"plan_ref": plan["id"]}, 20)
    return {
        "ok": True,
        "video_export_plan": plan,
        "package_path": package_id,
        "executed": False,
        "written": False,
    }
