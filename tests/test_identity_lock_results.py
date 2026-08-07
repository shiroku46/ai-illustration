from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zlib

from ai_illustration import identity_lock as il
from ai_illustration import identity_lock_results as ir


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + kind + data + crc.to_bytes(4, "big")


def png(width: int = 2, height: int = 2) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    rows = b"".join(b"\x00" + bytes([30, 60, 90, 255]) * width for _ in range(height))
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


def result_document(plan: dict[str, object]) -> dict[str, object]:
    entries = []
    for row in il.expand_matrix(plan):
        entries.append(
            {
                **{key: row[key] for key in (
                    "run_id", "model_family", "model_profile_ref", "model_profile_sha256",
                    "workflow_sha256", "role", "candidate_id", "request_id", "identity_sha256",
                    "strategy_id", "strategy_type", "pose", "expression", "control_sha256",
                )},
                "state": "failed",
                "elapsed_ms": 1000,
                "error": {"code": "not-run", "message": "fixture failure"},
            }
        )
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


def make_succeeded(results: dict[str, object], plan: dict[str, object], root: Path, index: int = 0) -> bytes:
    row = il.expand_matrix(plan)[index]
    payload = png()
    path = root / row["output_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    entry = results["results"][index]
    entry.pop("error")
    entry.update(
        {
            "state": "succeeded",
            "image_path": row["output_path"],
            "image_sha256": hashlib.sha256(payload).hexdigest(),
            "width": 2,
            "height": 2,
        }
    )
    return payload


class IdentityLockResultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.result_root = self.root / "results"
        self.result_root.mkdir()
        self.plan = valid_plan()
        self.results = result_document(self.plan)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def validate(self, results: dict[str, object] | None = None):
        return ir.validate_results(results or self.results, self.plan, self.result_root)

    def test_complete_failed_result_set_is_valid_evidence(self) -> None:
        diagnostics, images = self.validate()
        self.assertEqual(diagnostics, [])
        self.assertEqual(images, {})
        self.assertEqual(len(self.results["results"]), 36)

    def test_exact_plan_binding_is_required(self) -> None:
        wrong_values = {
            "plan_ref": "different-plan",
            "plan_version": "v999",
            "plan_sha256": SHA_A,
        }
        for field, value in wrong_values.items():
            with self.subTest(field=field):
                results = copy.deepcopy(self.results)
                results[field] = value
                self.assertIn("PLAN_BINDING", {item["code"] for item in self.validate(results)[0]})

    def test_matrix_coverage_rejects_missing_extra_and_duplicate_runs(self) -> None:
        missing = copy.deepcopy(self.results)
        missing["results"].pop()
        self.assertIn("MISSING_RUNS", {item["code"] for item in self.validate(missing)[0]})

        extra = copy.deepcopy(self.results)
        item = copy.deepcopy(extra["results"][0])
        item["run_id"] = "identity-run-extra"
        extra["results"].append(item)
        self.assertIn("EXTRA_RUNS", {entry["code"] for entry in self.validate(extra)[0]})

        duplicate = copy.deepcopy(self.results)
        duplicate["results"][1]["run_id"] = duplicate["results"][0]["run_id"]
        self.assertIn("DUPLICATE_RUN", {entry["code"] for entry in ir.validate_document(duplicate)})

    def test_matrix_metadata_and_hashes_must_match(self) -> None:
        for field, value in (
            ("identity_sha256", SHA_A),
            ("workflow_sha256", SHA_C),
            ("candidate_id", "candidate-other"),
            ("pose", "different-pose"),
            ("control_sha256", None),
        ):
            with self.subTest(field=field):
                results = copy.deepcopy(self.results)
                target = next(
                    entry for entry in results["results"]
                    if field != "control_sha256" or entry["control_sha256"] is not None
                )
                target[field] = value
                self.assertIn("RUN_BINDING", {item["code"] for item in self.validate(results)[0]})

    def test_succeeded_png_is_checksum_and_dimension_verified(self) -> None:
        payload = make_succeeded(self.results, self.plan, self.result_root)
        diagnostics, images = self.validate()
        self.assertEqual(diagnostics, [])
        run_id = self.results["results"][0]["run_id"]
        self.assertEqual(images[run_id], payload)

        wrong_sha = copy.deepcopy(self.results)
        wrong_sha["results"][0]["image_sha256"] = SHA_A
        self.assertIn("IMAGE_CHECKSUM", {item["code"] for item in self.validate(wrong_sha)[0]})

        wrong_size = copy.deepcopy(self.results)
        wrong_size["results"][0]["width"] = 3
        self.assertIn("IMAGE_DIMENSIONS", {item["code"] for item in self.validate(wrong_size)[0]})

    def test_png_path_tamper_and_trailing_data_fail(self) -> None:
        make_succeeded(self.results, self.plan, self.result_root)
        unsafe = copy.deepcopy(self.results)
        unsafe["results"][0]["image_path"] = "../escape.png"
        self.assertIn("UNSAFE_PATH", {item["code"] for item in ir.validate_document(unsafe)})

        entry = self.results["results"][0]
        image_path = self.result_root / entry["image_path"]
        changed = image_path.read_bytes() + b"trailing"
        image_path.write_bytes(changed)
        entry["image_sha256"] = hashlib.sha256(changed).hexdigest()
        codes = {item["code"] for item in self.validate()[0]}
        self.assertIn("PNG_TRAILING_DATA", codes)

    def test_symlinked_image_fails_closed(self) -> None:
        make_succeeded(self.results, self.plan, self.result_root)
        entry = self.results["results"][0]
        image_path = self.result_root / entry["image_path"]
        outside = self.root / "outside.png"
        outside.write_bytes(image_path.read_bytes())
        image_path.unlink()
        try:
            image_path.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.assertIn("IMAGE_SYMLINK", {item["code"] for item in self.validate()[0]})

    def test_state_specific_fields_fail_closed(self) -> None:
        succeeded_without_image = copy.deepcopy(self.results)
        succeeded_without_image["results"][0]["state"] = "succeeded"
        succeeded_without_image["results"][0].pop("error")
        self.assertIn("MISSING_FIELD", {item["code"] for item in ir.validate_document(succeeded_without_image)})

        failed_with_image = copy.deepcopy(self.results)
        failed_with_image["results"][0]["image_path"] = "extra.png"
        self.assertIn("UNKNOWN_FIELD", {item["code"] for item in ir.validate_document(failed_with_image)})

    def test_forbidden_automatic_scoring_and_selection_fields_are_rejected(self) -> None:
        for key in ("identity_score", "rank", "winner", "similarity_threshold", "selected_strategy", "automatic_promotion"):
            with self.subTest(key=key):
                results = copy.deepcopy(self.results)
                results["results"][0][key] = 1
                self.assertIn("AUTOMATIC_SELECTION_FORBIDDEN", {item["code"] for item in ir.validate_document(results)})

    def test_consistency_sheets_use_fixed_pose_expression_grid_and_visible_failures(self) -> None:
        make_succeeded(self.results, self.plan, self.result_root)
        diagnostics, images = self.validate()
        self.assertEqual(diagnostics, [])
        manifest, files = ir.build_sheet_package(self.results, self.plan, images)
        self.assertEqual(len(manifest["sheets"]), 4)
        self.assertEqual(manifest["pose_order"], sorted(self.plan["pose_targets"]))
        self.assertEqual(manifest["expression_order"], sorted(self.plan["expression_targets"]))
        self.assertEqual(manifest["decision_policy"], "owner-only")
        sheet = next(payload.decode("utf-8") for path, payload in files.items() if path.endswith(".svg"))
        all_sheets = "\n".join(payload.decode("utf-8") for path, payload in files.items() if path.endswith(".svg"))
        self.assertIn("data:image/png;base64,", all_sheets)
        self.assertIn("EXECUTION FAILED", sheet)
        for prohibited in ("<script", "https://", "href=\"http", "xlink:href", "@import", "<foreignObject"):
            self.assertNotIn(prohibited, all_sheets)

    def test_sheet_package_is_byte_deterministic(self) -> None:
        first_manifest, first_files = ir.build_sheet_package(self.results, self.plan, {})
        second_manifest, second_files = ir.build_sheet_package(copy.deepcopy(self.results), copy.deepcopy(self.plan), {})
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(first_files, second_files)
        for item in first_manifest["sheets"]:
            self.assertEqual(hashlib.sha256(first_files[item["path"]]).hexdigest(), item["sha256"])
        self.assertEqual(first_files[ir.PACKAGE_MANIFEST], ir.document_bytes(first_manifest))

    def test_publish_requires_fresh_destination_and_does_not_mutate_sources(self) -> None:
        _, files = ir.build_sheet_package(self.results, self.plan, {})
        before = json.dumps(self.results, sort_keys=True)
        output = self.root / "package"
        ir.publish_package(output, files)
        self.assertTrue((output / ir.PACKAGE_MANIFEST).is_file())
        self.assertEqual(json.dumps(self.results, sort_keys=True), before)
        with self.assertRaises(ir.IdentityLockResultsError) as caught:
            ir.publish_package(output, files)
        self.assertEqual(caught.exception.code, "OUTPUT_EXISTS")

    def test_cli_checks_and_renders_without_touching_inputs(self) -> None:
        plan_path = self.root / "plan.json"
        results_path = self.root / "results.json"
        plan_bytes = json.dumps(self.plan, sort_keys=True).encode("utf-8")
        results_bytes = json.dumps(self.results, sort_keys=True).encode("utf-8")
        plan_path.write_bytes(plan_bytes)
        results_path.write_bytes(results_bytes)

        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(ir.main(["results-check", str(results_path), str(plan_path), "--result-root", str(self.result_root)]), 0)
        self.assertTrue(json.loads(stream.getvalue())["ok"])

        output = self.root / "rendered"
        stream = io.StringIO()
        with redirect_stdout(stream):
            self.assertEqual(ir.main(["render-sheets", str(results_path), str(plan_path), "--result-root", str(self.result_root), "--output-dir", str(output)]), 0)
        parsed = json.loads(stream.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["sheet_count"], 4)
        self.assertEqual(plan_path.read_bytes(), plan_bytes)
        self.assertEqual(results_path.read_bytes(), results_bytes)

    def test_schema_json_mirrors_contract(self) -> None:
        path = Path(__file__).resolve().parents[1] / "schemas" / "identity-lock-results.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["kind"]["const"], "identity-lock-results")
        text = json.dumps(schema, sort_keys=True)
        self.assertIn('"succeeded"', text)
        self.assertIn('"failed"', text)
        self.assertIn("control_sha256", text)
        self.assertIn("image_sha256", text)


if __name__ == "__main__":
    unittest.main()
