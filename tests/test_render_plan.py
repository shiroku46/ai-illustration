from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.naming import canonical_json
from ai_illustration.render_plan import FRAME_INVENTORY, RENDER_PLAN_MANIFEST, RenderPlanError, build_render_plan_package, check_render_plan_package


def canonical_bytes(value: object) -> bytes:
    return canonical_json(value) + b"\n"


class RenderPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio_preview_root = self.root / "audio-previews"
        self.preview_root = self.root / "previews"
        self.package_root = self.root / "packages"
        self.audio_root = self.root / "audio"
        self.output_root = self.root / "render-plans"
        for directory in (self.audio_preview_root, self.preview_root, self.package_root, self.audio_root):
            directory.mkdir()
        self.audio_preview_id = "paper-theater-audio-preview-" + "1" * 20
        self.audio_preview_dir = self.audio_preview_root / self.audio_preview_id
        self.audio_preview_dir.mkdir()
        self.audio_preview_manifest = self.audio_preview_dir / "audio-preview-manifest.json"
        self.audio_preview_manifest.write_bytes(canonical_bytes({"id": self.audio_preview_id}))
        asset_a = hashlib.sha256(b"a").hexdigest()
        asset_b = hashlib.sha256(b"b").hexdigest()
        role = {"package_id": "variant-export-package-" + "2" * 20, "package_manifest_sha256": "a" * 64, "variant_set_ref": "variant-set-" + "3" * 20, "character_ref": "character-boke", "license_status": "reviewing", "stage_slot": "left"}
        boke_a = {"key": "boke.a", "variant_id": "variant-boke-a", "asset_path": f"assets/{asset_a}.png", "png_sha256": asset_a}
        boke_b = {"key": "boke.b", "variant_id": "variant-boke-b", "asset_path": f"assets/{asset_b}.png", "png_sha256": asset_b}
        tsukkomi = {"key": "tsukkomi.a", "variant_id": "variant-tsukkomi-a", "asset_path": f"assets/{asset_a}.png", "png_sha256": asset_a}
        self.audio_preview = {
            "id": self.audio_preview_id,
            "kind": "paper-theater-audio-preview",
            "schema_version": "1.0",
            "source_preview_ref": "paper-theater-preview-" + "4" * 20,
            "source_preview_path": "preview/preview-manifest.json",
            "source_preview_sha256": "b" * 64,
            "scene_plan_ref": "paper-theater-scene-plan-" + "5" * 20,
            "intent": "evaluation",
            "width": 1280,
            "height": 720,
            "scene_duration_ms": 1001,
            "offset_ms": -125,
            "duration_policy": "scene-at-least-audio",
            "synchronized_audio_end_ms": 875,
            "roles": {"boke": role, "tsukkomi": {**role, "character_ref": "character-tsukkomi", "stage_slot": "right"}},
            "segments": [
                {"start_ms": 0, "end_ms": 500, "stage_slots": {"boke": "left", "tsukkomi": "right"}, "boke": boke_a, "tsukkomi": tsukkomi},
                {"start_ms": 500, "end_ms": 1001, "stage_slots": {"boke": "left", "tsukkomi": "right"}, "boke": boke_b, "tsukkomi": tsukkomi},
            ],
            "assets": [{"path": f"assets/{asset_a}.png", "sha256": asset_a, "size": 1}, {"path": f"assets/{asset_b}.png", "sha256": asset_b, "size": 1}],
            "audio": {"source_path": "voice.wav", "path": "audio/" + "c" * 64 + ".wav", "sha256": "c" * 64, "sample_rate": 48000, "frame_count": 48000, "duration_ms": 1000, "license_status": "reviewing"},
        }

    def _checked(self):
        return {"ok": True, "audio_preview": self.audio_preview, "file_count": 7, "segment_count": 2}

    def _build(self, **kwargs):
        options = {"fps_num": 30000, "fps_den": 1001}
        options.update(kwargs)
        with patch("ai_illustration.render_plan.check_audio_preview_package", return_value=self._checked()):
            return build_render_plan_package(self.audio_preview_manifest, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root, self.output_root, **options)

    def test_deterministic_dry_run_and_ceiling_frame_count(self) -> None:
        first = self._build()
        second = self._build()
        self.assertEqual(first, second)
        self.assertFalse(self.output_root.exists())
        plan = first["render_plan"]
        expected = (1001 * 30000 + 1000 * 1001 - 1) // (1000 * 1001)
        self.assertEqual(plan["frame_count"], expected)
        self.assertEqual(plan["frames"][-1]["end_time_num"], 1001 * 30000)
        self.assertEqual(plan["frame_boundary_policy"], "state-at-frame-start")
        self.assertFalse(plan["output_target"]["media_created"])

    def test_common_rational_rates_and_boundary_selection(self) -> None:
        for numerator, denominator in ((24, 1), (25, 1), (30, 1), (30000, 1001)):
            plan = self._build(fps_num=numerator, fps_den=denominator)["render_plan"]
            self.assertEqual(plan["fps_num"], numerator)
            self.assertEqual(plan["fps_den"], denominator)
            self.assertEqual(plan["frames"][0]["index"], 0)
        boundary = self._build(fps_num=2, fps_den=1)["render_plan"]
        self.assertEqual(boundary["frames"][0]["boke"]["variant_id"], "variant-boke-a")
        self.assertEqual(boundary["frames"][1]["boke"]["variant_id"], "variant-boke-b")
        self.assertEqual(len(boundary["spans"]), 2)

    def test_audio_sample_placement_is_exact_and_signed(self) -> None:
        placement = self._build(fps_num=25, fps_den=1)["render_plan"]["audio_placement"]
        self.assertEqual(placement["offset_ms"], -125)
        self.assertEqual(placement["start_sample_num"], -125 * 48000)
        self.assertEqual(placement["start_sample_den"], 1000)
        self.assertEqual(placement["policy"], "signed-rational-sample-offset-no-resampling")

    def test_write_is_atomic_idempotent_and_checkable(self) -> None:
        first = self._build(write=True)
        second = self._build(write=True)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        manifest = self.output_root / first["render_plan"]["id"] / RENDER_PLAN_MANIFEST
        with patch("ai_illustration.render_plan.check_audio_preview_package", return_value=self._checked()):
            checked = check_render_plan_package(manifest, self.output_root, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root)
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["frame_count"], first["render_plan"]["frame_count"])

    def test_checker_rejects_modified_inventory_and_extra_file(self) -> None:
        result = self._build(write=True)
        directory = self.output_root / result["render_plan"]["id"]
        inventory = directory / FRAME_INVENTORY
        original = inventory.read_bytes()
        inventory.write_bytes(original + b" ")
        with patch("ai_illustration.render_plan.check_audio_preview_package", return_value=self._checked()):
            with self.assertRaisesRegex(RenderPlanError, "FILE_MISMATCH"):
                check_render_plan_package(directory / RENDER_PLAN_MANIFEST, self.output_root, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root)
        inventory.write_bytes(original)
        (directory / "extra.txt").write_text("extra", encoding="utf-8")
        with patch("ai_illustration.render_plan.check_audio_preview_package", return_value=self._checked()):
            with self.assertRaisesRegex(RenderPlanError, "FILE_SET_MISMATCH"):
                check_render_plan_package(directory / RENDER_PLAN_MANIFEST, self.output_root, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root)

    def test_fps_and_segment_failures_are_closed(self) -> None:
        for numerator, denominator in ((0, 1), (-1, 1), (1, 0), (1_000_001, 1)):
            with self.assertRaisesRegex(RenderPlanError, "INTEGER_RANGE"):
                self._build(fps_num=numerator, fps_den=denominator)
        self.audio_preview["segments"][1]["start_ms"] = 499
        with self.assertRaisesRegex(RenderPlanError, "SEGMENT_COVERAGE"):
            self._build(fps_num=25, fps_den=1)

    def test_no_renderer_or_execution_instruction_is_emitted(self) -> None:
        payload = canonical_json(self._build(fps_num=24, fps_den=1)["render_plan"]).decode("ascii").lower()
        for forbidden in ("ffmpeg", "subprocess", "shell", "filter_complex", "http://", "https://", "credential", "secret"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
