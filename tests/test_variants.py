from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from ai_illustration import identity_lock as il
from ai_illustration import identity_lock_results as ir
from ai_illustration import identity_lock_review as rv
from ai_illustration.quality import (
    CREATIVE_CANDIDATE,
    TECHNICAL_CANDIDATE,
    TRANSPORT_SMOKE_OUTPUT,
)
from ai_illustration.variants import (
    IdentityEvidence,
    VariantError,
    check_variant_set,
    plan_variant_set,
    validate_variant_set,
)


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
        self.identity_tmp = tempfile.TemporaryDirectory()
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
        self.identity_evidence = self._build_identity_evidence()

    def tearDown(self) -> None:
        self.identity_tmp.cleanup()
        self.tmp.cleanup()

    def read_json(self, name: str) -> dict[str, object]:
        return json.loads((self.root / name).read_text(encoding="utf-8"))

    def _build_identity_evidence(self) -> IdentityEvidence:
        identity_root = Path(self.identity_tmp.name)
        result_root = identity_root / "results"
        result_root.mkdir(parents=True)
        poses = ["front-neutral", "three-quarter", "seated-asymmetric"]
        plan = {
            "kind": "identity-lock-plan",
            "schema_version": "1.0",
            "id": "variant-source-identity-lock",
            "version": "v001",
            "status": "prepared",
            "selected_model": {
                "family": "fixture-family",
                "profile_ref": "fixture-model@v001",
                "profile_sha256": "a" * 64,
                "workflow_sha256": "b" * 64,
                "benchmark_review_ref": "benchmark-review-fixture",
                "benchmark_review_sha256": "c" * 64,
                "production_eligible": True,
            },
            "roles": [
                {
                    "role": "boke",
                    "candidate_id": "candidate-demo",
                    "request_id": "request-demo",
                    "image_sha256": self.sha,
                    "reference_path": "identities/boke.png",
                },
                {
                    "role": "tsukkomi",
                    "candidate_id": "candidate-tsukkomi",
                    "request_id": "request-tsukkomi",
                    "image_sha256": "d" * 64,
                    "reference_path": "identities/tsukkomi.png",
                },
            ],
            "pose_targets": poses,
            "expression_targets": ["neutral", "smile", "surprised"],
            "strategies": [
                {"id": "reference-baseline", "type": "reference-only"},
                {
                    "id": "reference-openpose",
                    "type": "reference-plus-pose",
                    "control_method": "openpose",
                    "control_assets": [
                        {"pose": pose, "path": f"controls/{pose}.png", "sha256": "e" * 64}
                        for pose in poses
                    ],
                },
            ],
        }
        payload = _png_bytes()
        payload_sha = hashlib.sha256(payload).hexdigest()
        result_entries: list[dict[str, object]] = []
        for row in il.expand_matrix(plan):
            common = {key: row[key] for key in (
                "run_id", "model_family", "model_profile_ref", "model_profile_sha256",
                "workflow_sha256", "role", "candidate_id", "request_id", "identity_sha256",
                "strategy_id", "strategy_type", "pose", "expression", "control_sha256",
            )}
            if row["strategy_id"] == "reference-baseline":
                image = result_root / row["output_path"]
                image.parent.mkdir(parents=True, exist_ok=True)
                image.write_bytes(payload)
                result_entries.append({
                    **common,
                    "state": "succeeded",
                    "elapsed_ms": 100,
                    "image_path": row["output_path"],
                    "image_sha256": payload_sha,
                    "width": 1,
                    "height": 1,
                })
            else:
                result_entries.append({
                    **common,
                    "state": "failed",
                    "elapsed_ms": 100,
                    "error": {"code": "fixture-failure", "message": "fixture control failure"},
                })
        results = {
            "kind": "identity-lock-results",
            "schema_version": "1.0",
            "id": "variant-identity-results",
            "version": "v001",
            "plan_ref": plan["id"],
            "plan_version": plan["version"],
            "plan_sha256": il.plan_sha256(plan),
            "results": result_entries,
        }
        diagnostics, images = ir.validate_results(results, plan, result_root)
        self.assertEqual(diagnostics, [])
        manifest, files = ir.build_sheet_package(results, plan, images)
        package_root = identity_root / "package"
        ir.publish_package(package_root, files)
        role_map = {item["role"]: item for item in plan["roles"]}
        model = plan["selected_model"]
        matrix = il.expand_matrix(plan)
        selections = []
        for role in ("boke", "tsukkomi"):
            identity = role_map[role]
            selections.append({
                "role": role,
                "strategy_id": "reference-baseline",
                "candidate_id": identity["candidate_id"],
                "request_id": identity["request_id"],
                "identity_sha256": identity["image_sha256"],
                "model_family": model["family"],
                "model_profile_ref": model["profile_ref"],
                "model_profile_sha256": model["profile_sha256"],
                "workflow_sha256": model["workflow_sha256"],
                "accepted_run_ids": sorted(
                    row["run_id"] for row in matrix
                    if row["role"] == role and row["strategy_id"] == "reference-baseline"
                ),
            })
        review = {
            "kind": "identity-lock-review",
            "schema_version": "1.0",
            "id": "identity-review-0000000000000000",
            "plan_ref": plan["id"],
            "plan_version": plan["version"],
            "plan_sha256": il.plan_sha256(plan),
            "results_ref": results["id"],
            "results_version": results["version"],
            "results_sha256": ir.results_sha256(results),
            "package_ref": manifest["id"],
            "package_sha256": hashlib.sha256(ir.document_bytes(manifest)).hexdigest(),
            "reviewer": "owner",
            "timestamp": "2026-08-07T07:00:00Z",
            "decision": "approve_identity_lock",
            "role_selections": selections,
            "rejected_evidence": [],
            "observations": ["fixture identities preserve the full reference grid"],
        }
        review["id"] = rv.expected_review_id(review)
        plan_path = identity_root / "plan.json"
        results_path = identity_root / "results.json"
        review_path = identity_root / "review.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        results_path.write_text(json.dumps(results), encoding="utf-8")
        review_path.write_text(json.dumps(review), encoding="utf-8")
        return IdentityEvidence(
            review=review_path,
            plan=plan_path,
            results=results_path,
            result_root=result_root,
            package_root=package_root,
        )

    def test_deterministic_and_shuffled_input(self) -> None:
        first = plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")
        shuffled = {"combinations": list(reversed(self.matrix["combinations"]))}
        second = plan_variant_set(self.root, "candidate-demo", shuffled, "evaluation")
        expected_path = Path(__file__).parent / "fixtures" / "variants" / "variant-set.json"
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        self.assertEqual(first, second)
        self.assertEqual(first, expected)
        self.assertEqual(validate_variant_set(first, self.root), first)
        self.assertEqual(first["identity_gate"], "evaluation-unlocked")
        self.assertIsNone(first["identity_review_ref"])
        self.assertEqual(first["identity_evidence_run_ids"], [])
        self.assertIsNone(first["identity_model"])
        self.assertEqual(len(first["variants"]), 2)
        self.assertTrue(all(item["path"].endswith(".png") for item in first["variants"]))

    def test_production_requires_exact_owner_identity_lock(self) -> None:
        with self.assertRaisesRegex(VariantError, "IDENTITY_LOCK_REQUIRED"):
            plan_variant_set(self.root, "candidate-demo", self.matrix, "production")
        production = plan_variant_set(
            self.root,
            "candidate-demo",
            self.matrix,
            "production",
            identity_evidence=self.identity_evidence,
        )
        evaluation = plan_variant_set(self.root, "candidate-demo", self.matrix, "evaluation")
        self.assertEqual(production["identity_gate"], "owner-approved")
        self.assertEqual(production["identity_strategy_id"], "reference-baseline")
        self.assertEqual(len(production["identity_evidence_run_ids"]), 9)
        self.assertEqual(production["identity_model"]["profile_ref"], "fixture-model@v001")
        self.assertNotEqual(production["id"], evaluation["id"])
        self.assertNotEqual(
            {item["id"] for item in production["variants"]},
            {item["id"] for item in evaluation["variants"]},
        )

    def test_production_check_revalidates_same_identity_evidence(self) -> None:
        production = plan_variant_set(
            self.root,
            "candidate-demo",
            self.matrix,
            "production",
            identity_evidence=self.identity_evidence,
        )
        path = self.root / "production-variant-set.json"
        _write(self.root, path.name, production)
        with self.assertRaisesRegex(VariantError, "IDENTITY_LOCK_REQUIRED"):
            check_variant_set(path, self.root)
        self.assertEqual(
            check_variant_set(path, self.root, identity_evidence=self.identity_evidence),
            production,
        )

    def test_evaluation_rejects_production_identity_evidence(self) -> None:
        with self.assertRaisesRegex(VariantError, "IDENTITY_EVIDENCE_NOT_ALLOWED"):
            plan_variant_set(
                self.root,
                "candidate-demo",
                self.matrix,
                "evaluation",
                identity_evidence=self.identity_evidence,
            )

    def test_tampered_identity_package_closes_production_gate(self) -> None:
        svg = next(self.identity_evidence.package_root.rglob("*.svg"))
        svg.write_bytes(svg.read_bytes() + b"tampered")
        with self.assertRaisesRegex(VariantError, "IDENTITY_LOCK_PACKAGE_BYTES"):
            plan_variant_set(
                self.root,
                "candidate-demo",
                self.matrix,
                "production",
                identity_evidence=self.identity_evidence,
            )

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
            plan_variant_set(
                self.root,
                "candidate-demo",
                self.matrix,
                "production",
                identity_evidence=self.identity_evidence,
            )

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
