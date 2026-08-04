"""Canonical validation and deterministic planning for local FFmpeg video exports."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from .frame_preview import (
    FRAME_PREVIEW_MANIFEST,
    FramePreviewError,
    check_frame_preview_package,
)
from .frame_renderer import FrameRenderError, decode_rgba_png
from .naming import SHA256_RE, canonical_json, content_identifier, safe_relative_path

VIDEO_EXPORT_PLAN = "video-export-plan.json"
VIDEO_EXPORT_MANIFEST = "video-export-manifest.json"
VIDEO_OUTPUT = "video.mp4"
PROFILE_KIND = "paper-theater-video-export-profile"
PLAN_KIND = "paper-theater-video-export-plan"
PACKAGE_KIND = "paper-theater-video-export-package"
PROFILE_FAMILY = "mp4-h264-aac-v1"

MAX_FFMPEG_BYTES = 512 * 1024 * 1024
MAX_PROFILE_OUTPUT_BYTES = 8 * 1024 * 1024 * 1024
MAX_FRAME_COUNT = 100_000
MAX_SCENE_DURATION_MS = 86_400_000
PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
}
PROFILE_FIELDS = {
    "id",
    "kind",
    "schema_version",
    "family",
    "container",
    "video_codec",
    "audio_codec",
    "pixel_format",
    "alpha_policy",
    "preset",
    "crf",
    "audio_bitrate_kbps",
    "movflags",
    "ffmpeg_sha256",
    "max_output_bytes",
}


@dataclass
class VideoExportError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


@dataclass(frozen=True)
class VideoExportContext:
    plan: dict[str, Any]
    plan_bytes: bytes
    profile: dict[str, Any]
    profile_bytes: bytes
    ffmpeg_path: Path
    frame_preview: dict[str, Any]
    frame_preview_path: Path
    frame_preview_package: Path
    frames_directory: Path
    audio_path: Path
    source_paths: frozenset[Path]


def json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        raise VideoExportError(
            "SCHEMA_KEYS",
            f"{field} keys must be exactly {sorted(expected)}; got {sorted(value)}",
            field,
        )


def _load_object(path: Path, field: str) -> tuple[dict[str, Any], bytes]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise VideoExportError("DUPLICATE_KEY", f"duplicate JSON key: {key}", field)
            result[key] = value
        return result

    try:
        payload = path.read_bytes()
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=pairs)
    except VideoExportError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VideoExportError("LOAD_ERROR", str(exc), field) from exc
    if not isinstance(value, dict):
        raise VideoExportError("ROOT_TYPE", f"{field} root must be an object", field)
    if payload != json_bytes(value):
        raise VideoExportError("NONCANONICAL_JSON", f"{field} is not canonical JSON", field)
    return value, payload


def _reject_lexical(path: Path, field: str) -> Path:
    raw = str(path)
    if "\x00" in raw or "\\" in raw:
        raise VideoExportError("UNSAFE_PATH", f"{field} contains a forbidden path character", field)
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise VideoExportError("UNSAFE_PATH", f"{field} contains parent traversal", field)
    return expanded


def _reject_symlink_components(path: Path, field: str) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    for candidate in (lexical, *lexical.parents):
        try:
            if candidate.exists() and candidate.is_symlink():
                raise VideoExportError("PATH_SYMLINK", f"{field} contains a symlink component", field)
        except OSError as exc:
            raise VideoExportError("PATH_ERROR", str(exc), field) from exc


def root(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = _reject_lexical(path, field)
    _reject_symlink_components(expanded, field)
    if must_exist and not expanded.is_dir():
        raise VideoExportError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise VideoExportError("ROOT_TYPE", f"{field} must be a directory", field)
    try:
        return expanded.resolve(strict=must_exist)
    except OSError as exc:
        raise VideoExportError("PATH_ERROR", str(exc), field) from exc


def within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def safe_file(base: Path, relative: str, field: str) -> tuple[str, Path]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise VideoExportError("UNSAFE_PATH", str(exc), field) from exc
    candidate = base.joinpath(*safe.parts)
    current = base
    for part in safe.parts:
        current /= part
        if current.is_symlink():
            raise VideoExportError("PATH_SYMLINK", f"{field} contains a symlink component", field)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VideoExportError("FILE_MISSING", str(exc), field) from exc
    if not within(base, resolved):
        raise VideoExportError("PATH_ESCAPE", f"{field} escapes its configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise VideoExportError("FILE_TYPE", f"{field} must be a regular file", field)
    return safe.as_posix(), resolved


def relative_file(path: Path, base: Path, field: str) -> tuple[str, Path]:
    expanded = _reject_lexical(path, field)
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        relative = lexical.relative_to(base).as_posix()
    except ValueError as exc:
        raise VideoExportError("PATH_ESCAPE", f"{field} must be beneath its configured root", field) from exc
    return safe_file(base, relative, field)


def bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise VideoExportError("INTEGER_RANGE", f"{field} must be from {minimum} to {maximum}", field)
    return value


def validate_profile(profile_path: Path, profile_root: Path) -> tuple[dict[str, Any], bytes, str, Path]:
    base = root(profile_root, must_exist=True, field="profile_root")
    relative, resolved = relative_file(profile_path, base, "profile")
    profile, payload = _load_object(resolved, "profile")
    _exact_keys(profile, PROFILE_FIELDS, "profile")
    if profile.get("kind") != PROFILE_KIND or profile.get("schema_version") != "1.0":
        raise VideoExportError("PROFILE_SCHEMA", "profile kind or schema version is invalid", "profile")
    fixed = {
        "family": PROFILE_FAMILY,
        "container": "mp4",
        "video_codec": "libx264",
        "audio_codec": "aac",
        "pixel_format": "yuv420p",
        "alpha_policy": "require-opaque-source",
        "movflags": "+faststart",
    }
    for key, expected in fixed.items():
        if profile.get(key) != expected:
            raise VideoExportError("PROFILE_UNSUPPORTED", f"{key} must be {expected}", key)
    if profile.get("preset") not in PRESETS:
        raise VideoExportError("PROFILE_PRESET", "unsupported libx264 preset", "preset")
    bounded_int(profile.get("crf"), "crf", 0, 51)
    bounded_int(profile.get("audio_bitrate_kbps"), "audio_bitrate_kbps", 32, 512)
    bounded_int(profile.get("max_output_bytes"), "max_output_bytes", 1, MAX_PROFILE_OUTPUT_BYTES)
    ffmpeg_sha = profile.get("ffmpeg_sha256")
    if not isinstance(ffmpeg_sha, str) or not SHA256_RE.fullmatch(ffmpeg_sha):
        raise VideoExportError("PROFILE_FFMPEG_SHA", "ffmpeg_sha256 must be one SHA-256", "ffmpeg_sha256")
    core = {key: value for key, value in profile.items() if key != "id"}
    expected_id = content_identifier(PROFILE_KIND, core, 20)
    if profile.get("id") != expected_id:
        raise VideoExportError("PROFILE_ID", "profile ID is not content-derived", "id")
    return profile, payload, relative, resolved


def validate_ffmpeg(executable: Path, expected_sha: str) -> tuple[Path, dict[str, Any]]:
    expanded = _reject_lexical(executable, "ffmpeg")
    if expanded.is_symlink():
        raise VideoExportError("FFMPEG_SYMLINK", "FFmpeg executable must not be a symlink", "ffmpeg")
    try:
        resolved = expanded.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise VideoExportError("FFMPEG_MISSING", str(exc), "ffmpeg") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise VideoExportError("FFMPEG_TYPE", "FFmpeg path must be a regular file", "ffmpeg")
    if not os.access(resolved, os.X_OK):
        raise VideoExportError("FFMPEG_NOT_EXECUTABLE", "FFmpeg file is not executable", "ffmpeg")
    if not 1 <= metadata.st_size <= MAX_FFMPEG_BYTES:
        raise VideoExportError("FFMPEG_SIZE", "FFmpeg file size is outside the configured limit", "ffmpeg")
    try:
        digest = sha256(resolved.read_bytes())
    except OSError as exc:
        raise VideoExportError("FFMPEG_READ", str(exc), "ffmpeg") from exc
    if digest != expected_sha:
        raise VideoExportError("FFMPEG_CHECKSUM", "FFmpeg checksum does not match the profile", "ffmpeg")
    return resolved, {
        "kind": "local-ffmpeg-executable",
        "sha256": digest,
        "size": metadata.st_size,
    }


def _binding(value: Any, field: str) -> tuple[str, str, str]:
    if not isinstance(value, dict) or set(value) != {"id", "path", "sha256"}:
        raise VideoExportError("BINDING_SCHEMA", f"{field} must be an exact binding", field)
    identifier, path, digest = value.get("id"), value.get("path"), value.get("sha256")
    if not isinstance(identifier, str) or not identifier:
        raise VideoExportError("BINDING_SCHEMA", f"{field}.id is invalid", f"{field}.id")
    if not isinstance(path, str):
        raise VideoExportError("BINDING_SCHEMA", f"{field}.path is invalid", f"{field}.path")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise VideoExportError("BINDING_SCHEMA", f"{field}.sha256 is invalid", f"{field}.sha256")
    return identifier, path, digest


def _scene_seconds(milliseconds: int) -> str:
    return f"{milliseconds // 1000}.{milliseconds % 1000:03d}"


def _trim_seconds(milliseconds: int) -> str:
    return f"{milliseconds // 1000}.{milliseconds % 1000:03d}"


def audio_filter(offset_ms: int, scene_duration_ms: int) -> str:
    duration = _scene_seconds(scene_duration_ms)
    if offset_ms > 0:
        return (
            f"[1:a]adelay=delays={offset_ms}:all=1,apad,"
            f"atrim=duration={duration},asetpts=PTS-STARTPTS[aout]"
        )
    if offset_ms < 0:
        return (
            f"[1:a]atrim=start={_trim_seconds(-offset_ms)},asetpts=PTS-STARTPTS,"
            f"apad,atrim=duration={duration}[aout]"
        )
    return f"[1:a]asetpts=PTS-STARTPTS,apad,atrim=duration={duration}[aout]"


def _argument_template(
    profile: dict[str, Any],
    fps_num: int,
    fps_den: int,
    frame_count: int,
    scene_duration_ms: int,
    filter_graph: str,
) -> list[str]:
    return [
        "{ffmpeg}",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-framerate",
        f"{fps_num}/{fps_den}",
        "-start_number",
        "0",
        "-i",
        "{frames}/%08d.png",
        "-i",
        "{audio}",
        "-filter_complex",
        filter_graph,
        "-map",
        "0:v:0",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        str(profile["preset"]),
        "-crf",
        str(profile["crf"]),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        f"{profile['audio_bitrate_kbps']}k",
        "-movflags",
        "+faststart",
        "-frames:v",
        str(frame_count),
        "-t",
        _scene_seconds(scene_duration_ms),
        "{output}",
    ]


def _validate_opaque_frames(
    preview_manifest: dict[str, Any],
    preview_package: Path,
) -> tuple[Path, Path]:
    canvas = preview_manifest.get("canvas")
    if not isinstance(canvas, dict):
        raise VideoExportError("PREVIEW_CANVAS", "frame preview canvas is missing", "canvas")
    width = bounded_int(canvas.get("width"), "canvas.width", 1, 8192)
    height = bounded_int(canvas.get("height"), "canvas.height", 1, 8192)
    if width % 2 or height % 2:
        raise VideoExportError("ODD_DIMENSIONS", "mp4-h264-aac-v1 requires even dimensions", "canvas")
    inventory_id, inventory_relative, inventory_sha = _binding(
        preview_manifest.get("frame_inventory"), "frame_inventory"
    )
    normalized_inventory, inventory_path = safe_file(
        preview_package, inventory_relative, "frame_inventory.path"
    )
    if normalized_inventory != "frame-inventory.json":
        raise VideoExportError("FRAME_INVENTORY_PATH", "frame inventory path is not canonical", "frame_inventory")
    inventory, inventory_bytes = _load_object(inventory_path, "frame_inventory")
    if inventory.get("id") != inventory_id or sha256(inventory_bytes) != inventory_sha:
        raise VideoExportError("FRAME_INVENTORY_BINDING", "frame inventory binding is stale", "frame_inventory")
    frames = inventory.get("frames")
    frame_count = bounded_int(preview_manifest.get("frame_count"), "frame_count", 1, MAX_FRAME_COUNT)
    if not isinstance(frames, list) or len(frames) != frame_count:
        raise VideoExportError("FRAME_INVENTORY_COUNT", "frame inventory count is invalid", "frame_inventory")
    frames_directory = preview_package / "frames"
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict) or frame.get("index") != index:
            raise VideoExportError("FRAME_ORDER", "frame indices must be contiguous", f"frames[{index}]")
        expected_relative = f"frames/{index:08d}.png"
        if frame.get("path") != expected_relative:
            raise VideoExportError("FRAME_PATH", "frame path is not canonical", f"frames[{index}].path")
        _, frame_path = safe_file(preview_package, expected_relative, f"frames[{index}].path")
        payload = frame_path.read_bytes()
        if frame.get("size") != len(payload) or frame.get("sha256") != sha256(payload):
            raise VideoExportError("FRAME_BINDING", "frame bytes do not match the inventory", expected_relative)
        try:
            image = decode_rgba_png(payload, expected_width=width, expected_height=height)
        except FrameRenderError as exc:
            raise VideoExportError(f"FRAME_{exc.code}", exc.message, expected_relative) from exc
        if any(alpha != 255 for alpha in image.pixels[3::4]):
            raise VideoExportError("NON_OPAQUE_FRAME", "all source frame pixels must be opaque", expected_relative)
    audio = preview_manifest.get("audio")
    if not isinstance(audio, dict) or not isinstance(audio.get("path"), str):
        raise VideoExportError("PREVIEW_AUDIO", "frame preview audio binding is missing", "audio")
    audio_relative = audio["path"]
    _, audio_path = safe_file(preview_package, audio_relative, "audio.path")
    payload = audio_path.read_bytes()
    if audio.get("size") != len(payload) or audio.get("sha256") != sha256(payload):
        raise VideoExportError("AUDIO_BINDING", "audio bytes do not match the frame preview", "audio")
    return frames_directory.resolve(), audio_path


def build_plan_context(
    frame_preview_manifest: Path,
    profile_path: Path,
    ffmpeg: Path,
    *,
    frame_preview_root: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
    profile_root: Path,
) -> VideoExportContext:
    frame_preview_base = root(frame_preview_root, must_exist=True, field="frame_preview_root")
    frame_render_base = root(frame_render_root, must_exist=True, field="frame_render_root")
    renderer_base = root(renderer_job_root, must_exist=True, field="renderer_job_root")
    render_plan_base = root(render_plan_root, must_exist=True, field="render_plan_root")
    audio_preview_base = root(audio_preview_root, must_exist=True, field="audio_preview_root")
    preview_base = root(preview_root, must_exist=True, field="preview_root")
    package_base = root(package_root, must_exist=True, field="package_root")
    audio_base = root(audio_root, must_exist=True, field="audio_root")
    profile_base = root(profile_root, must_exist=True, field="profile_root")
    preview_relative, preview_path = relative_file(
        frame_preview_manifest, frame_preview_base, "frame_preview_manifest"
    )
    try:
        checked = check_frame_preview_package(
            preview_path,
            frame_preview_base,
            frame_render_base,
            renderer_base,
            render_plan_base,
            audio_preview_base,
            preview_base,
            package_base,
            audio_base,
        )
    except FramePreviewError as exc:
        raise VideoExportError(f"FRAME_PREVIEW_{exc.code}", exc.message, exc.field) from exc
    preview_manifest = checked.get("frame_preview")
    if not isinstance(preview_manifest, dict):
        raise VideoExportError("FRAME_PREVIEW_RESULT", "frame-preview checker result is malformed", "frame_preview")
    canonical_preview, preview_bytes = _load_object(preview_path, "frame_preview")
    if canonical_preview != preview_manifest:
        raise VideoExportError("FRAME_PREVIEW_RESULT", "frame-preview checker returned different data", "frame_preview")
    expected_preview_path = frame_preview_base / str(preview_manifest.get("id")) / FRAME_PREVIEW_MANIFEST
    if preview_path != expected_preview_path.resolve():
        raise VideoExportError("FRAME_PREVIEW_LOCATION", "frame-preview manifest path is not canonical", "frame_preview")
    preview_package = preview_path.parent.resolve()
    frames_directory, source_audio = _validate_opaque_frames(preview_manifest, preview_package)
    profile, profile_bytes, profile_relative, _ = validate_profile(profile_path, profile_base)
    ffmpeg_path, ffmpeg_binding = validate_ffmpeg(ffmpeg, str(profile["ffmpeg_sha256"]))
    fps_num = bounded_int(preview_manifest.get("fps_num"), "fps_num", 1, 1_000_000)
    fps_den = bounded_int(preview_manifest.get("fps_den"), "fps_den", 1, 1_000_000)
    frame_count = bounded_int(preview_manifest.get("frame_count"), "frame_count", 1, MAX_FRAME_COUNT)
    scene_duration = bounded_int(
        preview_manifest.get("scene_duration_ms"),
        "scene_duration_ms",
        1,
        MAX_SCENE_DURATION_MS,
    )
    placement = preview_manifest.get("audio_placement")
    if not isinstance(placement, dict):
        raise VideoExportError("AUDIO_PLACEMENT", "audio placement is missing", "audio_placement")
    offset_ms = bounded_int(placement.get("offset_ms"), "audio_placement.offset_ms", -MAX_SCENE_DURATION_MS, MAX_SCENE_DURATION_MS)
    filter_graph = audio_filter(offset_ms, scene_duration)
    core = {
        "kind": PLAN_KIND,
        "schema_version": "1.0",
        "source_frame_preview": {
            "id": preview_manifest["id"],
            "path": preview_relative,
            "sha256": sha256(preview_bytes),
        },
        "profile": {
            "id": profile["id"],
            "path": profile_relative,
            "sha256": sha256(profile_bytes),
        },
        "ffmpeg": ffmpeg_binding,
        "intent": preview_manifest["intent"],
        "canvas": {"width": preview_manifest["canvas"]["width"], "height": preview_manifest["canvas"]["height"]},
        "fps_num": fps_num,
        "fps_den": fps_den,
        "frame_count": frame_count,
        "scene_duration_ms": scene_duration,
        "audio_placement": placement,
        "audio_filter": filter_graph,
        "argument_template": _argument_template(
            profile,
            fps_num,
            fps_den,
            frame_count,
            scene_duration,
            filter_graph,
        ),
        "output": {
            "filename": VIDEO_OUTPUT,
            "extension": ".mp4",
            "container": "mp4",
            "video_codec": "libx264",
            "audio_codec": "aac",
            "pixel_format": "yuv420p",
        },
        "safety_limits": {
            "max_output_bytes": profile["max_output_bytes"],
            "max_diagnostic_bytes": 1_048_576,
        },
        "reproducibility_scope": "exact-source-profile-ffmpeg-binding-and-recorded-output-bytes",
        "media_created": False,
    }
    plan_id = content_identifier(PLAN_KIND, core, 20)
    plan = {"id": plan_id, **core}
    source_paths = frozenset(
        {
            frame_preview_base,
            frame_render_base,
            renderer_base,
            render_plan_base,
            audio_preview_base,
            preview_base,
            package_base,
            audio_base,
            profile_base,
            ffmpeg_path,
        }
    )
    return VideoExportContext(
        plan=plan,
        plan_bytes=json_bytes(plan),
        profile=profile,
        profile_bytes=profile_bytes,
        ffmpeg_path=ffmpeg_path,
        frame_preview=preview_manifest,
        frame_preview_path=preview_path,
        frame_preview_package=preview_package,
        frames_directory=frames_directory,
        audio_path=source_audio,
        source_paths=source_paths,
    )


def output_candidate(path: Path) -> Path:
    expanded = _reject_lexical(path, "output_root")
    _reject_symlink_components(expanded, "output_root")
    if expanded.exists() and not expanded.is_dir():
        raise VideoExportError("ROOT_TYPE", "output_root must be a directory", "output_root")
    return expanded.resolve(strict=False)


def reject_output_overlap(output_root: Path, sources: frozenset[Path]) -> Path:
    candidate = output_candidate(output_root)
    for source in sorted(sources, key=str):
        resolved = source.resolve(strict=False)
        if candidate == resolved or within(resolved, candidate) or within(candidate, resolved):
            raise VideoExportError(
                "OUTPUT_OVERLAPS_SOURCE",
                f"output_root overlaps source {resolved}",
                "output_root",
            )
    return candidate


def plan_video_export(
    frame_preview_manifest: Path,
    profile_path: Path,
    ffmpeg: Path,
    output_root: Path,
    **roots: Path,
) -> dict[str, Any]:
    context = build_plan_context(
        frame_preview_manifest,
        profile_path,
        ffmpeg,
        **roots,
    )
    reject_output_overlap(output_root, context.source_paths)
    return {
        "ok": True,
        "video_export_plan": context.plan,
        "plan_path": context.plan["id"],
        "executed": False,
        "media_created": False,
    }
