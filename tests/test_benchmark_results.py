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
from unittest import mock
import zlib

from ai_illustration import art_direction as ad
from ai_illustration import benchmark_results as br
from ai_illustration import model_benchmark as mb


FAMILIES = ("animagine-xl", "illustrious-xl", "anima-aesthetic")


def _chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _png(width: int = 2, height: int = 2, color_type: int = 6) -> bytes:
    channels = {2: 3, 4: 2, 6: 4}[color_type]
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            if color_type == 2:
                rows.extend((20 + x, 40 + y, 80))
            elif color_type == 4:
                rows.extend((40 + x + y, 255))
            else:
                rows.extend((20 + x, 40 + y, 80, 255))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        br.PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"sRGB", b"\x00")
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
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


class BenchmarkResultsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.references = self.root / "references"
        self.result_root = self.root / "result-root"
        for path in (self.workspace, self.references, self.result_root):
            path.mkdir()
        for relative in ("art", "hardware", "models", "workflows"):
            (self.workspace / relative).mkdir()

        checksums: dict[str, str] = {}
        for role in ("boke", "tsukkomi"):
            payload = _png() + role.encode("ascii")
            # Art-direction references use a lightweight signature check, not the benchmark PNG parser.
            (self.references / f"{role}.png").write_bytes(payload)
            checksums[role] = hashlib.sha256(payload).hexdigest()
        self.art_profile = _art_profile(checksums)
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
        self.image_payload = _png()
        self.results = self._results()
        self.results_path = self.workspace / "results.json"
        self.results_path.write_text(json.dumps(self.results), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_json(self, relative: str, value: object) -> None:
        path = self.workspace / relative
        path.write_text(json.dumps(value), encoding="utf-8")

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

    def _entry(self, row: dict[str, object], index: int) -> dict[str, object]:
        common: dict[str, object] = {
            "run_id": row["run_id"],
            "state": "failed" if index == 0 else "succeeded",
            "model_family": row["model_family"],
            "model_profile_ref": row["model_profile_ref"],
            "model_profile_sha256": row["model_profile_sha256"],
            "workflow_sha256": row["workflow_sha256"],
            "seed": row["seed"],
            "prompt_case_id": row["prompt_case_id"],
            "role_scope": row["role_scope"],
            "settings": row["settings"],
            "elapsed_ms": 1500 + index,
            "peak_vram_mib": 7120,
        }
        if index == 0:
            common["error"] = {"code": "out-of-memory", "message": "fixture execution failure"}
            return common
        image_path = self.result_root / str(row["image_path"])
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(self.image_payload)
        common.update(
            {
                "image_path": row["image_path"],
                "image_sha256": hashlib.sha256(self.image_payload).hexdigest(),
                "width": 2,
                "height": 2,
            }
        )
        return common

    def _results(self) -> dict[str, object]:
        return {
            "kind": br.RESULTS_KIND,
            "schema_version": br.SCHEMA_VERSION,
            "id": "manzai-model-bakeoff-results",
            "version": "v001",
            "plan_ref": self.plan["id"],
            "plan_version": self.plan["version"],
            "plan_sha256": mb.canonical_sha256(self.plan),
            "results": [self._entry(row, index) for index, row in enumerate(self.matrix)],
        }

    def _validate(self, results: dict[str, object] | None = None):
        return br.validate_results(
            self.results if results is None else results,
            self.plan,
            workspace_root=self.workspace,
            reference_root=self.references,
            result_root=self.result_root,
        )

    def test_complete_bound_results_pass(self) -> None:
        diagnostics, images = self._validate()
        self.assertEqual(diagnostics, [])
        self.assertEqual(len(images), len(self.matrix) - 1)
        self.assertEqual(br.validate_document(self.results), [])

    def test_missing_extra_duplicate_and_mismatched_runs_fail(self) -> None:
        missing = copy.deepcopy(self.results)
        missing["results"] = missing["results"][:-1]
        self.assertTrue(any(item["code"] == "MISSING_RUNS" for item in self._validate(missing)[0]))

        extra = copy.deepcopy(self.results)
        invented = copy.deepcopy(extra["results"][-1])
        invented["run_id"] = "bench-invented"
        extra["results"].append(invented)
        self.assertTrue(any(item["code"] == "EXTRA_RUNS" for item in self._validate(extra)[0]))

        duplicate = copy.deepcopy(self.results)
        duplicate["results"].append(copy.deepcopy(duplicate["results"][-1]))
        self.assertTrue(any(item["code"] == "DUPLICATE_RUN" for item in br.validate_document(duplicate)))

        mismatched = copy.deepcopy(self.results)
        mismatched["results"][1]["seed"] = 999
        self.assertTrue(any(item["code"] == "RUN_BINDING" for item in self._validate(mismatched)[0]))

    def test_plan_binding_is_exact(self) -> None:
        stale = copy.deepcopy(self.results)
        stale["plan_sha256"] = "0" * 64
        self.assertTrue(any(item["code"] == "PLAN_BINDING" for item in self._validate(stale)[0]))

    def test_success_failure_conditionals_and_decision_fields(self) -> None:
        bad_success = copy.deepcopy(self.results)
        bad_success["results"][1].pop("image_path")
        self.assertTrue(any(item["code"] == "MISSING_FIELD" for item in br.validate_document(bad_success)))

        bad_failure = copy.deepcopy(self.results)
        bad_failure["results"][0]["image_path"] = "forbidden.png"
        self.assertTrue(any(item["code"] == "UNKNOWN_FIELD" for item in br.validate_document(bad_failure)))

        scored = copy.deepcopy(self.results)
        scored["results"][1]["aesthetic_score"] = 99
        codes = {item["code"] for item in br.validate_document(scored)}
        self.assertIn("AUTOMATIC_SELECTION_FORBIDDEN", codes)
        self.assertIn("UNKNOWN_FIELD", codes)

    def test_rgb_grayscale_alpha_and_rgba_png_are_supported(self) -> None:
        for color_type in (2, 4, 6):
            with self.subTest(color_type=color_type):
                self.assertEqual(br._parse_png(_png(color_type=color_type)), (2, 2))

    def test_png_path_missing_checksum_dimensions_and_trailing_data_fail(self) -> None:
        traversal = copy.deepcopy(self.results)
        traversal["results"][1]["image_path"] = "../escape.png"
        self.assertTrue(any(item["code"] == "UNSAFE_PATH" for item in br.validate_document(traversal)))

        missing = copy.deepcopy(self.results)
        source = self.result_root / str(missing["results"][1]["image_path"])
        source.unlink()
        self.assertTrue(any(item["code"] == "IMAGE_MISSING" for item in self._validate(missing)[0]))
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(self.image_payload)

        checksum = copy.deepcopy(self.results)
        checksum["results"][1]["image_sha256"] = "0" * 64
        self.assertTrue(any(item["code"] == "IMAGE_CHECKSUM" for item in self._validate(checksum)[0]))

        dimensions = copy.deepcopy(self.results)
        dimensions["results"][1]["width"] = 3
        self.assertTrue(any(item["code"] == "IMAGE_DIMENSIONS" for item in self._validate(dimensions)[0]))

        trailing = copy.deepcopy(self.results)
        source.write_bytes(self.image_payload + b"trailing")
        trailing["results"][1]["image_sha256"] = hashlib.sha256(self.image_payload + b"trailing").hexdigest()
        self.assertTrue(any(item["code"] == "PNG_TRAILING_DATA" for item in self._validate(trailing)[0]))

    def test_malformed_and_oversized_png_fail(self) -> None:
        source = self.result_root / str(self.results["results"][1]["image_path"])
        malformed = copy.deepcopy(self.results)
        source.write_bytes(b"not-png")
        malformed["results"][1]["image_sha256"] = hashlib.sha256(b"not-png").hexdigest()
        self.assertTrue(any(item["code"] == "PNG_SIGNATURE" for item in self._validate(malformed)[0]))

        source.write_bytes(self.image_payload)
        with mock.patch.object(br, "MAX_RESULT_IMAGE_BYTES", 4):
            self.assertTrue(any(item["code"] == "IMAGE_SIZE" for item in self._validate(self.results)[0]))

    def test_symlinked_image_is_rejected(self) -> None:
        entry = self.results["results"][1]
        source = self.result_root / str(entry["image_path"])
        target = source.parent / "target.png"
        target.write_bytes(self.image_payload)
        source.unlink()
        try:
            source.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        self.assertTrue(any(item["code"] == "PATH_SYMLINK" for item in self._validate()[0]))

    def test_contact_sheets_include_failures_and_embedded_only_images(self) -> None:
        diagnostics, images = self._validate()
        self.assertEqual(diagnostics, [])
        manifest, files = br.build_contact_sheet_package(self.results, images)
        self.assertEqual(len(manifest["sheets"]), len(FAMILIES) * len(mb.REQUIRED_PROMPT_CASES))
        first_group = (self.results["results"][0]["model_family"], self.results["results"][0]["prompt_case_id"])
        path = f"{br.SHEETS_DIR}/{first_group[0]}/{first_group[1]}.svg"
        svg = files[path].decode("utf-8")
        self.assertIn("EXECUTION FAILED", svg)
        self.assertIn("fixture execution failure", svg)
        self.assertIn("data:image/png;base64,", svg)
        for prohibited in ("<script", "http://", "https://", "<link", "<iframe", "@import"):
            self.assertNotIn(prohibited, svg)
        self.assertEqual(files[br.PACKAGE_MANIFEST], br.document_bytes(manifest))
        self.assertEqual(manifest["selection_policy"], "owner-only")

    def test_sheet_seed_order_paths_hashes_and_bytes_are_deterministic(self) -> None:
        diagnostics, images = self._validate()
        self.assertEqual(diagnostics, [])
        first_manifest, first_files = br.build_contact_sheet_package(self.results, images)
        reordered = copy.deepcopy(self.results)
        reordered["results"].reverse()
        second_manifest, second_files = br.build_contact_sheet_package(reordered, images)
        # Result-set hashes intentionally bind order; sheet bytes and paths remain semantic-order deterministic.
        sheet_files = {key: value for key, value in first_files.items() if key.endswith(".svg")}
        second_sheets = {key: value for key, value in second_files.items() if key.endswith(".svg")}
        self.assertEqual(sheet_files, second_sheets)
        for sheet in first_manifest["sheets"]:
            self.assertEqual(sheet["sha256"], hashlib.sha256(first_files[sheet["path"]]).hexdigest())
            seeds = [next(item["seed"] for item in self.results["results"] if item["run_id"] == run_id) for run_id in sheet["run_ids"]]
            self.assertEqual(seeds, sorted(seeds))
        self.assertNotEqual(first_manifest["results_sha256"], second_manifest["results_sha256"])

    def test_publish_requires_fresh_output_and_preserves_sources(self) -> None:
        diagnostics, images = self._validate()
        self.assertEqual(diagnostics, [])
        manifest, files = br.build_contact_sheet_package(self.results, images)
        source_before = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.result_root.rglob("*.png")
        }
        output = self.root / "contact-package"
        br.publish_package(output, files)
        self.assertTrue((output / br.PACKAGE_MANIFEST).is_file())
        self.assertEqual((output / br.PACKAGE_MANIFEST).read_bytes(), br.document_bytes(manifest))
        source_after = {
            path: (path.stat().st_size, path.stat().st_mtime_ns)
            for path in self.result_root.rglob("*.png")
        }
        self.assertEqual(source_before, source_after)
        with self.assertRaisesRegex(br.BenchmarkResultsError, "OUTPUT_EXISTS"):
            br.publish_package(output, files)

        second = self.root / "contact-package-2"
        br.publish_package(second, files)
        first_bytes = {
            path.relative_to(output).as_posix(): path.read_bytes()
            for path in output.rglob("*")
            if path.is_file()
        }
        second_bytes = {
            path.relative_to(second).as_posix(): path.read_bytes()
            for path in second.rglob("*")
            if path.is_file()
        }
        self.assertEqual(first_bytes, second_bytes)

    def test_publish_cleans_staging_after_failure(self) -> None:
        output = self.root / "failed-package"
        with mock.patch.object(Path, "write_bytes", side_effect=OSError("fixture write failure")):
            with self.assertRaises(OSError):
                br.publish_package(output, {"file.svg": b"x"})
        self.assertFalse(output.exists())
        self.assertFalse(any(path.name.startswith(".failed-package.staging-") for path in self.root.iterdir()))

    def test_cli_validation_and_rendering(self) -> None:
        check_args = [
            "results-check",
            str(self.results_path),
            str(self.plan_path),
            "--workspace-root",
            str(self.workspace),
            "--reference-root",
            str(self.references),
            "--result-root",
            str(self.result_root),
        ]
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(br.main(check_args), 0)
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["run_count"], 144)

        package = self.root / "cli-package"
        render_args = [
            "render-contact-sheets",
            str(self.results_path),
            str(self.plan_path),
            "--workspace-root",
            str(self.workspace),
            "--reference-root",
            str(self.references),
            "--result-root",
            str(self.result_root),
            "--output-dir",
            str(package),
        ]
        rendered = StringIO()
        with redirect_stdout(rendered):
            self.assertEqual(br.main(render_args), 0)
        render_result = json.loads(rendered.getvalue())
        self.assertTrue(render_result["ok"])
        self.assertEqual(render_result["sheet_count"], 18)
        self.assertTrue((package / br.PACKAGE_MANIFEST).is_file())

    def test_schema_is_strict_and_conditional(self) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "model-benchmark-results.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["kind"]["const"], br.RESULTS_KIND)
        entry = schema["$defs"]["result"]
        self.assertFalse(entry["additionalProperties"])
        self.assertEqual(set(entry["properties"]["state"]["enum"]), set(br.EXECUTION_STATES))
        for forbidden in br.FORBIDDEN_DECISION_TERMS:
            self.assertNotIn(forbidden, entry["properties"])

    def test_source_has_no_execution_or_network_surface(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "ai_illustration" / "benchmark_results.py").read_text(encoding="utf-8")
        for prohibited in (
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "http.client",
            "ComfyUI",
            "launch_browser",
            "aesthetic_score",
            "winner",
            "https://",
            "http://",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
