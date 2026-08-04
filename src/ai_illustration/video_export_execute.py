"""Atomic local execution and publication of a video export."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

from .video_export_common import (
    MAX_VIDEO_BYTES, VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT,
    VideoExportError, _json_bytes, _reject_native_lexical,
    _reject_symlink_components, _sha, _sha_file,
)
from .video_export_package import _existing_result, _package_id
from .video_export_plan import _build_plan, _reject_output_overlap
from .video_export_process import _run_process

def run_video_export(
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
    *,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    plan, plan_bytes, source_package, executable_path, sources = _build_plan(
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
    root_path = _reject_native_lexical(output_root, "output_root")
    _reject_symlink_components(root_path, "output_root")
    if root_path.exists() and not root_path.is_dir():
        raise VideoExportError("ROOT_TYPE", "output_root must be a directory", "output_root")
    root_path.mkdir(parents=True, exist_ok=True)
    root = root_path.resolve()
    package_id = _package_id(plan)
    destination = root / package_id
    existing = _existing_result(destination, plan, plan_bytes)
    if existing is not None:
        return {
            "ok": True,
            "video_export": existing,
            "package_path": package_id,
            "executed": False,
            "written": False,
        }
    staging = root / f".{package_id}.tmp"
    if staging.exists():
        if staging.is_symlink():
            raise VideoExportError("STAGING_CONFLICT", "staging path is a symlink", "output_root")
        shutil.rmtree(staging)
    try:
        staging.mkdir()
        (staging / VIDEO_EXPORT_PLAN).write_bytes(plan_bytes)
        output_path = staging / VIDEO_OUTPUT
        arguments = [
            str(executable_path) if value == "@FFMPEG@" else str(output_path) if value == "@OUTPUT@" else value
            for value in plan["command_template"]
        ]
        _run_process(arguments, source_package, timeout_seconds)
        if output_path.is_symlink() or not output_path.is_file():
            raise VideoExportError("VIDEO_MISSING", "FFmpeg did not create the expected video", VIDEO_OUTPUT)
        video_sha, video_size = _sha_file(output_path, MAX_VIDEO_BYTES, VIDEO_OUTPUT)
        core = {
            "kind": "paper-theater-video-export-package",
            "schema_version": "1.0",
            "plan": {
                "id": plan["id"],
                "path": VIDEO_EXPORT_PLAN,
                "sha256": _sha(plan_bytes),
            },
            "source_frame_preview": plan["source_frame_preview"],
            "profile": plan["profile"],
            "ffmpeg": plan["ffmpeg"],
            "intent": plan["intent"],
            "audio_license_status": plan["audio_license_status"],
            "video": {
                "path": VIDEO_OUTPUT,
                "sha256": video_sha,
                "size": video_size,
                "container": plan["output"]["container"],
                "extension": plan["output"]["extension"],
            },
            "execution": {"completed": True, "shell": False, "network": False},
            "reproducibility_scope": plan["reproducibility_scope"],
        }
        manifest = {"id": package_id, **core}
        manifest["files"] = [
            {"path": VIDEO_EXPORT_PLAN, "sha256": _sha(plan_bytes), "size": len(plan_bytes)},
            {"path": VIDEO_OUTPUT, "sha256": video_sha, "size": video_size},
        ]
        (staging / VIDEO_EXPORT_MANIFEST).write_bytes(_json_bytes(manifest))
        if destination.exists():
            raise VideoExportError("OUTPUT_CONFLICT", "destination appeared during execution", "output_root")
        staging.replace(destination)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    return {
        "ok": True,
        "video_export": manifest,
        "package_path": package_id,
        "executed": True,
        "written": True,
    }
