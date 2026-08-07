from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ai_illustration import identity_lock as il
from ai_illustration import identity_lock_results as ir
from ai_illustration import identity_lock_review as rv
from ai_illustration.exporter import ExportError, build_export_package, check_export_package
from ai_illustration.naming import canonical_json, content_identifier
from ai_illustration.quality import CREATIVE_CANDIDATE, TECHNICAL_CANDIDATE
from ai_illustration.variant_review import expected_review_id, variant_set_sha256
from ai_illustration.variants import IdentityEvidence, VariantError, plan_variant_set


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_canonical(path: Path, data: dict[str, object]) -> None:
    path.write_bytes(canonical_json(data) + b"\n")


def _png(
    pixel: tuple[int, ...],
    *,
    width: int = 1,
    height: int = 1,
    srgb: bool = True,
    color_type: int = 6,
    plte: bytes | None = None,
) -> bytes:
    channels = 4 if color_type == 6 else 2 if color_type == 4 else 3
    if len(pixel) != channels:
        raise ValueError("pixel channel count mismatch")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(pixel) * width
    payload = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    )
    if srgb:
        payload += chunk(b"sRGB", b"\x00")
    if plte is not None:
        payload += chunk(b"PLTE", plte)
    payload += chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")
    return payload


class ExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifests = self.root / "manifests"
        self.sources = self.root / "sources"
        self.approvals = self.root / "approvals"
        self.output = self.root / "output"
        self.manifests.mkdir()
        self.sources.mkdir()
        self.approvals.mkdir()

        candidate_payload = _png((0, 0, 0, 0))
        candidate_sha = hashlib.sha256(candidate_payload).hexdigest()
        self.candidate_sha = candidate_sha
        (self.manifests / "candidate.png").write_bytes(candidate_payload)
        _write_json(self.manifests / "character.json", {
            "kind": "character-spec", "schema_version": "1.0", "id": "boke",
            "version": "v001", "role": "boke", "review_status": "approved",
            "identity_anchors": ["fixture"], "license_status": "approved",
        })
        _write_json(self.manifests / "style.json", {
            "kind": "style-profile", "schema_version": "1.0", "id": "rough-flat",
            "version": "v001", "line": {}, "palette": {},
            "anti_ai_checks": ["fixture"], "license_status": "approved",
        })
        _write_json(self.manifests / "request.json", {
            "kind": "generation-request", "schema_version": "1.0", "id": "request-demo",
            "character_ref": "boke@v001", "style_ref": "rough-flat@v001",
            "pose": "neutral", "expression": "neutral", "crop": "full", "facing": "front",
            "tool_id": "fixture-tool", "model_id": "fixture-model",
            "license_status": "approved", "config": {}, "output_intent": "candidate",
            "provenance": {"source": "fixture"},
        })
        _write_json(self.manifests / "candidate.json", {
            "kind": "candidate-asset", "schema_version": "1.0", "id": "candidate-demo",
            "request_ref": "request-demo", "path": "candidate.png", "sha256": candidate_sha,
            "width": 1, "height": 1, "color_space": "sRGB", "has_alpha": True,
            "media_type": "image/png", "status": "technically_valid",
            "quality_stage": TECHNICAL_CANDIDATE,
            "provenance": {"source": "fixture"},
        })
        _write_json(self.manifests / "review.json", {
            "kind": "review-decision", "schema_version": "1.0", "id": "review-demo",
            "candidate_ref": "candidate-demo", "candidate_request_ref": "request-demo",
            "candidate_sha256": candidate_sha, "decision": "accept", "reviewer": "owner",
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
        self.variant_set = plan_variant_set(self.manifests, "candidate-demo", self.matrix, "evaluation")
        self.variant_set_path = self.root / "variant-set.json"
        _write_json(self.variant_set_path, self.variant_set)
        self._restore_sources_for(self.variant_set)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _clear(self, directory: Path) -> None:
        for child in list(directory.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _restore_sources_for(self, variant_set: dict[str, object]) -> None:
        self._clear(self.sources)
        for variant in variant_set["variants"]:
            pixel = (255, 0, 0, 255) if variant["expression"] == "neutral" else (0, 0, 255, 255)
            (self.sources / f"{variant['id']}.png").write_bytes(_png(pixel))

    def _restore_sources(self) -> None:
        self._restore_sources_for(self.variant_set)

    def _build_identity_evidence(self) -> IdentityEvidence:
        identity_root = self.root / "identity-evidence"
        result_root = identity_root / "results"
        result_root.mkdir(parents=True)
        poses = ["front-neutral", "three-quarter", "seated-asymmetric"]
        plan = {
            "kind": "identity-lock-plan", "schema_version": "1.0",
            "id": "export-source-identity-lock", "version": "v001", "status": "prepared",
            "selected_model": {
                "family": "fixture-family", "profile_ref": "fixture-model@v001",
                "profile_sha256": "a" * 64, "workflow_sha256": "b" * 64,
                "benchmark_review_ref": "benchmark-review-fixture",
                "benchmark_review_sha256": "c" * 64, "production_eligible": True,
            },
            "roles": [
                {"role": "boke", "candidate_id": "candidate-demo", "request_id": "request-demo", "image_sha256": self.candidate_sha, "reference_path": "identities/boke.png"},
                {"role": "tsukkomi", "candidate_id": "candidate-tsukkomi", "request_id": "request-tsukkomi", "image_sha256": "d" * 64, "reference_path": "identities/tsukkomi.png"},
            ],
            "pose_targets": poses,
            "expression_targets": ["neutral", "smile", "surprised"],
            "strategies": [
                {"id": "reference-baseline", "type": "reference-only"},
                {"id": "reference-openpose", "type": "reference-plus-pose", "control_method": "openpose", "control_assets": [
                    {"pose": pose, "path": f"controls/{pose}.png", "sha256": "e" * 64} for pose in poses
                ]},
            ],
        }
        identity_png = _png((10, 20, 30, 255))
        identity_png_sha = hashlib.sha256(identity_png).hexdigest()
        entries: list[dict[str, object]] = []
        for row in il.expand_matrix(plan):
            common = {key: row[key] for key in (
                "run_id", "model_family", "model_profile_ref", "model_profile_sha256",
                "workflow_sha256", "role", "candidate_id", "request_id", "identity_sha256",
                "strategy_id", "strategy_type", "pose", "expression", "control_sha256",
            )}
            if row["strategy_id"] == "reference-baseline":
                path = result_root / row["output_path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(identity_png)
                entries.append({**common, "state": "succeeded", "elapsed_ms": 100, "image_path": row["output_path"], "image_sha256": identity_png_sha, "width": 1, "height": 1})
            else:
                entries.append({**common, "state": "failed", "elapsed_ms": 100, "error": {"code": "fixture-failure", "message": "fixture control failure"}})
        results = {
            "kind": "identity-lock-results", "schema_version": "1.0",
            "id": "export-identity-results", "version": "v001",
            "plan_ref": plan["id"], "plan_version": plan["version"],
            "plan_sha256": il.plan_sha256(plan), "results": entries,
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
                "role": role, "strategy_id": "reference-baseline",
                "candidate_id": identity["candidate_id"], "request_id": identity["request_id"],
                "identity_sha256": identity["image_sha256"], "model_family": model["family"],
                "model_profile_ref": model["profile_ref"], "model_profile_sha256": model["profile_sha256"],
                "workflow_sha256": model["workflow_sha256"],
                "accepted_run_ids": sorted(row["run_id"] for row in matrix if row["role"] == role and row["strategy_id"] == "reference-baseline"),
            })
        review = {
            "kind": "identity-lock-review", "schema_version": "1.0",
            "id": "identity-review-0000000000000000",
            "plan_ref": plan["id"], "plan_version": plan["version"], "plan_sha256": il.plan_sha256(plan),
            "results_ref": results["id"], "results_version": results["version"], "results_sha256": ir.results_sha256(results),
            "package_ref": manifest["id"], "package_sha256": hashlib.sha256(ir.document_bytes(manifest)).hexdigest(),
            "reviewer": "owner", "timestamp": "2026-08-07T07:00:00Z", "decision": "approve_identity_lock",
            "role_selections": selections, "rejected_evidence": [],
            "observations": ["fixture identity lock is complete"],
        }
        review["id"] = rv.expected_review_id(review)
        plan_path = identity_root / "plan.json"
        results_path = identity_root / "results.json"
        review_path = identity_root / "review.json"
        _write_json(plan_path, plan)
        _write_json(results_path, results)
        _write_json(review_path, review)
        return IdentityEvidence(review=review_path, plan=plan_path, results=results_path, result_root=result_root, package_root=package_root)

    def _build(
        self,
        output: Path | None = None,
        *,
        write: bool = False,
        variant_set_path: Path | None = None,
        approval_root: Path | None = None,
    ) -> dict[str, object]:
        path = variant_set_path or self.variant_set_path
        identity = self.identity_evidence if path != self.variant_set_path else None
        return build_export_package(
            path,
            self.manifests,
            self.sources,
            output or self.output,
            approval_root=approval_root,
            identity_evidence=identity,
            write=write,
        )

    def _production_variant_set(self) -> tuple[dict[str, object], Path]:
        production = plan_variant_set(
            self.manifests,
            "candidate-demo",
            self.matrix,
            "production",
            identity_evidence=self.identity_evidence,
        )
        path = self.root / "variant-set-production.json"
        _write_json(path, production)
        self._restore_sources_for(production)
        return production, path

    def _formal_review(self, variant_set: dict[str, object], variant: dict[str, object]) -> dict[str, object]:
        png_path = self.sources / f"{variant['id']}.png"
        review: dict[str, object] = {
            "kind": "variant-review-decision",
            "schema_version": "1.0",
            "id": "variant-review-" + "0" * 20,
            "variant_set_ref": variant_set["id"],
            "variant_set_sha256": variant_set_sha256(variant_set),
            "variant_id": variant["id"],
            "png_sha256": hashlib.sha256(png_path.read_bytes()).hexdigest(),
            "source_candidate_ref": variant_set["source_candidate_ref"],
            "source_request_ref": variant_set["source_request_ref"],
            "source_candidate_sha256": variant_set["source_candidate_sha256"],
            "identity_gate": variant_set["identity_gate"],
            "identity_review_ref": variant_set["identity_review_ref"],
            "identity_review_sha256": variant_set["identity_review_sha256"],
            "identity_strategy_id": variant_set["identity_strategy_id"],
            "identity_evidence_run_ids": list(variant_set["identity_evidence_run_ids"]),
            "identity_model": copy.deepcopy(variant_set["identity_model"]),
            "decision": "accept",
            "result_state": "production-variant-approved",
            "reviewer": "owner",
            "timestamp": "2026-08-07T08:00:00Z",
            "hard_fail_categories": [],
            "observations": ["fixture production variant approved"],
        }
        review["id"] = expected_review_id(review)
        return review

    def _write_approvals(self, variant_set: dict[str, object]) -> None:
        self._clear(self.approvals)
        for variant in variant_set["variants"]:
            _write_json(self.approvals / f"{variant['id']}.json", self._formal_review(variant_set, variant))

    def _write_legacy_approvals(self, variant_set: dict[str, object]) -> None:
        self._clear(self.approvals)
        for variant in variant_set["variants"]:
            png_path = self.sources / f"{variant['id']}.png"
            core = {
                "kind": "variant-review-decision",
                "schema_version": "1.0",
                "variant_set_ref": variant_set["id"],
                "variant_id": variant["id"],
                "png_sha256": hashlib.sha256(png_path.read_bytes()).hexdigest(),
                "decision": "accept",
                "reviewer": "owner",
            }
            review = {"id": content_identifier("variant-review", core, 20), **core}
            _write_json(self.approvals / f"{variant['id']}.json", review)

    def test_deterministic_dry_run_matches_fixtures_and_does_not_write(self) -> None:
        first = self._build()
        second = self._build()
        package_fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "export" / "package-manifest.json").read_text(encoding="utf-8")
        )
        index_fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "export" / "paper-theater-index.json").read_text(encoding="utf-8")
        )
        self.assertEqual(first, second)
        self.assertEqual(first["package"], package_fixture)
        self.assertEqual(first["paper_theater_index"], index_fixture)
        self.assertFalse(self.output.exists())

    def test_write_is_byte_identical_atomic_idempotent_and_checkable(self) -> None:
        result = self._build(write=True)
        self.assertTrue(result["published"])
        package_root = self.output / result["package_directory"]
        for item in result["package"]["items"]:
            source = self.sources / f"{item['variant_id']}.png"
            exported = package_root / item["png_path"]
            self.assertEqual(source.read_bytes(), exported.read_bytes())
        checked = check_export_package(package_root / "package-manifest.json", self.output)
        self.assertTrue(checked["ok"])
        repeated = self._build(write=True)
        self.assertTrue(repeated["idempotent"])
        self.assertFalse(repeated["published"])

    def test_production_export_revalidates_identity_evidence(self) -> None:
        production, production_path = self._production_variant_set()
        self._write_approvals(production)
        with self.assertRaisesRegex(VariantError, "IDENTITY_LOCK_REQUIRED"):
            build_export_package(
                production_path,
                self.manifests,
                self.sources,
                self.root / "missing-identity-output",
                approval_root=self.approvals,
                write=False,
            )

    def test_production_rejects_legacy_review_format(self) -> None:
        production, production_path = self._production_variant_set()
        self._write_legacy_approvals(production)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_SCHEMA"):
            self._build(variant_set_path=production_path, approval_root=self.approvals)

    def test_production_requires_included_exact_byte_bound_accept_reviews(self) -> None:
        production, production_path = self._production_variant_set()
        with self.assertRaisesRegex(ExportError, "PRODUCTION_VARIANT_REVIEW_REQUIRED"):
            self._build(variant_set_path=production_path)
        self._write_approvals(production)
        production_output = self.root / "production-output"
        result = self._build(
            production_output,
            write=True,
            variant_set_path=production_path,
            approval_root=self.approvals,
        )
        package = result["package"]
        self.assertEqual(package["intent"], "production")
        self.assertEqual(package["variant_set_sha256"], variant_set_sha256(production))
        self.assertEqual(package["identity_gate"], "owner-approved")
        self.assertEqual(package["identity_review_ref"], production["identity_review_ref"])
        self.assertEqual(package["identity_model"], production["identity_model"])
        self.assertTrue(all("variant_review_path" in item for item in package["items"]))
        package_root = production_output / result["package_directory"]
        for item in package["items"]:
            review_path = package_root / item["variant_review_path"]
            self.assertTrue(review_path.is_file())
            review = json.loads(review_path.read_text(encoding="utf-8"))
            self.assertEqual(review["id"], item["variant_review_ref"])
            self.assertEqual(hashlib.sha256(review_path.read_bytes()).hexdigest(), item["variant_review_sha256"])
        self.assertTrue(check_export_package(package_root / "package-manifest.json", production_output)["ok"])

        first = production["variants"][0]
        review_path = self.approvals / f"{first['id']}.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["png_sha256"] = "0" * 64
        review["id"] = expected_review_id(review)
        _write_json(review_path, review)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_BINDING_MISMATCH"):
            self._build(variant_set_path=production_path, approval_root=self.approvals)

        self._write_approvals(production)
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["decision"] = "reject"
        review["result_state"] = "rejected"
        review["id"] = expected_review_id(review)
        _write_json(review_path, review)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_NOT_ACCEPTED"):
            self._build(variant_set_path=production_path, approval_root=self.approvals)

    def test_production_review_bindings_and_hard_fails_fail_closed(self) -> None:
        production, production_path = self._production_variant_set()
        first = production["variants"][0]
        cases = {
            "variant_set_sha256": "0" * 64,
            "source_candidate_ref": "candidate-other",
            "identity_strategy_id": "other-strategy",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self._write_approvals(production)
                review_path = self.approvals / f"{first['id']}.json"
                review = json.loads(review_path.read_text(encoding="utf-8"))
                review[field] = value
                review["id"] = expected_review_id(review)
                _write_json(review_path, review)
                with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_BINDING_MISMATCH"):
                    self._build(variant_set_path=production_path, approval_root=self.approvals)

        self._write_approvals(production)
        review_path = self.approvals / f"{first['id']}.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["identity_model"] = {**review["identity_model"], "family": "other-family"}
        review["id"] = expected_review_id(review)
        _write_json(review_path, review)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_BINDING_MISMATCH"):
            self._build(variant_set_path=production_path, approval_root=self.approvals)

        self._write_approvals(production)
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["hard_fail_categories"] = ["identity_drift"]
        review["id"] = expected_review_id(review)
        _write_json(review_path, review)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_HARD_FAIL"):
            self._build(variant_set_path=production_path, approval_root=self.approvals)

    def test_export_check_rejects_tampered_production_review_binding(self) -> None:
        production, production_path = self._production_variant_set()
        self._write_approvals(production)
        output = self.root / "tampered-production"
        result = self._build(output, write=True, variant_set_path=production_path, approval_root=self.approvals)
        package_root = output / result["package_directory"]
        first_item = result["package"]["items"][0]
        review_path = package_root / first_item["variant_review_path"]
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["png_sha256"] = "0" * 64
        review["id"] = expected_review_id(review)
        _write_canonical(review_path, review)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_FILE_MISMATCH"):
            check_export_package(package_root / "package-manifest.json", output)

    def test_export_check_rejects_self_consistent_rejected_production_license(self) -> None:
        production, production_path = self._production_variant_set()
        self._write_approvals(production)
        output = self.root / "rejected-production-license"
        result = self._build(
            output,
            write=True,
            variant_set_path=production_path,
            approval_root=self.approvals,
        )
        old_root = output / result["package_directory"]
        malicious_package = copy.deepcopy(result["package"])
        malicious_package["license_status"] = "rejected"
        sidecar_payloads: dict[str, bytes] = {}
        for item in malicious_package["items"]:
            sidecar = json.loads((old_root / item["sidecar_path"]).read_text(encoding="utf-8"))
            sidecar["license_status"] = "rejected"
            payload = canonical_json(sidecar) + b"\n"
            item["sidecar_sha256"] = hashlib.sha256(payload).hexdigest()
            sidecar_payloads[item["sidecar_path"]] = payload
        package_core = {key: value for key, value in malicious_package.items() if key != "id"}
        malicious_package["id"] = content_identifier("variant-export-package", package_core, 20)
        malicious_root = output / malicious_package["id"]
        shutil.copytree(old_root, malicious_root)
        for relative, payload in sidecar_payloads.items():
            (malicious_root / relative).write_bytes(payload)
        _write_canonical(malicious_root / "package-manifest.json", malicious_package)
        with self.assertRaisesRegex(ExportError, "PRODUCTION_LICENSE_NOT_APPROVED"):
            check_export_package(malicious_root / "package-manifest.json", output)

    def test_evaluation_rejects_approval_root_and_embedded_approval_claims(self) -> None:
        self.assertEqual(self._build()["package"]["identity_gate"], "evaluation-unlocked")
        self.assertIsNone(self._build()["package"]["identity_review_ref"])
        self.assertEqual(self._build()["package"]["identity_evidence_run_ids"], [])
        with self.assertRaisesRegex(ExportError, "APPROVAL_ROOT_NOT_ALLOWED"):
            self._build(approval_root=self.approvals)
        output = self.root / "evaluation-claim"
        result = self._build(output, write=True)
        old_root = output / result["package_directory"]

        malicious_item_package = copy.deepcopy(result["package"])
        malicious_item = malicious_item_package["items"][0]
        malicious_item["variant_review_ref"] = "variant-review-" + "0" * 20
        malicious_item["variant_review_path"] = f"reviews/{malicious_item['variant_id']}.json"
        malicious_item["variant_review_sha256"] = "0" * 64
        item_core = {key: value for key, value in malicious_item_package.items() if key != "id"}
        malicious_item_package["id"] = content_identifier("variant-export-package", item_core, 20)
        item_root = output / malicious_item_package["id"]
        shutil.copytree(old_root, item_root)
        _write_canonical(item_root / "package-manifest.json", malicious_item_package)
        with self.assertRaisesRegex(ExportError, "EVALUATION_REVIEW_CLAIM"):
            check_export_package(item_root / "package-manifest.json", output)

        reviewer_only_package = copy.deepcopy(result["package"])
        reviewer_only_item = reviewer_only_package["items"][0]
        reviewer_only_item["variant_reviewer"] = "attacker"
        reviewer_core = {key: value for key, value in reviewer_only_package.items() if key != "id"}
        reviewer_only_package["id"] = content_identifier("variant-export-package", reviewer_core, 20)
        reviewer_root = output / reviewer_only_package["id"]
        shutil.copytree(old_root, reviewer_root)
        _write_canonical(reviewer_root / "package-manifest.json", reviewer_only_package)
        with self.assertRaisesRegex(ExportError, "EVALUATION_REVIEW_CLAIM"):
            check_export_package(reviewer_root / "package-manifest.json", output)

        malicious_identity = copy.deepcopy(result["package"])
        malicious_identity["identity_gate"] = "owner-approved"
        identity_core = {key: value for key, value in malicious_identity.items() if key != "id"}
        malicious_identity["id"] = content_identifier("variant-export-package", identity_core, 20)
        identity_root = output / malicious_identity["id"]
        shutil.copytree(old_root, identity_root)
        _write_canonical(identity_root / "package-manifest.json", malicious_identity)
        with self.assertRaisesRegex(ExportError, "EVALUATION_IDENTITY_CLAIM"):
            check_export_package(identity_root / "package-manifest.json", output)

        malicious_sidecar_package = copy.deepcopy(result["package"])
        sidecar_item = malicious_sidecar_package["items"][0]
        sidecar_path = old_root / sidecar_item["sidecar_path"]
        malicious_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        malicious_sidecar["variant_review_ref"] = "variant-review-" + "0" * 20
        malicious_sidecar["variant_review_path"] = f"reviews/{sidecar_item['variant_id']}.json"
        malicious_sidecar["variant_review_sha256"] = "0" * 64
        malicious_sidecar["variant_reviewer"] = "attacker"
        malicious_sidecar_payload = canonical_json(malicious_sidecar) + b"\n"
        sidecar_item["sidecar_sha256"] = hashlib.sha256(malicious_sidecar_payload).hexdigest()
        sidecar_core = {key: value for key, value in malicious_sidecar_package.items() if key != "id"}
        malicious_sidecar_package["id"] = content_identifier("variant-export-package", sidecar_core, 20)
        sidecar_root = output / malicious_sidecar_package["id"]
        shutil.copytree(old_root, sidecar_root)
        (sidecar_root / sidecar_item["sidecar_path"]).write_bytes(malicious_sidecar_payload)
        _write_canonical(sidecar_root / "package-manifest.json", malicious_sidecar_package)
        with self.assertRaisesRegex(ExportError, "EVALUATION_REVIEW_CLAIM"):
            check_export_package(sidecar_root / "package-manifest.json", output)

    def test_export_check_rejects_self_consistent_wrong_index_binding(self) -> None:
        output = self.root / "index-binding"
        result = self._build(output, write=True)
        old_root = output / result["package_directory"]

        malicious_package = copy.deepcopy(result["package"])
        index_path = old_root / malicious_package["paper_theater_index_path"]
        malicious_index = json.loads(index_path.read_text(encoding="utf-8"))
        malicious_index["entries"][0]["sha256"] = "0" * 64
        index_core = {key: value for key, value in malicious_index.items() if key != "id"}
        malicious_index["id"] = content_identifier("paper-theater-index", index_core, 20)
        index_payload = canonical_json(malicious_index) + b"\n"
        malicious_package["paper_theater_index_sha256"] = hashlib.sha256(index_payload).hexdigest()
        package_core = {key: value for key, value in malicious_package.items() if key != "id"}
        malicious_package["id"] = content_identifier("variant-export-package", package_core, 20)

        malicious_root = output / malicious_package["id"]
        shutil.copytree(old_root, malicious_root)
        (malicious_root / malicious_package["paper_theater_index_path"]).write_bytes(index_payload)
        _write_canonical(malicious_root / "package-manifest.json", malicious_package)
        with self.assertRaisesRegex(ExportError, "INDEX_BINDING_MISMATCH"):
            check_export_package(malicious_root / "package-manifest.json", output)

    def test_export_check_rejects_self_consistent_invalid_packaged_png(self) -> None:
        output = self.root / "packaged-png"
        result = self._build(output, write=True)
        old_root = output / result["package_directory"]

        def publish_tampered(png_payload: bytes) -> Path:
            malicious_package = copy.deepcopy(result["package"])
            first_item = malicious_package["items"][0]
            png_sha = hashlib.sha256(png_payload).hexdigest()
            first_item["png_sha256"] = png_sha

            sidecar_path = old_root / first_item["sidecar_path"]
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            sidecar["source_sha256"] = png_sha
            sidecar["output_sha256"] = png_sha
            sidecar_payload = canonical_json(sidecar) + b"\n"
            first_item["sidecar_sha256"] = hashlib.sha256(sidecar_payload).hexdigest()

            index_path = old_root / malicious_package["paper_theater_index_path"]
            index = json.loads(index_path.read_text(encoding="utf-8"))
            matching = next(
                entry for entry in index["entries"]
                if entry["variant_id"] == first_item["variant_id"]
            )
            matching["sha256"] = png_sha
            index_core = {key: value for key, value in index.items() if key != "id"}
            index["id"] = content_identifier("paper-theater-index", index_core, 20)
            index_payload = canonical_json(index) + b"\n"
            malicious_package["paper_theater_index_sha256"] = hashlib.sha256(index_payload).hexdigest()

            package_core = {key: value for key, value in malicious_package.items() if key != "id"}
            malicious_package["id"] = content_identifier("variant-export-package", package_core, 20)
            malicious_root = output / malicious_package["id"]
            shutil.copytree(old_root, malicious_root)
            (malicious_root / first_item["png_path"]).write_bytes(png_payload)
            (malicious_root / first_item["sidecar_path"]).write_bytes(sidecar_payload)
            (malicious_root / malicious_package["paper_theater_index_path"]).write_bytes(index_payload)
            _write_canonical(malicious_root / "package-manifest.json", malicious_package)
            return malicious_root

        cases = (
            (b"not-a-png", "PNG_STRUCTURE"),
            (_png((0, 255, 0, 255), width=2), "PNG_DECLARATION_MISMATCH"),
            (_png((0, 255, 0, 255), srgb=False), "PNG_SRGB_REQUIRED"),
            (_png((0, 255), color_type=4, plte=b"\x00\x00\x00"), "PNG_STRUCTURE"),
        )
        for png_payload, code in cases:
            with self.subTest(code=code):
                malicious_root = publish_tampered(png_payload)
                with self.assertRaisesRegex(ExportError, code):
                    check_export_package(malicious_root / "package-manifest.json", output)

    def test_export_check_rejects_self_consistent_package_provenance_changes(self) -> None:
        output = self.root / "provenance-binding"
        result = self._build(output, write=True)
        old_root = output / result["package_directory"]
        cases = {
            "source_candidate_ref": "candidate-other",
            "source_candidate_sha256": "0" * 64,
            "source_request_ref": "request-other",
            "review_ref": "review-other",
            "character_ref": "other@v001",
            "style_ref": "other-style@v001",
            "license_status": "rejected",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                malicious_package = copy.deepcopy(result["package"])
                malicious_package[field] = value
                package_core = {key: item for key, item in malicious_package.items() if key != "id"}
                malicious_package["id"] = content_identifier("variant-export-package", package_core, 20)
                malicious_root = output / malicious_package["id"]
                shutil.copytree(old_root, malicious_root)
                _write_canonical(malicious_root / "package-manifest.json", malicious_package)
                with self.assertRaisesRegex(ExportError, "SIDECAR_BINDING_MISMATCH"):
                    check_export_package(malicious_root / "package-manifest.json", output)

    def test_missing_extra_malformed_dimension_srgb_and_alpha_fail_closed(self) -> None:
        first_id = self.variant_set["variants"][0]["id"]
        target = self.sources / f"{first_id}.png"
        target.unlink()
        with self.assertRaisesRegex(ExportError, "SOURCE_MISSING"):
            self._build()
        self._restore_sources()
        (self.sources / "extra.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "EXTRA_SOURCE_FILE"):
            self._build()
        self._restore_sources()
        target = self.sources / f"{first_id}.png"
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(ExportError, "PNG_STRUCTURE"):
            self._build()
        self._restore_sources()
        target.write_bytes(_png((255, 0, 0, 255), width=2))
        with self.assertRaisesRegex(ExportError, "PNG_DIMENSION_MISMATCH"):
            self._build()
        self._restore_sources()
        target.write_bytes(_png((255, 0, 0, 255), srgb=False))
        with self.assertRaisesRegex(ExportError, "PNG_SRGB_REQUIRED"):
            self._build()
        self._restore_sources()
        target.write_bytes(_png((255, 0, 0), color_type=2))
        with self.assertRaisesRegex(ExportError, "PNG_STRUCTURE"):
            self._build()

    def test_roots_symlinks_and_defensive_duplicate_or_unsafe_plans_fail(self) -> None:
        for unsafe_output in (self.sources / "out", self.manifests / "out"):
            with self.assertRaisesRegex(ExportError, "ROOT_OVERLAP"):
                build_export_package(
                    self.variant_set_path, self.manifests, self.sources, unsafe_output, write=False
                )
        if hasattr(os, "symlink"):
            outside = self.root / "outside.png"
            outside.write_bytes(_png((1, 2, 3, 255)))
            link = self.sources / "link.png"
            try:
                link.symlink_to(outside)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(ExportError, "SOURCE_SYMLINK"):
                    self._build()
                link.unlink()

        duplicate = copy.deepcopy(self.variant_set)
        duplicate["variants"][1]["paper_theater_key"] = duplicate["variants"][0]["paper_theater_key"]
        with patch("ai_illustration.exporter.check_variant_set", return_value=duplicate):
            with self.assertRaisesRegex(ExportError, "DUPLICATE_PAPER_THEATER_KEY"):
                self._build()
        unsafe = copy.deepcopy(self.variant_set)
        unsafe["variants"][0]["path"] = "../escape.png"
        with patch("ai_illustration.exporter.check_variant_set", return_value=unsafe):
            with self.assertRaises(ExportError):
                self._build()

    def test_atomic_failure_leaves_no_published_or_staging_package(self) -> None:
        with patch("ai_illustration.exporter.os.replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self._build(write=True)
        if self.output.exists():
            self.assertEqual(list(self.output.iterdir()), [])

    def test_conflicts_and_modified_package_files_fail_closed(self) -> None:
        result = self._build(write=True)
        package_root = self.output / result["package_directory"]
        first_item = result["package"]["items"][0]
        (package_root / first_item["png_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ExportError, "PACKAGE_FILE_MISMATCH"):
            check_export_package(package_root / "package-manifest.json", self.output)
        with self.assertRaisesRegex(ExportError, "OUTPUT_CONFLICT"):
            self._build(write=True)

        for kind in ("sidecar", "index", "manifest"):
            out = self.root / f"check-{kind}"
            fresh = self._build(out, write=True)
            root = out / fresh["package_directory"]
            if kind == "sidecar":
                path = root / fresh["package"]["items"][0]["sidecar_path"]
                path.write_bytes(path.read_bytes() + b" ")
                code = "PACKAGE_FILE_MISMATCH"
            elif kind == "index":
                path = root / "paper-theater-index.json"
                path.write_bytes(path.read_bytes() + b" ")
                code = "INDEX_CHECKSUM_MISMATCH"
            else:
                path = root / "package-manifest.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                code = "PACKAGE_CANONICAL"
            with self.assertRaisesRegex(ExportError, code):
                check_export_package(root / "package-manifest.json", out)

    def test_output_contains_no_remote_execution_or_secret_material(self) -> None:
        text = json.dumps(self._build(), sort_keys=True)
        for forbidden in ("http://", "https://", "credential", "secret", "subprocess", "execute"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
