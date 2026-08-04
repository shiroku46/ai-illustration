from __future__ import annotations

from tests.video_export_test_support import *


class VideoExportRuntimeTests(VideoExportTestCase):
    def test_run_writes_checks_and_is_idempotent(self):
        first = self.execute()
        self.assertTrue(first["executed"])
        destination = self.output_root / first["package_path"]
        self.assertEqual((destination / VIDEO_OUTPUT).read_bytes(), b"FAKE-MP4")
        self.assertEqual({path.name for path in destination.iterdir()}, {VIDEO_EXPORT_MANIFEST, VIDEO_EXPORT_PLAN, VIDEO_OUTPUT})
        second = self.execute()
        self.assertFalse(second["executed"])
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            checked = check_video_export_package(
                destination / VIDEO_EXPORT_MANIFEST,
                self.profile_path,
                self.ffmpeg,
                self.output_root,
                self.frame_preview_root,
                self.frame_render_root,
                self.renderer_root,
                self.plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
                self.profile_root,
            )
        self.assertTrue(checked["ok"])

    def test_process_is_shell_free_sanitized_and_isolated(self):
        from ai_illustration import video_export_process
        captured = {}
        real = video_export_process.subprocess.Popen

        class CapturingPopen:
            def __new__(cls, *args, **kwargs):
                captured.update(kwargs)
                return real(*args, **kwargs)

        original_frame = (self.package_dir / "frames/00000000.png").read_bytes()
        with patch.object(video_export_process.subprocess, "Popen", CapturingPopen):
            self.execute()
        self.assertIs(captured["shell"], False)
        self.assertNotIn("GITHUB_TOKEN", captured["env"])
        self.assertNotIn("PATH", captured["env"])
        self.assertEqual(captured["env"]["LC_ALL"], "C")
        work = Path(captured["cwd"]).resolve()
        self.assertEqual(work.name, "work")
        self.assertNotEqual(work, self.package_dir.resolve())
        for key in ("HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR"):
            self.assertEqual(Path(captured["env"][key]).resolve(), work)
        self.assertEqual((self.package_dir / "frames/00000000.png").read_bytes(), original_frame)

    def test_existing_staging_conflict_is_preserved(self):
        package_id = self.plan()["package_path"]
        staging = self.output_root / f".{package_id}.tmp"
        staging.mkdir(parents=True)
        marker = staging / "keep.txt"
        marker.write_text("owned elsewhere", encoding="utf-8")
        with self.assertRaises(VideoExportError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "STAGING_CONFLICT")
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "owned elsewhere")

    def test_diagnostic_limit_is_shared_across_stdout_and_stderr(self):
        self.ffmpeg.write_text(
            f"#!{sys.executable}\nimport os,sys\nos.write(1,b'x'*10)\nos.write(2,b'y'*10)\nsys.exit(0)\n",
            encoding="utf-8",
        )
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        with patch("ai_illustration.video_export_process.MAX_DIAGNOSTIC_BYTES", 16):
            with self.assertRaises(VideoExportError) as caught:
                self.execute()
        self.assertEqual(caught.exception.code, "FFMPEG_DIAGNOSTIC_LIMIT")
        self.assertEqual(list(self.output_root.glob(".*.tmp")), [])

    def test_executable_cannot_publish_extra_staging_files(self):
        self.ffmpeg.write_text(
            f"#!{sys.executable}\nimport pathlib,sys\nout=pathlib.Path(sys.argv[-1]);out.write_bytes(b'OK');out.with_name('extra.txt').write_text('extra')\n",
            encoding="utf-8",
        )
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        with self.assertRaises(VideoExportError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "STAGING_FILE_SET")
        self.assertEqual(list(self.output_root.glob(".*.tmp")), [])

    def test_failure_cleans_staging(self):
        self.ffmpeg.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(7)\n", encoding="utf-8")
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        with self.assertRaises(VideoExportError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "FFMPEG_FAILED")
        self.assertEqual(list(self.output_root.glob(".*.tmp")), [])

    def test_output_tamper_and_extra_file_fail(self):
        result = self.execute()
        destination = self.output_root / result["package_path"]
        (destination / VIDEO_OUTPUT).write_bytes(b"tampered")
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            with self.assertRaises(VideoExportError) as caught:
                check_video_export_package(
                    destination / VIDEO_EXPORT_MANIFEST,
                    self.profile_path,
                    self.ffmpeg,
                    self.output_root,
                    self.frame_preview_root,
                    self.frame_render_root,
                    self.renderer_root,
                    self.plan_root,
                    self.audio_preview_root,
                    self.preview_root,
                    self.package_root,
                    self.audio_root,
                    self.profile_root,
                )
        self.assertEqual(caught.exception.code, "VIDEO_MISMATCH")

    def test_unknown_manifest_field_is_rejected(self):
        result = self.execute()
        destination = self.output_root / result["package_path"]
        manifest_path = destination / VIDEO_EXPORT_MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["unexpected"] = True
        manifest_path.write_bytes(canonical(manifest))
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            with self.assertRaises(VideoExportError) as caught:
                check_video_export_package(
                    manifest_path,
                    self.profile_path,
                    self.ffmpeg,
                    self.output_root,
                    self.frame_preview_root,
                    self.frame_render_root,
                    self.renderer_root,
                    self.plan_root,
                    self.audio_preview_root,
                    self.preview_root,
                    self.package_root,
                    self.audio_root,
                    self.profile_root,
                )
        self.assertEqual(caught.exception.code, "MANIFEST_SCHEMA")

    def test_timeout_and_oversize_failures_clean_staging(self):
        self.ffmpeg.write_text(
            f"#!{sys.executable}\nimport time\ntime.sleep(5)\n",
            encoding="utf-8",
        )
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        with self.assertRaises(VideoExportError) as timed_out:
            self.execute(timeout=1)
        self.assertEqual(timed_out.exception.code, "FFMPEG_TIMEOUT")
        self.assertEqual(list(self.output_root.glob(".*.tmp")), [])

        self.ffmpeg.write_text(
            f"#!{sys.executable}\nimport pathlib,sys\npathlib.Path(sys.argv[-1]).write_bytes(b'12345678')\n",
            encoding="utf-8",
        )
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        with patch("ai_illustration.video_export_execute.MAX_VIDEO_BYTES", 4):
            with self.assertRaises(VideoExportError) as oversized:
                self.execute()
        self.assertEqual(oversized.exception.code, "FILE_TOO_LARGE")
        self.assertEqual(list(self.output_root.glob(".*.tmp")), [])

    def test_checker_rejects_extra_empty_directory_and_symlink_files(self):
        result = self.execute()
        destination = self.output_root / result["package_path"]
        extra = destination / "extra.txt"
        extra.write_text("extra", encoding="utf-8")
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            with self.assertRaises(VideoExportError) as extra_error:
                check_video_export_package(
                    destination / VIDEO_EXPORT_MANIFEST, self.profile_path, self.ffmpeg, self.output_root,
                    self.frame_preview_root, self.frame_render_root, self.renderer_root, self.plan_root,
                    self.audio_preview_root, self.preview_root, self.package_root, self.audio_root, self.profile_root,
                )
        self.assertEqual(extra_error.exception.code, "FILE_SET_MISMATCH")
        extra.unlink()
        empty_dir = destination / "unexpected-dir"
        empty_dir.mkdir()
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            with self.assertRaises(VideoExportError) as directory_error:
                check_video_export_package(
                    destination / VIDEO_EXPORT_MANIFEST, self.profile_path, self.ffmpeg, self.output_root,
                    self.frame_preview_root, self.frame_render_root, self.renderer_root, self.plan_root,
                    self.audio_preview_root, self.preview_root, self.package_root, self.audio_root, self.profile_root,
                )
        self.assertEqual(directory_error.exception.code, "FILE_SET_MISMATCH")
        empty_dir.rmdir()
        target = destination / VIDEO_OUTPUT
        outside = self.base / "outside.mp4"
        outside.write_bytes(b"outside")
        target.unlink()
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            with self.assertRaises(VideoExportError) as symlinked:
                check_video_export_package(
                    destination / VIDEO_EXPORT_MANIFEST, self.profile_path, self.ffmpeg, self.output_root,
                    self.frame_preview_root, self.frame_render_root, self.renderer_root, self.plan_root,
                    self.audio_preview_root, self.preview_root, self.package_root, self.audio_root, self.profile_root,
                )
        self.assertEqual(symlinked.exception.code, "FILE_SET_MISMATCH")

    def test_checker_rejects_profile_and_executable_changes(self):
        result = self.execute()
        destination = self.output_root / result["package_path"]
        changed_profile = profile_value(crf=19)
        self.profile_path.write_bytes(canonical(changed_profile))
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            with self.assertRaises(VideoExportError) as changed:
                check_video_export_package(
                    destination / VIDEO_EXPORT_MANIFEST, self.profile_path, self.ffmpeg, self.output_root,
                    self.frame_preview_root, self.frame_render_root, self.renderer_root, self.plan_root,
                    self.audio_preview_root, self.preview_root, self.package_root, self.audio_root, self.profile_root,
                )
        self.assertEqual(changed.exception.code, "MANIFEST_BINDING_MISMATCH")
        self.profile_path.write_bytes(canonical(self.profile))
        self.ffmpeg.write_bytes(self.ffmpeg.read_bytes() + b"\n# changed\n")
        self.ffmpeg.chmod(self.ffmpeg.stat().st_mode | stat.S_IXUSR)
        with patch("ai_illustration.video_export_source.check_frame_preview_package", return_value=self._checker_result()):
            with self.assertRaises(VideoExportError) as executable_changed:
                check_video_export_package(
                    destination / VIDEO_EXPORT_MANIFEST, self.profile_path, self.ffmpeg, self.output_root,
                    self.frame_preview_root, self.frame_render_root, self.renderer_root, self.plan_root,
                    self.audio_preview_root, self.preview_root, self.package_root, self.audio_root, self.profile_root,
                )
        self.assertEqual(executable_changed.exception.code, "MANIFEST_BINDING_MISMATCH")


if __name__ == "__main__":
    unittest.main()
