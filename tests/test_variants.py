from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from ai_illustration.quality import (
    CREATIVE_CANDIDATE,
    TECHNICAL_CANDIDATE,
    TRANSPORT_SMOKE_OUTPUT,
)
from ai_illustration.variants import VariantError, plan_variant_set, validate_variant_set


def _write(root: Path, name: str, data: dict[str, object]) -> None:
    (root / name).write_text(json.dumps(data), encoding="utf-8")


def _png_bytes() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
        + chunk(b"sRGB", b"\x00")
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


class VariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        payload = _png_bytes()
        (self.root / "candidate.png").write_bytes(payload)
        self.sha = hashlib.sha256(payload).hexdigest()
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
            "license_status": "approved", "config": {}, "output_intent": "candidate",
            "provenance": {"source": "fixture"},
        })
        _write(self.root, "candidate.json", {
            "kind": "candidate-asset", "schema_version": "1.0", "id": "candidate-demo",
            "request_ref": "request-demo", "path": "candidate.png", "sha256": self.sha,
            "width": 1, "height": 1, "color_space": "sRGB", "has_alpha": True,
            "media_type": "image/png", "status": "technically_valid",
            "quality_stage": TECHNICAL_CANDIDATE,
            "provenance": {"source": "fixture"},
        })
        _write(self.root, "review.json", {
            "kind": "review-decision", "schema_version": "1.0", "id": "review-demo",
            "candidate_ref": "candidate-demo", "candidate_request_ref": "request-demo",
            "candidate_sha256": self.sha, "decision": "accept", "reviewer": "owner",
            "timestamp": "2026-08-02T00:00:00Z", "categories": [],
            "review_scope": "creative", "resulting_quality_stage": CREATIVE_CANDIDATE,
            "hard_fail_categories": [],
        })
        self.matrix = {
            "combinations": [
                {"expression": "smile", "pose": "talking", "facing": "front", "crop": "full"},
                {"expression": "neutral", "pose": "listening", "facing": "front", "crop": "full", "mouth_state": "closed"},
            ]
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def read_json(self, name: str) -> dict[str, object]:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def test_deterministic_and_shuffled_input(self) -> None:
        first = plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")
        shuffled = {"combinations": list(reversed(self.matrix["combinations"]))}
        second = plan_variant_set(self.root, "candidate-demo", shuffled, "evaluation")
        expected_path = Path(__file__).parent / "fixtures" / "variants" / "variant-set.json"
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertEqual(first, second)
        self.assertEqual(first, expected)
        self.assertEqual(validate_variant_set(first, self.root), first)
        self.assertEqual(len(first["variants"]), 2)
        self.assertTrue(all(item["path"].endswith(".png") for item in first["variants"]))

    def test_duplicate_combination_fails_closed(self) -> None:
        item = self.matrix["combinations"][0]
        with self.assertRaisesRegex(VariantError, "DUPLICATE_COMBINATION"):
            plan_variant_set(self.root, "candidate-demo", {"combinations": [item, item]}, "evaluation")

    def test_stale_review_checksum_fails_closed(self) -> None:
        review = self.read_json("review.json")
        review["candidate_sha256"] = "b" * 64
        _write(self.root, "review.json", review)
        with self.assertRaisesRegex(VariantError, "STALE_REVIEW"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_source_bytes_are_required_and_verified(self) -> None:
        (self.root / "candidate.png").unlink()
        with self.assertRaisesRegex(VariantError, "ASSET_MISSING"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")
        (self.root / "candidate.png").write_bytes(b"tampered")
        with self.assertRaisesRegex(VariantError, "ASSET_CHECKSUM_MISMATCH"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_evaluation_does_not_imply_commercial_approval(self) -> None:
        request = self.read_json("request.json")
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
            "candidate_sha256": self.sha, "decision": "reject", "reviewer": "owner",
            "timestamp": "2026-08-03T00:00:00Z", "categories": [],
            "review_scope": "creative", "resulting_quality_stage": TECHNICAL_CANDIDATE,
            "hard_fail_categories": ["identity_drift"],
        })
        with self.assertRaisesRegex(VariantError, "ACCEPT_REVIEW_REQUIRED"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_legacy_accept_cannot_unlock_variant_planning(self) -> None:
        review = self.read_json("review.json")
        for field in ("review_scope", "resulting_quality_stage", "hard_fail_categories"):
            review.pop(field)
        _write(self.root, "review.json", review)
        with self.assertRaisesRegex(VariantError, "CREATIVE_REVIEW_REQUIRED"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_smoke_and_missing_candidate_stage_cannot_unlock_variants(self) -> None:
        candidate = self.read_json("candidate.json")
        candidate["quality_stage"] = TRANSPORT_SMOKE_OUTPUT
        _write(self.root, "candidate.json", candidate)
        with self.assertRaisesRegex(VariantError, "SMOKE_OUTPUT_FORBIDDEN"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

        candidate.pop("quality_stage")
        _write(self.root, "candidate.json", candidate)
        with self.assertRaisesRegex(VariantError, "CREATIVE_GATE_REQUIRED"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_technical_accept_cannot_unlock_variant_planning(self) -> None:
        review = self.read_json("review.json")
        review["review_scope"] = "technical"
        review["resulting_quality_stage"] = TECHNICAL_CANDIDATE
        _write(self.root, "review.json", review)
        with self.assertRaisesRegex(VariantError, "CREATIVE_REVIEW_REQUIRED"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_creative_accept_with_hard_fail_is_rejected_before_planning(self) -> None:
        review = self.read_json("review.json")
        review["hard_fail_categories"] = ["identity_drift"]
        _write(self.root, "review.json", review)
        with self.assertRaisesRegex(VariantError, "CREATIVE_HARD_FAIL"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")

    def test_output_has_no_remote_or_execution_fields(self) -> None:
        text = json.dumps(plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation"), sort_keys=True)
        for forbidden in ("http://", "https://", "credential", "secret", "execute", "subprocess"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
