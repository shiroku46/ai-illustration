from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from ai_illustration import benchmark_workflow as bw

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads(
    (ROOT / "benchmark" / "model-install-manifest.v001.json").read_text(encoding="utf-8")
)


class BenchmarkWorkflowTest(unittest.TestCase):
    def _model(self, family: str) -> dict[str, object]:
        return copy.deepcopy(
            next(model for model in MANIFEST["models"] if model["family"] == family)
        )

    def _payload(self, model: dict[str, object]) -> bytes:
        return (ROOT / model["workflow"]["path"]).read_bytes()

    def test_real_workflows_match_exact_manifest_settings(self) -> None:
        expected = {
            "animagine-xl": (28, 5.0, "euler_ancestral", "normal", 7),
            "illustrious-xl": (28, 5.0, "euler_ancestral", "normal", 7),
            "anima-aesthetic": (40, 4.5, "er_sde", "simple", 10),
        }
        for family, values in expected.items():
            with self.subTest(family=family):
                model = self._model(family)
                diagnostics, summary = bw.validate_workflow_bytes(
                    self._payload(model), model
                )
                self.assertEqual([], diagnostics)
                steps, cfg, sampler, scheduler, nodes = values
                self.assertEqual(101, summary["seed"])
                self.assertEqual(steps, summary["steps"])
                self.assertEqual(cfg, summary["cfg"])
                self.assertEqual(sampler, summary["sampler"])
                self.assertEqual(scheduler, summary["scheduler"])
                self.assertEqual(nodes, summary["node_count"])
                self.assertEqual(model["workflow"]["sha256"], summary["sha256"])

    def test_changed_settings_and_model_names_fail_closed(self) -> None:
        model = self._model("animagine-xl")
        workflow = json.loads(self._payload(model))
        sampler = next(
            node for node in workflow.values() if node["class_type"] == "KSampler"
        )
        sampler["inputs"]["steps"] = 1
        loader = next(
            node
            for node in workflow.values()
            if node["class_type"] == "CheckpointLoaderSimple"
        )
        loader["inputs"]["ckpt_name"] = "wrong.safetensors"
        payload = json.dumps(workflow, sort_keys=True).encode("utf-8")
        diagnostics, _ = bw.validate_workflow_bytes(payload, model)
        codes = {item["code"] for item in diagnostics}
        self.assertIn("WORKFLOW_SETTING", codes)
        self.assertIn("WORKFLOW_MODEL", codes)

    def test_anima_requires_split_loaders_shift_and_simple_scheduler(self) -> None:
        model = self._model("anima-aesthetic")
        workflow = json.loads(self._payload(model))
        sampler = next(
            node for node in workflow.values() if node["class_type"] == "KSampler"
        )
        sampler["inputs"]["scheduler"] = "normal"
        sampling = next(
            node
            for node in workflow.values()
            if node["class_type"] == "ModelSamplingAuraFlow"
        )
        sampling["inputs"]["shift"] = 1.0
        clip = next(
            node for node in workflow.values() if node["class_type"] == "CLIPLoader"
        )
        clip["inputs"]["clip_name"] = "wrong.safetensors"
        diagnostics, _ = bw.validate_workflow_bytes(
            json.dumps(workflow, sort_keys=True).encode("utf-8"), model
        )
        codes = {item["code"] for item in diagnostics}
        self.assertIn("WORKFLOW_SETTING", codes)
        self.assertIn("WORKFLOW_MODEL", codes)

    def test_missing_nodes_links_and_prompt_quality_fail_closed(self) -> None:
        model = self._model("illustrious-xl")
        workflow = json.loads(self._payload(model))
        output_id = next(
            node_id
            for node_id, node in workflow.items()
            if node["class_type"] == "SaveImage"
        )
        del workflow[output_id]
        sampler = next(
            node for node in workflow.values() if node["class_type"] == "KSampler"
        )
        sampler["inputs"]["model"] = ["missing", 0]
        text = next(
            node
            for node in workflow.values()
            if node["class_type"] == "CLIPTextEncode"
        )
        text["inputs"]["text"] = ""
        diagnostics, _ = bw.validate_workflow_bytes(
            json.dumps(workflow, sort_keys=True).encode("utf-8"), model
        )
        codes = {item["code"] for item in diagnostics}
        self.assertIn("WORKFLOW_CLASS_MISSING", codes)
        self.assertIn("WORKFLOW_NODE_COUNT", codes)
        self.assertIn("WORKFLOW_LINK", codes)
        self.assertIn("WORKFLOW_PROMPT", codes)

    def test_credentials_are_rejected(self) -> None:
        model = self._model("animagine-xl")
        workflow = json.loads(self._payload(model))
        workflow["1"]["inputs"]["api_key"] = "hf_example"
        diagnostics, _ = bw.validate_workflow_bytes(
            json.dumps(workflow, sort_keys=True).encode("utf-8"), model
        )
        self.assertTrue(any(item["code"] == "WORKFLOW_SECRET" for item in diagnostics))

    def test_source_has_no_execution_network_or_mutation_surface(self) -> None:
        source = (
            ROOT / "src" / "ai_illustration" / "benchmark_workflow.py"
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
