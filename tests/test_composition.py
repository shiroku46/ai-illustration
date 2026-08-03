from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.composition import (
    COMPOSITION_PROFILE,
    RENDERER_JOB_MANIFEST,
    SPAN_TRANSFORMS,
    CompositionError,
    build_composition_job_package,
    check_composition_job_package,
)
from ai_illustration.naming import canonical_json, content_identifier
from ai_illustration.render_plan import RenderPlanError


def canonical_bytes(value: object) -> bytes:
    return canonical_json(value) + b"\n"


class CompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.render_plan_root = self.root / "render-plans"
        self.audio_preview_root = self.root / "audio-previews"
        self.preview_root = self.root / "previews"
        self.package_root = self.root / "packages"
        self.audio_root = self.root / "audio"
        self.output_root = self.root / "renderer-jobs"
        for directory in (
            self.render_plan_root,
            self.audio_preview_root,
            self.preview_root,
            self.package_root,
            self.audio_root,
        ):
            directory.mkdir()
        self.render_plan_id = "paper-theater-render-plan-" + "1" * 20
        self.render_plan_dir = self.render_plan_root / self.render_plan_id
        self.render_plan_dir.mkdir()
        self.render_plan_manifest = self.render_plan_dir / "render-plan-manifest.json"
        self.render_plan_manifest.write_bytes(canonical_bytes({"id": self.render_plan_id}))
        self.render_plan = {
            "id": self.render_plan_id,
            "kind": "paper-theater-render-plan",
            "schema_version": "1.0",
            "source_bindings": {
                "audio_preview": {
                    "id": "paper-theater-audio-preview-" + "2" * 20,
                    "path": "audio-preview/audio-preview-manifest.json",
                    "sha256": "a" * 64,
                },
                "roles": {
                    "boke": {"character_ref": "character-boke", "stage_slot": "right"},
                    "tsukkomi": {"character_ref": "character-tsukkomi", "stage_slot": "left"},
                },
            },
            "intent": "evaluation",
            "audio_license_status": "reviewing",
            "width": 1280,
            "height": 720,
            "scene_duration_ms": 1000,
            "fps_num": 24,
            "fps_den": 1,
            "frame_count": 24,
            "spans": [
                {
                    "start_frame": 0,
                    "end_frame": 12,
                    "start_time_num": 0,
                    "end_time_num": 12000,
                    "time_den": 24,
                    "boke": self._asset("boke", "right", "b" * 64),
                    "tsukkomi": self._asset("tsukkomi", "left", "c" * 64),
                },
                {
                    "start_frame": 12,
                    "end_frame": 24,
                    "start_time_num": 12000,
                    "end_time_num": 24000,
                    "time_den": 24,
                    "boke": self._asset("boke", "right", "d" * 64),
                    "tsukkomi": self._asset("tsukkomi", "left", "c" * 64),
                },
            ],
            "audio_placement": {
                "policy": "signed-rational-sample-offset-no-resampling",
                "offset_ms": -125,
                "start_sample_num": -6000000,
                "start_sample_den": 1000,
                "source_sample_rate": 48000,
                "source_frame_count": 48000,
                "duration_policy": "exact",
                "synchronized_audio_end_ms": 875,
            },
        }
        self.profile_path = self.root / "composition-profile.json"
        self._write_profile(self._profile_core())

    def _asset(self, role: str, slot: str, sha: str) -> dict[str, str]:
        return {
            "key": f"{role}.neutral",
            "variant_id": f"variant-{role}-{sha[0]}",
            "asset_path": f"assets/{role}-{sha[0]}.png",
            "png_sha256": sha,
            "stage_slot": slot,
        }

    def _profile_core(self) -> dict[str, object]:
        return {
            "kind": "paper-theater-composition-profile",
            "schema_version": "1.0",
            "canvas": {"width": 1280, "height": 720, "background_rgba": [12, 23, 34, 0]},
            "slots": [
                {
                    "name": "left",
                    "source_anchor": {"x": 0, "y": 720},
                    "target_anchor": {"x": 320, "y": 720},
                    "scale": {"numerator": 2, "denominator": 3},
                    "translation": {"x": -7, "y": 5},
                    "z_order": 10,
                },
                {
                    "name": "right",
                    "source_anchor": {"x": 0, "y": 720},
                    "target_anchor": {"x": 960, "y": 720},
                    "scale": {"numerator": 1, "denominator": 1},
                    "translation": {"x": 11, "y": -3},
                    "z_order": 5,
                },
            ],
        }

    def _write_profile(self, core: dict[str, object]) -> None:
        profile = {
            "id": content_identifier("paper-theater-composition-profile", core, 20),
            **core,
        }
        self.profile_path.write_bytes(canonical_bytes(profile))

    def _checked(self) -> dict[str, object]:
        return {
            "ok": True,
            "render_plan": self.render_plan,
            "file_count": 4,
            "frame_count": 24,
            "span_count": 2,
        }

    def _build(self, **kwargs):
        with patch("ai_illustration.composition.check_render_plan_package", return_value=self._checked()):
            return build_composition_job_package(
                self.render_plan_manifest,
                self.profile_path,
                self.render_plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
                self.output_root,
                **kwargs,
            )

    def test_deterministic_dry_run_preserves_swapped_slots_and_rationals(self) -> None:
        first = self._build()
        second = self._build()
        self.assertEqual(first, second)
        self.assertFalse(self.output_root.exists())
        manifest = first["renderer_job"]
        self.assertEqual(manifest["frame_count"], 24)
        self.assertEqual(manifest["span_count"], 2)
        self.assertEqual(manifest["audio_placement"], self.render_plan["audio_placement"])
        self.assertEqual(manifest["intent"], "evaluation")
        result = self._build(write=True)
        inventory = self.output_root / result["renderer_job"]["id"] / SPAN_TRANSFORMS
        spans = __import__("json").loads(inventory.read_text(encoding="utf-8"))["spans"]
        placements = spans[0]["placements"]
        self.assertEqual([item["role"] for item in placements], ["boke", "tsukkomi"])
        self.assertEqual(placements[0]["slot"], "right")
        self.assertEqual(placements[0]["translation"], {"x": 11, "y": -3})
        self.assertEqual(placements[1]["scale"], {"numerator": 2, "denominator": 3})

    def test_write_is_atomic_idempotent_and_checkable(self) -> None:
        first = self._build(write=True)
        second = self._build(write=True)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        manifest = self.output_root / first["renderer_job"]["id"] / RENDERER_JOB_MANIFEST
        with patch("ai_illustration.composition.check_render_plan_package", return_value=self._checked()):
            checked = check_composition_job_package(
                manifest,
                self.output_root,
                self.render_plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
            )
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["span_count"], 2)

    def test_checker_rejects_modified_inventory_and_extra_file(self) -> None:
        result = self._build(write=True)
        directory = self.output_root / result["renderer_job"]["id"]
        inventory = directory / SPAN_TRANSFORMS
        original = inventory.read_bytes()
        inventory.write_bytes(original + b" ")
        with patch("ai_illustration.composition.check_render_plan_package", return_value=self._checked()):
            with self.assertRaisesRegex(CompositionError, "FILE_MISMATCH"):
                check_composition_job_package(
                    directory / RENDERER_JOB_MANIFEST,
                    self.output_root,
                    self.render_plan_root,
                    self.audio_preview_root,
                    self.preview_root,
                    self.package_root,
                    self.audio_root,
                )
        inventory.write_bytes(original)
        (directory / "extra.txt").write_text("extra", encoding="utf-8")
        with patch("ai_illustration.composition.check_render_plan_package", return_value=self._checked()):
            with self.assertRaisesRegex(CompositionError, "FILE_SET_MISMATCH"):
                check_composition_job_package(
                    directory / RENDERER_JOB_MANIFEST,
                    self.output_root,
                    self.render_plan_root,
                    self.audio_preview_root,
                    self.preview_root,
                    self.package_root,
                    self.audio_root,
                )

    def test_profile_validation_fails_closed(self) -> None:
        cases = []
        canvas = self._profile_core()
        canvas["canvas"] = {**canvas["canvas"], "width": 1279}
        cases.append((canvas, "CANVAS_MISMATCH"))
        missing = self._profile_core()
        missing["slots"] = missing["slots"][:1]
        cases.append((missing, "PROFILE_SLOTS"))
        duplicate = self._profile_core()
        duplicate["slots"][1] = {**duplicate["slots"][1], "name": "left"}
        cases.append((duplicate, "PROFILE_SLOTS"))
        non_reduced = self._profile_core()
        non_reduced["slots"][0]["scale"] = {"numerator": 2, "denominator": 4}
        cases.append((non_reduced, "SCALE_NOT_REDUCED"))
        floating = self._profile_core()
        floating["slots"][0]["translation"] = {"x": 1.5, "y": 0}
        cases.append((floating, "INTEGER_RANGE"))
        for core, code in cases:
            with self.subTest(code=code):
                self._write_profile(core)
                with self.assertRaisesRegex(CompositionError, code):
                    self._build()
        self._write_profile(self._profile_core())

    def test_span_coverage_and_upstream_failure_are_closed(self) -> None:
        self.render_plan["spans"][1]["start_frame"] = 11
        with self.assertRaisesRegex(CompositionError, "SPAN_COVERAGE"):
            self._build()
        self.render_plan["spans"][1]["start_frame"] = 12
        failure = RenderPlanError("FILE_MISMATCH", "modified", "render-plan")
        with patch("ai_illustration.composition.check_render_plan_package", side_effect=failure):
            with self.assertRaisesRegex(CompositionError, "RENDER_PLAN_FILE_MISMATCH"):
                build_composition_job_package(
                    self.render_plan_manifest,
                    self.profile_path,
                    self.render_plan_root,
                    self.audio_preview_root,
                    self.preview_root,
                    self.package_root,
                    self.audio_root,
                    self.output_root,
                )

    def test_symlink_and_conflicting_output_are_rejected(self) -> None:
        result = self._build(write=True)
        directory = self.output_root / result["renderer_job"]["id"]
        (directory / COMPOSITION_PROFILE).write_bytes(b"different")
        with self.assertRaisesRegex(CompositionError, "OUTPUT_CONFLICT"):
            self._build(write=True)
        link = self.root / "profile-link.json"
        try:
            link.symlink_to(self.profile_path)
        except OSError:
            return
        original = self.profile_path
        self.profile_path = link
        with self.assertRaisesRegex(CompositionError, "PATH_SYMLINK"):
            self._build()
        self.profile_path = original

    def test_no_media_execution_instruction_is_emitted(self) -> None:
        payload = canonical_json(self._build()["renderer_job"]).decode("ascii").lower()
        for forbidden in (
            "ffmpeg",
            "subprocess",
            "shell",
            "http://",
            "https://",
            "credential",
            "secret",
            "video-output",
            "image-output",
        ):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
