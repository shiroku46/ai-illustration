from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.frame_renderer import RGBAImage, encode_rgba_png
from ai_illustration.naming import canonical_json, content_identifier
from ai_illustration.video_export import (
    VIDEO_EXPORT_MANIFEST,
    VIDEO_EXPORT_PLAN,
    VIDEO_OUTPUT,
    VideoExportError,
    build_video_export_plan,
    check_video_export_package,
    main,
    run_video_export,
)


def canonical(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def sha(payload: bytes) -> str:
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def profile_value(*, crf: int = 18) -> dict:
    core = {
        "kind": "paper-theater-video-export-profile",
        "schema_version": "1.0",
        "family": "mp4-h264-aac-v1",
        "container": "mp4",
        "extension": "mp4",
        "alpha_policy": "require-opaque-source",
        "frame_policy": "exact-numbered-sequence-no-resize",
        "metadata_policy": "strip-input-and-fix-creation-time",
        "video": {"codec": "libx264", "pixel_format": "yuv420p", "preset": "medium", "crf": crf},
        "audio": {"codec": "aac", "bitrate_kbps": 192},
    }
    return {"id": content_identifier("paper-theater-video-export-profile", core, 20), **core}


class VideoExportTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.frame_preview_root = self.base / "frame-previews"
        self.frame_render_root = self.base / "frame-renders"
        self.renderer_root = self.base / "renderer-jobs"
        self.plan_root = self.base / "render-plans"
        self.audio_preview_root = self.base / "audio-previews"
        self.preview_root = self.base / "previews"
        self.package_root = self.base / "packages"
        self.audio_root = self.base / "audio-root"
        self.profile_root = self.base / "profiles"
        self.output_root = self.base / "video-exports"
        for root in (
            self.frame_preview_root,
            self.frame_render_root,
            self.renderer_root,
            self.plan_root,
            self.audio_preview_root,
            self.preview_root,
            self.package_root,
            self.audio_root,
            self.profile_root,
        ):
            root.mkdir(parents=True)

        self.package_id = "paper-theater-frame-preview-package-11111111111111111111"
        self.package_dir = self.frame_preview_root / self.package_id
        (self.package_dir / "frames").mkdir(parents=True)
        (self.package_dir / "audio").mkdir()
        opaque1 = encode_rgba_png(RGBAImage(2, 2, bytes([255, 0, 0, 255] * 4)))
        opaque2 = encode_rgba_png(RGBAImage(2, 2, bytes([0, 255, 0, 255] * 4)))
        self.frame_payloads = [opaque1, opaque2]
        for index, payload in enumerate(self.frame_payloads):
            (self.package_dir / f"frames/{index:08d}.png").write_bytes(payload)
        self.audio_payload = b"RIFF" + b"\0" * 124
        self.audio_relative = f"audio/{sha(self.audio_payload)}.wav"
        (self.package_dir / self.audio_relative).write_bytes(self.audio_payload)
        self.manifest = {
            "id": self.package_id,
            "kind": "paper-theater-frame-preview-package",
            "schema_version": "1.0",
            "intent": "evaluation",
            "audio_license_status": "reviewing",
            "canvas": {"width": 2, "height": 2, "background_rgba": [0, 0, 0, 255]},
            "scene_duration_ms": 1000,
            "fps_num": 2,
            "fps_den": 1,
            "frame_count": 2,
            "audio_placement": {"offset_ms": 100},
            "audio": {
                "path": self.audio_relative,
                "sha256": sha(self.audio_payload),
                "size": len(self.audio_payload),
                "duration_ms": 900,
                "channels": 1,
                "sample_rate": 8000,
                "bits_per_sample": 16,
            },
            "files": [
                *[
                    {"path": f"frames/{index:08d}.png", "sha256": sha(payload), "size": len(payload)}
                    for index, payload in enumerate(self.frame_payloads)
                ],
                {"path": self.audio_relative, "sha256": sha(self.audio_payload), "size": len(self.audio_payload)},
            ],
        }
        self.manifest_path = self.package_dir / "frame-preview-manifest.json"
        self._write_manifest()
        self.profile = profile_value()
        self.profile_path = self.profile_root / "video-export-profile.json"
        self.profile_path.write_bytes(canonical(self.profile))
        self.ffmpeg = self.base / "fake-ffmpeg"
        self.ffmpeg.write_text(
            f"#!{sys.executable}\nimport pathlib,sys\npathlib.Path(sys.argv[-1]).write_bytes(b'FAKE-MP4')\n",
            encoding="utf-8",
        )
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_manifest(self) -> None:
        self.manifest_path.write_bytes(canonical(self.manifest))

    def _checker_result(self):
        return {"ok": True, "frame_preview": self.manifest}

    def _args(self):
        return (
            self.manifest_path,
            self.profile_path,
            self.ffmpeg,
            self.frame_preview_root,
            self.frame_render_root,
            self.renderer_root,
            self.plan_root,
            self.audio_preview_root,
            self.preview_root,
            self.package_root,
            self.audio_root,
            self.profile_root,
            self.output_root,
        )

    def plan(self):
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            return build_video_export_plan(*self._args())

    def execute(self, timeout=30):
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            return run_video_export(*self._args(), timeout_seconds=timeout)
