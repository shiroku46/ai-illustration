from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from ai_illustration import benchmark_readiness as br
from ai_illustration import model_install_manifest as mim
from ai_illustration.adapters.base import AdapterError

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "benchmark" / "model-install-manifest.v001.json"


class FakeClient:
    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:8188",
        limits: object | None = None,
        *,
        missing_class: str | None = None,
        missing_filename: str | None = None,
        malformed_loader: str | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.limits = limits
        self.missing_class = missing_class
        self.missing_filename = missing_filename
        self.malformed_loader = malformed_loader
        self.requested_routes: list[str] = []

    def system_stats(self) -> dict[str, object]:
        self.requested_routes.append("/system_stats")
        return {
            "system": {
                "comfyui_version": "0.30.2",
                "python_version": "3.12.13",
                "pytorch_version": "2.8.0",
            },
            "devices": [
                {
                    "name": "NVIDIA GeForce RTX 4060",
                    "type": "cuda",
                    "vram_total": 8 * 1024**3,
                    "vram_free": 7 * 1024**3,
                }
            ],
        }

    def object_info(self, node_class: str) -> dict[str, object]:
        self.requested_routes.append(f"/object_info/{node_class}")
        if node_class == self.missing_class:
            return {}
        choices = {
            "CheckpointLoaderSimple": (
                "ckpt_name",
                [
                    "animagine-xl-4.0-opt.safetensors",
                    "Illustrious-XL-v2.0.safetensors",
                ],
            ),
            "UNETLoader": (
                "unet_name",
                ["anima-aesthetic-v1.1.safetensors"],
            ),
            "CLIPLoader": (
                "clip_name",
                ["qwen_3_06b_base.safetensors"],
            ),
            "VAELoader": (
                "vae_name",
                ["qwen_image_vae.safetensors"],
            ),
        }
        required: dict[str, object] = {}
        if node_class in choices:
            input_name, values = choices[node_class]
            filtered = [
                value for value in values if value != self.missing_filename
            ]
            required[input_name] = [filtered]
            if node_class == self.malformed_loader:
                required[input_name] = ["not-a-choice-list"]
        return {
            node_class: {
                "input": {
                    "required": required,
                }
            }
        }


class BenchmarkReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        diagnostics, summaries = mim.validate_manifest(
            self.manifest,
            workspace_root=ROOT,
        )
        self.assertEqual([], diagnostics)
        self.summaries = summaries

    def _tiny_installation(
        self,
        root: Path,
        *,
        omit_artifact: str | None = None,
        corrupt_artifact: str | None = None,
    ) -> dict[str, object]:
        manifest = copy.deepcopy(self.manifest)
        for model in manifest["models"]:
            for artifact in model["artifacts"]:
                payload = f"fixture:{artifact['id']}".encode("utf-8")
                artifact["size_bytes"] = len(payload)
                artifact["sha256"] = hashlib.sha256(payload).hexdigest()
                if artifact["id"] == omit_artifact:
                    continue
                target = root / artifact["destination"] / artifact["filename"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(
                    payload + b"changed"
                    if artifact["id"] == corrupt_artifact
                    else payload
                )
        return manifest

    def _write_manifest(self, directory: Path, manifest: dict[str, object]) -> Path:
        path = directory / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_required_runtime_contract_covers_exact_classes_and_artifacts(self) -> None:
        diagnostics, classes, requirements = br.required_runtime_contract(
            self.manifest,
            self.summaries,
        )
        self.assertEqual([], diagnostics)
        self.assertEqual(
            {
                "CheckpointLoaderSimple",
                "CLIPLoader",
                "CLIPTextEncode",
                "EmptyLatentImage",
                "KSampler",
                "ModelSamplingAuraFlow",
                "SaveImage",
                "UNETLoader",
                "VAEDecode",
                "VAELoader",
            },
            set(classes),
        )
        self.assertEqual(5, len(requirements))
        self.assertEqual(
            {
                "animagine-xl-4.0-opt.safetensors",
                "Illustrious-XL-v2.0.safetensors",
                "anima-aesthetic-v1.1.safetensors",
                "qwen_3_06b_base.safetensors",
                "qwen_image_vae.safetensors",
            },
            {item["filename"] for item in requirements},
        )

    def test_exact_local_artifacts_pass_and_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._tiny_installation(root)
            before = {
                path: (path.stat().st_size, path.stat().st_mtime_ns)
                for path in root.rglob("*")
                if path.is_file()
            }
            diagnostics, artifacts = br.verify_local_artifacts(
                manifest,
                comfyui_root=root,
            )
            self.assertEqual([], diagnostics)
            self.assertEqual(5, len(artifacts))
            self.assertTrue(all(item["available"] for item in artifacts))
            self.assertTrue(all(item["size_ok"] for item in artifacts))
            self.assertTrue(all(item["sha256_ok"] for item in artifacts))
            after = {
                path: (path.stat().st_size, path.stat().st_mtime_ns)
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_missing_changed_and_symlinked_artifacts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._tiny_installation(
                root,
                omit_artifact="animagine-xl-4-0-opt-checkpoint",
                corrupt_artifact="anima-qwen-image-vae",
            )
            diagnostics, _ = br.verify_local_artifacts(
                manifest,
                comfyui_root=root,
            )
            codes = {item["code"] for item in diagnostics}
            self.assertIn("ARTIFACT_UNAVAILABLE", codes)
            self.assertIn("ARTIFACT_SIZE", codes)
            self.assertIn("ARTIFACT_SHA256", codes)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self._tiny_installation(root)
            artifact = manifest["models"][0]["artifacts"][0]
            path = root / artifact["destination"] / artifact["filename"]
            target = path.with_name("symlink-target.safetensors")
            target.write_bytes(path.read_bytes())
            path.unlink()
            try:
                path.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are unavailable")
            diagnostics, _ = br.verify_local_artifacts(
                manifest,
                comfyui_root=root,
            )
            self.assertTrue(
                any(item["code"] == "ARTIFACT_UNAVAILABLE" for item in diagnostics)
            )

    def test_runtime_success_checks_only_read_only_routes(self) -> None:
        diagnostics, classes, requirements = br.required_runtime_contract(
            self.manifest,
            self.summaries,
        )
        self.assertEqual([], diagnostics)
        client = FakeClient()
        runtime_diagnostics, runtime = br.evaluate_runtime(
            client,
            required_classes=classes,
            loader_requirements=requirements,
        )
        self.assertEqual([], runtime_diagnostics)
        self.assertEqual(classes, runtime["available_node_classes"])
        self.assertEqual([], runtime["missing_node_classes"])
        self.assertTrue(
            all(item["available"] for item in runtime["loader_requirements"])
        )
        self.assertEqual("/system_stats", client.requested_routes[0])
        self.assertEqual(
            {f"/object_info/{node_class}" for node_class in classes},
            set(client.requested_routes[1:]),
        )
        self.assertTrue(
            all(
                route == "/system_stats" or route.startswith("/object_info/")
                for route in client.requested_routes
            )
        )

    def test_missing_class_choice_and_malformed_choices_fail_closed(self) -> None:
        _, classes, requirements = br.required_runtime_contract(
            self.manifest,
            self.summaries,
        )
        missing = FakeClient(
            missing_class="KSampler",
            missing_filename="qwen_image_vae.safetensors",
        )
        diagnostics, _ = br.evaluate_runtime(
            missing,
            required_classes=classes,
            loader_requirements=requirements,
        )
        codes = {item["code"] for item in diagnostics}
        self.assertIn("NODE_CLASSES_MISSING", codes)
        self.assertIn("MODEL_CHOICE_MISSING", codes)

        malformed = FakeClient(malformed_loader="CheckpointLoaderSimple")
        with self.assertRaises(AdapterError) as context:
            br.evaluate_runtime(
                malformed,
                required_classes=classes,
                loader_requirements=requirements,
            )
        self.assertEqual("OBJECT_INFO_CHOICES", context.exception.code)

    def test_offline_and_runtime_preflight_are_gated_by_exact_local_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            comfyui = temp / "ComfyUI"
            comfyui.mkdir()
            manifest = self._tiny_installation(comfyui)
            manifest_path = self._write_manifest(temp, manifest)

            offline = br.run_offline_preflight(
                manifest_path,
                workspace_root=ROOT,
                comfyui_root=comfyui,
            )
            self.assertTrue(offline["ready"])
            self.assertFalse(offline["network_contacted"])
            self.assertFalse(offline["filesystem_mutated"])
            self.assertFalse(offline["external_process_started"])
            self.assertFalse(offline["prompt_queued"])

            created: list[FakeClient] = []

            def factory(endpoint: str, limits: object) -> FakeClient:
                client = FakeClient(endpoint, limits)
                created.append(client)
                return client

            runtime = br.run_runtime_preflight(
                manifest_path,
                workspace_root=ROOT,
                comfyui_root=comfyui,
                endpoint="http://127.0.0.1:8188",
                client_factory=factory,
            )
            self.assertTrue(runtime["ready"])
            self.assertTrue(runtime["network_contacted"])
            self.assertEqual(1, len(created))
            self.assertFalse(runtime["filesystem_mutated"])
            self.assertFalse(runtime["external_process_started"])
            self.assertFalse(runtime["prompt_queued"])
            self.assertNotIn("/prompt", runtime["requested_routes"])

            first_artifact = manifest["models"][0]["artifacts"][0]
            (comfyui / first_artifact["destination"] / first_artifact["filename"]).unlink()
            created.clear()
            blocked = br.run_runtime_preflight(
                manifest_path,
                workspace_root=ROOT,
                comfyui_root=comfyui,
                endpoint="http://127.0.0.1:8188",
                client_factory=factory,
            )
            self.assertFalse(blocked["ready"])
            self.assertFalse(blocked["network_contacted"])
            self.assertEqual([], created)

    def test_cli_is_deterministic_for_offline_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            comfyui = temp / "ComfyUI"
            comfyui.mkdir()
            manifest = self._tiny_installation(
                comfyui,
                omit_artifact="animagine-xl-4-0-opt-checkpoint",
            )
            manifest_path = self._write_manifest(temp, manifest)
            args = [
                "offline-check",
                str(manifest_path),
                "--workspace-root",
                str(ROOT),
                "--comfyui-root",
                str(comfyui),
            ]
            first = StringIO()
            with redirect_stdout(first):
                self.assertEqual(1, br.main(args))
            second = StringIO()
            with redirect_stdout(second):
                self.assertEqual(1, br.main(args))
            self.assertEqual(first.getvalue(), second.getvalue())
            parsed = json.loads(first.getvalue())
            self.assertFalse(parsed["ready"])
            self.assertFalse(parsed["network_contacted"])
            self.assertFalse(parsed["prompt_queued"])

    def test_source_has_no_mutation_execution_or_prompt_surface(self) -> None:
        source = (
            ROOT / "src" / "ai_illustration" / "benchmark_readiness.py"
        ).read_text(encoding="utf-8")
        for prohibited in (
            "import subprocess",
            "from subprocess",
            "write_text(",
            "write_bytes(",
            "mkdir(",
            "os.replace(",
            "shutil.",
            "webbrowser",
            '"/prompt"',
            '"/history"',
            '"/view"',
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
