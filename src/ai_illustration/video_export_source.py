"""Verified Phase 13 source inspection and FFmpeg argument planning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .frame_preview import FRAME_PREVIEW_MANIFEST, FramePreviewError, check_frame_preview_package
from .frame_renderer import FrameRenderError, decode_rgba_png
from .video_export_bindings import _file_inventory, _verify_file
from .video_export_common import (
    MAX_FRAME_COUNT, MAX_VIDEO_BYTES, VideoExportError, _bounded_int,
    _canonical_object, _relative_file, _root, _sha,
)

def _source_reference(
    manifest_path: Path,
    frame_preview_root: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
) -> tuple[dict[str, Any], str, Path, bytes, Path, dict[str, Path]]:
    preview_base = _root(frame_preview_root, must_exist=True, field="frame_preview_root")
    relative, resolved = _relative_file(manifest_path, preview_base, "frame_preview_manifest")
    try:
        checked = check_frame_preview_package(
            resolved,
            preview_base,
            frame_render_root,
            renderer_job_root,
            render_plan_root,
            audio_preview_root,
            preview_root,
            package_root,
            audio_root,
        )
    except FramePreviewError as exc:
        raise VideoExportError(f"FRAME_PREVIEW_{exc.code}", exc.message, exc.field or "frame_preview_manifest") from exc
    manifest = checked.get("frame_preview")
    if not isinstance(manifest, dict):
        raise VideoExportError("FRAME_PREVIEW_RESULT", "frame-preview checker result is malformed", "frame_preview_manifest")
    canonical, payload = _canonical_object(resolved, "frame_preview_manifest")
    if canonical != manifest:
        raise VideoExportError("FRAME_PREVIEW_RESULT", "frame-preview checker returned different manifest data", "frame_preview_manifest")
    package_id = manifest.get("id")
    if not isinstance(package_id, str):
        raise VideoExportError("FRAME_PREVIEW_SCHEMA", "frame-preview ID is missing", "id")
    expected = preview_base / package_id / FRAME_PREVIEW_MANIFEST
    if resolved != expected.resolve():
        raise VideoExportError("FRAME_PREVIEW_LOCATION", "frame-preview manifest path is not canonical", "frame_preview_manifest")
    package = resolved.parent.resolve()
    inventory = _file_inventory(manifest)
    frame_count = _bounded_int(manifest.get("frame_count"), "frame_count", 1, MAX_FRAME_COUNT)
    canvas = manifest.get("canvas")
    if not isinstance(canvas, dict):
        raise VideoExportError("CANVAS_SCHEMA", "source canvas is missing", "canvas")
    width = _bounded_int(canvas.get("width"), "canvas.width", 1, 8192)
    height = _bounded_int(canvas.get("height"), "canvas.height", 1, 8192)
    if width % 2 or height % 2:
        raise VideoExportError("ODD_DIMENSIONS", "mp4-h264-aac-v1 requires even width and height", "canvas")
    paths: dict[str, Path] = {}
    for index in range(frame_count):
        relative_frame = f"frames/{index:08d}.png"
        frame_path = _verify_file(package, inventory, relative_frame, f"frames[{index}]")
        try:
            image = decode_rgba_png(frame_path.read_bytes(), expected_width=width, expected_height=height)
        except FrameRenderError as exc:
            raise VideoExportError(f"FRAME_{exc.code}", exc.message, relative_frame) from exc
        if any(image.pixels[offset] != 255 for offset in range(3, len(image.pixels), 4)):
            raise VideoExportError("NONOPAQUE_FRAME", "profile requires every source pixel to be opaque", relative_frame)
        paths[relative_frame] = frame_path
    audio = manifest.get("audio")
    if not isinstance(audio, dict) or not isinstance(audio.get("path"), str):
        raise VideoExportError("AUDIO_SCHEMA", "source audio binding is missing", "audio")
    audio_path = _verify_file(package, inventory, audio["path"], "audio.path")
    paths["audio"] = audio_path
    return manifest, relative, resolved, payload, package, paths


def _audio_filter(offset_ms: int, scene_duration_ms: int) -> str:
    if offset_ms > 0:
        return (
            f"[1:a]asetpts=PTS-STARTPTS,adelay={offset_ms}:all=1,"
            f"apad,atrim=end={scene_duration_ms}ms[aout]"
        )
    if offset_ms < 0:
        return (
            f"[1:a]atrim=start={abs(offset_ms)}ms,asetpts=PTS-STARTPTS,"
            f"apad,atrim=end={scene_duration_ms}ms[aout]"
        )
    return f"[1:a]asetpts=PTS-STARTPTS,apad,atrim=end={scene_duration_ms}ms[aout]"


def _command_template(
    source: dict[str, Any],
    profile: dict[str, Any],
    audio_relative: str,
    audio_filter: str,
) -> list[str]:
    fps_num = _bounded_int(source.get("fps_num"), "fps_num", 1, 1_000_000)
    fps_den = _bounded_int(source.get("fps_den"), "fps_den", 1, 1_000_000)
    frame_count = _bounded_int(source.get("frame_count"), "frame_count", 1, MAX_FRAME_COUNT)
    scene_duration = _bounded_int(source.get("scene_duration_ms"), "scene_duration_ms", 1, 24 * 60 * 60 * 1000)
    video = profile["video"]
    audio = profile["audio"]
    return [
        "@FFMPEG@",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "image2",
        "-framerate",
        f"{fps_num}/{fps_den}",
        "-start_number",
        "0",
        "-i",
        "frames/%08d.png",
        "-i",
        audio_relative,
        "-filter_complex",
        audio_filter,
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-frames:v",
        str(frame_count),
        "-c:v",
        video["codec"],
        "-preset",
        video["preset"],
        "-crf",
        str(video["crf"]),
        "-pix_fmt",
        video["pixel_format"],
        "-fps_mode",
        "passthrough",
        "-threads:v",
        "1",
        "-c:a",
        audio["codec"],
        "-b:a",
        f"{audio['bitrate_kbps']}k",
        "-threads:a",
        "1",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-metadata",
        "creation_time=1970-01-01T00:00:00Z",
        "-movflags",
        "+faststart",
        "-t",
        f"{scene_duration}ms",
        "-shortest",
        "-fs",
        str(MAX_VIDEO_BYTES),
        "-f",
        profile["container"],
        "@OUTPUT@",
    ]
