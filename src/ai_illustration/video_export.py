"""Public API and CLI for bounded local Phase 14 FFmpeg video exports."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Sequence

from .video_export_core import VideoExportError, json_bytes, plan_video_export
from .video_export_runtime import check_video_export_package, run_video_export


def _roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--frame-preview-root", type=Path, required=True)
    parser.add_argument("--frame-render-root", type=Path, required=True)
    parser.add_argument("--renderer-job-root", type=Path, required=True)
    parser.add_argument("--render-plan-root", type=Path, required=True)
    parser.add_argument("--audio-preview-root", type=Path, required=True)
    parser.add_argument("--preview-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--profile-root", type=Path, required=True)


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("frame_preview_manifest", type=Path)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--ffmpeg", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    _roots(parser)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="python -m ai_illustration.video_export")
    commands = result.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan")
    _source_arguments(planning)
    running = commands.add_parser("run")
    _source_arguments(running)
    running.add_argument("--timeout-seconds", type=int, required=True)
    checking = commands.add_parser("check")
    checking.add_argument("video_export_manifest", type=Path)
    checking.add_argument("profile", type=Path)
    checking.add_argument("--ffmpeg", type=Path, required=True)
    checking.add_argument("--output-root", type=Path, required=True)
    _roots(checking)
    return result


def _root_values(args: argparse.Namespace) -> dict[str, Path]:
    return {
        "frame_preview_root": args.frame_preview_root,
        "frame_render_root": args.frame_render_root,
        "renderer_job_root": args.renderer_job_root,
        "render_plan_root": args.render_plan_root,
        "audio_preview_root": args.audio_preview_root,
        "preview_root": args.preview_root,
        "package_root": args.package_root,
        "audio_root": args.audio_root,
        "profile_root": args.profile_root,
    }


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(json_bytes(value))


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        roots = _root_values(args)
        if args.command == "plan":
            result = plan_video_export(
                args.frame_preview_manifest,
                args.profile,
                args.ffmpeg,
                args.output_root,
                **roots,
            )
            _emit(result)
            print(
                f"video export plan ready: {result['video_export_plan']['id']} (media_created=false)",
                file=sys.stderr,
            )
            return 0
        if args.command == "run":
            result = run_video_export(
                args.frame_preview_manifest,
                args.profile,
                args.ffmpeg,
                args.output_root,
                timeout_seconds=args.timeout_seconds,
                **roots,
            )
            _emit(result)
            print(
                f"video export ready: {result['video_export']['id']} "
                f"(executed={result['executed']}, bytes={result['video_size']})",
                file=sys.stderr,
            )
            return 0
        result = check_video_export_package(
            args.video_export_manifest,
            args.profile,
            args.ffmpeg,
            args.output_root,
            **roots,
        )
        _emit(result)
        print(
            f"video export valid: {result['video_export']['id']} ({result['video_size']} bytes)",
            file=sys.stderr,
        )
        return 0
    except VideoExportError as exc:
        _emit({"ok": False, "error": exc.to_dict()})
        print(str(exc), file=sys.stderr)
        return 1


__all__ = [
    "VideoExportError",
    "plan_video_export",
    "run_video_export",
    "check_video_export_package",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
