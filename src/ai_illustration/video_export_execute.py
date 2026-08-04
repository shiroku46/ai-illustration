"""Atomic isolated local execution and publication of a video export."""

from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any

from .naming import safe_relative_path
from .video_export_bindings import _executable_reference
from .video_export_common import (
    MAX_SOURCE_COPY_BYTES, MAX_VIDEO_BYTES, VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT,
    VideoExportError, _json_bytes, _reject_native_lexical,
    _reject_symlink_components, _sha, _sha_file,
)
from .video_export_package import _existing_result, _package_id, _require_flat_regular_files
from .video_export_plan import _build_plan, _reject_output_overlap
from .video_export_process import _run_process


def _copy_bound_sources(work: Path, source_files: dict[str, tuple[Path, str, int]]) -> None:
    total = 0
    for relative, (source, expected_sha, expected_size) in sorted(source_files.items()):
        current_sha, current_size = _sha_file(source, MAX_SOURCE_COPY_BYTES, relative)
        if current_sha != expected_sha or current_size != expected_size:
            raise VideoExportError("SOURCE_CHANGED", f"source changed after plan validation: {relative}", relative)
        total += current_size
        if total > MAX_SOURCE_COPY_BYTES:
            raise VideoExportError("SOURCE_COPY_LIMIT", "isolated source copy exceeds configured limit", relative)
        target = work.joinpath(*safe_relative_path(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise VideoExportError("SOURCE_COPY_FAILED", str(exc), relative) from exc
        copied_sha, copied_size = _sha_file(target, MAX_SOURCE_COPY_BYTES, relative)
        if copied_sha != expected_sha or copied_size != expected_size:
            raise VideoExportError("SOURCE_COPY_MISMATCH", f"isolated source copy differs: {relative}", relative)


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
    plan, plan_bytes, _source_package, executable_path, source_files, sources = _build_plan(
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
        stored_plan = staging / VIDEO_EXPORT_PLAN
        stored_plan.write_bytes(plan_bytes)
        work = staging / "work"
        work.mkdir()
        _copy_bound_sources(work, source_files)
        current_executable, current_executable_path = _executable_reference(executable_path)
        if current_executable != plan["ffmpeg"] or current_executable_path != executable_path:
            raise VideoExportError("FFMPEG_CHANGED", "FFmpeg changed after plan validation", "ffmpeg")
        output_path = staging / VIDEO_OUTPUT
        arguments = [
            str(executable_path) if value == "@FFMPEG@" else str(output_path) if value == "@OUTPUT@" else value
            for value in plan["command_template"]
        ]
        _run_process(arguments, work, timeout_seconds)
        final_executable, final_executable_path = _executable_reference(executable_path)
        if final_executable != plan["ffmpeg"] or final_executable_path != executable_path:
            raise VideoExportError("FFMPEG_CHANGED", "FFmpeg changed during execution", "ffmpeg")
        if output_path.is_symlink() or not output_path.is_file():
            raise VideoExportError("VIDEO_MISSING", "FFmpeg did not create the expected video", VIDEO_OUTPUT)
        video_sha, video_size = _sha_file(output_path, MAX_VIDEO_BYTES, VIDEO_OUTPUT)
        shutil.rmtree(work)
        if stored_plan.is_symlink() or not stored_plan.is_file() or stored_plan.read_bytes() != plan_bytes:
            raise VideoExportError("STAGING_PLAN_CHANGED", "stored export plan changed during execution", VIDEO_EXPORT_PLAN)
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
            "execution": {
                "completed": True,
                "shell": False,
                "network_inputs": False,
                "source_copy_isolated": True,
            },
            "reproducibility_scope": plan["reproducibility_scope"],
        }
        manifest = {"id": package_id, **core}
        manifest["files"] = [
            {"path": VIDEO_EXPORT_PLAN, "sha256": _sha(plan_bytes), "size": len(plan_bytes)},
            {"path": VIDEO_OUTPUT, "sha256": video_sha, "size": video_size},
        ]
        (staging / VIDEO_EXPORT_MANIFEST).write_bytes(_json_bytes(manifest))
        _require_flat_regular_files(
            staging,
            {VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT},
            "STAGING_FILE_SET",
        )
        if stored_plan.read_bytes() != plan_bytes:
            raise VideoExportError("STAGING_PLAN_CHANGED", "stored export plan changed before publication", VIDEO_EXPORT_PLAN)
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
