from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_illustration.comfyui_smoke import (
    MANIFEST_FILE,
    check_bundle,
    inspect_workflow,
    prepare_bundle,
)


def sdxl_turbo_api_workflow() -> dict[str, object]:
    return {
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"batch_size": 1, "height": 512, "width": 512},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "clip": ["20", 1],
                "text": "beautiful landscape with a cute fennec fox",
            },
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["20", 1], "text": "text, watermark"},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["13", 0], "vae": ["20", 2]},
        },
        "13": {
            "class_type": "SamplerCustom",
            "inputs": {
                "add_noise": True,
                "cfg": 1.0,
                "latent_image": ["5", 0],
                "model": ["20", 0],
                "negative": ["7", 0],
                "noise_seed": 0,
                "positive": ["6", 0],
                "sampler": ["14", 0],
                "sigmas": ["22", 0],
            },
        },
        "14": {
            "class_type": "KSamplerSelect",
            "inputs": {"sampler_name": "euler_ancestral"},
        },
        "20": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "sd_xl_turbo_1.0_fp16.safetensors"},
        },
        "22": {
            "class_type": "SDTurboScheduler",
            "inputs": {"denoise": 1.0, "model": ["20", 0], "steps": 1},
        },
        "27": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]},
        },
    }


class SDXLTurboSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input_root = self.root / "input"
        self.output_root = self.root / "bundles"
        self.input_root.mkdir()
        self.workflow = self.input_root / "sdxlturbo-api.json"
        self.workflow.write_text(
            json.dumps(sdxl_turbo_api_workflow(), indent=2),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_official_sampler_custom_graph_is_traced_to_scalar_owners(self) -> None:
        report = inspect_workflow(self.workflow)
        self.assertTrue(report["ok"], report["diagnostics"])
        selection = report["selection"]
        self.assertEqual(selection["sampler_node_id"], "13")
        self.assertEqual(selection["sampler_class"], "SamplerCustom")
        self.assertEqual(selection["scheduler_node_id"], "22")
        self.assertEqual(selection["scheduler_class"], "SDTurboScheduler")
        self.assertEqual(selection["sampler_select_node_id"], "14")
        self.assertEqual(selection["sampler_select_class"], "KSamplerSelect")
        self.assertEqual(selection["checkpoint_node_id"], "20")
        self.assertEqual(selection["size_node_id"], "5")
        self.assertEqual(selection["positive_node_id"], "6")
        self.assertEqual(selection["negative_node_id"], "7")
        self.assertEqual(selection["output_node_ids"], ["27"])

        self.assertEqual(report["values"]["seed"], 0)
        self.assertEqual(report["values"]["steps"], 1)
        self.assertEqual(report["values"]["width"], 512)
        self.assertEqual(report["values"]["height"], 512)
        bindings = report["bindings"]
        self.assertEqual(
            bindings["seed"],
            {"node_id": "13", "input": "noise_seed", "source": "seed"},
        )
        self.assertEqual(bindings["steps"]["node_id"], "22")
        self.assertEqual(bindings["denoise"]["node_id"], "22")
        self.assertEqual(bindings["sampler_name"]["node_id"], "14")
        self.assertEqual(bindings["cfg"]["node_id"], "13")
        self.assertEqual(
            bindings["checkpoint_name"]["source"],
            "config.checkpoint_name",
        )

    def test_official_layout_prepares_and_reconstructs_approved_bundle(self) -> None:
        prepared = prepare_bundle(
            self.workflow,
            self.output_root,
            profile_state="approved",
            review_date="2026-08-05",
            tool_evidence_url="https://github.com/comfyanonymous/ComfyUI",
            model_evidence_url="https://huggingface.co/stabilityai/sdxl-turbo",
            confirm_tool_license=True,
            confirm_model_license=True,
            confirm_commercial_use=True,
            write=True,
        )
        self.assertTrue(prepared["ok"])
        self.assertTrue(prepared["execution_ready"])
        package = self.output_root / str(prepared["bundle_path"])
        checked = check_bundle(package / MANIFEST_FILE, self.output_root)
        self.assertTrue(checked["ok"])
        self.assertTrue(checked["execution_ready"])
        self.assertFalse(checked["network_contacted"])
        self.assertFalse(checked["external_process_started"])


if __name__ == "__main__":
    unittest.main()
