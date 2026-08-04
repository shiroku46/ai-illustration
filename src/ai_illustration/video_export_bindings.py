"""Canonical profile, executable, and file binding checks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .naming import SHA256_RE, content_identifier
from .video_export_common import (
    MAX_FFMPEG_BYTES, MAX_VIDEO_BYTES, PRESETS, PROFILE_FAMILY,
    VideoExportError, _bounded_int, _canonical_object, _exact_keys,
    _relative_file, _reject_native_lexical, _reject_symlink_components,
    _root, _safe_file, _sha_file,
)

def _profile_reference(profile_path: Path, profile_root: Path) -> tuple[dict[str, Any], str, Path, bytes]:
    root = _root(profile_root, must_exist=True, field="profile_root")
    relative, resolved = _relative_file(profile_path, root, "profile")
    profile, payload = _canonical_object(resolved, "profile")
    _exact_keys(
        profile,
        {
            "id",
            "kind",
            "schema_version",
            "family",
            "container",
            "extension",
            "alpha_policy",
            "frame_policy",
            "metadata_policy",
            "video",
            "audio",
        },
        "profile",
    )
    if profile.get("kind") != "paper-theater-video-export-profile" or profile.get("schema_version") != "1.0":
        raise VideoExportError("PROFILE_SCHEMA", "profile kind or schema version is invalid", "profile")
    fixed = {
        "family": PROFILE_FAMILY,
        "container": "mp4",
        "extension": "mp4",
        "alpha_policy": "require-opaque-source",
        "frame_policy": "exact-numbered-sequence-no-resize",
        "metadata_policy": "strip-input-and-fix-creation-time",
    }
    for key, expected in fixed.items():
        if profile.get(key) != expected:
            raise VideoExportError("PROFILE_VALUE", f"profile.{key} must be {expected!r}", f"profile.{key}")
    video = profile.get("video")
    audio = profile.get("audio")
    if not isinstance(video, dict) or not isinstance(audio, dict):
        raise VideoExportError("PROFILE_SCHEMA", "profile video and audio sections are required", "profile")
    _exact_keys(video, {"codec", "pixel_format", "preset", "crf"}, "profile.video")
    _exact_keys(audio, {"codec", "bitrate_kbps"}, "profile.audio")
    if video.get("codec") != "libx264" or video.get("pixel_format") != "yuv420p":
        raise VideoExportError("PROFILE_VALUE", "video codec/pixel format must be libx264/yuv420p", "profile.video")
    if video.get("preset") not in PRESETS:
        raise VideoExportError("PROFILE_VALUE", f"video preset must be one of {PRESETS}", "profile.video.preset")
    _bounded_int(video.get("crf"), "profile.video.crf", 0, 51)
    if audio.get("codec") != "aac":
        raise VideoExportError("PROFILE_VALUE", "audio codec must be aac", "profile.audio.codec")
    _bounded_int(audio.get("bitrate_kbps"), "profile.audio.bitrate_kbps", 32, 512)
    core = {key: value for key, value in profile.items() if key != "id"}
    expected_id = content_identifier("paper-theater-video-export-profile", core, 20)
    if profile.get("id") != expected_id:
        raise VideoExportError("PROFILE_ID", "profile ID does not match canonical content", "profile.id")
    return profile, relative, resolved, payload


def _executable_reference(path: Path) -> tuple[dict[str, Any], Path]:
    expanded = _reject_native_lexical(path, "ffmpeg")
    _reject_symlink_components(expanded, "ffmpeg")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise VideoExportError("FFMPEG_MISSING", str(exc), "ffmpeg") from exc
    if expanded.is_symlink() or not resolved.is_file():
        raise VideoExportError("FFMPEG_TYPE", "ffmpeg must be a non-symlink regular file", "ffmpeg")
    if not os.access(resolved, os.X_OK):
        raise VideoExportError("FFMPEG_NOT_EXECUTABLE", "ffmpeg file is not executable", "ffmpeg")
    sha256, size = _sha_file(resolved, MAX_FFMPEG_BYTES, "ffmpeg")
    return {"name": resolved.name, "sha256": sha256, "size": size}, resolved


def _file_inventory(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise VideoExportError("SOURCE_FILE_INVENTORY", "frame-preview file inventory is missing", "files")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise VideoExportError("SOURCE_FILE_INVENTORY", "file entry must be an object", f"files[{index}]")
        relative = item.get("path")
        sha256 = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(relative, str)
            or not isinstance(sha256, str)
            or not SHA256_RE.fullmatch(sha256)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or relative in result
        ):
            raise VideoExportError("SOURCE_FILE_INVENTORY", "file entry is invalid or duplicated", f"files[{index}]")
        result[relative] = item
    return result


def _verify_file(package: Path, inventory: dict[str, dict[str, Any]], relative: str, field: str) -> Path:
    item = inventory.get(relative)
    if item is None:
        raise VideoExportError("SOURCE_FILE_BINDING", f"source inventory does not contain {relative}", field)
    normalized, resolved = _safe_file(package, relative, field)
    sha256, size = _sha_file(resolved, MAX_VIDEO_BYTES, field)
    if normalized != relative or sha256 != item["sha256"] or size != item["size"]:
        raise VideoExportError("SOURCE_FILE_MISMATCH", f"source file differs: {relative}", field)
    return resolved
