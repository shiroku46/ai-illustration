from __future__ import annotations

from tests.video_export_test_support import *


class VideoExportPlanTests(VideoExportTestCase):
    def test_plan_is_deterministic_and_non_mutating(self):
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)
        self.assertFalse(self.output_root.exists())
        plan = first["video_export_plan"]
        self.assertEqual(plan["command_template"][0], "@FFMPEG@")
        self.assertEqual(plan["command_template"][-1], "@OUTPUT@")
        self.assertNotIn(str(self.base), json.dumps(plan))
        self.assertIn("adelay=100:all=1", plan["audio_filter"])
        self.assertIn("-framerate", plan["command_template"])
        self.assertIn("-frames:v", plan["command_template"])

    def test_zero_and_negative_offset_filters(self):
        self.manifest["audio_placement"]["offset_ms"] = 0
        self._write_manifest()
        self.assertNotIn(
            "adelay", self.plan()["video_export_plan"]["audio_filter"]
        )
        self.manifest["audio_placement"]["offset_ms"] = -250
        self._write_manifest()
        self.assertIn(
            "atrim=start=250ms",
            self.plan()["video_export_plan"]["audio_filter"],
        )

    def test_rejects_nonopaque_frame(self):
        payload = encode_rgba_png(
            RGBAImage(
                2,
                2,
                bytes([255, 0, 0, 0] + [255, 0, 0, 255] * 3),
            )
        )
        path = self.package_dir / "frames/00000000.png"
        path.write_bytes(payload)
        self.manifest["files"][0] = {
            "path": "frames/00000000.png",
            "sha256": sha(payload),
            "size": len(payload),
        }
        self._write_manifest()
        with self.assertRaises(VideoExportError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "NONOPAQUE_FRAME")

    def test_rejects_odd_dimensions(self):
        self.manifest["canvas"]["width"] = 3
        self._write_manifest()
        with self.assertRaises(VideoExportError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "ODD_DIMENSIONS")

    def test_profile_id_and_executable_are_bound(self):
        result = self.plan()["video_export_plan"]
        self.assertEqual(result["profile"]["id"], self.profile["id"])
        self.assertEqual(
            result["ffmpeg"]["sha256"], sha(self.ffmpeg.read_bytes())
        )
        broken = dict(self.profile)
        broken["id"] = (
            "paper-theater-video-export-profile-" + "0" * 20
        )
        self.profile_path.write_bytes(canonical(broken))
        with self.assertRaises(VideoExportError) as caught:
            self.plan()
        self.assertEqual(caught.exception.code, "PROFILE_ID")

    def test_rejects_nonexecutable_and_symlink_executable(self):
        # Windows does not expose POSIX execute bits. Patching the exact access
        # decision keeps the product rejection branch covered on every OS.
        with patch(
            "ai_illustration.video_export_bindings.os.access",
            return_value=False,
        ):
            with self.assertRaises(VideoExportError) as caught:
                self.plan()
        self.assertEqual(caught.exception.code, "FFMPEG_NOT_EXECUTABLE")

        link = self.base / f"ffmpeg-link{self.ffmpeg.suffix}"
        try:
            link.symlink_to(self.ffmpeg)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        args = list(self._args())
        args[2] = link
        with patch(
            "ai_illustration.video_export_source.check_frame_preview_package",
            return_value=self._checker_result(),
        ):
            with self.assertRaises(VideoExportError) as symlinked:
                build_video_export_plan(*args)
        self.assertEqual(symlinked.exception.code, "PATH_SYMLINK")

    def test_rejects_output_overlap_both_directions(self):
        args = list(self._args())
        args[-1] = self.package_dir / "nested"
        with patch(
            "ai_illustration.video_export_source.check_frame_preview_package",
            return_value=self._checker_result(),
        ):
            with self.assertRaises(VideoExportError) as nested:
                build_video_export_plan(*args)
        self.assertEqual(nested.exception.code, "OUTPUT_OVERLAPS_SOURCE")
        args[-1] = self.base
        with patch(
            "ai_illustration.video_export_source.check_frame_preview_package",
            return_value=self._checker_result(),
        ):
            with self.assertRaises(VideoExportError) as parent:
                build_video_export_plan(*args)
        self.assertEqual(parent.exception.code, "OUTPUT_OVERLAPS_SOURCE")

    def test_module_cli_plan(self):
        argv = [
            "plan",
            str(self.manifest_path),
            str(self.profile_path),
            "--ffmpeg",
            str(self.ffmpeg),
            "--frame-preview-root",
            str(self.frame_preview_root),
            "--frame-render-root",
            str(self.frame_render_root),
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
            "--profile-root",
            str(self.profile_root),
            "--output-root",
            str(self.output_root),
        ]
        output_bytes = io.BytesIO()
        output = io.TextIOWrapper(output_bytes, encoding="utf-8")
        stderr = io.StringIO()
        with patch(
            "ai_illustration.video_export_source.check_frame_preview_package",
            return_value=self._checker_result(),
        ), patch("sys.stdout", output), redirect_stderr(stderr):
            code = main(argv)
            output.flush()
        result = json.loads(output_bytes.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertIn("video export plan ready", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
