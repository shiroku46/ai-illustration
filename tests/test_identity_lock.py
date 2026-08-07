from __future__ import annotations

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from ai_illustration import identity_lock as il
from ai_illustration.naming import canonical_json


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


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
            {
                "role": "boke",
                "candidate_id": "candidate-boke",
                "request_id": "request-boke",
                "image_sha256": SHA_D,
                "reference_path": "identities/boke.png",
            },
            {
                "role": "tsukkomi",
                "candidate_id": "candidate-tsukkomi",
                "request_id": "request-tsukkomi",
                "image_sha256": SHA_E,
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
                    {"pose": pose, "path": f"controls/{pose}.png", "sha256": SHA_F}
                    for pose in poses
                ],
            },
        ],
    }


class IdentityLockPlanTest(unittest.TestCase):
    def test_valid_minimum_plan_expands_to_36_deterministic_runs(self) -> None:
        plan = valid_plan()
        self.assertEqual(il.validate_plan(plan), [])
        first = il.expand_matrix(plan)
        second = il.expand_matrix(copy.deepcopy(plan))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 36)
        self.assertEqual(len({row["run_id"] for row in first}), 36)
        self.assertTrue(all(row["model_profile_sha256"] == SHA_A for row in first))
        self.assertTrue(all(row["workflow_sha256"] == SHA_B for row in first))
        boke = [row for row in first if row["role"] == "boke"]
        tsukkomi = [row for row in first if row["role"] == "tsukkomi"]
        self.assertTrue(all(row["identity_sha256"] == SHA_D for row in boke))
        self.assertTrue(all(row["identity_sha256"] == SHA_E for row in tsukkomi))
        pose_rows = [row for row in first if row["strategy_type"] == "reference-plus-pose"]
        self.assertTrue(all(row["control_sha256"] == SHA_F for row in pose_rows))
        baseline = [row for row in first if row["strategy_type"] == "reference-only"]
        self.assertTrue(all(row["control_sha256"] is None for row in baseline))
        self.assertTrue(all(row["output_path"].endswith(f"/{row['run_id']}.png") for row in first))

    def test_roles_are_exactly_boke_and_tsukkomi(self) -> None:
        for mutator in (
            lambda plan: plan["roles"].pop(),
            lambda plan: plan["roles"].__setitem__(1, copy.deepcopy(plan["roles"][0])),
            lambda plan: plan["roles"].append(copy.deepcopy(plan["roles"][0])),
        ):
            with self.subTest(mutator=mutator):
                plan = valid_plan()
                mutator(plan)
                self.assertIn("ROLE_COVERAGE", {item["code"] for item in il.validate_plan(plan)})

    def test_selected_model_requires_exact_production_eligible_lock(self) -> None:
        plan = valid_plan()
        plan["selected_model"]["production_eligible"] = False
        self.assertIn("PRODUCTION_ELIGIBILITY", {item["code"] for item in il.validate_plan(plan)})
        for field in ("profile_sha256", "workflow_sha256", "benchmark_review_sha256"):
            broken = valid_plan()
            broken["selected_model"][field] = "not-a-sha"
            self.assertIn("CHECKSUM", {item["code"] for item in il.validate_plan(broken)})

    def test_each_role_requires_exact_identity_and_safe_reference_path(self) -> None:
        for field in ("candidate_id", "request_id", "image_sha256", "reference_path"):
            plan = valid_plan()
            del plan["roles"][0][field]
            self.assertIn("MISSING_FIELD", {item["code"] for item in il.validate_plan(plan)})
        plan = valid_plan()
        plan["roles"][0]["reference_path"] = "../outside.png"
        self.assertIn("UNSAFE_PATH", {item["code"] for item in il.validate_plan(plan)})

    def test_shared_pose_and_expression_targets_require_three_unique_values(self) -> None:
        plan = valid_plan()
        plan["pose_targets"] = ["front", "side"]
        self.assertIn("TARGET_COUNT", {item["code"] for item in il.validate_plan(plan)})
        plan = valid_plan()
        plan["expression_targets"] = ["neutral", "neutral", "smile"]
        self.assertIn("DUPLICATE_VALUE", {item["code"] for item in il.validate_plan(plan)})

    def test_required_strategies_and_complete_pose_control_coverage(self) -> None:
        plan = valid_plan()
        plan["strategies"] = [plan["strategies"][0]]
        codes = {item["code"] for item in il.validate_plan(plan)}
        self.assertIn("STRATEGIES", codes)
        plan = valid_plan()
        plan["strategies"][1]["control_assets"].pop()
        self.assertIn("CONTROL_COVERAGE", {item["code"] for item in il.validate_plan(plan)})
        plan = valid_plan()
        plan["strategies"][1]["control_method"] = "magic-pose"
        self.assertIn("CONTROL_METHOD", {item["code"] for item in il.validate_plan(plan)})

    def test_optional_character_lora_is_fail_closed_on_provenance_and_license(self) -> None:
        plan = valid_plan()
        plan["strategies"].append(
            {
                "id": "curated-character-lora",
                "type": "character-lora",
                "dataset_manifest_sha256": SHA_A,
                "training_artifact_sha256": SHA_B,
                "training_config_sha256": SHA_C,
                "license_status": "approved",
                "provenance_status": "approved",
            }
        )
        self.assertEqual(il.validate_plan(plan), [])
        self.assertEqual(len(il.expand_matrix(plan)), 54)
        plan["strategies"][-1]["license_status"] = "reviewing"
        self.assertIn("LORA_LICENSE", {item["code"] for item in il.validate_plan(plan)})

    def test_automatic_selection_and_prompt_only_shortcuts_are_rejected_recursively(self) -> None:
        for key in ("identity_score", "winner", "recommendation", "model_override", "prompt_only_identity"):
            plan = valid_plan()
            plan["roles"][0][key] = "forbidden"
            self.assertIn(
                "AUTOMATIC_SELECTION_FORBIDDEN",
                {item["code"] for item in il.validate_plan(plan)},
            )

    def test_plan_sha_is_canonical_and_note_changes_are_visible(self) -> None:
        plan = valid_plan()
        expected = __import__("hashlib").sha256(canonical_json(plan)).hexdigest()
        self.assertEqual(il.plan_sha256(plan), expected)
        changed = copy.deepcopy(plan)
        changed["notes"] = "owner note"
        self.assertNotEqual(il.plan_sha256(plan), il.plan_sha256(changed))

    def test_cli_is_deterministic_and_read_only(self) -> None:
        plan = valid_plan()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "plan.json"
            original = json.dumps(plan, sort_keys=True).encode("utf-8")
            path.write_bytes(original)
            outputs: list[str] = []
            for command in ("plan-check", "matrix"):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    code = il.main([command, str(path)])
                self.assertEqual(code, 0)
                parsed = json.loads(stream.getvalue())
                self.assertTrue(parsed["ok"])
                if command == "matrix":
                    self.assertEqual(parsed["run_count"], 36)
                outputs.append(stream.getvalue())
            self.assertEqual(path.read_bytes(), original)
            again = io.StringIO()
            with redirect_stdout(again):
                self.assertEqual(il.main(["matrix", str(path)]), 0)
            self.assertEqual(outputs[1], again.getvalue())
            self.assertEqual(sorted(item.name for item in root.iterdir()), ["plan.json"])

    def test_schema_json_mirrors_required_contract(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "identity-lock-plan.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["kind"]["const"], "identity-lock-plan")
        self.assertEqual(schema["properties"]["status"]["const"], "prepared")
        self.assertEqual(schema["properties"]["pose_targets"]["minItems"], 3)
        self.assertEqual(schema["properties"]["expression_targets"]["minItems"], 3)
        self.assertEqual(schema["$defs"]["selectedModel"]["properties"]["production_eligible"]["const"], True)
        strategy_text = json.dumps(schema["$defs"]["strategy"], sort_keys=True)
        self.assertIn("reference-only", strategy_text)
        self.assertIn("reference-plus-pose", strategy_text)
        self.assertIn("character-lora", strategy_text)

    def test_source_exposes_no_execution_or_network_surface(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "ai_illustration" / "identity_lock.py").read_text(encoding="utf-8")
        for prohibited in (
            "subprocess",
            "urllib",
            "requests",
            "http.client",
            "socket",
            "ComfyUI",
            "open(",
            "write_text",
            "write_bytes",
            "mkdir(",
            "unlink(",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
