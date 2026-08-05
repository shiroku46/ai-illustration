from __future__ import annotations

import binascii
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from ai_illustration.adapters.base import AdapterError
from ai_illustration.adapters.comfyui_execution_common import MANIFEST_FILE, PLAN_FILE, json_bytes
from ai_illustration.adapters.comfyui_execution_package import (
    candidate_files,
    check_execution_package,
    execution_manifest,
)
from ai_illustration.comfyui_smoke_bundle import REQUEST_FILE, prepare_bundle
from ai_illustration.comfyui_smoke_check import check_bundle
from ai_illustration.models import Manifest
from ai_illustration.quality import TECHNICAL_CANDIDATE, TRANSPORT_SMOKE_OUTPUT
from ai_illustration.validation import validate_document


def chunk(name: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + name
        + payload
        + struct.pack(">I", binascii.crc32(name + payload) & 0xFFFFFFFF)
    )


def rgba_png(width: int = 8, height: int = 8) -> bytes:
    raw = b"".join(b"\x00" + b"\x22\x44\x66\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"sRGB", b"\x00")
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def workflow() -> dict[str, object]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "real.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "girl", "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 8, "height": 8, "batch_size": 1}},
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": 1,
                "steps": 4,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "smoke", "images": ["6", 0]}},
    }


def plan(output_intent: str | None) -> dict[str, object]:
    request: dict[str, object] = {"id": "request-quality"}
    if output_intent is not None:
        request["output_intent"] = output_intent
    return {
        "id": "plan-quality",
        "expected_width": 8,
        "expected_height": 8,
        "limits": {"max_png_bytes": 1024 * 1024},
        "output_node_ids": ["7"],
        "request": request,
        "endpoint": "http://127.0.0.1:8188",
        "workflow": {"sha256": "2" * 64, "bound_sha256": "3" * 64},
        "tool_profile": {"id": "comfyui-local-api"},
        "model_profile": {"id": "model-one"},
        "execution_profile": {"id": "execution-profile"},
    }


def descriptor() -> dict[str, str]:
    return {
        "node_id": "7",
        "filename": "image.png",
        "subfolder": "",
        "type": "output",
    }


class QualityPackagingTests(unittest.TestCase):
    def test_smoke_bundle_request_is_transport_only_and_reconstructable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_root = root / "input"
            output_root = root / "bundles"
            input_root.mkdir()
            workflow_path = input_root / "workflow.json"
            workflow_path.write_text(json.dumps(workflow()), encoding="utf-8")
            result = prepare_bundle(
                workflow_path,
                output_root,
                profile_state="reviewing",
                review_date="2026-08-06",
                write=True,
            )
            package = output_root / result["bundle_path"]
            request = json.loads((package / REQUEST_FILE).read_text(encoding="utf-8"))
            self.assertEqual(request["output_intent"], "transport-smoke")
            self.assertTrue(check_bundle(package / MANIFEST_FILE, output_root)["ok"])

    def test_smoke_normal_and_legacy_sidecars_keep_technical_status_separate(self) -> None:
        payload = rgba_png()
        smoke, smoke_bytes, _, _ = candidate_files(
            plan("transport-smoke"), "prompt", descriptor(), payload, 0
        )
        normal, normal_bytes, _, _ = candidate_files(
            plan("candidate"), "prompt", descriptor(), payload, 0
        )
        legacy, legacy_bytes, _, _ = candidate_files(
            plan(None), "prompt", descriptor(), payload, 0
        )
        self.assertEqual(smoke["status"], "technically_valid")
        self.assertEqual(normal["status"], "technically_valid")
        self.assertEqual(legacy["status"], "technically_valid")
        self.assertEqual(smoke["quality_stage"], TRANSPORT_SMOKE_OUTPUT)
        self.assertEqual(normal["quality_stage"], TECHNICAL_CANDIDATE)
        self.assertEqual(legacy["quality_stage"], TECHNICAL_CANDIDATE)
        self.assertEqual(json.loads(smoke_bytes)["quality_stage"], TRANSPORT_SMOKE_OUTPUT)
        self.assertEqual(json.loads(normal_bytes)["quality_stage"], TECHNICAL_CANDIDATE)
        self.assertEqual(json.loads(legacy_bytes)["quality_stage"], TECHNICAL_CANDIDATE)

    def test_unknown_output_intent_fails_packaging(self) -> None:
        with self.assertRaises(AdapterError) as raised:
            candidate_files(plan("transport-smok"), "prompt", descriptor(), rgba_png(), 0)
        self.assertEqual(raised.exception.code, "OUTPUT_INTENT")

    def test_candidate_validation_accepts_known_and_legacy_stages_only(self) -> None:
        base = {
            "kind": "candidate-asset",
            "schema_version": "1.0",
            "id": "candidate-one",
            "request_ref": "request-quality",
            "path": "candidate.png",
            "sha256": "a" * 64,
            "width": 8,
            "height": 8,
            "color_space": "sRGB",
            "has_alpha": True,
            "media_type": "image/png",
            "status": "received",
            "provenance": {"source": "test"},
        }
        for stage in (TRANSPORT_SMOKE_OUTPUT, TECHNICAL_CANDIDATE):
            self.assertEqual(
                validate_document(Manifest(Path("known.json"), {**base, "quality_stage": stage})),
                [],
            )
        self.assertEqual(validate_document(Manifest(Path("legacy.json"), base)), [])
        for stage in ("creative_candidate", "unknown-stage"):
            diagnostics = validate_document(
                Manifest(Path("unknown.json"), {**base, "quality_stage": stage})
            )
            self.assertEqual([item.code for item in diagnostics], ["QUALITY_STAGE"])

    def test_package_verification_rejects_removed_or_changed_stage(self) -> None:
        execution_plan = plan("candidate")
        payload = rgba_png()
        sidecar, sidecar_bytes, png_path, png_bytes = candidate_files(
            execution_plan, "prompt", descriptor(), payload, 0
        )
        sidecar_path = f"candidates/{sidecar['id']}.json"
        plan_bytes = json_bytes(execution_plan)
        generated = {
            PLAN_FILE: plan_bytes,
            sidecar_path: sidecar_bytes,
            png_path: png_bytes,
        }
        inventory = [{
            "id": sidecar["id"],
            "path": png_path,
            "sidecar_path": sidecar_path,
            "sha256": sidecar["sha256"],
            "size": len(png_bytes),
            "width": sidecar["width"],
            "height": sidecar["height"],
            "output_node_id": descriptor()["node_id"],
            "server_filename": descriptor()["filename"],
            "server_subfolder": descriptor()["subfolder"],
            "index": 0,
        }]
        manifest = execution_manifest(
            execution_plan, plan_bytes, "prompt", inventory, generated
        )
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / execution_plan["id"]
            package.mkdir()
            for relative, contents in generated.items():
                target = package / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
            (package / MANIFEST_FILE).write_bytes(json_bytes(manifest))
            self.assertEqual(
                check_execution_package(package / MANIFEST_FILE, execution_plan),
                manifest,
            )
            original = json.loads((package / sidecar_path).read_text(encoding="utf-8"))
            for replacement in (None, TRANSPORT_SMOKE_OUTPUT):
                tampered = dict(original)
                if replacement is None:
                    tampered.pop("quality_stage")
                else:
                    tampered["quality_stage"] = replacement
                (package / sidecar_path).write_bytes(json_bytes(tampered))
                with self.assertRaises(AdapterError) as raised:
                    check_execution_package(package / MANIFEST_FILE, execution_plan)
                self.assertIn(
                    raised.exception.code,
                    {"SIDECAR_BINDING", "FILE_INVENTORY"},
                )
            (package / sidecar_path).write_bytes(sidecar_bytes)


if __name__ == "__main__":
    unittest.main()
