from __future__ import annotations

import json
from pathlib import Path
import unittest

from ai_illustration.quality import (
    CREATIVE_CANDIDATE,
    HARD_FAIL_CATEGORIES,
    TECHNICAL_CANDIDATE,
    TRANSPORT_SMOKE_OUTPUT,
    QualityGateError,
    packaged_quality_stage,
    require_creative_candidate,
)


class QualityContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = {
            "id": "candidate-one",
            "request_ref": "request-one",
            "status": "technically_valid",
            "quality_stage": TECHNICAL_CANDIDATE,
            "sha256": "a" * 64,
        }
        self.review = {
            "candidate_ref": "candidate-one",
            "candidate_request_ref": "request-one",
            "candidate_sha256": "a" * 64,
            "decision": "accept",
            "review_scope": "creative",
            "resulting_quality_stage": CREATIVE_CANDIDATE,
            "hard_fail_categories": [],
        }

    def assert_gate_code(self, code: str, candidate=None, review=None) -> None:
        with self.assertRaises(QualityGateError) as raised:
            require_creative_candidate(
                candidate or self.candidate,
                review or self.review,
                request_id="request-one",
                candidate_id="candidate-one",
            )
        self.assertEqual(raised.exception.code, code)

    def test_packaging_never_grants_creative_approval(self) -> None:
        self.assertEqual(
            packaged_quality_stage({"output_intent": "transport-smoke"}),
            TRANSPORT_SMOKE_OUTPUT,
        )
        self.assertEqual(packaged_quality_stage({}), TECHNICAL_CANDIDATE)
        self.assertNotEqual(packaged_quality_stage({}), CREATIVE_CANDIDATE)

    def test_missing_stage_and_smoke_output_fail_closed(self) -> None:
        self.assert_gate_code("CREATIVE_GATE_REQUIRED", {**self.candidate, "quality_stage": None})
        self.assert_gate_code(
            "SMOKE_OUTPUT_FORBIDDEN",
            {**self.candidate, "quality_stage": TRANSPORT_SMOKE_OUTPUT},
        )

    def test_technical_or_stale_review_is_rejected(self) -> None:
        self.assert_gate_code("CREATIVE_REVIEW_REQUIRED", review={**self.review, "review_scope": "technical"})
        self.assert_gate_code("STALE_REVIEW", review={**self.review, "candidate_ref": "candidate-two"})
        self.assert_gate_code("STALE_REVIEW", review={**self.review, "candidate_request_ref": "request-two"})
        self.assert_gate_code("STALE_REVIEW", review={**self.review, "candidate_sha256": "b" * 64})

    def test_hard_fail_categories_are_fail_closed(self) -> None:
        self.assert_gate_code(
            "CREATIVE_HARD_FAIL",
            review={**self.review, "hard_fail_categories": ["identity_drift"]},
        )
        self.assert_gate_code(
            "HARD_FAIL_CATEGORIES",
            review={**self.review, "hard_fail_categories": ["unknown_failure"]},
        )

    def test_explicit_clean_creative_approval_passes(self) -> None:
        require_creative_candidate(
            self.candidate,
            self.review,
            request_id="request-one",
            candidate_id="candidate-one",
        )

    def test_schema_vocabulary_and_legacy_readability(self) -> None:
        root = Path(__file__).resolve().parents[1]
        candidate_schema = json.loads((root / "schemas/candidate-asset.schema.json").read_text())
        review_schema = json.loads((root / "schemas/review-decision.schema.json").read_text())
        self.assertEqual(
            set(candidate_schema["properties"]["quality_stage"]["enum"]),
            {TRANSPORT_SMOKE_OUTPUT, TECHNICAL_CANDIDATE},
        )
        self.assertNotIn("quality_stage", candidate_schema["required"])
        quality_fields = {"review_scope", "resulting_quality_stage", "hard_fail_categories"}
        self.assertTrue(quality_fields <= set(review_schema["dependentRequired"]))
        self.assertNotIn("review_scope", review_schema["required"])
        self.assertEqual(
            set(review_schema["properties"]["hard_fail_categories"]["items"]["enum"]),
            set(HARD_FAIL_CATEGORIES),
        )


if __name__ == "__main__":
    unittest.main()
