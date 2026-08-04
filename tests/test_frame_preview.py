from __future__ import annotations

from contextlib import contextmanager, redirect_stderr
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.frame_preview import (
    FRAME_PREVIEW_MANIFEST,
    FramePreviewError,
    build_frame_preview_package,
    check_frame_preview_package,
    main,
)
from ai_illustration.frame_renderer import RGBAImage, encode_rgba_png
from ai_illustration.naming import canonical_json


def canonical(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def sha(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def wav_bytes(frame_count: int = 7200, sample_rate: int = 8000) -> bytes:
    data = b"\0\0" * frame_count
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    return (
        b"RIFF"
        + struct.pack("<I", 4 + (8 + len(fmt)) + (8 + len(data)))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


class FramePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.frame_root = self.base / "frame-renders"
        self.renderer_root = self.base / "renderer-jobs"
        self.plan_root = self.base / "render-plans"
        self.audio_preview_root = self.base / "audio-previews"
        self.preview_root = self.base / "previews"
        self.package_root = self.base / "packages"
        self.audio_root = self.base / "audio-input"
        for root in (
            self.frame_root,
            self.renderer_root,
            self.plan_root,
            self.audio_preview_root,
            self.preview_root,
            self.package_root,
            self.audio_root,
        ):
            root.mkdir(parents=True)

        self.frame_id = "paper-theater-frame-render-package-11111111111111111111"
        self.renderer_id = "paper-theater-renderer-job-22222222222222222222"
        self.plan_id = "paper-theater-render-plan-33333333333333333333"
        self.audio_id = "paper-theater-audio-preview-44444444444444444444"
        self.frame_dir = self.frame_root / self.frame_id
        self.renderer_dir = self.renderer_root / self.renderer_id
        self.plan_dir = self.plan_root / self.plan_id
        self.audio_dir = self.audio_preview_root / self.audio_id
        for directory in (self.frame_dir, self.renderer_dir, self.plan_dir, self.audio_dir):
            directory.mkdir(parents=True)

        placement = {
            "policy": "signed-rational-sample-offset-no-resampling",
            "offset_ms": 100,
            "start_sample_num": 800000,
            "start_sample_den": 1000,
            "source_sample_rate": 8000,
            "source_frame_count": 7200,
            "duration_policy": "exact",
            "synchronized_audio_end_ms": 1000,
        }
        self.audio_payload = wav_bytes()
        self.audio_relative = f"audio/{sha(self.audio_payload)}.wav"
        (self.audio_dir / "audio").mkdir()
        (self.audio_dir / self.audio_relative).write_bytes(self.audio_payload)

        self.audio_manifest = {
            "id": self.audio_id,
            "kind": "paper-theater-audio-preview",
            "schema_version": "1.0",
            "source_preview_ref": "paper-theater-preview-55555555555555555555",
            "source_preview_path": "paper-theater-preview-55555555555555555555/preview-manifest.json",
            "source_preview_sha256": "a" * 64,
            "scene_plan_ref": "paper-theater-scene-plan-66666666666666666666",
            "intent": "evaluation",
            "width": 2,
            "height": 1,
            "scene_duration_ms": 1000,
            "offset_ms": 100,
            "duration_policy": "exact",
            "duration_tolerance_ms": 5,
            "synchronized_audio_end_ms": 1000,
            "clock": "audio-current-time",
            "roles": {},
            "segments": [],
            "assets": [],
            "audio": {
                "source_path": "voice.wav",
                "path": self.audio_relative,
                "sha256": sha(self.audio_payload),
                "size": len(self.audio_payload),
                "license_status": "reviewing",
                "container": "wav",
                "duration_ms": 900,
                "channels": 1,
                "sample_rate": 8000,
                "bits_per_sample": 16,
            },
            "files": [
                {
                    "path": self.audio_relative,
                    "sha256": sha(self.audio_payload),
                    "size": len(self.audio_payload),
                }
            ],
        }
        self.audio_manifest_path = self.audio_dir / "audio-preview-manifest.json"
        self.audio_manifest_path.write_bytes(canonical(self.audio_manifest))

        self.render_plan = {
            "id": self.plan_id,
            "kind": "paper-theater-render-plan",
            "schema_version": "1.0",
            "source_bindings": {
                "audio_preview": {
                    "id": self.audio_id,
                    "path": f"{self.audio_id}/audio-preview-manifest.json",
                    "sha256": sha(self.audio_manifest_path.read_bytes()),
                }
            },
            "intent": "evaluation",
            "audio_license_status": "reviewing",
            "width": 2,
            "height": 1,
            "scene_duration_ms": 1000,
            "fps_num": 2,
            "fps_den": 1,
            "frame_count": 2,
            "audio_placement": placement,
        }
        self.plan_path = self.plan_dir / "render-plan-manifest.json"
        self.plan_path.write_bytes(canonical(self.render_plan))

        self.renderer_job = {
            "id": self.renderer_id,
            "kind": "paper-theater-renderer-job",
            "schema_version": "1.0",
            "source_bindings": {
                "render_plan": {
                    "id": self.plan_id,
                    "path": f"{self.plan_id}/render-plan-manifest.json",
                    "sha256": sha(self.plan_path.read_bytes()),
                }
            },
            "intent": "evaluation",
        }
        self.renderer_path = self.renderer_dir / "renderer-job-manifest.json"
        self.renderer_path.write_bytes(canonical(self.renderer_job))

        first = encode_rgba_png(RGBAImage(2, 1, bytes((255, 0, 0, 255, 0, 0, 0, 0))))
        second = encode_rgba_png(RGBAImage(2, 1, bytes((0, 255, 0, 255, 0, 0, 0, 0))))
        (self.frame_dir / "frames").mkdir()
        (self.frame_dir / "frames/00000000.png").write_bytes(first)
        (self.frame_dir / "frames/00000001.png").write_bytes(second)
        self.inventory = {
            "id": "paper-theater-frame-inventory-77777777777777777777",
            "kind": "paper-theater-frame-inventory",
            "schema_version": "1.0",
            "renderer_job_ref": self.renderer_id,
            "frame_count": 2,
            "fps_num": 2,
            "fps_den": 1,
            "time_unit": "milliseconds",
            "frames": [
                {
                    "index": 0,
                    "path": "frames/00000000.png",
                    "sha256": sha(first),
                    "size": len(first),
                    "start_time_num": 0,
                    "end_time_num": 1000,
                    "time_den": 2,
                    "span_index": 0,
                },
                {
                    "index": 1,
                    "path": "frames/00000001.png",
                    "sha256": sha(second),
                    "size": len(second),
                    "start_time_num": 1000,
                    "end_time_num": 2000,
                    "time_den": 2,
                    "span_index": 1,
                },
            ],
        }
        self.inventory_path = self.frame_dir / "frame-inventory.json"
        self.inventory_path.write_bytes(canonical(self.inventory))
        self.frame_manifest = {
            "id": self.frame_id,
            "kind": "paper-theater-frame-render-package",
            "schema_version": "1.0",
            "source_renderer_job": {
                "id": self.renderer_id,
                "path": f"{self.renderer_id}/renderer-job-manifest.json",
                "sha256": sha(self.renderer_path.read_bytes()),
            },
            "intent": "evaluation",
            "audio_license_status": "reviewing",
            "canvas": {"width": 2, "height": 1, "background_rgba": [0, 0, 0, 0]},
            "fps_num": 2,
            "fps_den": 1,
            "frame_count": 2,
            "span_count": 2,
            "audio_placement": placement,
            "frame_inventory": {
                "id": self.inventory["id"],
                "path": "frame-inventory.json",
                "sha256": sha(self.inventory_path.read_bytes()),
            },
            "media_created": True,
            "files": [],
        }
        self.frame_manifest_path = self.frame_dir / "frame-render-manifest.json"
        self.frame_manifest_path.write_bytes(canonical(self.frame_manifest))
        self.output_root = self.base / "frame-previews"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @contextmanager
    def validated(self):
        with (
            patch(
                "ai_illustration.frame_preview.check_frame_render_package",
                return_value={"ok": True, "frame_render": self.frame_manifest},
            ),
            patch(
                "ai_illustration.frame_preview.check_audio_preview_package",
                return_value={"ok": True, "audio_preview": self.audio_manifest},
            ),
        ):
            yield

    def build(self, *, write: bool = False, output_root: Path | None = None):
        with self.validated():
            return build_frame_preview_package(
                self.frame_manifest_path,
                self.audio_manifest_path,
                self.frame_root,
                self.renderer_root,
                self.plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
                output_root or self.output_root,
                write=write,
            )

    def test_dry_run_is_deterministic_and_non_mutating(self) -> None:
        first = self.build()
        second = self.build()
        self.assertEqual(first, second)
        self.assertFalse(first["written"])
        self.assertFalse(self.output_root.exists())
        self.assertEqual(first["frame_preview"]["frame_count"], 2)

    def test_write_check_and_idempotent_write(self) -> None:
        first = self.build(write=True)
        package = self.output_root / first["package_path"]
        self.assertTrue((package / "index.html").is_file())
        self.assertEqual((package / self.audio_relative).read_bytes(), self.audio_payload)
        self.assertEqual(
            (package / "frames/00000000.png").read_bytes(),
            (self.frame_dir / "frames/00000000.png").read_bytes(),
        )
        second = self.build(write=True)
        self.assertFalse(second["written"])
        with self.validated():
            checked = check_frame_preview_package(
                package / FRAME_PREVIEW_MANIFEST,
                self.output_root,
                self.frame_root,
                self.renderer_root,
                self.plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
            )
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["frame_count"], 2)

    def test_rejects_audio_preview_not_bound_by_render_plan(self) -> None:
        other_id = "paper-theater-audio-preview-99999999999999999999"
        other_dir = self.audio_preview_root / other_id
        other_dir.mkdir()
        other = dict(self.audio_manifest)
        other["id"] = other_id
        other_path = other_dir / "audio-preview-manifest.json"
        other_path.write_bytes(canonical(other))
        with self.validated(), self.assertRaises(FramePreviewError) as caught:
            build_frame_preview_package(
                self.frame_manifest_path,
                other_path,
                self.frame_root,
                self.renderer_root,
                self.plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
                self.output_root,
            )
        self.assertEqual(caught.exception.code, "AUDIO_PREVIEW_BINDING_MISMATCH")

    def test_rejects_tampered_frame_or_audio_bytes(self) -> None:
        frame = self.frame_dir / "frames/00000000.png"
        original_frame = frame.read_bytes()
        frame.write_bytes(original_frame + b"x")
        with self.assertRaises(FramePreviewError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "FRAME_FILE_MISMATCH")
        frame.write_bytes(original_frame)
        audio = self.audio_dir / self.audio_relative
        audio.write_bytes(self.audio_payload + b"x")
        with self.assertRaises(FramePreviewError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "AUDIO_FILE_MISMATCH")

    def test_rejects_output_overlap_in_both_directions(self) -> None:
        with self.assertRaises(FramePreviewError) as nested:
            self.build(write=True, output_root=self.frame_dir / "nested-output")
        self.assertEqual(nested.exception.code, "OUTPUT_OVERLAPS_SOURCE")
        with self.assertRaises(FramePreviewError) as parent:
            self.build(write=True, output_root=self.base)
        self.assertEqual(parent.exception.code, "OUTPUT_OVERLAPS_SOURCE")

    def test_static_player_is_local_only_and_preserves_rational_timing(self) -> None:
        result = self.build(write=True)
        package = self.output_root / result["package_path"]
        html_text = (package / "index.html").read_text(encoding="utf-8")
        js_text = (package / "player.js").read_text(encoding="utf-8")
        data_text = (package / "preview-data.js").read_text(encoding="utf-8")
        self.assertIn("connect-src &#x27;none&#x27;", html_text)
        self.assertIn("form-action &#x27;none&#x27;", html_text)
        for forbidden in ("http:", "https:", "fetch(", "XMLHttpRequest", "WebSocket", "localStorage"):
            self.assertNotIn(forbidden, html_text + js_text)
        self.assertIn('"offset_ms":100', data_text)
        self.assertIn('"end_time_num":2000', data_text)
        self.assertIn('"time_den":2', data_text)
        self.assertIn("frame.sha256!==shownSha", js_text)

    def test_checker_rejects_modified_or_extra_output(self) -> None:
        result = self.build(write=True)
        package = self.output_root / result["package_path"]
        player = package / "player.js"
        player.write_bytes(player.read_bytes() + b"x")
        with self.validated(), self.assertRaises(FramePreviewError) as modified:
            check_frame_preview_package(
                package / FRAME_PREVIEW_MANIFEST,
                self.output_root,
                self.frame_root,
                self.renderer_root,
                self.plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
            )
        self.assertEqual(modified.exception.code, "FILE_MISMATCH")
        player.write_bytes(player.read_bytes()[:-1])
        (package / "extra.txt").write_text("extra", encoding="utf-8")
        with self.validated(), self.assertRaises(FramePreviewError) as extra:
            check_frame_preview_package(
                package / FRAME_PREVIEW_MANIFEST,
                self.output_root,
                self.frame_root,
                self.renderer_root,
                self.plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
            )
        self.assertEqual(extra.exception.code, "FILE_SET_MISMATCH")

    def test_checker_rejects_symlinked_output(self) -> None:
        result = self.build(write=True)
        package = self.output_root / result["package_path"]
        target = package / "style.css"
        outside = self.base / "outside.css"
        outside.write_text("outside", encoding="utf-8")
        target.unlink()
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.validated(), self.assertRaises(FramePreviewError) as caught:
            check_frame_preview_package(
                package / FRAME_PREVIEW_MANIFEST,
                self.output_root,
                self.frame_root,
                self.renderer_root,
                self.plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
            )
        self.assertEqual(caught.exception.code, "PACKAGE_SYMLINK")

    def test_module_cli_build(self) -> None:
        args = [
            "build",
            str(self.frame_manifest_path),
            str(self.audio_manifest_path),
            "--frame-render-root",
            str(self.frame_root),
            "--renderer-job-root",
            str(self.renderer_root),
            "--render-plan-root",
            str(self.plan_root),
            "--audio-preview-root",
            str(self.audio_preview_root),
            "--preview-root",
            str(self.preview_root),
            "--package-root",
            str(self.package_root),
            "--audio-root",
            str(self.audio_root),
            "--output-root",
            str(self.output_root),
        ]
        stdout_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
        stderr = io.StringIO()
        with self.validated(), patch("sys.stdout", stdout), redirect_stderr(stderr):
            exit_code = main(args)
            stdout.flush()
        payload = json.loads(stdout_bytes.getvalue().decode("utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("frame preview ready", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
