from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_illustration.catalog import (
    catalog_listing,
    evaluate_compatibility,
    load_catalog,
    validate_hardware_profile,
    validate_tool_profile,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "catalog"


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool_path = FIXTURES / "tool-profile.json"
        self.hardware_path = FIXTURES / "hardware-profile.json"
        self.tool = json.loads(self.tool_path.read_text(encoding="utf-8"))
        self.hardware = json.loads(self.hardware_path.read_text(encoding="utf-8"))

    def test_valid_profiles(self) -> None:
        self.assertEqual([], validate_tool_profile(self.tool_path))
        self.assertEqual([], validate_hardware_profile(self.hardware_path))

    def test_catalog_listing_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "z.json").write_text(json.dumps({**self.tool, "id": "z-tool"}), encoding="utf-8")
            (root / "a.json").write_text(json.dumps({**self.tool, "id": "a-tool"}), encoding="utf-8")
            profiles, diagnostics = load_catalog(root)
            self.assertEqual([], diagnostics)
            first = json.dumps(catalog_listing(profiles), sort_keys=True, separators=(",", ":"))
            second = json.dumps(catalog_listing(reversed(profiles)), sort_keys=True, separators=(",", ":"))
            self.assertEqual(first, second)
            self.assertEqual(["a-tool", "z-tool"], [item["id"] for item in catalog_listing(profiles)])

    def test_compatible_by_declaration_does_not_approve_license(self) -> None:
        tool = {**self.tool, "license_evidence_state": "approved", "commercial_use_review_state": "approved"}
        result = evaluate_compatibility(tool, self.hardware)
        self.assertEqual("compatible-by-declaration", result.status)
        self.assertEqual("false", result.to_dict()["licensing"]["compatibility_implies_license_approval"])

    def test_hard_incompatible(self) -> None:
        hardware = {**self.hardware, "vram_gb": 2}
        result = evaluate_compatibility(self.tool, hardware)
        self.assertEqual("hard-incompatible", result.status)
        self.assertIn("vram", result.hard_incompatibilities)

    def test_missing_evidence(self) -> None:
        tool = {**self.tool, "evidence_references": []}
        result = evaluate_compatibility(tool, self.hardware)
        self.assertEqual("missing-evidence", result.status)
        self.assertIn("evidence", result.missing_evidence)

    def test_approval_requires_license_and_commercial_review(self) -> None:
        tool = {**self.tool, "decision_state": "approved"}
        diagnostics = validate_tool_profile(self.tool_path, tool)
        codes = {item.code for item in diagnostics}
        self.assertIn("APPROVAL_WITHOUT_LICENSE", codes)
        self.assertIn("APPROVAL_WITHOUT_COMMERCIAL_REVIEW", codes)

    def test_rejects_contradictory_or_unknown_fields(self) -> None:
        tool = copy.deepcopy(self.tool)
        tool["unexpected"] = True
        tool["supported_operating_systems"] = ["plan9"]
        diagnostics = validate_tool_profile(self.tool_path, tool)
        codes = {item.code for item in diagnostics}
        self.assertIn("UNKNOWN_FIELD", codes)
        self.assertIn("INVALID_ENUM", codes)


if __name__ == "__main__":
    unittest.main()
