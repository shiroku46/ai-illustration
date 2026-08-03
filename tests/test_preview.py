from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.naming import canonical_json, content_identifier
from ai_illustration.preview import PreviewError, PREVIEW_MANIFEST, build_preview_package, check_preview_package


def canonical_bytes(value: object) -> bytes:
    return canonical_json(value) + b"\n"


class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package_root = self.root / "packages"
        self.package_root.mkdir()
        self.output_root = self.root / "preview"
        self.scene_path = self.package_root / "scene.json"

        self.boke_png = b"\x89PNG\r\n\x1a\nboke"
        self.tsukkomi_png = b"\x89PNG\r\n\x1a\ntsukkomi"
        boke_sha = hashlib.sha256(self.boke_png).hexdigest()
        tsukkomi_sha = hashlib.sha256(self.tsukkomi_png).hexdigest()
        (self.package_root / "pkg-boke" / "assets").mkdir(parents=True)
        (self.package_root / "pkg-tsukkomi" / "assets").mkdir(parents=True)
        (self.package_root / "pkg-boke" / "assets" / "boke.png").write_bytes(self.boke_png)
        (self.package_root / "pkg-tsukkomi" / "assets" / "tsukkomi.png").write_bytes(self.tsukkomi_png)

        core = {
            "kind": "paper-theater-scene-plan",
            "schema_version": "1.0",
            "intent": "evaluation",
            "duration_ms": 1000,
            "roles": {
                "boke": {
                    "package_manifest_path": "pkg-boke/package-manifest.json",
                    "package_id": "variant-export-package-" + "1" * 20,
                    "package_manifest_sha256": "a" * 64,
                    "variant_set_ref": "variant-set-" + "2" * 20,
                    "character_ref": "character-boke",
                    "license_status": "reviewing",
                    "stage_slot": "left",
                    "initial_key": "boke.neutral",
                },
                "tsukkomi": {
                    "package_manifest_path": "pkg-tsukkomi/package-manifest.json",
                    "package_id": "variant-export-package-" + "3" * 20,
                    "package_manifest_sha256": "b" * 64,
                    "variant_set_ref": "variant-set-" + "4" * 20,
                    "character_ref": "character-tsukkomi",
                    "license_status": "reviewing",
                    "stage_slot": "right",
                    "initial_key": "tsukkomi.neutral",
                },
            },
            "events": [],
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 1000,
                    "stage_slots": {"boke": "left", "tsukkomi": "right"},
                    "boke": {
                        "key": "boke.neutral",
                        "variant_id": "variant-boke",
                        "png_path": "pkg-boke/assets/boke.png",
                        "png_sha256": boke_sha,
                    },
                    "tsukkomi": {
                        "key": "tsukkomi.neutral",
                        "variant_id": "variant-tsukkomi",
                        "png_path": "pkg-tsukkomi/assets/tsukkomi.png",
                        "png_sha256": tsukkomi_sha,
                    },
                }
            ],
        }
        self.scene = {"id": content_identifier("paper-theater-scene-plan", core, 20), **core}
        self.scene_path.write_bytes(canonical_bytes(self.scene))
        self.scene_result = {"ok": True, "scene_plan": self.scene, "segment_count": 1}

    def _patch_scene(self):
        return patch("ai_illustration.preview.check_scene_plan", return_value=self.scene_result)

    def test_deterministic_dry_run_does_not_write(self) -> None:
        with self._patch_scene():
            first = build_preview_package(self.scene_path, self.package_root, self.output_root, width=1920, height=1080)
            second = build_preview_package(self.scene_path, self.package_root, self.output_root, width=1920, height=1080)
        self.assertEqual(first, second)
        self.assertFalse(self.output_root.exists())
        self.assertFalse(first["written"])

    def test_write_is_byte_preserving_idempotent_and_checkable(self) -> None:
        with self._patch_scene():
            first = build_preview_package(self.scene_path, self.package_root, self.output_root, width=1280, height=720, write=True)
            second = build_preview_package(self.scene_path, self.package_root, self.output_root, width=1280, height=720, write=True)
            manifest_path = self.output_root / first["preview"]["id"] / PREVIEW_MANIFEST
            checked = check_preview_package(manifest_path, self.output_root, self.package_root)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        self.assertTrue(checked["ok"])
        for asset in first["preview"]["assets"]:
            copied = self.output_root / first["preview"]["id"] / asset["path"]
            source = self.package_root / asset["source_path"]
            self.assertEqual(copied.read_bytes(), source.read_bytes())

    def test_tamper_and_extra_files_fail_closed(self) -> None:
        with self._patch_scene():
            result = build_preview_package(self.scene_path, self.package_root, self.output_root, width=640, height=480, write=True)
            directory = self.output_root / result["preview"]["id"]
            manifest_path = directory / PREVIEW_MANIFEST
            (directory / "index.html").write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(PreviewError, "FILE_MISMATCH"):
                check_preview_package(manifest_path, self.output_root, self.package_root)

        other = self.root / "other"
        with self._patch_scene():
            result = build_preview_package(self.scene_path, self.package_root, other, width=640, height=480, write=True)
            directory = other / result["preview"]["id"]
            (directory / "extra.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(PreviewError, "FILE_SET_MISMATCH"):
                check_preview_package(directory / PREVIEW_MANIFEST, other, self.package_root)

    def test_invalid_dimensions_and_source_tamper_fail(self) -> None:
        with self._patch_scene():
            with self.assertRaisesRegex(PreviewError, "DIMENSION"):
                build_preview_package(self.scene_path, self.package_root, self.output_root, width=0, height=720)
            (self.package_root / "pkg-boke" / "assets" / "boke.png").write_bytes(b"tampered")
            with self.assertRaisesRegex(PreviewError, "SOURCE_ASSET_MISMATCH"):
                build_preview_package(self.scene_path, self.package_root, self.output_root, width=640, height=480)


if __name__ == "__main__":
    unittest.main()
