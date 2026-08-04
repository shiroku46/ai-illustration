"""CLI and public API for bounded local FFmpeg video export."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Sequence

from .video_export_common import (
    VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT, VideoExportError, _json_bytes,
)
from .video_export_plan import build_video_export_plan
from .video_export_execute import run_video_export
from .video_export_check import check_video_export_package


def _common_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--frame-preview-root", type=Path, required=True)
    parser.add_argument("--frame-render-root", type=Path, required=True)
    parser.add_argument("--renderer-job-root", type=Path, required=True)
    parser.add_argument("--render-plan-root", type=Path, required=True)
    parser.add_argument("--audio-preview-root", type=Path, required=True)
    parser.add_argument("--preview-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--profile-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ai_illustration.video_export")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "run"):
        command = subparsers.add_parser(name)
        command.add_argument("frame_preview_manifest", type=Path)
        command.add_argument("profile", type=Path)
        _common_roots(command)
        if name == "run":
            command.add_argument("--timeout-seconds", type=int, default=1800)
    check = subparsers.add_parser("check")
    check.add_argument("video_export_manifest", type=Path)
    check.add_argument("profile", type=Path)
    _common_roots(check)
    return parser


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(_json_bytes(value))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = build_video_export_plan(
                args.frame_preview_manifest,
                args.profile,
                args.ffmpeg,
                args.frame_preview_root,
                args.frame_render_root,
                args.renderer_job_root,
                args.render_plan_root,
                args.audio_preview_root,
                args.preview_root,
                args.package_root,
                args.audio_root,
                args.profile_root,
                args.output_root,
            )
            _emit(result)
            print(f"video export plan ready: {result['video_export_plan']['id']}", file=sys.stderr)
            return 0
        if args.command == "run":
            result = run_video_export(
                args.frame_preview_manifest,
                args.profile,
                args.ffmpeg,
                args.frame_preview_root,
                args.frame_render_root,
                args.renderer_job_root,
                args.render_plan_root,
                args.audio_preview_root,
                args.preview_root,
                args.package_root,
                args.audio_root,
                args.profile_root,
                args.output_root,
                timeout_seconds=args.timeout_seconds,
            )
            _emit(result)
            print(
                f"video export ready: {result['video_export']['id']} "
                f"(executed={result['executed']}, written={result['written']})",
                file=sys.stderr,
            )
            return 0
        result = check_video_export_package(
            args.video_export_manifest,
            args.profile,
            args.ffmpeg,
            args.output_root,
            args.frame_preview_root,
            args.frame_render_root,
            args.renderer_job_root,
            args.render_plan_root,
            args.audio_preview_root,
            args.preview_root,
            args.package_root,
            args.audio_root,
            args.profile_root,
        )
        _emit(result)
        print(f"video export valid: {result['video_export']['id']}", file=sys.stderr)
        return 0
    except VideoExportError as exc:
        _emit({"ok": False, "errors": [exc.to_dict()]})
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
