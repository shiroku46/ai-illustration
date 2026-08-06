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
    evaluate_model_license_eligibility,
    load_catalog,
    validate_hardware_profile,
    validate_tool_profile,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "catalog"


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool_path = FIXTURES / "tool-profile.json"
        self.hardware_path = FIXTURES / "hardware-profile.json"
        self.tool = json.loads(self.tool_path.read_text(encoding="utf-8"))
        self.hardware = json.loads(self.hardware_path.read_text(encoding="utf-8"))

    def _model_profile(self, **overrides: object) -> dict[str, object]:
        profile: dict[str, object] = {
            **self.tool,
            "id": "test-model",
            "profile_type": "model-configuration",
            "license_evidence_state": "approved",
            "commercial_use_review_state": "approved",
            "decision_state": "approved",
            "benchmark_use_review_state": "approved",
            "production_model_use_review_state": "rejected",
            "commercial_output_use_review_state": "approved",
        }
        profile.update(overrides)
        return profile

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

    def test_legacy_model_profile_remains_valid_and_eligible(self) -> None:
        model = {
            **self.tool,
            "id": "legacy-model",
            "profile_type": "model-configuration",
            "license_evidence_state": "approved",
            "commercial_use_review_state": "approved",
            "decision_state": "approved",
        }
        self.assertEqual([], validate_tool_profile(self.tool_path, model))
        eligibility = evaluate_model_license_eligibility(model)
        self.assertTrue(eligibility.benchmark_eligible)
        self.assertTrue(eligibility.production_eligible)
        self.assertTrue(eligibility.commercial_output_eligible)
        self.assertEqual("legacy", eligibility.states["scope_contract"])
        self.assertEqual("compatible-by-declaration", evaluate_compatibility(model, self.hardware).status)

    def test_partial_model_scope_is_rejected(self) -> None:
        model = {
            **self.tool,
            "profile_type": "model-configuration",
            "benchmark_use_review_state": "approved",
        }
        diagnostics = validate_tool_profile(self.tool_path, model)
        missing = {item.field for item in diagnostics if item.code == "MISSING_FIELD"}
        self.assertEqual(
            {
                "production_model_use_review_state",
                "commercial_output_use_review_state",
            },
            missing,
        )
        eligibility = evaluate_model_license_eligibility(model)
        self.assertFalse(eligibility.benchmark_eligible)
        self.assertIn("license-scope-contract-incomplete", eligibility.denial_reasons)

    def test_benchmark_only_model_is_not_production_eligible(self) -> None:
        model = self._model_profile()
        self.assertEqual([], validate_tool_profile(self.tool_path, model))
        eligibility = evaluate_model_license_eligibility(model)
        self.assertTrue(eligibility.benchmark_eligible)
        self.assertFalse(eligibility.production_eligible)
        self.assertTrue(eligibility.commercial_output_eligible)
        self.assertIn("production-model-use-not-approved", eligibility.denial_reasons)
        self.assertEqual("explicit", eligibility.states["scope_contract"])
        self.assertEqual("compatible-by-declaration", evaluate_compatibility(model, self.hardware).status)

    def test_fully_approved_model_is_production_eligible(self) -> None:
        model = self._model_profile(production_model_use_review_state="approved")
        eligibility = evaluate_model_license_eligibility(model)
        self.assertTrue(eligibility.benchmark_eligible)
        self.assertTrue(eligibility.production_eligible)
        self.assertTrue(eligibility.commercial_output_eligible)
        self.assertEqual((), eligibility.denial_reasons)

    def test_ambiguous_scope_fails_closed(self) -> None:
        model = self._model_profile(benchmark_use_review_state="reviewing")
        diagnostics = validate_tool_profile(self.tool_path, model)
        self.assertTrue(any(item.code == "APPROVAL_WITHOUT_BENCHMARK_USE" for item in diagnostics))
        eligibility = evaluate_model_license_eligibility(model)
        self.assertFalse(eligibility.benchmark_eligible)
        self.assertFalse(eligibility.production_eligible)
        self.assertIn("benchmark-use-not-approved", eligibility.denial_reasons)
        compatibility = evaluate_compatibility(model, self.hardware)
        self.assertEqual("missing-evidence", compatibility.status)

    def test_published_schema_enforces_approval_prerequisites(self) -> None:
        schema = json.loads((ROOT / "schemas" / "tool-profile.schema.json").read_text(encoding="utf-8"))
        conditional = schema["allOf"][0]
        self.assertEqual("approved", conditional["if"]["properties"]["decision_state"]["const"])
        then_properties = conditional["then"]["properties"]
        self.assertEqual("approved", then_properties["license_evidence_state"]["const"])
        self.assertEqual("approved", then_properties["commercial_use_review_state"]["const"])
        self.assertEqual(1, then_properties["evidence_references"]["minItems"])

        model_required = set(schema["allOf"][1]["then"]["required"])
        self.assertEqual(
            {
                "benchmark_use_review_state",
                "production_model_use_review_state",
                "commercial_output_use_review_state",
            },
            model_required,
        )
        approved_model = schema["allOf"][2]["then"]["properties"]
        self.assertEqual("approved", approved_model["benchmark_use_review_state"]["const"])
        self.assertEqual(
            {"approved", "rejected", "not-applicable"},
            set(approved_model["production_model_use_review_state"]["enum"]),
        )
        self.assertEqual(
            {"approved", "not-applicable"},
            set(approved_model["commercial_output_use_review_state"]["enum"]),
        )
        approved_required = set(schema["allOf"][2]["if"]["required"])
        self.assertTrue(model_required <= approved_required)

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
