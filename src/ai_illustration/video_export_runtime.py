"""Bounded FFmpeg execution, atomic publication, and checking for Phase 14."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
from typing import Any

from .frame_preview import FRAME_PREVIEW_MANIFEST
from .naming import SHA256_RE, content_identifier
from .video_export_core import (
    MAX_FFMPEG_BYTES,
    PACKAGE_KIND,
    VIDEO_EXPORT_MANIFEST,
    VIDEO_EXPORT_PLAN,
    VIDEO_OUTPUT,
    VideoExportContext,
    VideoExportError,
    _load_object,
    bounded_int,
    build_plan_context,
    json_bytes,
    reject_output_overlap,
    relative_file,
    root,
    safe_file,
    sha256,
)

MAX_TIMEOUT_SECONDS = 3600
MAX_DIAGNOSTIC_BYTES = 1_048_576
STREAM_CHUNK_BYTES = 1024 * 1024
MANIFEST_FIELDS = {
    "id",
    "kind",
    "schema_version",
    "plan",
    "source_frame_preview",
    "profile",
    "ffmpeg",
    "intent",
    "video",
    "reproducibility_scope",
    "files",
}


def _cleanup(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _stream_sha256(path: Path, *, maximum: int, field: str) -> tuple[str, int]:
    try:
        before = path.stat()
    except OSError as exc:
        raise VideoExportError("FILE_STAT", str(exc), field) from exc
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise VideoExportError("FILE_TYPE", f"{field} must be a regular non-symlink file", field)
    if before.st_size < 0 or before.st_size > maximum:
        raise VideoExportError("FILE_SIZE", f"{field} exceeds the configured limit", field)
    total = 0
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise VideoExportError("FILE_SIZE", f"{field} exceeds the configured limit", field)
                hasher.update(chunk)
        after = path.stat()
    except VideoExportError:
        raise
    except OSError as exc:
        raise VideoExportError("FILE_READ", str(exc), field) from exc
    if (
        total != before.st_size
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise VideoExportError("FILE_CHANGED", f"{field} changed while hashing", field)
    return hasher.hexdigest(), total


def _actual_arguments(context: VideoExportContext, staging: Path) -> list[str]:
    replacements = {
        "{ffmpeg}": str(context.ffmpeg_path),
        "{frames}": str(context.frames_directory),
        "{audio}": str(context.audio_path),
        "{output}": str(staging / VIDEO_OUTPUT),
    }
    arguments: list[str] = []
    for value in context.plan["argument_template"]:
        replaced = value
        for placeholder, actual in replacements.items():
            replaced = replaced.replace(placeholder, actual)
        if "{" in replaced or "}" in replaced:
            raise VideoExportError("ARGUMENT_PLACEHOLDER", "unresolved argument placeholder", "argument_template")
        arguments.append(replaced)
    if arguments[0] != str(context.ffmpeg_path) or arguments[-1] != str(staging / VIDEO_OUTPUT):
        raise VideoExportError("ARGUMENT_BINDING", "FFmpeg argument binding is invalid", "argument_template")
    return arguments


def _sanitized_environment(staging: Path) -> dict[str, str]:
    environment = {
        "HOME": str(staging),
        "TMPDIR": str(staging),
        "TMP": str(staging),
        "TEMP": str(staging),
        "PATH": "",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    if os.name == "nt" and os.environ.get("SYSTEMROOT"):
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return environment


def _run_with_bounded_diagnostics(
    arguments: list[str], staging: Path, timeout: int
) -> tuple[subprocess.CompletedProcess[Any], str]:
    read_fd, write_fd = os.pipe()
    captured = bytearray()
    truncated = False
    read_errors: list[OSError] = []

    def drain() -> None:
        nonlocal truncated
        try:
            with os.fdopen(read_fd, "rb", closefd=True) as stream:
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    room = MAX_DIAGNOSTIC_BYTES - len(captured)
                    if room > 0:
                        captured.extend(chunk[:room])
                    if len(chunk) > max(room, 0):
                        truncated = True
        except OSError as exc:
            read_errors.append(exc)

    reader = threading.Thread(target=drain, name="ffmpeg-diagnostic-drain", daemon=True)
    reader.start()
    try:
        try:
            completed = subprocess.run(
                arguments,
                cwd=str(staging),
                env=_sanitized_environment(staging),
                stdin=subprocess.DEVNULL,
                stdout=write_fd,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                shell=False,
                check=False,
                close_fds=True,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            raise VideoExportError("FFMPEG_TIMEOUT", f"FFmpeg exceeded {timeout} seconds", "ffmpeg") from exc
        except OSError as exc:
            raise VideoExportError("FFMPEG_START", str(exc), "ffmpeg") from exc
    finally:
        os.close(write_fd)
        reader.join()
    if read_errors:
        raise VideoExportError("DIAGNOSTIC_READ", str(read_errors[0]), "ffmpeg")
    diagnostic = captured.decode("utf-8", errors="replace")
    if truncated:
        diagnostic += "\n[diagnostic truncated]"
    return completed, diagnostic


def _require_live_ffmpeg(context: VideoExportContext) -> None:
    path = context.ffmpeg_path
    try:
        metadata = path.stat()
    except OSError as exc:
        raise VideoExportError("FFMPEG_MISSING", str(exc), "ffmpeg") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise VideoExportError("FFMPEG_TYPE", "FFmpeg executable must remain a regular file", "ffmpeg")
    if not os.access(path, os.X_OK):
        raise VideoExportError("FFMPEG_NOT_EXECUTABLE", "FFmpeg executable permission changed", "ffmpeg")
    expected = context.plan["ffmpeg"]
    digest, size = _stream_sha256(path, maximum=MAX_FFMPEG_BYTES, field="ffmpeg")
    if digest != expected["sha256"] or size != expected["size"]:
        raise VideoExportError("FFMPEG_CHECKSUM", "FFmpeg executable binding changed", "ffmpeg")


def _manifest_for_result(
    context: VideoExportContext, video_sha256: str, video_size: int
) -> dict[str, Any]:
    plan_binding = {
        "id": context.plan["id"],
        "path": VIDEO_EXPORT_PLAN,
        "sha256": sha256(context.plan_bytes),
    }
    video = {
        "path": VIDEO_OUTPUT,
        "sha256": video_sha256,
        "size": video_size,
    }
    files = [
        {"path": VIDEO_EXPORT_PLAN, "sha256": sha256(context.plan_bytes), "size": len(context.plan_bytes)},
        video,
    ]
    core = {
        "kind": PACKAGE_KIND,
        "schema_version": "1.0",
        "plan": plan_binding,
        "source_frame_preview": context.plan["source_frame_preview"],
        "profile": context.plan["profile"],
        "ffmpeg": context.plan["ffmpeg"],
        "intent": context.plan["intent"],
        "video": video,
        "reproducibility_scope": context.plan["reproducibility_scope"],
        "files": files,
    }
    return {"id": content_identifier(PACKAGE_KIND, core, 20), **core}


def _file_set(directory: Path) -> set[str]:
    files: set[str] = set()
    for candidate in directory.rglob("*"):
        if candidate.is_symlink():
            raise VideoExportError("PACKAGE_SYMLINK", "video export package contains a symlink", str(candidate))
        if candidate.is_file():
            files.add(candidate.relative_to(directory).as_posix())
    return files


def _bound_preview_path(manifest: dict[str, Any], frame_preview_root: Path) -> Path:
    binding = manifest.get("source_frame_preview")
    if not isinstance(binding, dict) or set(binding) != {"id", "path", "sha256"}:
        raise VideoExportError("MANIFEST_SCHEMA", "source frame preview binding is invalid", "source_frame_preview")
    identifier, relative, digest = binding.get("id"), binding.get("path"), binding.get("sha256")
    if not isinstance(identifier, str) or not isinstance(relative, str):
        raise VideoExportError("MANIFEST_SCHEMA", "source frame preview ID or path is invalid", "source_frame_preview")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise VideoExportError("MANIFEST_SCHEMA", "source frame preview checksum is invalid", "source_frame_preview")
    if relative != f"{identifier}/{FRAME_PREVIEW_MANIFEST}":
        raise VideoExportError("SOURCE_BINDING", "source frame preview path is not canonical", "source_frame_preview")
    base = root(frame_preview_root, must_exist=True, field="frame_preview_root")
    _, path = safe_file(base, relative, "source_frame_preview.path")
    if sha256(path.read_bytes()) != digest:
        raise VideoExportError("SOURCE_BINDING", "source frame preview checksum changed", "source_frame_preview")
    return path


def check_video_export_package(
    manifest_path: Path,
    profile_path: Path,
    ffmpeg: Path,
    output_root: Path,
    **roots: Path,
) -> dict[str, Any]:
    output_base = root(output_root, must_exist=True, field="output_root")
    _, resolved_manifest = relative_file(manifest_path, output_base, "video_export_manifest")
    manifest, manifest_bytes = _load_object(resolved_manifest, "video_export_manifest")
    if set(manifest) != MANIFEST_FIELDS:
        raise VideoExportError("MANIFEST_SCHEMA", "video export manifest has unexpected fields", "video_export_manifest")
    context = build_plan_context(
        _bound_preview_path(manifest, roots["frame_preview_root"]),
        profile_path,
        ffmpeg,
        **roots,
    )
    reject_output_overlap(output_base, context.source_paths)
    package = output_base / context.plan["id"]
    expected_manifest_path = package / VIDEO_EXPORT_MANIFEST
    if resolved_manifest != expected_manifest_path.resolve():
        raise VideoExportError("MANIFEST_LOCATION", "video export manifest path is not plan-addressed", str(manifest_path))
    expected_files = {VIDEO_EXPORT_PLAN, VIDEO_EXPORT_MANIFEST, VIDEO_OUTPUT}
    actual_files = _file_set(package)
    if actual_files != expected_files:
        raise VideoExportError(
            "FILE_SET_MISMATCH",
            f"missing={sorted(expected_files - actual_files)}; extra={sorted(actual_files - expected_files)}",
            str(package),
        )
    plan_path = package / VIDEO_EXPORT_PLAN
    plan, plan_bytes = _load_object(plan_path, "video_export_plan")
    if plan != context.plan or plan_bytes != context.plan_bytes:
        raise VideoExportError("PLAN_MISMATCH", "video export plan is stale or modified", VIDEO_EXPORT_PLAN)
    video_path = package / VIDEO_OUTPUT
    max_output = bounded_int(context.profile["max_output_bytes"], "max_output_bytes", 1, 8 * 1024 * 1024 * 1024)
    video_digest, video_size = _stream_sha256(video_path, maximum=max_output, field=VIDEO_OUTPUT)
    if video_size <= 0:
        raise VideoExportError("VIDEO_EMPTY", "video output is empty", VIDEO_OUTPUT)
    expected_manifest = _manifest_for_result(context, video_digest, video_size)
    if manifest != expected_manifest or manifest_bytes != json_bytes(expected_manifest):
        raise VideoExportError("MANIFEST_MISMATCH", "video export manifest is stale or modified", VIDEO_EXPORT_MANIFEST)
    return {
        "ok": True,
        "video_export": manifest,
        "plan": context.plan,
        "video_sha256": video_digest,
        "video_size": video_size,
    }


def run_video_export(
    frame_preview_manifest: Path,
    profile_path: Path,
    ffmpeg: Path,
    output_root: Path,
    *,
    timeout_seconds: int,
    **roots: Path,
) -> dict[str, Any]:
    timeout = bounded_int(timeout_seconds, "timeout_seconds", 1, MAX_TIMEOUT_SECONDS)
    context = build_plan_context(
        frame_preview_manifest,
        profile_path,
        ffmpeg,
        **roots,
    )
    output_candidate = reject_output_overlap(output_root, context.source_paths)
    output_candidate.mkdir(parents=True, exist_ok=True)
    output_base = output_candidate.resolve()
    destination = output_base / context.plan["id"]
    if destination.is_symlink():
        raise VideoExportError("OUTPUT_SYMLINK", "video export destination is a symlink", "output_root")
    if destination.exists():
        checked = check_video_export_package(
            destination / VIDEO_EXPORT_MANIFEST,
            profile_path,
            ffmpeg,
            output_base,
            **roots,
        )
        return {
            **checked,
            "executed": False,
            "idempotent": True,
            "package_path": context.plan["id"],
        }
    staging = output_base / f".{context.plan['id']}.tmp"
    if staging.exists() or staging.is_symlink():
        raise VideoExportError("STAGING_CONFLICT", "video export staging path already exists", "output_root")
    try:
        staging.mkdir()
        (staging / VIDEO_EXPORT_PLAN).write_bytes(context.plan_bytes)
        _require_live_ffmpeg(context)
        arguments = _actual_arguments(context, staging)
        completed, diagnostic = _run_with_bounded_diagnostics(arguments, staging, timeout)
        _require_live_ffmpeg(context)
        if completed.returncode != 0:
            raise VideoExportError(
                "FFMPEG_FAILED",
                f"FFmpeg exited with {completed.returncode}: {diagnostic}",
                "ffmpeg",
            )
        video_path = staging / VIDEO_OUTPUT
        max_output = bounded_int(context.profile["max_output_bytes"], "max_output_bytes", 1, 8 * 1024 * 1024 * 1024)
        video_digest, video_size = _stream_sha256(video_path, maximum=max_output, field=VIDEO_OUTPUT)
        if video_size <= 0:
            raise VideoExportError("VIDEO_EMPTY", "FFmpeg produced an empty video", VIDEO_OUTPUT)
        manifest = _manifest_for_result(context, video_digest, video_size)
        (staging / VIDEO_EXPORT_MANIFEST).write_bytes(json_bytes(manifest))
        if _file_set(staging) != {VIDEO_EXPORT_PLAN, VIDEO_EXPORT_MANIFEST, VIDEO_OUTPUT}:
            raise VideoExportError("STAGING_FILE_SET", "staging contains an unexpected file", str(staging))
        if destination.exists():
            raise VideoExportError("OUTPUT_CONFLICT", "video export destination appeared during execution", "output_root")
        staging.replace(destination)
    except Exception:
        _cleanup(staging)
        raise
    return {
        "ok": True,
        "video_export": manifest,
        "plan": context.plan,
        "video_sha256": manifest["video"]["sha256"],
        "video_size": manifest["video"]["size"],
        "executed": True,
        "idempotent": False,
        "package_path": context.plan["id"],
    }
