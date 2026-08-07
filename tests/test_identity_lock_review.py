from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zlib

from ai_illustration import identity_lock as il
from ai_illustration import identity_lock_results as ir
from ai_illustration import identity_lock_review as rv


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + (zlib.crc32(kind + data) & 0xFFFFFFFF).to_bytes(4, "big")


def png() -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = (2).to_bytes(4, "big") + (2).to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    rows = b"".join(b"\x00" + bytes([20, 40, 80, 255]) * 2 for _ in range(2))
    return signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")


def valid_plan() -> dict[str, object]:
    poses = ["front-neutral", "three-quarter", "seated-asymmetric"]
    return {
        "kind": "identity-lock-plan",
        "schema_version": "1.0",
        "id": "manzai-duo-identity-lock",
        "version": "v001",
        "status": "prepared",
        "selected_model": {
            "family": "animagine-xl",
            "profile_ref": "animagine-xl-4-0-opt@v001",
            "profile_sha256": SHA_A,
            "workflow_sha256": SHA_B,
            "benchmark_review_ref": "benchmark-review-demo",
            "benchmark_review_sha256": SHA_C,
            "production_eligible": True,
        },
        "roles": [
            {"role": "boke", "candidate_id": "candidate-boke", "request_id": "request-boke", "image_sha256": SHA_D, "reference_path": "identities/boke.png"},
            {"role": "tsukkomi", "candidate_id": "candidate-tsukkomi", "request_id": "request-tsukkomi", "image_sha256": SHA_E, "reference_path": "identities/tsukkomi.png"},
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
                    {"pose": pose, "path": f"controls/{pose}.png", "sha256": SHA_F}
                    for pose in poses
                ],
            },
        ],
    }


def make_results(plan: dict[str, object], root: Path) -> dict[str, object]:
    payload = png()
    image_sha = hashlib.sha256(payload).hexdigest()
    entries: list[dict[str, object]] = []
    for row in il.expand_matrix(plan):
        common = {key: row[key] for key in (
            "run_id", "model_family", "model_profile_ref", "model_profile_sha256", "workflow_sha256",
            "role", "candidate_id", "request_id", "identity_sha256", "strategy_id", "strategy_type",
            "pose", "expression", "control_sha256",
        )}
        if row["strategy_id"] == "reference-baseline":
            path = root / row["output_path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            entries.append({**common, "state": "succeeded", "elapsed_ms": 1200, "image_path": row["output_path"], "image_sha256": image_sha, "width": 2, "height": 2})
        else:
            entries.append({**common, "state": "failed", "elapsed_ms": 900, "error": {"code": "fixture-failure", "message": "controlled fixture failure"}})
    return {
        "kind": "identity-lock-results",
        "schema_version": "1.0",
        "id": "identity-results-demo",
        "version": "v001",
        "plan_ref": plan["id"],
        "plan_version": plan["version"],
        "plan_sha256": il.plan_sha256(plan),
        "results": entries,
    }


def make_review(plan: dict[str, object], results: dict[str, object], manifest: dict[str, object]) -> dict[str, object]:
    model = plan["selected_model"]
    roles = {item["role"]: item for item in plan["roles"]}
    matrix = il.expand_matrix(plan)
    selections = []
    for role in ("boke", "tsukkomi"):
        identity = roles[role]
        accepted = sorted(row["run_id"] for row in matrix if row["role"] == role and row["strategy_id"] == "reference-baseline")
        selections.append(
            {
                "role": role,
                "strategy_id": "reference-baseline",
                "candidate_id": identity["candidate_id"],
                "request_id": identity["request_id"],
                "identity_sha256": identity["image_sha256"],
                "model_family": model["family"],
                "model_profile_ref": model["profile_ref"],
                "model_profile_sha256": model["profile_sha256"],
                "workflow_sha256": model["workflow_sha256"],
                "accepted_run_ids": accepted,
            }
        )
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
        "observations": ["both identities remain readable across the full grid"],
    }
    review["id"] = rv.expected_review_id(review)
    return review


class IdentityLockReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.result_root = self.root / "results"
        self.result_root.mkdir()
        self.plan = valid_plan()
        self.results = make_results(self.plan, self.result_root)
        diagnostics, images = ir.validate_results(self.results, self.plan, self.result_root)
        self.assertEqual(diagnostics, [])
        self.manifest, files = ir.build_sheet_package(self.results, self.plan, images)
        self.package_root = self.root / "package"
        ir.publish_package(self.package_root, files)
        self.review = make_review(self.plan, self.results, self.manifest)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def validate(self, review: dict[str, object] | None = None, results: dict[str, object] | None = None):
        return rv.validate_review(review or self.review, self.plan, results or self.results, result_root=self.result_root, package_root=self.package_root)

    def _rebuild_package_and_review(self, results: dict[str, object], review: dict[str, object]) -> None:
        diagnostics, images = ir.validate_results(results, self.plan, self.result_root)
        self.assertEqual(diagnostics, [])
        manifest, files = ir.build_sheet_package(results, self.plan, images)
        shutil.rmtree(self.package_root)
        ir.publish_package(self.package_root, files)
        review["results_sha256"] = ir.results_sha256(results)
        review["package_ref"] = manifest["id"]
        review["package_sha256"] = hashlib.sha256(ir.document_bytes(manifest)).hexdigest()
        review["id"] = rv.expected_review_id(review)

    def test_valid_owner_approval_returns_exact_role_locks(self) -> None:
        diagnostics, locks = self.validate()
        self.assertEqual(diagnostics, [])
        self.assertEqual(set(locks), {"boke", "tsukkomi"})
        self.assertEqual(locks["boke"]["identity_sha256"], SHA_D)
        self.assertEqual(locks["tsukkomi"]["identity_sha256"], SHA_E)
        self.assertEqual(len(locks["boke"]["accepted_run_ids"]), 9)
        self.assertEqual(locks["boke"]["review_ref"], self.review["id"])

    def test_exact_evidence_bindings_are_required(self) -> None:
        wrong = {"plan_ref": "other-plan", "plan_version": "v999", "plan_sha256": SHA_F, "results_ref": "other-results", "results_version": "v999", "results_sha256": SHA_F, "package_ref": "other-package", "package_sha256": SHA_F}
        for field, value in wrong.items():
            with self.subTest(field=field):
                review = copy.deepcopy(self.review)
                review[field] = value
                review["id"] = rv.expected_review_id(review)
                self.assertIn("EVIDENCE_BINDING", {item["code"] for item in self.validate(review)[0]})

    def test_package_missing_extra_changed_and_symlink_fail_closed(self) -> None:
        svg = next(path for path in self.package_root.rglob("*.svg"))
        svg.unlink()
        self.assertIn("PACKAGE_MISSING", {item["code"] for item in self.validate()[0]})

    def test_changed_package_bytes_fail_closed(self) -> None:
        svg = next(path for path in self.package_root.rglob("*.svg"))
        svg.write_bytes(svg.read_bytes() + b"changed")
        self.assertIn("PACKAGE_BYTES", {item["code"] for item in self.validate()[0]})

    def test_extra_package_file_fails_closed(self) -> None:
        (self.package_root / "extra.txt").write_text("extra", encoding="utf-8")
        self.assertIn("PACKAGE_EXTRA", {item["code"] for item in self.validate()[0]})

    def test_symlinked_package_file_fails_closed(self) -> None:
        svg = next(path for path in self.package_root.rglob("*.svg"))
        outside = self.root / "outside.svg"
        outside.write_bytes(svg.read_bytes())
        svg.unlink()
        try:
            svg.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertIn("PACKAGE_SYMLINK", {item["code"] for item in self.validate()[0]})

    def test_review_id_is_deterministic_and_notes_are_nonsemantic(self) -> None:
        original = self.review["id"]
        changed = copy.deepcopy(self.review)
        changed["notes"] = "free form note"
        self.assertEqual(rv.expected_review_id(changed), original)
        changed["observations"].append("second observation")
        self.assertNotEqual(rv.expected_review_id(changed), original)

    def test_approval_requires_exact_two_role_coverage(self) -> None:
        review = copy.deepcopy(self.review)
        review["role_selections"].pop()
        review["id"] = rv.expected_review_id(review)
        self.assertIn("ROLE_COVERAGE", {item["code"] for item in self.validate(review)[0]})

    def test_approval_requires_complete_successful_pose_expression_grid(self) -> None:
        review = copy.deepcopy(self.review)
        review["role_selections"][0]["accepted_run_ids"].pop()
        review["id"] = rv.expected_review_id(review)
        self.assertIn("GRID_COVERAGE", {item["code"] for item in self.validate(review)[0]})

    def test_failed_run_cannot_be_accepted(self) -> None:
        results = copy.deepcopy(self.results)
        accepted = self.review["role_selections"][0]["accepted_run_ids"][0]
        entry = next(item for item in results["results"] if item["run_id"] == accepted)
        for field in ("image_path", "image_sha256", "width", "height"):
            entry.pop(field)
        entry["state"] = "failed"
        entry["error"] = {"code": "identity-drift", "message": "fixture drift"}
        review = copy.deepcopy(self.review)
        self._rebuild_package_and_review(results, review)
        self.assertIn("ACCEPTED_RUN_FAILED", {item["code"] for item in self.validate(review, results)[0]})

    def test_other_strategy_run_cannot_replace_grid_evidence(self) -> None:
        review = copy.deepcopy(self.review)
        other = next(row["run_id"] for row in il.expand_matrix(self.plan) if row["role"] == "boke" and row["strategy_id"] == "reference-openpose")
        review["role_selections"][0]["accepted_run_ids"][0] = other
        review["id"] = rv.expected_review_id(review)
        codes = {item["code"] for item in self.validate(review)[0]}
        self.assertIn("GRID_COVERAGE", codes)
        self.assertIn("ACCEPTED_RUN_BINDING", codes)

    def test_role_identity_and_model_bindings_are_exact(self) -> None:
        for field, value in (("identity_sha256", SHA_A), ("candidate_id", "candidate-other"), ("workflow_sha256", SHA_C), ("strategy_id", "unknown-strategy")):
            with self.subTest(field=field):
                review = copy.deepcopy(self.review)
                review["role_selections"][0][field] = value
                review["id"] = rv.expected_review_id(review)
                codes = {item["code"] for item in self.validate(review)[0]}
                self.assertTrue({"ROLE_BINDING", "STRATEGY_BINDING"} & codes)

    def test_rejected_hard_fail_evidence_is_separate_and_known(self) -> None:
        run_id = next(row["run_id"] for row in il.expand_matrix(self.plan) if row["strategy_id"] == "reference-openpose")
        review = copy.deepcopy(self.review)
        review["rejected_evidence"] = [{"run_id": run_id, "hard_fail_categories": ["identity_drift"]}]
        review["id"] = rv.expected_review_id(review)
        self.assertEqual(self.validate(review)[0], [])

        unknown = copy.deepcopy(review)
        unknown["rejected_evidence"][0]["hard_fail_categories"] = ["invented_failure"]
        unknown["id"] = rv.expected_review_id(unknown)
        self.assertIn("HARD_FAIL_CATEGORY", {item["code"] for item in rv.validate_review_document(unknown)})

        overlap = copy.deepcopy(self.review)
        overlap["rejected_evidence"] = [{"run_id": overlap["role_selections"][0]["accepted_run_ids"][0], "hard_fail_categories": ["identity_drift"]}]
        overlap["id"] = rv.expected_review_id(overlap)
        self.assertIn("ACCEPT_REJECT_OVERLAP", {item["code"] for item in rv.validate_review_document(overlap)})

    def test_reject_and_needs_revision_never_return_locks(self) -> None:
        for decision in ("reject", "needs_revision"):
            with self.subTest(decision=decision):
                review = copy.deepcopy(self.review)
                review["decision"] = decision
                review["role_selections"] = []
                review["id"] = rv.expected_review_id(review)
                diagnostics, locks = self.validate(review)
                self.assertEqual(diagnostics, [])
                self.assertEqual(locks, {})

    def test_forbidden_automatic_decision_fields_are_rejected(self) -> None:
        for key in ("identity_score", "rank", "winner", "similarity_threshold", "inferred_strategy", "automatic_promotion"):
            with self.subTest(key=key):
                review = copy.deepcopy(self.review)
                review[key] = 1
                review["id"] = rv.expected_review_id(review)
                self.assertIn("AUTOMATIC_APPROVAL_FORBIDDEN", {item["code"] for item in rv.validate_review_document(review)})

    def test_cli_is_deterministic_and_read_only(self) -> None:
        plan_path = self.root / "plan.json"
        results_path = self.root / "results.json"
        review_path = self.root / "review.json"
        plan_path.write_text(json.dumps(self.plan, sort_keys=True), encoding="utf-8")
        results_path.write_text(json.dumps(self.results, sort_keys=True), encoding="utf-8")
        review_path.write_text(json.dumps(self.review, sort_keys=True), encoding="utf-8")
        before = {path: path.read_bytes() for path in (plan_path, results_path, review_path)}
        first = io.StringIO()
        with redirect_stdout(first):
            self.assertEqual(rv.main(["review-check", str(review_path), str(plan_path), str(results_path), "--result-root", str(self.result_root), "--package-root", str(self.package_root)]), 0)
        parsed = json.loads(first.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(set(parsed["identity_locks"]), {"boke", "tsukkomi"})
        second = io.StringIO()
        with redirect_stdout(second):
            self.assertEqual(rv.main(["review-check", str(review_path), str(plan_path), str(results_path), "--result-root", str(self.result_root), "--package-root", str(self.package_root)]), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        for path, payload in before.items():
            self.assertEqual(path.read_bytes(), payload)

    def test_schema_json_mirrors_owner_gate(self) -> None:
        path = Path(__file__).resolve().parents[1] / "schemas" / "identity-lock-review.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["kind"]["const"], "identity-lock-review")
        text = json.dumps(schema, sort_keys=True)
        self.assertIn("approve_identity_lock", text)
        self.assertIn("accepted_run_ids", text)
        self.assertIn("identity_drift", text)
        self.assertIn("maxItems", text)


if __name__ == "__main__":
    unittest.main()
