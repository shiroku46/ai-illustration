"""Video-export package identity and existing-output validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .naming import content_identifier
from .video_export_common import (
    MAX_VIDEO_BYTES, VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT,
    VideoExportError, _canonical_object, _json_bytes, _sha, _sha_file,
)

def _package_id(plan: dict[str, Any]) -> str:
    return content_identifier("paper-theater-video-export-package", {"plan_ref": plan["id"]}, 20)


def _validate_result_manifest(
    manifest: dict[str, Any],
    package_id: str,
    plan: dict[str, Any],
    plan_bytes: bytes,
    video_sha: str,
    video_size: int,
) -> None:
    if manifest.get("id") != package_id or manifest.get("kind") != "paper-theater-video-export-package" or manifest.get("schema_version") != "1.0":
        raise VideoExportError("MANIFEST_BINDING_MISMATCH", "video export identity or schema changed", VIDEO_EXPORT_MANIFEST)
    expected_plan = {"id": plan["id"], "path": VIDEO_EXPORT_PLAN, "sha256": _sha(plan_bytes)}
    if manifest.get("plan") != expected_plan:
        raise VideoExportError("PLAN_BINDING_MISMATCH", "manifest plan binding is invalid", "plan")
    for key in ("source_frame_preview", "profile", "ffmpeg", "intent", "audio_license_status", "reproducibility_scope"):
        if manifest.get(key) != plan.get(key):
            raise VideoExportError("MANIFEST_BINDING_MISMATCH", f"manifest {key} binding changed", key)
    expected_video = {
        "path": VIDEO_OUTPUT,
        "sha256": video_sha,
        "size": video_size,
        "container": plan["output"]["container"],
        "extension": plan["output"]["extension"],
    }
    if manifest.get("video") != expected_video:
        raise VideoExportError("VIDEO_BINDING", "video binding differs from actual bytes or plan", "video")
    if manifest.get("execution") != {"completed": True, "shell": False, "network": False}:
        raise VideoExportError("EXECUTION_BINDING", "execution safety record is invalid", "execution")
    if manifest.get("files") != [
        {"path": VIDEO_EXPORT_PLAN, "sha256": _sha(plan_bytes), "size": len(plan_bytes)},
        {"path": VIDEO_OUTPUT, "sha256": video_sha, "size": video_size},
    ]:
        raise VideoExportError("FILE_INVENTORY", "result file inventory is invalid", "files")


def _existing_result(destination: Path, plan: dict[str, Any], plan_bytes: bytes) -> dict[str, Any] | None:
    if not destination.exists():
        return None
    if destination.is_symlink() or not destination.is_dir():
        raise VideoExportError("OUTPUT_CONFLICT", "video export destination is not a regular directory", "output_root")
    manifest_path = destination / VIDEO_EXPORT_MANIFEST
    plan_path = destination / VIDEO_EXPORT_PLAN
    video_path = destination / VIDEO_OUTPUT
    for path, field in ((manifest_path, VIDEO_EXPORT_MANIFEST), (plan_path, VIDEO_EXPORT_PLAN), (video_path, VIDEO_OUTPUT)):
        if path.is_symlink() or not path.is_file():
            raise VideoExportError("OUTPUT_CONFLICT", f"existing package is missing {field}", field)
    actual: set[str] = set()
    for candidate in destination.rglob("*"):
        if candidate.is_symlink():
            raise VideoExportError("OUTPUT_CONFLICT", "existing package contains a symlink", str(candidate))
        if candidate.is_file():
            actual.add(candidate.relative_to(destination).as_posix())
    expected = {VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT}
    if actual != expected:
        raise VideoExportError("OUTPUT_CONFLICT", "existing package file set differs", "output_root")
    if plan_path.read_bytes() != plan_bytes:
        raise VideoExportError("OUTPUT_CONFLICT", "existing export plan differs", VIDEO_EXPORT_PLAN)
    manifest, manifest_bytes = _canonical_object(manifest_path, VIDEO_EXPORT_MANIFEST)
    if manifest_bytes != _json_bytes(manifest):
        raise VideoExportError("OUTPUT_CONFLICT", "existing manifest is not canonical", VIDEO_EXPORT_MANIFEST)
    sha256, size = _sha_file(video_path, MAX_VIDEO_BYTES, VIDEO_OUTPUT)
    try:
        _validate_result_manifest(manifest, destination.name, plan, plan_bytes, sha256, size)
    except VideoExportError as exc:
        raise VideoExportError("OUTPUT_CONFLICT", exc.message, exc.field) from exc
    return manifest
