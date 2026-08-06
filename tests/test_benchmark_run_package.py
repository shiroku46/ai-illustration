from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from ai_illustration import benchmark_run_package as brp
from ai_illustration import model_benchmark as mb
from ai_illustration import model_install_manifest as mim

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "benchmark" / "model-benchmark-plan.v001.json"
INSTALL_PATH = ROOT / "benchmark" / "model-install-manifest.v001.json"


class BenchmarkRunPackageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
        self.install = json.loads(INSTALL_PATH.read_text(encoding="utf-8"))

    def test_exact_plan_structure_and_repository_bindings(self) -> None:
        self.assertEqual([], mb.validate_plan(self.plan))
        self.assertEqual([], brp.validate_cross_bindings(self.plan, self.install))
        matrix = mb.expand_matrix(self.plan)
        self.assertEqual(brp.EXPECTED_RUN_COUNT, len(matrix))
        self.assertEqual(144, len({row["run_id"] for row in matrix}))
        self.assertEqual(
            {101, 202, 303, 404, 505, 606, 707, 808},
            {row["seed"] for row in matrix},
        )
        self.assertEqual(
            {"animagine-xl", "illustrious-xl", "anima-aesthetic"},
            {row["model_family"] for row in matrix},
        )
        self.assertEqual(mb.REQUIRED_PROMPT_CASES, {row["prompt_case_id"] for row in matrix})

        art = self.plan["art_direction"]
        profile = json.loads((ROOT / art["profile_path"]).read_text(encoding="utf-8"))
        review = json.loads((ROOT / art["review_path"]).read_text(encoding="utf-8"))
        self.assertEqual(art["profile_sha256"], mb.canonical_sha256(profile))
        self.assertEqual(art["review_sha256"], mb.canonical_sha256(review))

        hardware = self.plan["hardware"]
        hardware_document = json.loads((ROOT / hardware["path"]).read_text(encoding="utf-8"))
        self.assertEqual(hardware["sha256"], mb.canonical_sha256(hardware_document))
        self.assertEqual(8, hardware_document["vram_gb"])
        self.assertEqual(32, hardware_document["ram_gb"])

        for model in self.plan["models"]:
            profile_document = json.loads((ROOT / model["profile_path"]).read_text(encoding="utf-8"))
            self.assertEqual(model["profile_sha256"], mb.canonical_sha256(profile_document))
            self.assertEqual(
                model["workflow_sha256"],
                __import__("hashlib").sha256((ROOT / model["workflow_path"]).read_bytes()).hexdigest(),
            )

    def test_builds_deterministic_exact_144_run_package(self) -> None:
        first_manifest, first_files = brp.build_package(
            self.plan,
            self.install,
            workspace_root=ROOT,
        )
        second_manifest, second_files = brp.build_package(
            copy.deepcopy(self.plan),
            copy.deepcopy(self.install),
            workspace_root=ROOT,
        )
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(first_files, second_files)
        self.assertEqual(144, first_manifest["run_count"])
        self.assertEqual(145, len(first_files))
        self.assertEqual(144, len(first_manifest["files"]))
        self.assertFalse(first_manifest["network_effect"])
        self.assertFalse(first_manifest["prompt_queued"])
        self.assertFalse(first_manifest["automatic_ranking"])
        self.assertFalse(first_manifest["automatic_selection"])
        self.assertEqual(
            {f"runs/{run['run_id']}.api.json" for run in first_manifest["runs"]},
            set(first_files) - {brp.PACKAGE_MANIFEST},
        )

    def test_generated_workflows_bind_run_and_exact_comfyui_settings(self) -> None:
        manifest, files = brp.build_package(
            self.plan,
            self.install,
            workspace_root=ROOT,
        )
        cases = {case["id"]: case for case in self.plan["prompt_cases"]}
        checked_families: set[str] = set()
        for run in manifest["runs"]:
            if run["seed"] != 101 or run["model_family"] in checked_families:
                continue
            checked_families.add(run["model_family"])
            workflow = json.loads(files[run["workflow_path"]])
            sampler = next(node for node in workflow.values() if node["class_type"] == "KSampler")
            latent = next(node for node in workflow.values() if node["class_type"] == "EmptyLatentImage")
            output = next(node for node in workflow.values() if node["class_type"] == "SaveImage")
            positive = next(
                node
                for node in workflow.values()
                if node["class_type"] == "CLIPTextEncode"
                and node.get("_meta", {}).get("title") == "Positive prompt"
            )
            negative = next(
                node
                for node in workflow.values()
                if node["class_type"] == "CLIPTextEncode"
                and node.get("_meta", {}).get("title") == "Negative prompt"
            )
            exact = run["exact_comfyui_settings"]
            self.assertEqual(run["seed"], sampler["inputs"]["seed"])
            self.assertEqual(exact["steps"], sampler["inputs"]["steps"])
            self.assertEqual(exact["cfg"], sampler["inputs"]["cfg"])
            self.assertEqual(exact["sampler"], sampler["inputs"]["sampler_name"])
            self.assertEqual(exact["scheduler"], sampler["inputs"]["scheduler"])
            self.assertEqual(exact["width"], latent["inputs"]["width"])
            self.assertEqual(exact["height"], latent["inputs"]["height"])
            prompt_case = cases[run["prompt_case_id"]]
            self.assertEqual(prompt_case["positive_contract"], positive["inputs"]["text"])
            self.assertEqual(prompt_case["negative_contract"], negative["inputs"]["text"])
            self.assertEqual(
                Path(run["image_path"]).with_suffix("").as_posix(),
                output["inputs"]["filename_prefix"],
            )
        self.assertEqual(
            {"animagine-xl", "illustrious-xl", "anima-aesthetic"},
            checked_families,
        )

    def test_cross_binding_tamper_fails_closed(self) -> None:
        changed = copy.deepcopy(self.install)
        changed["models"][0]["benchmark_settings"]["steps"] = 1
        diagnostics = brp.validate_cross_bindings(self.plan, changed)
        self.assertTrue(any(item["code"] == "SETTINGS_BINDING" for item in diagnostics))
        with self.assertRaises(brp.RunPackageError):
            brp.build_package(self.plan, changed, workspace_root=ROOT)

        changed = copy.deepcopy(self.plan)
        changed["models"][0]["workflow_sha256"] = "0" * 64
        diagnostics = brp.validate_cross_bindings(changed, self.install)
        self.assertTrue(any(item["code"] == "WORKFLOW_BINDING" for item in diagnostics))

    def test_publish_and_validate_detects_changed_extra_and_missing_files(self) -> None:
        manifest, files = brp.build_package(
            self.plan,
            self.install,
            workspace_root=ROOT,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            brp.publish_package(package, files)
            self.assertEqual([], brp.validate_package(package, self.plan, self.install, workspace_root=ROOT))
            run_file = next((package / "runs").glob("*.json"))
            original = run_file.read_bytes()
            run_file.write_bytes(original + b"\n")
            diagnostics = brp.validate_package(package, self.plan, self.install, workspace_root=ROOT)
            self.assertTrue(any(item["code"] == "PACKAGE_BYTES" for item in diagnostics))
            run_file.write_bytes(original)
            (package / "extra.txt").write_text("unexpected", encoding="utf-8")
            diagnostics = brp.validate_package(package, self.plan, self.install, workspace_root=ROOT)
            self.assertTrue(any(item["code"] == "PACKAGE_EXTRA" for item in diagnostics))
            (package / "extra.txt").unlink()
            run_file.unlink()
            diagnostics = brp.validate_package(package, self.plan, self.install, workspace_root=ROOT)
            self.assertTrue(any(item["code"] == "PACKAGE_MISSING" for item in diagnostics))
            self.assertEqual(144, manifest["run_count"])

    def test_source_has_no_network_execution_ranking_or_selection_surface(self) -> None:
        source = (ROOT / "src" / "ai_illustration" / "benchmark_run_package.py").read_text(encoding="utf-8")
        for prohibited in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "import urllib",
            "import requests",
            "import http.client",
            '"/prompt"',
            "queue_prompt",
            "automatic_score",
            "aesthetic_score",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
