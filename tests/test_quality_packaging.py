from __future__ import annotations

import binascii
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from ai_illustration.adapters.base import AdapterError
from ai_illustration.adapters.comfyui_execution_common import MANIFEST_FILE, PLAN_FILE, json_bytes
from ai_illustration.adapters.comfyui_execution_package import execution_manifest
from ai_illustration.adapters.comfyui_execution_quality import candidate_files, check_execution_package
from ai_illustration.comfyui_smoke_bundle import REQUEST_FILE
from ai_illustration.comfyui_smoke_quality import prepare_bundle
from ai_illustration.models import Manifest
from ai_illustration.quality import TECHNICAL_CANDIDATE, TRANSPORT_SMOKE_OUTPUT
from ai_illustration.validation import validate_document


def chunk(name: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", binascii.crc32(name + payload) & 0xFFFFFFFF)


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
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                "latent_image": ["4", 0], "seed": 1, "steps": 4, "cfg": 1.0,
                "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "smoke", "images": ["6", 0]}},
    }


def plan(output_intent: str) -> dict[str, object]:
    return {
        "id": "plan-quality",
        "kind": "comfyui-execution-plan",
        "schema_version": "1.0",
        "plan_sha256": "1" * 64,
        "endpoint": "http://127.0.0.1:8188",
        "workflow_sha256": "2" * 64,
        "execution_profile_ref": "execution-profile",
        "tool_profile_ref": "comfyui-local-api",
        "model_profile_ref": "model-one",
        "request": {
            "id": "request-quality",
            "character_ref": "tsukkomi@v001",
            "style_ref": "flat@v001",
            "pose": "standing",
            "expression": "neutral",
            "crop": "full-body",
            "facing": "front",
            "tool_id": "comfyui-local-api",
            "model_id": "model-one",
            "license_status": "approved",
            "config": {},
            "output_intent": output_intent,
            "provenance": {"source": "test"},
        },
        "workflow": {},
        "bindings": {},
        "tool_profile": {"id": "comfyui-local-api"},
        "model_profile": {"id": "model-one"},
        "execution_profile": {
            "expected_width": 8,
            "expected_height": 8,
            "limits": {"max_images": 1},
        },
        "network_scope": "loopback-only",
        "subprocess_started": False,
    }


class QualityPackagingTests(unittest.TestCase):
    def test_smoke_bundle_request_is_transport_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workflow_path = root / "workflow.json"
            workflow_path.write_text(json.dumps(workflow()), encoding="utf-8")
            output_root = root / "bundle-output"
            result = prepare_bundle(
                workflow_path,
                output_root,
                profile_state="reviewing",
                review_date="2026-08-06",
                checkpoint_name="real.safetensors",
                write=True,
            )
            request = json.loads(
                (output_root / result["bundle_path"] / REQUEST_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(request["output_intent"], "transport-smoke")

    def test_smoke_and_normal_sidecars_keep_technical_status_separate(self) -> None:
        png = {"payload": rgba_png(), "sha256": hashlib.sha256(rgba_png()).hexdigest(), "width": 8, "height": 8}
        descriptor = {"node_id": "7", "output_index": 0, "filename": "image.png", "subfolder": "", "type": "output"}
        smoke_candidate, _, smoke_bytes, _ = candidate_files(plan("transport-smoke"), "prompt", descriptor, png, 0)
        normal_candidate, _, normal_bytes, _ = candidate_files(plan("candidate"), "prompt", descriptor, png, 0)
        smoke_sidecar = json.loads(smoke_bytes)
        normal_sidecar = json.loads(normal_bytes)
        self.assertEqual(smoke_candidate["status"], "technically_valid")
        self.assertEqual(normal_candidate["status"], "technically_valid")
        self.assertEqual(smoke_candidate["quality_stage"], TRANSPORT_SMOKE_OUTPUT)
        self.assertEqual(normal_candidate["quality_stage"], TECHNICAL_CANDIDATE)
        self.assertEqual(smoke_sidecar["quality_stage"], TRANSPORT_SMOKE_OUTPUT)
        self.assertEqual(normal_sidecar["quality_stage"], TECHNICAL_CANDIDATE)

    def test_unknown_output_intent_fails_packaging(self) -> None:
        png_payload = rgba_png()
        png = {"payload": png_payload, "sha256": hashlib.sha256(png_payload).hexdigest(), "width": 8, "height": 8}
        descriptor = {"node_id": "7", "output_index": 0, "filename": "image.png", "subfolder": "", "type": "output"}
        with self.assertRaises(AdapterError) as raised:
            candidate_files(plan("transport-smok"), "prompt", descriptor, png, 0)
        self.assertEqual(raised.exception.code, "OUTPUT_INTENT")

    def test_candidate_validation_accepts_known_stage_and_rejects_unknown(self) -> None:
        base = {
            "kind": "candidate-asset", "schema_version": "1.0", "id": "candidate-one",
            "request_ref": "request-quality", "path": "candidate.png", "sha256": "a" * 64,
            "width": 8, "height": 8, "color_space": "sRGB", "has_alpha": True,
            "media_type": "image/png", "status": "technically_valid",
            "provenance": {"source": "test"},
        }
        self.assertEqual(validate_document(Manifest(Path("known.json"), {**base, "quality_stage": TECHNICAL_CANDIDATE})), [])
        self.assertEqual(validate_document(Manifest(Path("legacy.json"), base)), [])
        diagnostics = validate_document(Manifest(Path("unknown.json"), {**base, "quality_stage": "creative_candidate"}))
        self.assertEqual([item.code for item in diagnostics], ["UNKNOWN_FIELD"])

    def test_package_verification_rejects_removed_or_changed_stage(self) -> None:
        execution_plan = plan("candidate")
        payload = rgba_png()
        png = {"payload": payload, "sha256": hashlib.sha256(payload).hexdigest(), "width": 8, "height": 8}
        descriptor = {"node_id": "7", "output_index": 0, "filename": "image.png", "subfolder": "", "type": "output"}
        candidate, sidecar_path, sidecar_bytes, png_path = candidate_files(execution_plan, "prompt", descriptor, png, 0)
        files = {PLAN_FILE: json_bytes(execution_plan), sidecar_path: sidecar_bytes, png_path: payload}
        manifest = execution_manifest(execution_plan, [candidate], files)
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp)
            for relative, contents in files.items():
                target = package / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)
            (package / MANIFEST_FILE).write_bytes(json_bytes(manifest))
            self.assertEqual(check_execution_package(package / MANIFEST_FILE, execution_plan), manifest)
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
                self.assertIn(raised.exception.code, {"FILE_CONTENT", "SIDECAR_BINDING"})
            (package / sidecar_path).write_bytes(sidecar_bytes)


if __name__ == "__main__":
    unittest.main()
