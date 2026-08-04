"""Integrity checking for published video-export packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .video_export_common import (
    MAX_VIDEO_BYTES, VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT,
    VideoExportError, _canonical_object, _json_bytes, _relative_file,
    _root, _safe_file, _sha_file,
)
from .video_export_package import _package_id, _validate_result_manifest
from .video_export_plan import _build_plan, _reject_output_overlap

def check_video_export_package(
    manifest_path: Path,
    profile_path: Path,
    ffmpeg_path: Path,
    output_root: Path,
    frame_preview_root: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
    profile_root: Path,
) -> dict[str, Any]:
    root = _root(output_root, must_exist=True, field="output_root")
    _, resolved = _relative_file(manifest_path, root, "video_export_manifest")
    manifest, payload = _canonical_object(resolved, "video_export_manifest")
    package_id = manifest.get("id")
    if not isinstance(package_id, str):
        raise VideoExportError("MANIFEST_SCHEMA", "video export package ID is missing", "id")
    canonical = root / package_id / VIDEO_EXPORT_MANIFEST
    if resolved != canonical.resolve():
        raise VideoExportError("MANIFEST_LOCATION", "video export manifest path is not canonical", "video_export_manifest")
    source_binding = manifest.get("source_frame_preview")
    if not isinstance(source_binding, dict) or not isinstance(source_binding.get("path"), str):
        raise VideoExportError("MANIFEST_SCHEMA", "source frame-preview binding is missing", "source_frame_preview")
    preview_base = _root(frame_preview_root, must_exist=True, field="frame_preview_root")
    _, source_manifest = _safe_file(preview_base, source_binding["path"], "source_frame_preview.path")
    plan, plan_bytes, _source_package, _executable_path, sources = _build_plan(
        source_manifest,
        profile_path,
        ffmpeg_path,
        preview_base,
        frame_render_root,
        renderer_job_root,
        render_plan_root,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
        profile_root,
    )
    _reject_output_overlap(root, sources)
    if package_id != _package_id(plan):
        raise VideoExportError("MANIFEST_BINDING_MISMATCH", "package ID differs from current plan", "id")
    destination = root / package_id
    actual: set[str] = set()
    for candidate in destination.rglob("*"):
        if candidate.is_symlink():
            raise VideoExportError("PACKAGE_SYMLINK", "video export package contains a symlink", str(candidate))
        if candidate.is_file():
            actual.add(candidate.relative_to(destination).as_posix())
    expected = {VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT}
    if actual != expected:
        raise VideoExportError(
            "FILE_SET_MISMATCH",
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}",
            str(destination),
        )
    if (destination / VIDEO_EXPORT_PLAN).read_bytes() != plan_bytes:
        raise VideoExportError("PLAN_MISMATCH", "stored export plan differs from current source/profile/executable", VIDEO_EXPORT_PLAN)
    video_sha, video_size = _sha_file(destination / VIDEO_OUTPUT, MAX_VIDEO_BYTES, VIDEO_OUTPUT)
    declared_video = manifest.get("video")
    if not isinstance(declared_video, dict) or declared_video.get("sha256") != video_sha or declared_video.get("size") != video_size:
        raise VideoExportError("VIDEO_MISMATCH", "video bytes differ from manifest", VIDEO_OUTPUT)
    _validate_result_manifest(manifest, package_id, plan, plan_bytes, video_sha, video_size)
    if payload != _json_bytes(manifest):
        raise VideoExportError("MANIFEST_BINDING_MISMATCH", "video export manifest is noncanonical", VIDEO_EXPORT_MANIFEST)
    return {
        "ok": True,
        "video_export": manifest,
        "file_count": len(expected),
        "video_size": video_size,
    }
