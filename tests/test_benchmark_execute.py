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

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "benchmark" / "model-benchmark-plan.v001.json"
INSTALL = ROOT / "benchmark" / "model-install-manifest.v001.json"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)


def _png(width: int = 1024, height: int = 1024) -> bytes:
    raw = b"".join(b"\x00" + (b"\x00\x00\x00" * width) for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


class FakeClient:
    def __init__(self, image: bytes, *, mode: str = "success") -> None:
        self.image_payload = image
        self.mode = mode
        self.queue_calls = 0
        self.history_calls = 0
        self.image_calls = 0
        self.output_node = ""

    def queue_prompt(self, workflow, *, timeout_seconds=None):
        self.queue_calls += 1
        if self.mode == "queue-failure":
            raise AdapterError("HTTP_STATUS", "HTTP 500", "/prompt")
        if self.mode == "interrupt":
            raise KeyboardInterrupt()
        self.output_node = next(
            node_id for node_id, node in workflow.items() if node.get("class_type") == "SaveImage"
        )
        return f"prompt-{self.queue_calls}"

    def history(self, prompt_id, *, timeout_seconds=None):
        self.history_calls += 1
        if self.mode == "pending":
            return {}
        if self.mode == "history-failure":
            return {
                prompt_id: {
                    "status": {"status_str": "error", "messages": [["execution_error", {}]]},
                    "outputs": {},
                }
            }
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
        self.image_calls += 1
        return self.image_payload


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def _ready(*args, **kwargs):
    return {"ready": True, "diagnostics": [], "network_contacted": True}


class BenchmarkExecuteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.results = self.root / "results"
        self.comfy = self.root / "ComfyUI"
        self.comfy.mkdir()
        plan = json.loads(PLAN.read_text(encoding="utf-8"))
        install = json.loads(INSTALL.read_text(encoding="utf-8"))
        _manifest, files = brp.build_package(plan, install, workspace_root=ROOT)
        brp.publish_package(self.package, files)
        self.image = _png()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _run(self, client: FakeClient, **overrides):
        options = {
            "endpoint": "http://127.0.0.1:8188",
            "execute": True,
            "max_runs": 1,
            "readiness_check": _ready,
            "client_factory": lambda endpoint, limits: client,
            "poll_interval_seconds": 0.05,
        }
        options.update(overrides)
        return be.run(
            self.package,
            PLAN,
            INSTALL,
            ROOT,
            self.results,
            self.comfy,
            **options,
        )

    def test_status_is_read_only_and_reports_empty_progress(self) -> None:
        self.assertFalse(self.results.exists())
        result = be.status(self.package, PLAN, INSTALL, ROOT, self.results)
        self.assertEqual(144, result["pending"])
        self.assertEqual(0, result["succeeded"])
        self.assertEqual(0, result["failed"])
        self.assertFalse(result["network_contacted"])
        self.assertFalse(result["prompt_queued"])
        self.assertFalse(self.results.exists())

    def test_success_persists_one_run_and_resumes_without_repeating_it(self) -> None:
        first_client = FakeClient(self.image)
        first = self._run(first_client)
        self.assertEqual(1, first["attempted"])
        self.assertEqual(1, first["succeeded"])
        self.assertEqual(143, first["pending"])
        self.assertEqual(1, first_client.queue_calls)
        self.assertTrue((self.results / be.RESULTS_FILE).is_file())
        journals = sorted((self.results / be.JOURNAL_DIR).glob("*.json"))
        self.assertEqual(1, len(journals))
        first_run_id = journals[0].stem

        second_client = FakeClient(self.image)
        second = self._run(second_client)
        self.assertEqual(2, second["succeeded"])
        self.assertEqual(142, second["pending"])
        self.assertEqual(1, second_client.queue_calls)
        self.assertTrue((self.results / be.JOURNAL_DIR / f"{first_run_id}.json").is_file())

    def test_interrupt_leaves_run_pending_and_resume_succeeds(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self._run(FakeClient(self.image, mode="interrupt"))
        if (self.results / be.JOURNAL_DIR).exists():
            self.assertEqual([], list((self.results / be.JOURNAL_DIR).glob("*.json")))
        resumed = self._run(FakeClient(self.image))
        self.assertEqual(1, resumed["succeeded"])
        self.assertEqual(143, resumed["pending"])

    def test_failure_is_persisted_and_only_retried_explicitly(self) -> None:
        failed = self._run(FakeClient(self.image, mode="queue-failure"))
        self.assertEqual(1, failed["failed"])
        self.assertEqual(143, failed["pending"])
        self.assertFalse(failed["prompt_queued"])
        failed_journal = next((self.results / be.JOURNAL_DIR).glob("*.json"))
        failed_run_id = failed_journal.stem
        failure = json.loads(failed_journal.read_text(encoding="utf-8"))
        self.assertEqual("failed", failure["result"]["state"])

        retried = self._run(FakeClient(self.image), retry_failed=True)
        self.assertEqual(1, retried["succeeded"])
        replacement = json.loads((self.results / be.JOURNAL_DIR / f"{failed_run_id}.json").read_text(encoding="utf-8"))
        self.assertEqual("succeeded", replacement["result"]["state"])

    def test_post_queue_failure_records_prompt_identity(self) -> None:
        failed = self._run(FakeClient(self.image, mode="history-failure"))
        self.assertEqual(1, failed["failed"])
        self.assertTrue(failed["prompt_queued"])
        journal = json.loads(next((self.results / be.JOURNAL_DIR).glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("prompt-1", journal["prompt_id"])
        self.assertEqual("failed", journal["result"]["state"])

    def test_tampered_completed_image_fails_closed_without_network(self) -> None:
        self._run(FakeClient(self.image))
        image = next((self.results / be.IMAGE_DIR).glob("*.png"))
        image.write_bytes(image.read_bytes() + b"tamper")
        with self.assertRaises(be.BenchmarkExecutionError):
            be.status(self.package, PLAN, INSTALL, ROOT, self.results)

    def test_non_loopback_endpoint_is_rejected_before_readiness_or_client(self) -> None:
        calls = {"readiness": 0, "client": 0}

        def readiness(*args, **kwargs):
            calls["readiness"] += 1
            return {"ready": True, "diagnostics": []}

        def factory(endpoint, limits):
            calls["client"] += 1
            return FakeClient(self.image)

        with self.assertRaises(AdapterError):
            be.run(
                self.package,
                PLAN,
                INSTALL,
                ROOT,
                self.results,
                self.comfy,
                endpoint="https://example.com",
                execute=True,
                max_runs=1,
                readiness_check=readiness,
                client_factory=factory,
            )
        self.assertEqual({"readiness": 0, "client": 0}, calls)

    def test_pending_history_times_out_bounded_and_persists_failure(self) -> None:
        clock = FakeClock()
        client = FakeClient(self.image, mode="pending")
        result = self._run(
            client,
            run_timeout_seconds=1.0,
            poll_interval_seconds=0.25,
            clock=clock,
            sleeper=clock.sleep,
        )
        self.assertEqual(1, result["failed"])
        self.assertTrue(result["prompt_queued"])
        self.assertLessEqual(client.history_calls, 5)
        journal = json.loads(next((self.results / be.JOURNAL_DIR).glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("run-timeout", journal["result"]["error"]["code"])
        self.assertEqual("prompt-1", journal["prompt_id"])

    def test_execute_acknowledgement_and_limits_fail_closed(self) -> None:
        with self.assertRaises(be.BenchmarkExecutionError):
            be.run(
                self.package,
                PLAN,
                INSTALL,
                ROOT,
                self.results,
                self.comfy,
                endpoint="http://127.0.0.1:8188",
                execute=False,
            )
        with self.assertRaises(be.BenchmarkExecutionError):
            self._run(FakeClient(self.image), max_runs=145)

    def test_source_has_no_scoring_selection_process_launch_or_remote_client(self) -> None:
        source = (ROOT / "src" / "ai_illustration" / "benchmark_execute.py").read_text(encoding="utf-8")
        for prohibited in (
            "import subprocess",
            "from subprocess",
            "import socket",
            "import requests",
            "import http.client",
            "aesthetic_score",
            "automatic_score",
            "select_model",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
