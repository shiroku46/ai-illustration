from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
from io import StringIO
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from ai_illustration import art_direction as ad
from ai_illustration import benchmark_results as br
from ai_illustration import benchmark_review as rv
from ai_illustration import model_benchmark as mb


FAMILIES = ("animagine-xl", "illustrious-xl", "anima-aesthetic")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
    raw = bytes([0, 20, 40, 60, 255, 21, 40, 60, 255]) * 2
    return (
        br.PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"sRGB", b"\x00")
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


def _role(role: str) -> dict[str, object]:
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
        "roles": [_role("boke"), _role("tsukkomi")],
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


def _art_review(profile: dict[str, object]) -> dict[str, object]:
    review: dict[str, object] = {
        "kind": ad.REVIEW_KIND,
        "schema_version": ad.SCHEMA_VERSION,
        "id": "placeholder",
        "profile_ref": profile["id"],
        "profile_version": profile["version"],
        "profile_sha256": ad.profile_sha256(profile),
        "decision": "approve",
        "reviewer": "owner",
        "timestamp": "2026-08-06T03:00:00Z",
        "observations": ["approved for benchmark evidence"],
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
    return [
        {
            "id": case_id,
            "role_scope": "two-character-secondary" if case_id == mb.SECONDARY_CASE else "single-role",
            "positive_contract": f"apply approved art direction for {case_id}",
            "negative_contract": "exclude approved anti-goals and anatomy failures",
            "crop": "face" if "close-up" in case_id else "full",
            "pose": "neutral" if case_id == "front-full-body-neutral" else "dynamic",
            "expression": "expressive" if "expressive" in case_id else "neutral",
        }
        for case_id in sorted(mb.REQUIRED_PROMPT_CASES)
    ]


class BenchmarkReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.references = self.root / "references"
        self.result_root = self.root / "results-root"
        self.package_root = self.root / "contact-package"
        for path in (self.workspace, self.references, self.result_root):
            path.mkdir()
        for relative in ("art", "hardware", "models", "workflows"):
            (self.workspace / relative).mkdir()

        reference_checksums: dict[str, str] = {}
        for role in ("boke", "tsukkomi"):
            payload = _png() + role.encode("ascii")
            (self.references / f"{role}.png").write_bytes(payload)
            reference_checksums[role] = hashlib.sha256(payload).hexdigest()
        self.art_profile = _art_profile(reference_checksums)
        self.art_review = _art_review(self.art_profile)
        self.hardware = _hardware()
        self._write_json("art/profile.json", self.art_profile)
        self._write_json("art/review.json", self.art_review)
        self._write_json("hardware/owner.json", self.hardware)

        self.profiles: dict[str, dict[str, object]] = {}
        self.workflows: dict[str, bytes] = {}
        for family in FAMILIES:
            profile = _model_profile(family)
            workflow = json.dumps(
                {"nodes": {"checkpoint": family, "sampler": "euler-a"}},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            self.profiles[family] = profile
            self.workflows[family] = workflow
            self._write_json(f"models/{family}.json", profile)
            (self.workspace / "workflows" / f"{family}.json").write_bytes(workflow)

        self.plan = self._plan()
        self.plan_path = self.workspace / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")
        self.matrix = mb.expand_matrix(self.plan)
        self.accepted_rows = self._accepted_rows()
        self.results = self._results()
        self.results_path = self.workspace / "results.json"
        self.results_path.write_text(json.dumps(self.results), encoding="utf-8")

        diagnostics, images = br.validate_results(
            self.results,
            self.plan,
            workspace_root=self.workspace,
            reference_root=self.references,
            result_root=self.result_root,
        )
        self.assertEqual(diagnostics, [])
        self.package_manifest, files = br.build_contact_sheet_package(self.results, images)
        br.publish_package(self.package_root, files)
        self.review = self._review()
        self.review_path = self.workspace / "review.json"
        self.review_path.write_text(json.dumps(self.review), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_json(self, relative: str, value: object) -> None:
        (self.workspace / relative).write_text(json.dumps(value), encoding="utf-8")

    def _plan(self) -> dict[str, object]:
        models = []
        for index, family in enumerate(FAMILIES):
            profile = self.profiles[family]
            workflow = self.workflows[family]
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
                    "evidence_note": "reviewed primary model guidance",
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

    def _accepted_rows(self) -> list[dict[str, object]]:
        family = FAMILIES[0]
        targets = {
            (101, "front-full-body-neutral"),
            (202, "three-quarter-readable-hands"),
            (303, "expressive-face-close-up"),
            (404, "clothing-detail-stress"),
        }
        return [
            row
            for row in self.matrix
            if row["model_family"] == family
            and (row["seed"], row["prompt_case_id"]) in targets
        ]

    def _results(self) -> dict[str, object]:
        payload = _png()
        accepted_ids = {row["run_id"] for row in self.accepted_rows}
        entries = []
        for index, row in enumerate(self.matrix):
            common: dict[str, object] = {
                "run_id": row["run_id"],
                "state": "succeeded" if row["run_id"] in accepted_ids else "failed",
                "model_family": row["model_family"],
                "model_profile_ref": row["model_profile_ref"],
                "model_profile_sha256": row["model_profile_sha256"],
                "workflow_sha256": row["workflow_sha256"],
                "seed": row["seed"],
                "prompt_case_id": row["prompt_case_id"],
                "role_scope": row["role_scope"],
                "settings": row["settings"],
                "elapsed_ms": 1000 + index,
                "peak_vram_mib": 7100,
            }
            if common["state"] == "succeeded":
                path = self.result_root / str(row["image_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                common.update(
                    {
                        "image_path": row["image_path"],
                        "image_sha256": hashlib.sha256(payload).hexdigest(),
                        "width": 2,
                        "height": 2,
                    }
                )
            else:
                common["error"] = {
                    "code": "fixture-rejected",
                    "message": "fixture execution or quality failure",
                }
            entries.append(common)
        return {
            "kind": br.RESULTS_KIND,
            "schema_version": br.SCHEMA_VERSION,
            "id": "manzai-model-bakeoff-results",
            "version": "v001",
            "plan_ref": self.plan["id"],
            "plan_version": self.plan["version"],
            "plan_sha256": mb.canonical_sha256(self.plan),
            "results": entries,
        }

    def _review(self, decision: str = "select_model") -> dict[str, object]:
        model = self.plan["models"][0]
        review: dict[str, object] = {
            "kind": rv.REVIEW_KIND,
            "schema_version": rv.SCHEMA_VERSION,
            "id": "placeholder",
            "plan_ref": self.plan["id"],
            "plan_version": self.plan["version"],
            "plan_sha256": mb.canonical_sha256(self.plan),
            "results_ref": self.results["id"],
            "results_version": self.results["version"],
            "results_sha256": br.result_set_sha256(self.results),
            "package_ref": self.package_manifest["id"],
            "package_sha256": hashlib.sha256(br.document_bytes(self.package_manifest)).hexdigest(),
            "reviewer": "owner",
            "timestamp": "2026-08-06T04:00:00Z",
            "decision": decision,
            "accepted_run_ids": [row["run_id"] for row in self.accepted_rows] if decision == "select_model" else [],
            "rejected_run_ids": [self.matrix[-1]["run_id"]],
            "hard_fail_categories": ["generic_ai_style"],
            "observations": ["multiple seeds preserve the approved art direction"],
        }
        if decision == "select_model":
            review["selected_model"] = {
                "family": model["family"],
                "profile_ref": f"{model['profile_id']}@{model['profile_version']}",
                "profile_sha256": model["profile_sha256"],
                "workflow_sha256": model["workflow_sha256"],
            }
        review["id"] = rv.expected_review_id(review)
        return review

    def _validate(self, review: dict[str, object] | None = None):
        return rv.validate_review(
            self.review if review is None else review,
            self.results,
            self.plan,
            workspace_root=self.workspace,
            reference_root=self.references,
            result_root=self.result_root,
            package_root=self.package_root,
        )

    def test_valid_owner_selection_returns_exact_lock(self) -> None:
        diagnostics, selected = self._validate()
        self.assertEqual(diagnostics, [])
        self.assertEqual(selected["family"], FAMILIES[0])
        self.assertEqual(selected["profile_ref"], self.review["selected_model"]["profile_ref"])
        self.assertEqual(selected["benchmark_review_id"], self.review["id"])
        self.assertEqual(rv.validate_review_document(self.review), [])

    def test_review_id_is_deterministic_and_notes_do_not_change_identity(self) -> None:
        reordered = copy.deepcopy(self.review)
        reordered["accepted_run_ids"].reverse()
        reordered["observations"].append("second observation")
        reordered["observations"].reverse()
        first_with_second = copy.deepcopy(self.review)
        first_with_second["observations"].append("second observation")
        self.assertEqual(rv.expected_review_id(reordered), rv.expected_review_id(first_with_second))

        with_notes = copy.deepcopy(self.review)
        with_notes["notes"] = "free-form context"
        self.assertEqual(rv.expected_review_id(with_notes), self.review["id"])

    def test_exact_evidence_bindings_are_required(self) -> None:
        for field in ("plan_sha256", "results_sha256", "package_sha256"):
            with self.subTest(field=field):
                review = copy.deepcopy(self.review)
                review[field] = "0" * 64
                review["id"] = rv.expected_review_id(review)
                diagnostics, selected = self._validate(review)
                self.assertIsNone(selected)
                self.assertTrue(any(item["code"].endswith("BINDING") for item in diagnostics))

    def test_package_manifest_and_svg_bytes_are_verified(self) -> None:
        manifest = self.package_root / br.PACKAGE_MANIFEST
        manifest.write_text("{}", encoding="utf-8")
        diagnostics, _ = self._validate()
        self.assertTrue(any(item["code"] in {"PACKAGE_BYTES", "PACKAGE_MANIFEST_BINDING"} for item in diagnostics))

    def test_missing_extra_and_changed_package_files_fail(self) -> None:
        svg = next(self.package_root.rglob("*.svg"))
        svg.unlink()
        self.assertTrue(any(item["code"] == "PACKAGE_MISSING" for item in self._validate()[0]))

    def test_extra_package_file_fails(self) -> None:
        (self.package_root / "extra.txt").write_text("unexpected", encoding="utf-8")
        self.assertTrue(any(item["code"] == "PACKAGE_EXTRA" for item in self._validate()[0]))

    def test_changed_svg_fails(self) -> None:
        svg = next(self.package_root.rglob("*.svg"))
        svg.write_text("<svg/>", encoding="utf-8")
        codes = {item["code"] for item in self._validate()[0]}
        self.assertIn("PACKAGE_BYTES", codes)
        self.assertIn("PACKAGE_CHECKSUM", codes)

    def test_symlinked_package_file_fails(self) -> None:
        svg = next(self.package_root.rglob("*.svg"))
        target = svg.parent / "target.svg"
        target.write_bytes(svg.read_bytes())
        svg.unlink()
        try:
            svg.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        self.assertTrue(any(item["code"] == "PACKAGE_SYMLINK" for item in self._validate()[0]))

    def test_failed_unknown_duplicate_other_family_and_overlap_runs_fail(self) -> None:
        failed = copy.deepcopy(self.review)
        failed["accepted_run_ids"][0] = self.matrix[-1]["run_id"]
        failed["id"] = rv.expected_review_id(failed)
        self.assertTrue(any(item["code"] == "ACCEPTED_RUN_FAILED" for item in self._validate(failed)[0]))

        unknown = copy.deepcopy(self.review)
        unknown["accepted_run_ids"][0] = "bench-unknown"
        unknown["id"] = rv.expected_review_id(unknown)
        self.assertTrue(any(item["code"] == "UNKNOWN_RUN" for item in self._validate(unknown)[0]))

        duplicate = copy.deepcopy(self.review)
        duplicate["accepted_run_ids"].append(duplicate["accepted_run_ids"][0])
        duplicate["id"] = rv.expected_review_id(duplicate)
        self.assertTrue(any(item["code"] == "DUPLICATE_VALUE" for item in rv.validate_review_document(duplicate)))

        other_family = copy.deepcopy(self.review)
        other_success = copy.deepcopy(self.results["results"][0])
        other_success["run_id"] = self.review["accepted_run_ids"][0]
        # The exact result-set gate makes fabricated metadata stale before owner selection can use it.
        other_family["selected_model"]["family"] = FAMILIES[1]
        other_family["id"] = rv.expected_review_id(other_family)
        self.assertTrue(any(item["code"] in {"ACCEPTED_RUN_FAMILY", "SELECTED_MODEL_BINDING"} for item in self._validate(other_family)[0]))

        overlap = copy.deepcopy(self.review)
        overlap["rejected_run_ids"].append(overlap["accepted_run_ids"][0])
        overlap["id"] = rv.expected_review_id(overlap)
        self.assertTrue(any(item["code"] == "RUN_OVERLAP" for item in rv.validate_review_document(overlap)))

    def test_run_seed_case_and_required_case_diversity(self) -> None:
        count = copy.deepcopy(self.review)
        count["accepted_run_ids"] = count["accepted_run_ids"][:3]
        count["id"] = rv.expected_review_id(count)
        self.assertTrue(any(item["code"] == "ACCEPTED_RUN_COUNT" for item in rv.validate_review_document(count)))

        same_seed_rows = [
            entry
            for entry in self.results["results"]
            if entry["model_family"] == FAMILIES[0] and entry["seed"] == 101
        ][:4]
        same_seed = copy.deepcopy(self.review)
        same_seed["accepted_run_ids"] = [entry["run_id"] for entry in same_seed_rows]
        same_seed["id"] = rv.expected_review_id(same_seed)
        self.assertTrue(any(item["code"] == "ACCEPTED_RUN_FAILED" for item in self._validate(same_seed)[0]))

        missing_required = copy.deepcopy(self.review)
        missing_required["accepted_run_ids"] = [
            row["run_id"]
            for row in self.accepted_rows
            if row["prompt_case_id"] != "three-quarter-readable-hands"
        ]
        missing_required["accepted_run_ids"].append(missing_required["accepted_run_ids"][0])
        missing_required["id"] = rv.expected_review_id(missing_required)
        diagnostics = rv.validate_review_document(missing_required)
        self.assertTrue(any(item["code"] == "DUPLICATE_VALUE" for item in diagnostics))

    def test_selected_model_profile_and_workflow_must_match_plan(self) -> None:
        for field in ("profile_sha256", "workflow_sha256"):
            with self.subTest(field=field):
                review = copy.deepcopy(self.review)
                review["selected_model"][field] = "0" * 64
                review["id"] = rv.expected_review_id(review)
                self.assertTrue(any(item["code"] == "SELECTED_MODEL_BINDING" for item in self._validate(review)[0]))

    def test_nonselection_decisions_never_return_lock(self) -> None:
        for decision in ("reject_all", "needs_revision"):
            with self.subTest(decision=decision):
                review = self._review(decision)
                diagnostics, selected = self._validate(review)
                self.assertEqual(diagnostics, [])
                self.assertIsNone(selected)

                invalid = copy.deepcopy(review)
                invalid["accepted_run_ids"] = [self.accepted_rows[0]["run_id"]]
                invalid["selected_model"] = copy.deepcopy(self.review["selected_model"])
                invalid["id"] = rv.expected_review_id(invalid)
                codes = {item["code"] for item in rv.validate_review_document(invalid)}
                self.assertIn("ACCEPTED_RUNS_FORBIDDEN", codes)
                self.assertIn("SELECTED_MODEL_FORBIDDEN", codes)

    def test_hard_fail_vocabulary_and_automatic_fields_are_rejected(self) -> None:
        hard_fail = copy.deepcopy(self.review)
        hard_fail["hard_fail_categories"] = ["invented_failure"]
        hard_fail["id"] = rv.expected_review_id(hard_fail)
        self.assertTrue(any(item["code"] == "HARD_FAIL_CATEGORY" for item in rv.validate_review_document(hard_fail)))

        scored = copy.deepcopy(self.review)
        scored["aesthetic_score"] = 100
        scored["id"] = rv.expected_review_id(scored)
        codes = {item["code"] for item in rv.validate_review_document(scored)}
        self.assertIn("AUTOMATIC_SELECTION_FORBIDDEN", codes)
        self.assertIn("UNKNOWN_FIELD", codes)

    def test_cli_is_deterministic_and_read_only(self) -> None:
        before = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }
        args = [
            "review-check",
            str(self.review_path),
            str(self.results_path),
            str(self.plan_path),
            "--workspace-root",
            str(self.workspace),
            "--reference-root",
            str(self.references),
            "--result-root",
            str(self.result_root),
            "--package-root",
            str(self.package_root),
        ]
        first = StringIO()
        with redirect_stdout(first):
            self.assertEqual(rv.main(args), 0)
        parsed = json.loads(first.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["selected_model_lock"]["family"], FAMILIES[0])

        second = StringIO()
        with redirect_stdout(second):
            self.assertEqual(rv.main(args), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        after = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)

    def test_schema_is_strict_and_matches_contract(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "model-benchmark-review.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["kind"]["const"], rv.REVIEW_KIND)
        self.assertEqual(set(schema["properties"]["decision"]["enum"]), set(rv.DECISIONS))
        self.assertEqual(schema["allOf"][0]["then"]["properties"]["accepted_run_ids"]["minItems"], rv.MIN_ACCEPTED_RUNS)
        categories = set(schema["properties"]["hard_fail_categories"]["items"]["enum"])
        self.assertEqual(categories, set(rv.HARD_FAIL_CATEGORIES))
        for forbidden in rv.FORBIDDEN_DECISION_FIELDS:
            self.assertNotIn(forbidden, schema["properties"])

    def test_source_has_no_mutation_execution_or_network_surface(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "ai_illustration"
            / "benchmark_review.py"
        ).read_text(encoding="utf-8")
        for prohibited in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "import urllib",
            "import requests",
            "import http.client",
            "write_text(",
            "write_bytes(",
            "mkdir(",
            "os.replace(",
            "shutil.",
            "webbrowser",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
