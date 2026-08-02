from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_illustration.variants import VariantError, plan_variant_set, validate_variant_set

SHA = "a" * 64


def _write(root: Path, name: str, data: dict[str, object]) -> None:
    (root / name).write_text(json.dumps(data), encoding="utf-8")


class VariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root, "character.json", {
            "kind": "character-spec", "schema_version": "1.0", "id": "boke",
            "version": "v001", "role": "boke", "review_status": "approved",
            "identity_anchors": ["fixture"], "license_status": "approved",
        })
        _write(self.root, "style.json", {
            "kind": "style-profile", "schema_version": "1.0", "id": "rough-flat",
            "version": "v001", "line": {}, "palette": {},
            "anti_ai_checks": ["fixture"], "license_status": "approved",
        })
        _write(self.root, "request.json", {
            "kind": "generation-request", "schema_version": "1.0", "id": "request-demo",
            "character_ref": "boke@v001", "style_ref": "rough-flat@v001",
            "pose": "neutral", "expression": "neutral", "crop": "full", "facing": "front",
            "tool_id": "fixture-tool", "model_id": "fixture-model",
            "license_status": "approved", "config": {}, "output_intent": "evaluation",
            "provenance": {"source": "fixture"},
        })
        _write(self.root, "candidate.json", {
            "kind": "candidate-asset", "schema_version": "1.0", "id": "candidate-demo",
            "request_ref": "request-demo", "path": "candidate.png", "sha256": SHA,
            "width": 1024, "height": 2048, "color_space": "sRGB", "has_alpha": True,
            "media_type": "image/png", "status": "technically_valid",
            "provenance": {"source": "fixture"},
        })
        _write(self.root, "review.json", {
            "kind": "review-decision", "schema_version": "1.0", "id": "review-demo",
            "candidate_ref": "candidate-demo", "candidate_request_ref": "request-demo",
            "candidate_sha256": SHA, "decision": "accept", "reviewer": "owner",
            "timestamp": "2026-08-02T00:00:00Z", "categories": [],
        })
        self.matrix = {
            "combinations": [
                {"expression": "smile", "pose": "talking", "facing": "front", "crop": "full"},
                {"expression": "neutral", "pose": "listening", "facing": "front", "crop": "full", "mouth_state": "closed"},
            ]
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_deterministic_and_shuffled_input(self) -> None:
        first = plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")
        shuffled = {"combinations": list(reversed(self.matrix["combinations"]))}
        second = plan_variant_set(self.root, "candidate-demo", shuffled, "evaluation")
        self.assertEqual(first, second)
        self.assertEqual(validate_variant_set(first, self.root), first)
        self.assertEqual(len(first["variants"]), 2)
        self.assertTrue(all(item["path"].endswith(".png") for item in first["variants"]))

    def test_duplicate_combination_fails_closed(self) -> None:
        item = self.matrix["combinations"][0]
        with self.assertRaisesRegex(VariantError, "DUPLICATE_COMBINATION"):
            plan_variant_set(self.root, "candidate-demo", {"combinations": [item, item]}, "evaluation")

    def test_stale_review_checksum_fails_closed(self) -> None:
        review = json.loads((self.root / "review.json").read_text(encoding="utf-8"))
        review["candidate_sha256"] = "b" * 64
        _write(self.root, "review.json", review)
        with self.assertRaisesRegex(VariantError, "STALE_REVIEW"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_evaluation_does_not_imply_commercial_approval(self) -> None:
        request = json.loads((self.root / "request.json").read_text(encoding="utf-8"))
        request["license_status"] = "reviewing"
        _write(self.root, "request.json", request)
        self.assertEqual(plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")["intent"], "evaluation")
        with self.assertRaisesRegex(VariantError, "PRODUCTION_LICENSE_NOT_APPROVED"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "production")

    def test_unsafe_values_and_latest_nonaccept_review_fail(self) -> None:
        unsafe = {"combinations": [{"expression": "../bad", "pose": "talking", "facing": "front", "crop": "full"}]}
        with self.assertRaisesRegex(VariantError, "INVALID_TOKEN"):
            plan_variant_set(self.root, "candidate-demo", unsafe, "evaluation")
        _write(self.root, "review-later.json", {
            "kind": "review-decision", "schema_version": "1.0", "id": "review-later",
            "candidate_ref": "candidate-demo", "candidate_request_ref": "request-demo",
            "candidate_sha256": SHA, "decision": "reject", "reviewer": "owner",
            "timestamp": "2026-08-03T00:00:00Z", "categories": [],
        })
        with self.assertRaisesRegex(VariantError, "ACCEPT_REVIEW_REQUIRED"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_output_has_no_remote_or_execution_fields(self) -> None:
        text = json.dumps(plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation"), sort_keys=True)
        for forbidden in ("http://", "https://", "credential", "secret", "execute", "subprocess"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
