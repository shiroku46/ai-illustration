from __future__ import annotations

from contextlib import redirect_stdout
import copy
from io import StringIO
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from ai_illustration import model_install_manifest as mim

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "benchmark" / "model-install-manifest.v001.json"
PROFILE_ROOT = ROOT / "benchmark" / "model-profiles"
WORKFLOW_ROOT = ROOT / "benchmark" / "workflows"


class ModelInstallManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_real_manifest_profiles_and_workflows_are_valid(self) -> None:
        diagnostics, models = mim.validate_manifest(self.manifest, workspace_root=ROOT)
        self.assertEqual([], diagnostics)
        self.assertEqual(
            ["anima-aesthetic", "animagine-xl", "illustrious-xl"],
            [model["family"] for model in models],
        )
        by_family = {model["family"]: model for model in models}
        self.assertEqual("evaluation-only", by_family["anima-aesthetic"]["benchmark_scope"])
        self.assertTrue(by_family["anima-aesthetic"]["eligibility"]["benchmark_eligible"])
        self.assertFalse(by_family["anima-aesthetic"]["eligibility"]["production_eligible"])
        self.assertTrue(by_family["anima-aesthetic"]["eligibility"]["commercial_output_eligible"])
        self.assertEqual("simple", by_family["anima-aesthetic"]["workflow_summary"]["scheduler"])
        for family in ("animagine-xl", "illustrious-xl"):
            self.assertEqual("production-candidate", by_family[family]["benchmark_scope"])
            self.assertTrue(by_family[family]["eligibility"]["production_eligible"])
            self.assertEqual("normal", by_family[family]["workflow_summary"]["scheduler"])
        for model in models:
            self.assertEqual("repository-template", model["workflow_status"])
            self.assertTrue(model["workflow_path"].startswith("benchmark/workflows/"))
            self.assertEqual(101, model["workflow_summary"]["seed"])
            self.assertEqual(1024, model["workflow_summary"]["width"])
            self.assertEqual(1024, model["workflow_summary"]["height"])

    def test_exact_artifact_checksums_and_destinations(self) -> None:
        expected = {
            "animagine-xl-4-0-opt-checkpoint": (
                "models/checkpoints",
                "6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac",
            ),
            "illustrious-xl-v2-0-stable-checkpoint": (
                "models/checkpoints",
                "c2a1a3eaa13d4c107dc7e00c3fe830cab427aa026362740ea094745b3422a331",
            ),
            "anima-aesthetic-v1-1-diffusion-model": (
                "models/diffusion_models",
                "3c1868387a3a1ff504bbb87c33678321965ead381fcf87afbd0264daa600c082",
            ),
            "anima-qwen-3-06b-text-encoder": (
                "models/text_encoders",
                "cd2a512003e2f9f3cd3c32a9c3573f820bb28c940f73c57b1ddaa983d9223eba",
            ),
            "anima-qwen-image-vae": (
                "models/vae",
                "a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f",
            ),
        }
        actual = {
            artifact["id"]: (artifact["destination"], artifact["sha256"])
            for model in self.manifest["models"]
            for artifact in model["artifacts"]
        }
        self.assertEqual(expected, actual)

    def test_exact_workflow_checksums_are_bound(self) -> None:
        expected = {
            "animagine-xl": "e45b7f2c8acf4f1e5167025d722075d068664ba98611be2efa7740fb93a6cfaf",
            "illustrious-xl": "eaab0b4bf5577488d8b6ac472ecfadb7ca00352d45570b9ce5bb860a099f0e9b",
            "anima-aesthetic": "bbb0ea8189fb7aba637117c7c1ea56dedb4a0510e9ab1932334d5b460335e248",
        }
        self.assertEqual(
            expected,
            {model["family"]: model["workflow"]["sha256"] for model in self.manifest["models"]},
        )

    def test_profile_change_and_scope_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "benchmark", root / "benchmark")
            changed = copy.deepcopy(self.manifest)
            changed["models"][0]["profile_sha256"] = "0" * 64
            diagnostics, _ = mim.validate_manifest(changed, workspace_root=root)
            self.assertTrue(any(item["code"] == "PROFILE_SHA_BINDING" for item in diagnostics))

            scope = copy.deepcopy(self.manifest)
            anima = next(model for model in scope["models"] if model["family"] == "anima-aesthetic")
            anima["benchmark_scope"] = "production-candidate"
            diagnostics, _ = mim.validate_manifest(scope, workspace_root=root)
            self.assertTrue(any(item["code"] == "SCOPE_PRODUCTION_MISMATCH" for item in diagnostics))

    def test_workflow_change_and_manifest_hash_mismatch_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "benchmark", root / "benchmark")
            path = root / "benchmark" / "workflows" / "animagine-xl.api.json"
            path.write_bytes(path.read_bytes() + b"\n")
            diagnostics, _ = mim.validate_manifest(self.manifest, workspace_root=root)
            self.assertTrue(any(item["code"] == "WORKFLOW_SHA_BINDING" for item in diagnostics))

            changed = copy.deepcopy(self.manifest)
            changed["models"][0]["workflow"]["sha256"] = "0" * 64
            diagnostics, _ = mim.validate_manifest(changed, workspace_root=ROOT)
            self.assertTrue(any(item["code"] == "WORKFLOW_SHA_BINDING" for item in diagnostics))

    def test_duplicates_and_unsafe_workflow_fail_closed(self) -> None:
        duplicate = copy.deepcopy(self.manifest)
        duplicate["models"][1]["family"] = duplicate["models"][0]["family"]
        duplicate["models"][1]["artifacts"][0]["id"] = duplicate["models"][0]["artifacts"][0]["id"]
        duplicate["models"][1]["artifacts"][0]["filename"] = duplicate["models"][0]["artifacts"][0]["filename"]
        diagnostics, _ = mim.validate_manifest(duplicate, workspace_root=ROOT)
        codes = {item["code"] for item in diagnostics}
        self.assertIn("DUPLICATE_FAMILY", codes)
        self.assertIn("DUPLICATE_ARTIFACT", codes)
        self.assertIn("DUPLICATE_INSTALL_TARGET", codes)

        unsafe = copy.deepcopy(self.manifest)
        unsafe["models"][0]["workflow"]["path"] = "../workflow.json"
        diagnostics, _ = mim.validate_manifest(unsafe, workspace_root=ROOT)
        self.assertTrue(any(item["code"] == "WORKFLOW_PATH" for item in diagnostics))

    def test_symlinked_profile_and_workflow_fail_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "benchmark", root / "benchmark")
            profile = root / "benchmark" / "model-profiles" / "animagine-xl-4-0-opt.v001.json"
            profile_target = root / "benchmark" / "model-profiles" / "animagine-target.json"
            profile_target.write_bytes(profile.read_bytes())
            profile.unlink()
            try:
                profile.symlink_to(profile_target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            diagnostics, _ = mim.validate_manifest(self.manifest, workspace_root=root)
            self.assertTrue(any(item["code"] == "DOCUMENT_READ" for item in diagnostics))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "benchmark", root / "benchmark")
            workflow = root / "benchmark" / "workflows" / "animagine-xl.api.json"
            workflow_target = root / "benchmark" / "workflows" / "animagine-target.api.json"
            workflow_target.write_bytes(workflow.read_bytes())
            workflow.unlink()
            try:
                workflow.symlink_to(workflow_target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            diagnostics, _ = mim.validate_manifest(self.manifest, workspace_root=root)
            self.assertTrue(any(item["code"] == "WORKFLOW_READ" for item in diagnostics))

    def test_cli_is_deterministic_and_read_only(self) -> None:
        observed = [
            MANIFEST_PATH,
            *sorted(PROFILE_ROOT.glob("*.json")),
            *sorted(WORKFLOW_ROOT.glob("*.json")),
        ]
        before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in observed}
        args = ["check", str(MANIFEST_PATH), "--workspace-root", str(ROOT)]
        first = StringIO()
        with redirect_stdout(first):
            self.assertEqual(0, mim.main(args))
        parsed = json.loads(first.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(
            "a8c2f9292e83f52fd30c839a905918c2e357abb6156b8b27fd7fa8f6feac10b0",
            parsed["manifest_sha256"],
        )

        second = StringIO()
        with redirect_stdout(second):
            self.assertEqual(0, mim.main(args))
        self.assertEqual(first.getvalue(), second.getvalue())
        after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in observed}
        self.assertEqual(before, after)

    def test_schema_matches_manifest_contract(self) -> None:
        schema = json.loads((ROOT / "schemas" / "model-install-manifest.schema.json").read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(mim.MANIFEST_KIND, schema["properties"]["kind"]["const"])
        self.assertEqual(3, schema["properties"]["models"]["minItems"])
        self.assertEqual(
            set(mim.BENCHMARK_SCOPES),
            set(schema["$defs"]["model"]["properties"]["benchmark_scope"]["enum"]),
        )
        self.assertEqual(
            set(mim.COMPONENT_DESTINATIONS),
            set(schema["$defs"]["artifact"]["properties"]["component"]["enum"]),
        )
        self.assertEqual(
            {"status", "path", "sha256"},
            set(schema["$defs"]["workflow"]["required"]),
        )
        self.assertEqual(
            "repository-template",
            schema["$defs"]["workflow"]["properties"]["status"]["const"],
        )

    def test_source_has_no_download_execution_or_mutation_surface(self) -> None:
        source = (ROOT / "src" / "ai_illustration" / "model_install_manifest.py").read_text(encoding="utf-8")
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
