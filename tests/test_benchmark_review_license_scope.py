from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_illustration import benchmark_review as rv
from ai_illustration.model_benchmark import canonical_sha256


class BenchmarkReviewLicenseScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "models").mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _profile(self, *, production: str = "approved", explicit: bool = True) -> dict[str, object]:
        profile: dict[str, object] = {
            "kind": "tool-profile",
            "schema_version": "1.0",
            "id": "selection-test-model",
            "version": "v001",
            "profile_type": "model-configuration",
            "adapter_type": "comfyui",
            "runtime_type": "pytorch",
            "offline_capability": "yes",
            "deterministic_seed_support": True,
            "control_capabilities": ["fixed-seed", "text-to-image"],
            "minimum_vram_gb": 8,
            "minimum_ram_gb": 16,
            "supported_operating_systems": ["windows"],
            "install_state": "uninstalled",
            "evidence_references": [
                {
                    "source_url": "https://example.invalid/model-license",
                    "retrieved_at": "2026-08-06",
                    "claim": "license scopes reviewed from primary evidence",
                }
            ],
            "license_evidence_state": "approved",
            "commercial_use_review_state": "approved",
            "decision_state": "approved",
        }
        if explicit:
            profile.update(
                {
                    "benchmark_use_review_state": "approved",
                    "production_model_use_review_state": production,
                    "commercial_output_use_review_state": "approved",
                }
            )
        return profile

    def _model(self, profile: dict[str, object]) -> dict[str, object]:
        path = self.root / "models" / "selection-test-model.json"
        path.write_text(json.dumps(profile), encoding="utf-8")
        return {
            "family": "selection-test",
            "profile_path": "models/selection-test-model.json",
            "profile_id": profile["id"],
            "profile_version": profile["version"],
            "profile_sha256": canonical_sha256(profile),
            "workflow_path": "workflows/selection-test.json",
            "workflow_sha256": "1" * 64,
        }

    def test_explicit_benchmark_only_profile_cannot_be_selected(self) -> None:
        model = self._model(self._profile(production="rejected"))
        diagnostics = rv.selected_model_production_diagnostics(
            model,
            workspace_root=self.root,
        )
        self.assertEqual(
            ["SELECTED_MODEL_NOT_PRODUCTION_ELIGIBLE"],
            [item["code"] for item in diagnostics],
        )
        self.assertIn("production-model-use-not-approved", diagnostics[0]["message"])

    def test_explicit_production_profile_can_be_selected(self) -> None:
        model = self._model(self._profile(production="approved"))
        self.assertEqual(
            [],
            rv.selected_model_production_diagnostics(
                model,
                workspace_root=self.root,
            ),
        )

    def test_legacy_profile_preserves_existing_selection_behavior(self) -> None:
        model = self._model(self._profile(explicit=False))
        self.assertEqual(
            [],
            rv.selected_model_production_diagnostics(
                model,
                workspace_root=self.root,
            ),
        )

    def test_profile_binding_and_path_fail_closed(self) -> None:
        model = self._model(self._profile())
        changed = dict(model)
        changed["profile_sha256"] = "0" * 64
        self.assertEqual(
            "SELECTED_MODEL_PROFILE_BINDING",
            rv.selected_model_production_diagnostics(
                changed,
                workspace_root=self.root,
            )[0]["code"],
        )

        missing = dict(model)
        missing["profile_path"] = "models/missing.json"
        self.assertEqual(
            "SELECTED_MODEL_PROFILE_READ",
            rv.selected_model_production_diagnostics(
                missing,
                workspace_root=self.root,
            )[0]["code"],
        )


if __name__ == "__main__":
    unittest.main()
