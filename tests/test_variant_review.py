from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ai_illustration.quality import CREATIVE_CANDIDATE, TECHNICAL_CANDIDATE
from ai_illustration import variant_review as vr
from ai_illustration.variants import plan_variant_set


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _png(*, width: int = 1, height: int = 1, srgb: bool = True) -> bytes:
    payload = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    if srgb:
        payload += _chunk(b"sRGB", b"\x00")
    rows = b"".join(b"\x00" + bytes([20, 40, 60, 255]) * width for _ in range(height))
    return payload + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


class VariantReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifests = self.root / "manifests"
        self.manifests.mkdir()
        self.results = self.root / "results"
        self.results.mkdir()
        candidate_png = _png()
        candidate_sha = hashlib.sha256(candidate_png).hexdigest()
        (self.manifests / "candidate.png").write_bytes(candidate_png)
        _write(self.manifests / "character.json", {
            "kind": "character-spec", "schema_version": "1.0", "id": "boke",
            "version": "v001", "role": "boke", "review_status": "approved",
            "identity_anchors": ["fixture"], "license_status": "approved",
        })
        _write(self.manifests / "style.json", {
            "kind": "style-profile", "schema_version": "1.0", "id": "rough-flat",
            "version": "v001", "line": {}, "palette": {},
            "anti_ai_checks": ["fixture"], "license_status": "approved",
        })
        _write(self.manifests / "request.json", {
            "kind": "generation-request", "schema_version": "1.0", "id": "request-demo",
            "character_ref": "boke@v001", "style_ref": "rough-flat@v001",
            "pose": "neutral", "expression": "neutral", "crop": "full", "facing": "front",
            "tool_id": "fixture-tool", "model_id": "fixture-model",
            "license_status": "approved", "config": {}, "output_intent": "candidate",
            "provenance": {"source": "fixture"},
        })
        _write(self.manifests / "candidate.json", {
            "kind": "candidate-asset", "schema_version": "1.0", "id": "candidate-demo",
            "request_ref": "request-demo", "path": "candidate.png", "sha256": candidate_sha,
            "width": 1, "height": 1, "color_space": "sRGB", "has_alpha": True,
            "media_type": "image/png", "status": "technically_valid",
            "quality_stage": TECHNICAL_CANDIDATE, "provenance": {"source": "fixture"},
        })
        _write(self.manifests / "review.json", {
            "kind": "review-decision", "schema_version": "1.0", "id": "review-demo",
            "candidate_ref": "candidate-demo", "candidate_request_ref": "request-demo",
            "candidate_sha256": candidate_sha, "decision": "accept", "reviewer": "owner",
            "timestamp": "2026-08-07T07:00:00Z", "categories": [],
            "review_scope": "creative", "resulting_quality_stage": CREATIVE_CANDIDATE,
            "hard_fail_categories": [],
        })
        matrix = {"combinations": [{"expression": "smile", "pose": "talking", "facing": "front", "crop": "full"}]}
        self.variant_set = plan_variant_set(self.manifests, "candidate-demo", matrix, "evaluation")
        self.variant_set_path = self.root / "variant-set.json"
        _write(self.variant_set_path, self.variant_set)
        self.variant = self.variant_set["variants"][0]
        self.png = _png()
        self.png_sha = hashlib.sha256(self.png).hexdigest()
        target = self.results / self.variant["path"]
        target.parent.mkdir(parents=True)
        target.write_bytes(self.png)
        self.review = self._review_for(self.variant_set, decision="accept")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _review_for(self, variant_set: dict[str, object], *, decision: str) -> dict[str, object]:
        result_state = {
            ("evaluation", "accept"): "evaluation-accepted",
            ("production", "accept"): "production-variant-approved",
            ("evaluation", "reject"): "rejected",
            ("production", "reject"): "rejected",
            ("evaluation", "needs_revision"): "needs-revision",
            ("production", "needs_revision"): "needs-revision",
        }[(variant_set["intent"], decision)]
        review = {
            "kind": "variant-review-decision",
            "schema_version": "1.0",
            "id": "variant-review-" + "0" * 20,
            "variant_set_ref": variant_set["id"],
            "variant_set_sha256": vr.variant_set_sha256(variant_set),
            "variant_id": variant_set["variants"][0]["id"],
            "png_sha256": self.png_sha,
            "source_candidate_ref": variant_set["source_candidate_ref"],
            "source_request_ref": variant_set["source_request_ref"],
            "source_candidate_sha256": variant_set["source_candidate_sha256"],
            "identity_gate": variant_set["identity_gate"],
            "identity_review_ref": variant_set["identity_review_ref"],
            "identity_review_sha256": variant_set["identity_review_sha256"],
            "identity_strategy_id": variant_set["identity_strategy_id"],
            "identity_evidence_run_ids": list(variant_set["identity_evidence_run_ids"]),
            "identity_model": copy.deepcopy(variant_set["identity_model"]),
            "decision": decision,
            "result_state": result_state,
            "reviewer": "owner",
            "timestamp": "2026-08-07T07:30:00Z",
            "hard_fail_categories": [],
            "observations": ["exact pose and identity are readable"],
        }
        review["id"] = vr.expected_review_id(review)
        return review

    def _production_variant_set(self) -> dict[str, object]:
        value = copy.deepcopy(self.variant_set)
        value["intent"] = "production"
        value["identity_gate"] = "owner-approved"
        value["identity_review_ref"] = "identity-review-" + "a" * 16
        value["identity_review_sha256"] = "b" * 64
        value["identity_strategy_id"] = "reference-baseline"
        value["identity_evidence_run_ids"] = ["identity-run-a", "identity-run-b"]
        value["identity_model"] = {
            "family": "fixture-family",
            "profile_ref": "fixture-model@v001",
            "profile_sha256": "c" * 64,
            "workflow_sha256": "d" * 64,
        }
        value["id"] = "variant-set-" + "e" * 20
        return value

    def test_valid_evaluation_accept_is_exact_and_nonproduction(self) -> None:
        diagnostics, variant = vr.validate_review(self.review, self.variant_set_path, self.manifests, self.results)
        self.assertEqual(diagnostics, [])
        self.assertEqual(variant["id"], self.variant["id"])
        self.assertEqual(self.review["result_state"], "evaluation-accepted")
        self.assertIsNone(self.review["identity_review_ref"])

    def test_review_id_is_deterministic_and_notes_are_nonsemantic(self) -> None:
        first = self.review["id"]
        changed = copy.deepcopy(self.review)
        changed["notes"] = "free-form owner note"
        self.assertEqual(vr.expected_review_id(changed), first)
        changed["observations"].append("second observation")
        self.assertNotEqual(vr.expected_review_id(changed), first)

    def test_exact_variant_set_and_source_bindings_are_required(self) -> None:
        for field, value in (
            ("variant_set_ref", "variant-set-" + "f" * 20),
            ("variant_set_sha256", "f" * 64),
            ("source_candidate_ref", "candidate-other"),
            ("source_request_ref", "request-other"),
            ("source_candidate_sha256", "f" * 64),
        ):
            with self.subTest(field=field):
                review = copy.deepcopy(self.review)
                review[field] = value
                review["id"] = vr.expected_review_id(review)
                self.assertIn("REVIEW_BINDING", {item["code"] for item in vr.validate_review(review, self.variant_set_path, self.manifests, self.results)[0]})

    def test_live_png_checksum_dimensions_srgb_and_structure_are_verified(self) -> None:
        target = self.results / self.variant["path"]
        target.write_bytes(_png(width=2))
        review = copy.deepcopy(self.review)
        review["png_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        review["id"] = vr.expected_review_id(review)
        self.assertIn("PNG_DIMENSIONS", {item["code"] for item in vr.validate_review(review, self.variant_set_path, self.manifests, self.results)[0]})

        target.write_bytes(_png(srgb=False))
        review["png_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        review["id"] = vr.expected_review_id(review)
        codes = {item["code"] for item in vr.validate_review(review, self.variant_set_path, self.manifests, self.results)[0]}
        self.assertTrue({"PNG_STRUCTURE", "PNG_SRGB"} & codes)

        target.write_bytes(b"not-a-png")
        review["png_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        review["id"] = vr.expected_review_id(review)
        self.assertIn("PNG_STRUCTURE", {item["code"] for item in vr.validate_review(review, self.variant_set_path, self.manifests, self.results)[0]})

    def test_missing_and_symlinked_live_png_fail_closed(self) -> None:
        target = self.results / self.variant["path"]
        target.unlink()
        self.assertIn("PNG_MISSING", {item["code"] for item in vr.validate_review(self.review, self.variant_set_path, self.manifests, self.results)[0]})
        outside = self.root / "outside.png"
        outside.write_bytes(self.png)
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertIn("PNG_SYMLINK", {item["code"] for item in vr.validate_review(self.review, self.variant_set_path, self.manifests, self.results)[0]})

    def test_tampered_png_checksum_and_trailing_data_fail(self) -> None:
        target = self.results / self.variant["path"]
        target.write_bytes(self.png + b"trailing")
        review = copy.deepcopy(self.review)
        review["png_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        review["id"] = vr.expected_review_id(review)
        self.assertIn("PNG_STRUCTURE", {item["code"] for item in vr.validate_review(review, self.variant_set_path, self.manifests, self.results)[0]})

        target.write_bytes(self.png)
        wrong = copy.deepcopy(self.review)
        wrong["png_sha256"] = "f" * 64
        wrong["id"] = vr.expected_review_id(wrong)
        self.assertIn("PNG_CHECKSUM", {item["code"] for item in vr.validate_review(wrong, self.variant_set_path, self.manifests, self.results)[0]})

    def test_production_review_requires_identity_evidence_before_variant_validation(self) -> None:
        production = self._production_variant_set()
        path = self.root / "production-set.json"
        _write(path, production)
        review = self._review_for(production, decision="accept")
        diagnostics, _ = vr.validate_review(review, path, self.manifests, self.results)
        self.assertIn("VARIANT_SET_IDENTITY_LOCK_REQUIRED", {item["code"] for item in diagnostics})

    def test_production_accept_requires_clean_hard_fail_list_and_exact_identity_projection(self) -> None:
        production = self._production_variant_set()
        review = self._review_for(production, decision="accept")
        with patch("ai_illustration.variant_review.check_variant_set", return_value=production):
            diagnostics, _ = vr.validate_review(review, self.variant_set_path, self.manifests, self.results)
            self.assertEqual(diagnostics, [])
            hard = copy.deepcopy(review)
            hard["hard_fail_categories"] = ["identity_drift"]
            hard["id"] = vr.expected_review_id(hard)
            self.assertIn("PRODUCTION_HARD_FAIL", {item["code"] for item in vr.validate_review(hard, self.variant_set_path, self.manifests, self.results)[0]})
            stale = copy.deepcopy(review)
            stale["identity_strategy_id"] = "other-strategy"
            stale["id"] = vr.expected_review_id(stale)
            self.assertIn("REVIEW_BINDING", {item["code"] for item in vr.validate_review(stale, self.variant_set_path, self.manifests, self.results)[0]})

    def test_evaluation_accept_cannot_claim_production_result_state(self) -> None:
        review = copy.deepcopy(self.review)
        review["result_state"] = "production-variant-approved"
        review["id"] = vr.expected_review_id(review)
        self.assertIn("RESULT_STATE", {item["code"] for item in vr.validate_review_document(review)})

    def test_reject_and_needs_revision_are_nonapproved_states(self) -> None:
        for decision, result_state in (("reject", "rejected"), ("needs_revision", "needs-revision")):
            with self.subTest(decision=decision):
                review = self._review_for(self.variant_set, decision=decision)
                review["hard_fail_categories"] = ["identity_drift"]
                review["id"] = vr.expected_review_id(review)
                diagnostics, _ = vr.validate_review(review, self.variant_set_path, self.manifests, self.results)
                self.assertEqual(diagnostics, [])
                self.assertEqual(review["result_state"], result_state)

    def test_forbidden_automatic_decision_fields_are_rejected_recursively(self) -> None:
        for key in ("score", "rank", "winner", "similarity_threshold", "automatic_approval", "variant_promotion"):
            with self.subTest(key=key):
                review = copy.deepcopy(self.review)
                review[key] = 1
                review["id"] = vr.expected_review_id(review)
                self.assertIn("AUTOMATIC_DECISION_FORBIDDEN", {item["code"] for item in vr.validate_review_document(review)})

    def test_cli_is_deterministic_and_read_only(self) -> None:
        review_path = self.root / "review.json"
        _write(review_path, self.review)
        before = {path: path.read_bytes() for path in (review_path, self.variant_set_path)}
        args = ["review-check", str(review_path), str(self.variant_set_path), "--manifest-root", str(self.manifests), "--result-root", str(self.results)]
        first = io.StringIO()
        with redirect_stdout(first):
            self.assertEqual(vr.main(args), 0)
        second = io.StringIO()
        with redirect_stdout(second):
            self.assertEqual(vr.main(args), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        self.assertTrue(json.loads(first.getvalue())["ok"])
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload)

    def test_schema_json_mirrors_contract(self) -> None:
        path = Path(__file__).resolve().parents[1] / "schemas" / "variant-review-decision.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["kind"]["const"], "variant-review-decision")
        text = json.dumps(schema, sort_keys=True)
        self.assertIn("production-variant-approved", text)
        self.assertIn("evaluation-accepted", text)
        self.assertIn("identity_review_sha256", text)
        self.assertIn("identity_drift", text)


if __name__ == "__main__":
    unittest.main()
