from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.naming import canonical_json
from ai_illustration.paper_theater import PaperTheaterError, check_scene_plan, plan_scene


def _write_json(path: Path, value: dict[str, object], *, canonical: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if canonical:
        path.write_bytes(canonical_json(value) + b"\n")
    else:
        path.write_text(json.dumps(value), encoding="utf-8")


class PaperTheaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packages = self.root / "packages"
        self.packages.mkdir()
        self.boke_id = "variant-export-package-" + "a" * 20
        self.tsukkomi_id = "variant-export-package-" + "b" * 20
        self.package_data = {
            self.boke_id: self._package(
                self.boke_id,
                "variant-set-" + "a" * 20,
                "boke@v001",
                [("boke.neutral", "1"), ("boke.smile", "2")],
            ),
            self.tsukkomi_id: self._package(
                self.tsukkomi_id,
                "variant-set-" + "b" * 20,
                "tsukkomi@v001",
                [("tsukkomi.neutral", "3"), ("tsukkomi.retort", "4")],
            ),
        }
        for package_id in self.package_data:
            _write_json(self.packages / package_id / "package-manifest.json", {}, canonical=True)
        self.cue = {
            "kind": "paper-theater-cue-sheet",
            "schema_version": "1.0",
            "duration_ms": 1000,
            "packages": {
                "boke": f"{self.boke_id}/package-manifest.json",
                "tsukkomi": f"{self.tsukkomi_id}/package-manifest.json",
            },
            "stage_slots": {"boke": "left", "tsukkomi": "right"},
            "initial": {"boke": "boke.neutral", "tsukkomi": "tsukkomi.neutral"},
            "events": [
                {"at_ms": 500, "role": "tsukkomi", "key": "tsukkomi.retort"},
                {"at_ms": 500, "role": "boke", "key": "boke.smile"},
            ],
        }
        self.cue_path = self.root / "cue.json"
        _write_json(self.cue_path, self.cue)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _package(
        package_id: str,
        variant_set: str,
        character_ref: str,
        keys: list[tuple[str, str]],
        *,
        intent: str = "evaluation",
        license_status: str = "approved",
    ) -> dict[str, object]:
        return {
            "id": package_id,
            "variant_set_ref": variant_set,
            "character_ref": character_ref,
            "license_status": license_status,
            "intent": intent,
            "items": [
                {
                    "paper_theater_key": key,
                    "variant_id": "variant-" + digit * 20,
                    "png_path": f"variants/v1/{key}.png",
                    "png_sha256": digit * 64,
                }
                for key, digit in keys
            ],
        }

    def _verify(self, path: Path, root: Path) -> dict[str, object]:
        self.assertEqual(root, self.packages.resolve())
        return {"ok": True, "package": copy.deepcopy(self.package_data[path.parent.name])}

    def _plan(self, cue: dict[str, object] | None = None, *, write: Path | None = None) -> dict[str, object]:
        if cue is not None:
            _write_json(self.cue_path, cue)
        with patch("ai_illustration.paper_theater.check_export_package", side_effect=self._verify):
            return plan_scene(self.cue_path, self.packages, write_path=write)

    def test_deterministic_plan_sorts_simultaneous_events_and_builds_segments(self) -> None:
        first = self._plan()
        cue = copy.deepcopy(self.cue)
        cue["events"].reverse()
        second = self._plan(cue)
        self.assertEqual(first["scene_plan"], second["scene_plan"])
        scene = first["scene_plan"]
        self.assertEqual([item["start_ms"] for item in scene["segments"]], [0, 500])
        self.assertEqual([item["end_ms"] for item in scene["segments"]], [500, 1000])
        self.assertEqual(scene["segments"][1]["boke"]["key"], "boke.smile")
        self.assertEqual(scene["segments"][1]["tsukkomi"]["key"], "tsukkomi.retort")
        self.assertEqual(scene["events"][0]["role"], "boke")

    def test_duplicate_role_time_fails_closed(self) -> None:
        cue = copy.deepcopy(self.cue)
        cue["events"].append({"at_ms": 500, "role": "boke", "key": "boke.neutral"})
        with self.assertRaisesRegex(PaperTheaterError, "DUPLICATE_ROLE_TIME"):
            self._plan(cue)

    def test_unknown_key_and_invalid_time_fail_closed(self) -> None:
        cue = copy.deepcopy(self.cue)
        cue["events"][0]["key"] = "tsukkomi.unknown"
        with self.assertRaisesRegex(PaperTheaterError, "UNKNOWN_EVENT_KEY"):
            self._plan(cue)
        cue = copy.deepcopy(self.cue)
        cue["events"][0]["at_ms"] = cue["duration_ms"]
        with self.assertRaisesRegex(PaperTheaterError, "EVENT_TIME"):
            self._plan(cue)

    def test_same_package_and_duplicate_slot_fail_closed(self) -> None:
        cue = copy.deepcopy(self.cue)
        cue["packages"]["tsukkomi"] = cue["packages"]["boke"]
        with self.assertRaisesRegex(PaperTheaterError, "PACKAGE_REUSE"):
            self._plan(cue)
        cue = copy.deepcopy(self.cue)
        cue["stage_slots"]["tsukkomi"] = "left"
        with self.assertRaisesRegex(PaperTheaterError, "DUPLICATE_STAGE_SLOT"):
            self._plan(cue)

    def test_intent_mismatch_and_unapproved_production_fail_closed(self) -> None:
        self.package_data[self.tsukkomi_id]["intent"] = "production"
        with self.assertRaisesRegex(PaperTheaterError, "INTENT_MISMATCH"):
            self._plan()
        self.package_data[self.boke_id]["intent"] = "production"
        self.package_data[self.boke_id]["license_status"] = "reviewing"
        with self.assertRaisesRegex(PaperTheaterError, "PRODUCTION_LICENSE_NOT_APPROVED"):
            self._plan()

    def test_traversal_and_symlink_escape_fail_closed(self) -> None:
        cue = copy.deepcopy(self.cue)
        cue["packages"]["boke"] = "../package-manifest.json"
        with self.assertRaisesRegex(PaperTheaterError, "UNSAFE_PATH"):
            self._plan(cue)
        link = self.packages / "escape"
        try:
            link.symlink_to(self.root)
        except (OSError, NotImplementedError):
            return
        cue = copy.deepcopy(self.cue)
        cue["packages"]["boke"] = "escape/cue.json"
        with self.assertRaisesRegex(PaperTheaterError, "PACKAGE_LOCATION|PATH_ESCAPE"):
            self._plan(cue)

    def test_explicit_write_is_canonical_idempotent_and_checkable(self) -> None:
        destination = self.root / "scene.json"
        first = self._plan(write=destination)
        self.assertTrue(first["written"])
        second = self._plan(write=destination)
        self.assertFalse(second["written"])
        with patch("ai_illustration.paper_theater.check_export_package", side_effect=self._verify):
            checked = check_scene_plan(destination, self.packages)
        self.assertTrue(checked["ok"])
        self.assertEqual(checked["segment_count"], 2)

    def test_write_conflict_noncanonical_and_stale_binding_fail_closed(self) -> None:
        destination = self.root / "scene.json"
        self._plan(write=destination)
        destination.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(PaperTheaterError, "WRITE_CONFLICT"):
            self._plan(write=destination)

        valid = self.root / "valid.json"
        self._plan(write=valid)
        parsed = json.loads(valid.read_text(encoding="utf-8"))
        valid.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(PaperTheaterError, "SCENE_CANONICAL"):
            check_scene_plan(valid, self.packages)

        stale = self.root / "stale.json"
        self._plan(write=stale)
        self.package_data[self.boke_id]["items"][0]["png_sha256"] = "f" * 64
        with patch("ai_illustration.paper_theater.check_export_package", side_effect=self._verify):
            with self.assertRaisesRegex(PaperTheaterError, "SCENE_BINDING_MISMATCH"):
                check_scene_plan(stale, self.packages)


if __name__ == "__main__":
    unittest.main()
