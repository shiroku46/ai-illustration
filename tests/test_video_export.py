from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.frame_preview import FRAME_PREVIEW_MANIFEST
from ai_illustration.frame_renderer import RGBAImage, encode_rgba_png
from ai_illustration.naming import canonical_json, content_identifier
from ai_illustration.video_export import main
from ai_illustration.video_export_core import (
    PROFILE_KIND,
    VIDEO_EXPORT_MANIFEST,
    VideoExportError,
    plan_video_export,
)
from ai_illustration.video_export_runtime import (
    check_video_export_package,
    run_video_export,
)


def canonical(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def digest(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


class VideoExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.frame_preview_root = self.base / "frame-previews"
        self.frame_render_root = self.base / "frame-renders"
        self.renderer_job_root = self.base / "renderer-jobs"
        self.render_plan_root = self.base / "render-plans"
        self.audio_preview_root = self.base / "audio-previews"
        self.preview_root = self.base / "previews"
        self.package_root = self.base / "packages"
        self.audio_root = self.base / "audio"
        self.profile_root = self.base / "profiles"
        self.output_root = self.base / "video-output"
        for directory in (
            self.frame_preview_root,
            self.frame_render_root,
            self.renderer_job_root,
            self.render_plan_root,
            self.audio_preview_root,
            self.preview_root,
            self.package_root,
            self.audio_root,
            self.profile_root,
        ):
            directory.mkdir(parents=True)

        self.ffmpeg = self.base / "ffmpeg-fixture"
        self.ffmpeg.write_bytes(b"fixed-local-ffmpeg-fixture")
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        self.profile_path = self.profile_root / "video-export-profile.json"
        self.profile = {
            "kind": PROFILE_KIND,
            "schema_version": "1.0",
            "family": "mp4-h264-aac-v1",
            "container": "mp4",
            "video_codec": "libx264",
            "audio_codec": "aac",
            "pixel_format": "yuv420p",
            "alpha_policy": "require-opaque-source",
            "preset": "medium",
            "crf": 23,
            "audio_bitrate_kbps": 128,
            "movflags": "+faststart",
            "ffmpeg_sha256": digest(self.ffmpeg.read_bytes()),
            "max_output_bytes": 1024 * 1024,
        }
        self.write_profile()

        self.preview_id = "paper-theater-frame-preview-package-11111111111111111111"
        self.preview_package = self.frame_preview_root / self.preview_id
        (self.preview_package / "frames").mkdir(parents=True)
        rgba = bytes((255, 0, 0, 255)) * 4
        self.frame_payload = encode_rgba_png(RGBAImage(2, 2, rgba))
        self.frame_path = self.preview_package / "frames/00000000.png"
        self.frame_path.write_bytes(self.frame_payload)
        self.inventory_path = self.preview_package / "frame-inventory.json"
        self.inventory = {
            "id": "paper-theater-frame-inventory-22222222222222222222",
            "kind": "paper-theater-frame-inventory",
            "schema_version": "1.0",
            "renderer_job_ref": "paper-theater-renderer-job-33333333333333333333",
            "frame_count": 1,
            "fps_num": 2,
            "fps_den": 1,
            "time_unit": "milliseconds",
            "frames": [
                {
                    "index": 0,
                    "path": "frames/00000000.png",
                    "sha256": digest(self.frame_payload),
                    "size": len(self.frame_payload),
                    "start_time_num": 0,
                    "end_time_num": 2000,
                    "time_den": 2,
                    "span_index": 0,
                }
            ],
        }
        self.inventory_path.write_bytes(canonical(self.inventory))
        self.audio_payload = b"RIFF" + b"\0" * 60
        self.audio_relative = f"audio/{digest(self.audio_payload)}.wav"
        audio_path = self.preview_package / self.audio_relative
        audio_path.parent.mkdir()
        audio_path.write_bytes(self.audio_payload)
        placement = {
            "policy": "signed-rational-sample-offset-no-resampling",
            "offset_ms": 100,
            "start_sample_num": 4800000,
            "start_sample_den": 1000,
            "source_sample_rate": 48000,
            "source_frame_count": 43200,
            "duration_policy": "exact",
            "synchronized_audio_end_ms": 1000,
        }
        self.preview = {
            "id": self.preview_id,
            "kind": "paper-theater-frame-preview-package",
            "schema_version": "1.0",
            "source_frame_render": {
                "id": "paper-theater-frame-render-package-44444444444444444444",
                "path": "paper-theater-frame-render-package-44444444444444444444/frame-render-manifest.json",
                "sha256": "a" * 64,
            },
            "source_audio_preview": {
                "id": "paper-theater-audio-preview-55555555555555555555",
                "path": "paper-theater-audio-preview-55555555555555555555/audio-preview-manifest.json",
                "sha256": "b" * 64,
            },
            "intent": "evaluation",
            "audio_license_status": "reviewing",
            "canvas": {"width": 2, "height": 2, "background_rgba": [0, 0, 0, 255]},
            "scene_duration_ms": 1000,
            "fps_num": 2,
            "fps_den": 1,
            "frame_count": 1,
            "audio_placement": placement,
            "frame_inventory": {
                "id": self.inventory["id"],
                "path": "frame-inventory.json",
                "sha256": digest(self.inventory_path.read_bytes()),
            },
            "audio": {
                "path": self.audio_relative,
                "sha256": digest(self.audio_payload),
                "size": len(self.audio_payload),
                "duration_ms": 900,
                "channels": 1,
                "sample_rate": 48000,
                "bits_per_sample": 16,
            },
            "playback_policy": {},
            "media_copied_unchanged": True,
            "video_created": False,
            "files": [],
        }
        self.preview_manifest = self.preview_package / FRAME_PREVIEW_MANIFEST
        self.write_preview()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def roots(self) -> dict[str, Path]:
        return {
            "frame_preview_root": self.frame_preview_root,
            "frame_render_root": self.frame_render_root,
            "renderer_job_root": self.renderer_job_root,
            "render_plan_root": self.render_plan_root,
            "audio_preview_root": self.audio_preview_root,
            "preview_root": self.preview_root,
            "package_root": self.package_root,
            "audio_root": self.audio_root,
            "profile_root": self.profile_root,
        }

    def write_profile(self) -> None:
        core = dict(self.profile)
        core.pop("id", None)
        self.profile = {"id": content_identifier(PROFILE_KIND, core, 20), **core}
        self.profile_path.write_bytes(canonical(self.profile))

    def write_preview(self) -> None:
        self.preview_manifest.write_bytes(canonical(self.preview))

    def write_frame(self, image: RGBAImage) -> None:
        self.frame_payload = encode_rgba_png(image)
        self.frame_path.write_bytes(self.frame_payload)
        frame = self.inventory["frames"][0]
        frame["sha256"] = digest(self.frame_payload)
        frame["size"] = len(self.frame_payload)
        self.inventory_path.write_bytes(canonical(self.inventory))
        self.preview["frame_inventory"]["sha256"] = digest(self.inventory_path.read_bytes())
        self.write_preview()

    @contextmanager
    def validated(self):
        with patch(
            "ai_illustration.video_export_core.check_frame_preview_package",
            return_value={"ok": True, "frame_preview": self.preview},
        ):
            yield

    def plan(self):
        with self.validated():
            return plan_video_export(
                self.preview_manifest,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                **self.roots(),
            )

    def test_plan_is_deterministic_non_mutating_and_placeholder_only(self) -> None:
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        self.assertFalse(self.output_root.exists())
        plan = first["video_export_plan"]
        self.assertFalse(plan["media_created"])
        self.assertEqual(plan["argument_template"][0], "{ffmpeg}")
        self.assertEqual(plan["argument_template"][-1], "{output}")
        joined = " ".join(plan["argument_template"])
        self.assertIn("{frames}/%08d.png", joined)
        self.assertIn("adelay=delays=100:all=1", plan["audio_filter"])
        self.assertNotIn(str(self.base), joined)

    def test_positive_zero_and_negative_audio_offsets(self) -> None:
        cases = (
            (100, "adelay=delays=100:all=1"),
            (0, "[1:a]asetpts=PTS-STARTPTS"),
            (-125, "atrim=start=0.125"),
        )
        for offset, expected in cases:
            with self.subTest(offset=offset):
                self.preview["audio_placement"]["offset_ms"] = offset
                self.write_preview()
                self.assertIn(expected, self.plan()["video_export_plan"]["audio_filter"])

    def test_rejects_nonopaque_and_odd_frames(self) -> None:
        self.write_frame(RGBAImage(2, 2, bytes((1, 2, 3, 254)) * 4))
        with self.assertRaisesRegex(VideoExportError, "NON_OPAQUE_FRAME"):
            self.plan()
        self.preview["canvas"]["width"] = 3
        self.write_frame(RGBAImage(3, 2, bytes((1, 2, 3, 255)) * 6))
        with self.assertRaisesRegex(VideoExportError, "ODD_DIMENSIONS"):
            self.plan()

    def test_rejects_ffmpeg_checksum_permission_and_symlink(self) -> None:
        self.profile["ffmpeg_sha256"] = "0" * 64
        self.write_profile()
        with self.assertRaisesRegex(VideoExportError, "FFMPEG_CHECKSUM"):
            self.plan()
        self.profile["ffmpeg_sha256"] = digest(self.ffmpeg.read_bytes())
        self.write_profile()
        self.ffmpeg.chmod(stat.S_IRUSR | stat.S_IWUSR)
        with self.assertRaisesRegex(VideoExportError, "FFMPEG_NOT_EXECUTABLE"):
            self.plan()
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        link = self.base / "ffmpeg-link"
        try:
            link.symlink_to(self.ffmpeg)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.validated(), self.assertRaisesRegex(VideoExportError, "FFMPEG_SYMLINK"):
            plan_video_export(
                self.preview_manifest,
                self.profile_path,
                link,
                self.output_root,
                **self.roots(),
            )

    def fake_success(self, video: bytes = b"encoded-video"):
        calls: list[tuple[list[str], dict[str, object]]] = []

        def execute(arguments, **kwargs):
            calls.append((list(arguments), dict(kwargs)))
            Path(arguments[-1]).write_bytes(video)
            kwargs["stdout"].write(b"fake ffmpeg ok")
            return subprocess.CompletedProcess(arguments, 0)

        return calls, execute

    def test_run_is_shell_free_sanitized_checkable_and_idempotent(self) -> None:
        calls, execute = self.fake_success()
        with self.validated(), patch(
            "ai_illustration.video_export_runtime.subprocess.run", side_effect=execute
        ) as mocked:
            first = run_video_export(
                self.preview_manifest,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                timeout_seconds=5,
                **self.roots(),
            )
            self.assertTrue(first["executed"])
            package = self.output_root / first["package_path"]
            checked = check_video_export_package(
                package / VIDEO_EXPORT_MANIFEST,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                **self.roots(),
            )
            self.assertTrue(checked["ok"])
            second = run_video_export(
                self.preview_manifest,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                timeout_seconds=5,
                **self.roots(),
            )
        self.assertTrue(second["idempotent"])
        self.assertFalse(second["executed"])
        mocked.assert_called_once()
        arguments, kwargs = calls[0]
        self.assertIsInstance(arguments, list)
        self.assertFalse(kwargs["shell"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["PATH"], "")
        self.assertNotIn("SECRET", " ".join(kwargs["env"]))
        self.assertEqual(arguments[0], str(self.ffmpeg.resolve()))
        self.assertEqual(arguments[-1], str((self.output_root / f".{first['plan']['id']}.tmp" / "video.mp4").resolve()))

    def test_timeout_failure_and_oversize_leave_no_package(self) -> None:
        with self.validated(), patch(
            "ai_illustration.video_export_runtime.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["ffmpeg"], 1),
        ), self.assertRaisesRegex(VideoExportError, "FFMPEG_TIMEOUT"):
            run_video_export(
                self.preview_manifest,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                timeout_seconds=1,
                **self.roots(),
            )
        self.assertEqual(list(self.output_root.iterdir()), [])

        def failed(arguments, **kwargs):
            kwargs["stdout"].write(b"failure")
            return subprocess.CompletedProcess(arguments, 2)

        with self.validated(), patch(
            "ai_illustration.video_export_runtime.subprocess.run", side_effect=failed
        ), self.assertRaisesRegex(VideoExportError, "FFMPEG_FAILED"):
            run_video_export(
                self.preview_manifest,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                timeout_seconds=5,
                **self.roots(),
            )
        self.assertEqual(list(self.output_root.iterdir()), [])

        self.profile["max_output_bytes"] = 3
        self.write_profile()
        _calls, oversized = self.fake_success(b"four")
        with self.validated(), patch(
            "ai_illustration.video_export_runtime.subprocess.run", side_effect=oversized
        ), self.assertRaisesRegex(VideoExportError, "VIDEO_TOO_LARGE"):
            run_video_export(
                self.preview_manifest,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                timeout_seconds=5,
                **self.roots(),
            )
        self.assertEqual(list(self.output_root.iterdir()), [])

    def test_checker_rejects_video_tampering_and_extra_files(self) -> None:
        _calls, execute = self.fake_success()
        with self.validated(), patch(
            "ai_illustration.video_export_runtime.subprocess.run", side_effect=execute
        ):
            result = run_video_export(
                self.preview_manifest,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                timeout_seconds=5,
                **self.roots(),
            )
        package = self.output_root / result["package_path"]
        video = package / "video.mp4"
        video.write_bytes(video.read_bytes() + b"x")
        with self.validated(), self.assertRaisesRegex(VideoExportError, "MANIFEST_MISMATCH"):
            check_video_export_package(
                package / VIDEO_EXPORT_MANIFEST,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                **self.roots(),
            )
        video.write_bytes(video.read_bytes()[:-1])
        (package / "extra.txt").write_text("extra", encoding="utf-8")
        with self.validated(), self.assertRaisesRegex(VideoExportError, "FILE_SET_MISMATCH"):
            check_video_export_package(
                package / VIDEO_EXPORT_MANIFEST,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                **self.roots(),
            )

    def test_rejects_source_output_overlap(self) -> None:
        for output in (self.frame_preview_root / "nested", self.profile_root / "nested", self.base):
            with self.subTest(output=output), self.assertRaisesRegex(VideoExportError, "OUTPUT_OVERLAPS_SOURCE"):
                with self.validated():
                    plan_video_export(
                        self.preview_manifest,
                        self.profile_path,
                        self.ffmpeg,
                        output,
                        **self.roots(),
                    )

    def test_module_cli_plan(self) -> None:
        arguments = [
            "plan",
            str(self.preview_manifest),
            str(self.profile_path),
            "--ffmpeg",
            str(self.ffmpeg),
            "--output-root",
            str(self.output_root),
        ]
        for name, value in self.roots().items():
            arguments.extend(["--" + name.replace("_", "-"), str(value)])
        raw = io.BytesIO()
        stdout = io.TextIOWrapper(raw, encoding="utf-8")
        stderr = io.StringIO()
        with self.validated(), patch("sys.stdout", stdout), redirect_stderr(stderr):
            status = main(arguments)
            stdout.flush()
        payload = json.loads(raw.getvalue().decode("utf-8"))
        self.assertEqual(status, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("video export plan ready", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
