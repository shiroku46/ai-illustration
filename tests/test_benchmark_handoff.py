from __future__ import annotations

import binascii
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from ai_illustration.adapters.base import AdapterError
from ai_illustration import benchmark_execute as be
from ai_illustration import benchmark_handoff as bh
from ai_illustration import benchmark_run_package as brp

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "benchmark" / "model-benchmark-plan.v001.json"
INSTALL = ROOT / "benchmark" / "model-install-manifest.v001.json"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png(width: int = 1024, height: int = 1024) -> bytes:
    raw = b"".join(b"\x00" + (b"\x00\x00\x00" * width) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )


class FakeClient:
    def __init__(self, image: bytes, *, fail_queue: bool = False) -> None:
        self.image_payload = image
        self.fail_queue = fail_queue
        self.output_node = ""

    def queue_prompt(self, workflow, *, timeout_seconds=None):
        if self.fail_queue:
            raise AdapterError("HTTP_STATUS", "HTTP 500 at C:\\private\\ComfyUI", "/prompt")
        self.output_node = next(
            node_id
            for node_id, node in workflow.items()
            if node.get("class_type") == "SaveImage"
        )
        return "prompt-1"

    def history(self, prompt_id, *, timeout_seconds=None):
        return {
            prompt_id: {
                "status": {"status_str": "success", "messages": []},
                "outputs": {
                    self.output_node: {
                        "images": [
                            {
                                "filename": "benchmark_00001_.png",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                },
            }
        }

    def image(self, filename, subfolder, *, timeout_seconds=None):
        return self.image_payload


def _ready(*args, **kwargs):
    return {"ready": True, "diagnostics": []}


class BenchmarkHandoffTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.results = self.root / "private-owner-path" / "results"
        self.comfy = self.root / "private-owner-path" / "ComfyUI"
        self.comfy.mkdir(parents=True)
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        install = json.loads(INSTALL.read_text(encoding="utf-8"))
        _manifest, files = brp.build_package(plan, install, workspace_root=ROOT)
        brp.publish_package(self.package, files)
        self.image = _png()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run_one(self, client: FakeClient) -> dict[str, object]:
        return be.run(
            self.package,
            PLAN,
            INSTALL,
            ROOT,
            self.results,
            self.comfy,
            endpoint="http://127.0.0.1:8188",
            execute=True,
            max_runs=1,
            readiness_check=_ready,
            client_factory=lambda endpoint, limits: client,
            poll_interval_seconds=0.05,
        )

    def _snapshot(self, limit: int = 3) -> dict[str, object]:
        return bh.snapshot(
            self.package,
            PLAN,
            INSTALL,
            ROOT,
            self.results,
            limit=limit,
        )

    def test_empty_snapshot_is_read_only(self) -> None:
        self.assertFalse(self.results.exists())
        result = self._snapshot()
        self.assertTrue(result["ok"])
        self.assertEqual(0, result["reported_count"])
        self.assertEqual([], result["runs"])
        self.assertEqual(144, result["pending"])
        self.assertFalse(result["network_contacted"])
        self.assertFalse(self.results.exists())

    def test_success_and_failure_are_sanitized_in_deterministic_order(self) -> None:
        self._run_one(FakeClient(self.image))
        self._run_one(FakeClient(self.image, fail_queue=True))
        result = self._snapshot(limit=2)
        self.assertEqual(2, result["reported_count"])
        runs = result["runs"]
        self.assertEqual("succeeded", runs[0]["state"])
        self.assertEqual("failed", runs[1]["state"])
        self.assertIn("image_sha256", runs[0])
        self.assertNotIn("image_path", runs[0])
        self.assertEqual("http-status", runs[1]["error"]["code"])
        self.assertIn("C:\\private\\ComfyUI", runs[1]["error"]["message"])

        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn(str(self.results), encoded)
        self.assertNotIn(str(self.comfy), encoded)
        self.assertNotIn("prompt-1", encoded)
        self.assertNotIn("benchmark_00001_.png", encoded)
        self.assertNotIn("api_key", encoded.lower())
        self.assertFalse(result["network_contacted"])
        self.assertFalse(result["prompt_queued"])

    def test_limit_is_bounded_and_deterministic(self) -> None:
        self._run_one(FakeClient(self.image))
        self._run_one(FakeClient(self.image))
        one = self._snapshot(limit=1)
        two = self._snapshot(limit=2)
        self.assertEqual(1, len(one["runs"]))
        self.assertEqual(one["runs"][0], two["runs"][0])
        with self.assertRaises(be.BenchmarkExecutionError):
            self._snapshot(limit=0)
        with self.assertRaises(be.BenchmarkExecutionError):
            self._snapshot(limit=145)

    def test_tampered_aggregate_fails_closed(self) -> None:
        self._run_one(FakeClient(self.image))
        aggregate = self.results / be.RESULTS_FILE
        aggregate.write_bytes(aggregate.read_bytes() + b"\n")
        with self.assertRaises(be.BenchmarkExecutionError):
            self._snapshot()

    def test_source_has_no_network_execution_or_selection_surface(self) -> None:
        source = (ROOT / "src" / "ai_illustration" / "benchmark_handoff.py").read_text(
            encoding="utf-8"
        )
        for prohibited in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "import urllib",
            "import requests",
            "queue_prompt",
            "aesthetic_score",
            "automatic_score",
            "select_model",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
