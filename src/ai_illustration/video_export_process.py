"""Bounded shell-free FFmpeg process execution."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import threading
from typing import BinaryIO

from .video_export_common import (
    MAX_DIAGNOSTIC_BYTES, MAX_TIMEOUT_SECONDS, VideoExportError, _bounded_int,
)

def _sanitized_environment() -> dict[str, str]:
    allowed = ("SystemRoot", "WINDIR", "TMP", "TEMP", "TMPDIR", "HOME", "USERPROFILE")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result.update({"LC_ALL": "C", "LANG": "C", "TZ": "UTC"})
    return result


def _drain_pipe(pipe: BinaryIO, sink: bytearray, overflow: threading.Event, process: subprocess.Popen[bytes]) -> None:
    try:
        while True:
            chunk = pipe.read(64 * 1024)
            if not chunk:
                return
            if len(sink) + len(chunk) > MAX_DIAGNOSTIC_BYTES:
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            sink.extend(chunk)
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _run_process(arguments: list[str], cwd: Path, timeout_seconds: int) -> tuple[bytes, bytes]:
    timeout = _bounded_int(timeout_seconds, "timeout_seconds", 1, MAX_TIMEOUT_SECONDS)
    try:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=_sanitized_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        )
    except OSError as exc:
        raise VideoExportError("FFMPEG_START", str(exc), "ffmpeg") from exc
    assert process.stdout is not None and process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    threads = [
        threading.Thread(target=_drain_pipe, args=(process.stdout, stdout, overflow, process), daemon=True),
        threading.Thread(target=_drain_pipe, args=(process.stderr, stderr, overflow, process), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=5)
        raise VideoExportError("FFMPEG_TIMEOUT", f"FFmpeg exceeded {timeout} seconds", "timeout_seconds") from exc
    for thread in threads:
        thread.join(timeout=5)
    if any(thread.is_alive() for thread in threads):
        process.kill()
        raise VideoExportError("FFMPEG_DIAGNOSTIC_READ", "FFmpeg diagnostic pipes did not close", "ffmpeg")
    if overflow.is_set():
        raise VideoExportError("FFMPEG_DIAGNOSTIC_LIMIT", "FFmpeg diagnostics exceeded the configured limit", "ffmpeg")
    if return_code != 0:
        detail = bytes(stderr).decode("utf-8", "replace")[-2000:]
        raise VideoExportError("FFMPEG_FAILED", f"FFmpeg exited with {return_code}: {detail}", "ffmpeg")
    return bytes(stdout), bytes(stderr)
