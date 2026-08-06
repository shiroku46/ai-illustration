from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ai_illustration import art_direction as ad
from ai_illustration import model_benchmark as mb


PNG = b"\x89PNG\r\n\x1a\nbenchmark-reference"
FAMILIES = ("animagine-xl", "illustrious-xl", "anima-aesthetic")


def _art_role(role: str) -> dict[str, object]:
    return {
        "role": role,
        "silhouette": f"distinct {role} silhouette",
        "body_ratio": "compact stylized proportions",
        "head_exaggeration": "slightly oversized",
        "hand_exaggeration": "readable enlarged hands",
        "foot_exaggeration": "stable simplified feet",
        "costume_construction": "explicit seams layers and closures",
        "palette": ["ink", f"{role}-accent", "paper"],
        "line_behavior": "uneven hand-drawn contour",
        "eye_design": "simple asymmetric eye construction",
        "shading_ceiling": "one flat shadow maximum",
        "front_full_body_neutral_target": "front neutral full-body",
        "background_isolation_target": "transparent or plain background",
        "identity_anchors": [f"{role} hair", f"{role} costume"],
        "prohibited_ai_traits": ["uniform polish", "generic face"],
    }


def _art_profile(checksums: dict[str, str]) -> dict[str, object]:
    return {
        "kind": ad.PROFILE_KIND,
        "schema_version": ad.SCHEMA_VERSION,
        "id": "manzai-art-direction",
        "version": "v001",
        "status": "reviewing",
        "roles": [_art_role("boke"), _art_role("tsukkomi")],
        "global_anti_goals": sorted(ad.REQUIRED_GLOBAL_ANTI_GOALS),
        "visual_references": [
            {
                "id": "boke-board",
                "role": "boke",
                "path": "boke.png",
                "media_type": "image/png",
                "sha256": checksums["boke"],
                "purpose": "owner rough board",
            },
            {
                "id": "tsukkomi-board",
                "role": "tsukkomi",
                "path": "tsukkomi.png",
                "media_type": "image/png",
                "sha256": checksums["tsukkomi"],
                "purpose": "owner rough board",
            },
        ],
    }


def _art_review(profile: dict[str, object], decision: str = "approve") -> dict[str, object]:
    review: dict[str, object] = {
        "kind": ad.REVIEW_KIND,
        "schema_version": ad.SCHEMA_VERSION,
        "id": "placeholder",
        "profile_ref": profile["id"],
        "profile_version": profile["version"],
        "profile_sha256": ad.profile_sha256(profile),
        "decision": decision,
        "reviewer": "owner",
        "timestamp": "2026-08-06T02:00:00Z",
        "observations": ["approved for model bake-off"],
    }
    review["id"] = ad.expected_review_id(review)
    return review


def _hardware() -> dict[str, object]:
    return {
        "kind": "hardware-profile",
        "schema_version": "1.0",
        "id": "owner-rtx4060",
        "operating_system": "windows",
        "ram_gb": 32,
        "vram_gb": 8,
        "runtime_types": ["pytorch"],
        "adapter_types": ["comfyui"],
    }


def _model_profile(family: str) -> dict[str, object]:
    return {
        "kind": "tool-profile",
        "schema_version": "1.0",
        "id": f"{family}-profile",
        "version": "v001",
        "profile_type": "model-configuration",
        "adapter_type": "comfyui",
        "runtime_type": "pytorch",
        "offline_capability": "yes",
        "deterministic_seed_support": True,
        "control_capabilities": ["text-to-image", "fixed-seed"],
        "minimum_vram_gb": 8,
        "minimum_ram_gb": 16,
        "supported_operating_systems": ["windows"],
        "install_state": "uninstalled",
        "evidence_references": [
            {
                "source_url": f"https://example.invalid/{family}",
                "retrieved_at": "2026-08-06",
                "claim": "primary model and license evidence reviewed",
            }
        ],
        "license_evidence_state": "approved",
        "commercial_use_review_state": "approved",
        "decision_state": "approved",
    }


def _prompt_cases() -> list[dict[str, object]]:
    values = []
    for case_id in sorted(mb.REQUIRED_PROMPT_CASES):
        values.append(
            {
                "id": case_id,
                "role_scope": "two-character-secondary" if case_id == mb.SECONDARY_CASE else "single-role",
                "positive_contract": f"apply approved art direction for {case_id}",
                "negative_contract": "exclude every approved anti-goal and anatomy failure",
                "crop": "full" if "close-up" not in case_id else "face",
                "pose": "neutral" if case_id == "front-full-body-neutral" else "dynamic",
                "expression": "expressive" if "expressive" in case_id else "neutral",
            }
        )
    return values


class ModelBenchmarkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.references = self.root / "references"
        self.workspace.mkdir()
        self.references.mkdir()
        (self.workspace / "art").mkdir()
        (self.workspace / "hardware").mkdir()
        (self.workspace / "models").mkdir()
        (self.workspace / "workflows").mkdir()

        checksums: dict[str, str] = {}
        for role in ("boke", "tsukkomi"):
            payload = PNG + role.encode("ascii")
            (self.references / f"{role}.png").write_bytes(payload)
            checksums[role] = hashlib.sha256(payload).hexdigest()
        self.art_profile = _art_profile(checksums)
        self.art_review = _art_review(self.art_profile)
        self.hardware = _hardware()
        self._write_json("art/profile.json", self.art_profile)
        self._write_json("art/review.json", self.art_review)
        self._write_json("hardware/owner.json", self.hardware)

        self.model_profiles: dict[str, dict[str, object]] = {}
        self.workflow_bytes: dict[str, bytes] = {}
        for index, family in enumerate(FAMILIES):
            profile = _model_profile(family)
            self.model_profiles[family] = profile
            self._write_json(f"models/{family}.json", profile)
            workflow = json.dumps(
                {"nodes": {"checkpoint": family, "sampler": "euler-a"}},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.workflow_bytes[family] = workflow
            (self.workspace / "workflows" / f"{family}.json").write_bytes(workflow)

        self.plan = self._make_plan()
        self.plan_path = self.workspace / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_json(self, relative: str, value: object) -> None:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def _make_plan(self) -> dict[str, object]:
        models = []
        for index, family in enumerate(FAMILIES):
            profile = self.model_profiles[family]
            workflow = self.workflow_bytes[family]
            models.append(
                {
                    "family": family,
                    "profile_path": f"models/{family}.json",
                    "profile_id": profile["id"],
                    "profile_version": profile["version"],
                    "profile_sha256": mb.canonical_sha256(profile),
                    "workflow_path": f"workflows/{family}.json",
                    "workflow_sha256": hashlib.sha256(workflow).hexdigest(),
                    "native_width": 1024 + index * 64,
                    "native_height": 1024,
                    "sampler": "euler-a",
                    "scheduler": "normal",
                    "steps": 25 + index,
                    "cfg": 5.0 + index,
                    "prompt_format": "tag-and-natural-language",
                    "evidence_note": "settings copied from the reviewed primary model guidance",
                }
            )
        return {
            "kind": mb.PLAN_KIND,
            "schema_version": mb.SCHEMA_VERSION,
            "id": "manzai-model-bakeoff",
            "version": "v001",
            "status": mb.PLAN_STATUS,
            "art_direction": {
                "profile_path": "art/profile.json",
                "profile_id": self.art_profile["id"],
                "profile_version": self.art_profile["version"],
                "profile_sha256": mb.canonical_sha256(self.art_profile),
                "review_path": "art/review.json",
                "review_id": self.art_review["id"],
                "review_sha256": mb.canonical_sha256(self.art_review),
            },
            "hardware": {
                "path": "hardware/owner.json",
                "id": self.hardware["id"],
                "sha256": mb.canonical_sha256(self.hardware),
            },
            "models": models,
            "seeds": [101, 202, 303, 404, 505, 606, 707, 808],
            "prompt_cases": _prompt_cases(),
            "output_root": "benchmark-output",
            "selection_policy": mb.SELECTION_POLICY,
        }

    def test_valid_plan_dependencies_and_matrix(self) -> None:
        self.assertEqual(mb.validate_plan(self.plan), [])
        self.assertEqual(
            mb.validate_dependencies(self.plan, self.workspace, self.references), []
        )
        matrix = mb.expand_matrix(self.plan)
        self.assertEqual(len(matrix), 3 * 8 * 6)
        self.assertTrue(all(row["run_id"].startswith("bench-") for row in matrix))
        self.assertTrue(all(row["image_path"].endswith(".png") for row in matrix))
        self.assertTrue(all(row["metadata_path"].endswith(".json") for row in matrix))

    def test_matrix_order_and_ids_are_deterministic(self) -> None:
        first = mb.expand_matrix(self.plan)
        reordered = copy.deepcopy(self.plan)
        reordered["models"].reverse()
        reordered["seeds"].reverse()
        reordered["prompt_cases"].reverse()
        second = mb.expand_matrix(reordered)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["model_family"], sorted(FAMILIES)[0])
        self.assertEqual(first[0]["seed"], 101)
        self.assertEqual(first[0]["prompt_case_id"], sorted(mb.REQUIRED_PROMPT_CASES)[0])

    def test_art_direction_binding_and_approval_fail_closed(self) -> None:
        stale = copy.deepcopy(self.plan)
        stale["art_direction"]["profile_sha256"] = "0" * 64
        self.assertTrue(
            any(
                item["code"] == "ART_BINDING"
                for item in mb.validate_dependencies(stale, self.workspace, self.references)
            )
        )

        rejected_review = _art_review(self.art_profile, "reject")
        self._write_json("art/review.json", rejected_review)
        rejected = copy.deepcopy(self.plan)
        rejected["art_direction"]["review_id"] = rejected_review["id"]
        rejected["art_direction"]["review_sha256"] = mb.canonical_sha256(rejected_review)
        diagnostics = mb.validate_dependencies(rejected, self.workspace, self.references)
        self.assertTrue(any(item["code"] == "NOT_APPROVED" for item in diagnostics))

    def test_hardware_and_model_bindings_fail_closed(self) -> None:
        stale = copy.deepcopy(self.plan)
        stale["hardware"]["sha256"] = "0" * 64
        self.assertTrue(
            any(
                item["code"] == "HARDWARE_BINDING"
                for item in mb.validate_dependencies(stale, self.workspace, self.references)
            )
        )

        bad_profile = copy.deepcopy(self.model_profiles[FAMILIES[0]])
        bad_profile["commercial_use_review_state"] = "reviewing"
        bad_profile["decision_state"] = "reviewing"
        self._write_json(f"models/{FAMILIES[0]}.json", bad_profile)
        bad = copy.deepcopy(self.plan)
        bad["models"][0]["profile_sha256"] = mb.canonical_sha256(bad_profile)
        diagnostics = mb.validate_dependencies(bad, self.workspace, self.references)
        self.assertTrue(any(item["code"] == "MODEL_APPROVAL" for item in diagnostics))

    def test_offline_seed_and_hardware_compatibility_are_required(self) -> None:
        family = FAMILIES[0]
        profile = copy.deepcopy(self.model_profiles[family])
        profile["offline_capability"] = "unknown"
        profile["deterministic_seed_support"] = False
        profile["minimum_vram_gb"] = 12
        self._write_json(f"models/{family}.json", profile)
        plan = copy.deepcopy(self.plan)
        plan["models"][0]["profile_sha256"] = mb.canonical_sha256(profile)
        codes = {
            item["code"]
            for item in mb.validate_dependencies(plan, self.workspace, self.references)
        }
        self.assertIn("MODEL_OFFLINE", codes)
        self.assertIn("MODEL_SEED", codes)
        self.assertIn("MODEL_COMPATIBILITY", codes)

    def test_model_family_and_seed_contract(self) -> None:
        too_few = copy.deepcopy(self.plan)
        too_few["models"] = too_few["models"][:2]
        self.assertTrue(any(item["code"] == "MODEL_COUNT" for item in mb.validate_plan(too_few)))

        duplicate = copy.deepcopy(self.plan)
        duplicate["models"][1]["family"] = duplicate["models"][0]["family"]
        self.assertTrue(any(item["code"] == "DUPLICATE_FAMILY" for item in mb.validate_plan(duplicate)))

        seeds = copy.deepcopy(self.plan)
        seeds["seeds"] = [1, 2, 3, 4, 5, 6, 7]
        self.assertTrue(any(item["code"] == "SEEDS" for item in mb.validate_plan(seeds)))
        seeds["seeds"] = [1, 2, 3, 4, 5, 6, 7, 7]
        self.assertTrue(any(item["code"] == "DUPLICATE_SEED" for item in mb.validate_plan(seeds)))

        override = copy.deepcopy(self.plan)
        override["models"][0]["seeds"] = [999]
        self.assertTrue(any(item["code"] == "UNKNOWN_FIELD" for item in mb.validate_plan(override)))

    def test_prompt_case_coverage_and_role_scope(self) -> None:
        missing = copy.deepcopy(self.plan)
        missing["prompt_cases"] = missing["prompt_cases"][:-1]
        self.assertTrue(any(item["code"] == "PROMPT_COVERAGE" for item in mb.validate_plan(missing)))

        wrong_scope = copy.deepcopy(self.plan)
        secondary = next(item for item in wrong_scope["prompt_cases"] if item["id"] == mb.SECONDARY_CASE)
        secondary["role_scope"] = "single-role"
        self.assertTrue(any(item["code"] == "ROLE_SCOPE" for item in mb.validate_plan(wrong_scope)))

    def test_model_specific_native_settings_are_preserved(self) -> None:
        matrix = mb.expand_matrix(self.plan)
        widths = {
            row["model_family"]: row["settings"]["width"]
            for row in matrix
        }
        expected = {
            model["family"]: model["native_width"]
            for model in self.plan["models"]
        }
        self.assertEqual(widths, expected)

    def test_workflow_path_json_secret_and_checksum_failures(self) -> None:
        traversal = copy.deepcopy(self.plan)
        traversal["models"][0]["workflow_path"] = "../workflow.json"
        self.assertTrue(any(item["code"] == "UNSAFE_PATH" for item in mb.validate_plan(traversal)))

        missing = copy.deepcopy(self.plan)
        missing["models"][0]["workflow_path"] = "workflows/missing.json"
        self.assertTrue(any(item["code"] == "FILE_MISSING" for item in mb.validate_dependencies(missing, self.workspace, self.references)))

        family = FAMILIES[0]
        workflow_path = self.workspace / "workflows" / f"{family}.json"
        workflow_path.write_text("not json", encoding="utf-8")
        invalid = copy.deepcopy(self.plan)
        invalid["models"][0]["workflow_sha256"] = hashlib.sha256(b"not json").hexdigest()
        self.assertTrue(any(item["code"] == "JSON" for item in mb.validate_dependencies(invalid, self.workspace, self.references)))

        secret_bytes = json.dumps({"password": "do-not-store"}).encode("utf-8")
        workflow_path.write_bytes(secret_bytes)
        secret = copy.deepcopy(self.plan)
        secret["models"][0]["workflow_sha256"] = hashlib.sha256(secret_bytes).hexdigest()
        self.assertTrue(any(item["code"] == "WORKFLOW_SECRET" for item in mb.validate_dependencies(secret, self.workspace, self.references)))

        workflow_path.write_bytes(self.workflow_bytes[family])
        mismatch = copy.deepcopy(self.plan)
        mismatch["models"][0]["workflow_sha256"] = "0" * 64
        self.assertTrue(any(item["code"] == "WORKFLOW_BINDING" for item in mb.validate_dependencies(mismatch, self.workspace, self.references)))

        with mock.patch.object(mb, "MAX_WORKFLOW_BYTES", 4):
            self.assertTrue(any(item["code"] == "FILE_SIZE" for item in mb.validate_dependencies(self.plan, self.workspace, self.references)))

    def test_symlinked_workflow_is_rejected(self) -> None:
        target = self.workspace / "workflows" / "target.json"
        target.write_bytes(b"{}")
        link = self.workspace / "workflows" / "link.json"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        plan = copy.deepcopy(self.plan)
        plan["models"][0]["workflow_path"] = "workflows/link.json"
        plan["models"][0]["workflow_sha256"] = hashlib.sha256(b"{}").hexdigest()
        self.assertTrue(any(item["code"] == "PATH_SYMLINK" for item in mb.validate_dependencies(plan, self.workspace, self.references)))

    def test_cli_is_deterministic_and_read_only(self) -> None:
        before = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }
        args = [
            "matrix",
            str(self.plan_path),
            "--workspace-root",
            str(self.workspace),
            "--reference-root",
            str(self.references),
        ]
        first = StringIO()
        with redirect_stdout(first):
            self.assertEqual(mb.main(args), 0)
        parsed = json.loads(first.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["matrix_count"], 144)

        second = StringIO()
        with redirect_stdout(second):
            self.assertEqual(mb.main(args), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        after = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_schema_is_strict_and_mirrors_contract(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "model-benchmark-plan.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["kind"]["const"], mb.PLAN_KIND)
        self.assertEqual(schema["properties"]["selection_policy"]["const"], mb.SELECTION_POLICY)
        self.assertEqual(schema["properties"]["seeds"]["minItems"], mb.MINIMUM_SEEDS)
        model_schema = schema["$defs"]["model"]
        self.assertFalse(model_schema["additionalProperties"])
        self.assertNotIn("seeds", model_schema["properties"])
        case_ids = set(schema["$defs"]["promptCase"]["properties"]["id"]["enum"])
        self.assertEqual(case_ids, set(mb.REQUIRED_PROMPT_CASES))

    def test_source_has_no_execution_or_mutation_surface(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "ai_illustration" / "model_benchmark.py").read_text(encoding="utf-8")
        for prohibited in (
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "http.client",
            "write_text(",
            "write_bytes(",
            "mkdir(",
            "ComfyUI",
            "rank_score",
            "winner",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
