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
from ai_illustration import benchmark_run_package as brp
from ai_illustration import benchmark_smoke as bs

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


PNG = _png()


class FakeClient:
    def __init__(
        self,
        image: bytes,
        *,
        fail_calls: set[int] | None = None,
        interrupt_calls: set[int] | None = None,
    ) -> None:
        self.image_payload = image
        self.fail_calls = fail_calls or set()
        self.interrupt_calls = interrupt_calls or set()
        self.queue_calls = 0
        self.history_calls = 0
        self.image_calls = 0
        self.output_nodes: dict[str, str] = {}

    def queue_prompt(self, workflow, *, timeout_seconds=None):
        self.queue_calls += 1
        if self.queue_calls in self.interrupt_calls:
            raise KeyboardInterrupt()
        if self.queue_calls in self.fail_calls:
            raise AdapterError("HTTP_STATUS", "HTTP 500", "/prompt")
        output_node = next(
            node_id
            for node_id, node in workflow.items()
            if node.get("class_type") == "SaveImage"
        )
        prompt_id = f"prompt-{self.queue_calls}"
        self.output_nodes[prompt_id] = output_node
        return prompt_id

    def history(self, prompt_id, *, timeout_seconds=None):
        self.history_calls += 1
        return {
            prompt_id: {
                "status": {"status_str": "success", "messages": []},
                "outputs": {
                    self.output_nodes[prompt_id]: {
                        "images": [
                            {
                                "filename": f"{prompt_id}.png",
                                "subfolder": "",
                                "type": "output",
                            }
                        ]
                    }
                },
            }
        }

    def image(self, filename, subfolder, *, timeout_seconds=None):
        self.image_calls += 1
        return self.image_payload


def _ready(*args, **kwargs):
    return {"ready": True, "diagnostics": []}


class BenchmarkSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.results = self.root / "results"
        self.comfy = self.root / "ComfyUI"
        self.comfy.mkdir()
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        install = json.loads(INSTALL.read_text(encoding="utf-8"))
        self.manifest, files = brp.build_package(plan, install, workspace_root=ROOT)
        brp.publish_package(self.package, files)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, client: FakeClient, **overrides):
        options = {
            "endpoint": "http://127.0.0.1:8188",
            "execute": True,
            "readiness_check": _ready,
            "client_factory": lambda endpoint, limits: client,
            "poll_interval_seconds": 0.05,
        }
        options.update(overrides)
        return bs.run(
            self.package,
            PLAN,
            INSTALL,
            ROOT,
            self.results,
            self.comfy,
            **options,
        )

    def test_selects_exactly_one_identical_baseline_case_per_family(self) -> None:
        selected = bs.select_smoke_runs(self.manifest)
        self.assertEqual(3, len(selected))
        self.assertEqual(
            ["anima-aesthetic", "animagine-xl", "illustrious-xl"],
            [run["model_family"] for run in selected],
        )
        self.assertEqual({101}, {run["seed"] for run in selected})
        self.assertEqual(
            {"front-full-body-neutral"},
            {run["prompt_case_id"] for run in selected},
        )
        package_ids = {run["run_id"] for run in self.manifest["runs"]}
        self.assertTrue({run["run_id"] for run in selected} <= package_ids)

    def test_status_is_read_only_before_any_smoke_run(self) -> None:
        self.assertFalse(self.results.exists())
        result = bs.status(self.package, PLAN, INSTALL, ROOT, self.results)
        self.assertEqual(3, result["smoke_pending"])
        self.assertEqual(0, result["smoke_succeeded"])
        self.assertEqual(0, result["smoke_failed"])
        self.assertEqual(144, result["pending"])
        self.assertFalse(result["network_contacted"])
        self.assertFalse(result["prompt_queued"])
        self.assertFalse(self.results.exists())

    def test_success_attempts_one_run_from_each_family_and_full_executor_can_resume(self) -> None:
        client = FakeClient(PNG)
        result = self._run(client)
        self.assertEqual(3, result["attempted"])
        self.assertEqual(3, client.queue_calls)
        self.assertEqual(3, result["smoke_succeeded"])
        self.assertEqual(0, result["smoke_failed"])
        self.assertEqual(0, result["smoke_pending"])
        self.assertEqual(3, result["succeeded"])
        self.assertEqual(141, result["pending"])

        journals = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.results / be.JOURNAL_DIR).glob("*.json"))
        ]
        self.assertEqual(3, len(journals))
        self.assertEqual(
            {"anima-aesthetic", "animagine-xl", "illustrious-xl"},
            {journal["result"]["model_family"] for journal in journals},
        )
        self.assertEqual({101}, {journal["result"]["seed"] for journal in journals})
        self.assertEqual(
            {"front-full-body-neutral"},
            {journal["result"]["prompt_case_id"] for journal in journals},
        )

        full_client = FakeClient(PNG)
        full = be.run(
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
            client_factory=lambda endpoint, limits: full_client,
            poll_interval_seconds=0.05,
        )
        self.assertEqual(1, full["attempted"])
        self.assertEqual(4, full["succeeded"])
        self.assertEqual(140, full["pending"])

    def test_failure_is_persisted_skipped_by_default_and_retried_explicitly(self) -> None:
        first = self._run(FakeClient(PNG, fail_calls={2}))
        self.assertEqual(2, first["smoke_succeeded"])
        self.assertEqual(1, first["smoke_failed"])
        self.assertEqual(0, first["smoke_pending"])

        skipped_client = FakeClient(PNG)
        skipped = self._run(skipped_client)
        self.assertEqual(0, skipped["attempted"])
        self.assertEqual(0, skipped_client.queue_calls)
        self.assertEqual(1, skipped["smoke_failed"])

        retry_client = FakeClient(PNG)
        retried = self._run(retry_client, retry_failed=True)
        self.assertEqual(1, retried["attempted"])
        self.assertEqual(1, retry_client.queue_calls)
        self.assertEqual(3, retried["smoke_succeeded"])
        self.assertEqual(0, retried["smoke_failed"])

    def test_interrupt_leaves_current_and_later_smoke_runs_pending_then_resumes(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._run(FakeClient(PNG, interrupt_calls={2}))
        state = bs.status(self.package, PLAN, INSTALL, ROOT, self.results)
        self.assertEqual(1, state["smoke_succeeded"])
        self.assertEqual(0, state["smoke_failed"])
        self.assertEqual(2, state["smoke_pending"])

        resumed_client = FakeClient(PNG)
        resumed = self._run(resumed_client)
        self.assertEqual(2, resumed["attempted"])
        self.assertEqual(2, resumed_client.queue_calls)
        self.assertEqual(3, resumed["smoke_succeeded"])
        self.assertEqual(0, resumed["smoke_pending"])

    def test_non_loopback_endpoint_is_rejected_before_readiness_or_client(self) -> None:
        calls = {"readiness": 0, "client": 0}

        def readiness(*args, **kwargs):
            calls["readiness"] += 1
            return {"ready": True, "diagnostics": []}

        def factory(endpoint, limits):
            calls["client"] += 1
            return FakeClient(PNG)

        with self.assertRaises(AdapterError):
            bs.run(
                self.package,
                PLAN,
                INSTALL,
                ROOT,
                self.results,
                self.comfy,
                endpoint="https://example.com",
                execute=True,
                readiness_check=readiness,
                client_factory=factory,
            )
        self.assertEqual({"readiness": 0, "client": 0}, calls)

    def test_source_has_no_install_process_launch_ranking_or_selection_surface(self) -> None:
        source = (ROOT / "src" / "ai_illustration" / "benchmark_smoke.py").read_text(
            encoding="utf-8"
        )
        for prohibited in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "import requests",
            "Invoke-WebRequest",
            "Start-Process",
            "aesthetic_score",
            "automatic_score",
            "select_model",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
