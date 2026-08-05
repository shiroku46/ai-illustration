from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

from ai_illustration.comfyui_preflight import main, run_preflight
from ai_illustration.comfyui_smoke_bundle import prepare_bundle
from ai_illustration.comfyui_smoke_common import MANIFEST_FILE, REQUEST_FILE


class _Stdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


class ServerState:
    def __init__(self) -> None:
        self.mode = "success"
        self.requests: list[str] = []
        self.posts = 0
        self.checkpoints = ["sd_xl_turbo_1.0_fp16.safetensors", "other.safetensors"]
        self.node_classes = {
            "CLIPTextEncode",
            "CheckpointLoaderSimple",
            "EmptyLatentImage",
            "KSampler",
            "SaveImage",
        }
        self.missing_node: str | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "preflight-test"

    @property
    def state(self) -> ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, *args: object) -> None:
        return None

    def _send(
        self,
        code: int,
        payload: bytes,
        *,
        content_type: str = "application/json",
        declared_length: int | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Length",
            str(len(payload) if declared_length is None else declared_length),
        )
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, value: object) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def do_POST(self) -> None:
        self.state.posts += 1
        self._send(405, b"{}")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        self.state.requests.append(parsed.path)
        if self.state.mode == "slow" and parsed.path == "/system_stats":
            time.sleep(0.2)
        if self.state.mode == "redirect" and parsed.path == "/system_stats":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/forbidden")
            self.end_headers()
            return
        if self.state.mode == "status" and parsed.path == "/system_stats":
            self._send(500, b"{}")
            return
        if parsed.path == "/system_stats":
            if self.state.mode == "wrong-content":
                self._send(200, b"{}", content_type="text/plain")
                return
            if self.state.mode == "malformed":
                self._send(200, b"{")
                return
            if self.state.mode == "duplicate":
                payload = (
                    b'{"system":{"comfyui_version":"a"},'
                    b'"system":{"comfyui_version":"b"},"devices":[]}'
                )
                self._send(200, payload)
                return
            if self.state.mode == "oversized":
                payload = b"{" + b'"padding":"' + (b"x" * (1024 * 1024)) + b'"}'
                self._send(200, payload)
                return
            if self.state.mode == "system-schema":
                self._send(200, self._json({"system": {}, "devices": []}))
                return
            value = {
                "system": {
                    "comfyui_version": "0.3.50",
                    "python_version": "3.13.3 (main build)",
                    "pytorch_version": "2.8.0+cu128",
                    "argv": [r"C:\Users\owner\ComfyUI\main.py"],
                    "unrelated_secret_path": r"C:\Users\owner\hidden",
                },
                "devices": [
                    {
                        "name": "cuda:0 NVIDIA GeForce RTX Test",
                        "type": "cuda",
                        "index": 0,
                        "vram_total": 12 * 1024 * 1024 * 1024,
                        "vram_free": 10 * 1024 * 1024 * 1024,
                        "torch_vram_total": 11 * 1024 * 1024 * 1024,
                    }
                ],
            }
            self._send(200, self._json(value))
            return
        if parsed.path == "/models/checkpoints":
            if self.state.mode == "models-schema":
                self._send(200, self._json({"not": "a-list"}))
                return
            if self.state.mode == "models-duplicate":
                self._send(200, self._json(["same.safetensors", "same.safetensors"]))
                return
            self._send(200, self._json(self.state.checkpoints))
            return
        if parsed.path.startswith("/object_info/"):
            node_class = unquote(parsed.path[len("/object_info/") :])
            if self.state.mode == "object-schema":
                self._send(200, self._json([node_class]))
                return
            if self.state.mode == "object-extra":
                self._send(
                    200,
                    self._json({node_class: {}, "Unexpected": {}}),
                )
                return
            if node_class == self.state.missing_node or node_class not in self.state.node_classes:
                self._send(200, b"{}")
            else:
                self._send(
                    200,
                    self._json(
                        {
                            node_class: {
                                "name": node_class,
                                "input": {},
                                "output": [],
                            }
                        }
                    ),
                )
            return
        self._send(404, b"{}")


def workflow() -> dict[str, object]:
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "sd_xl_turbo_1.0_fp16.safetensors"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "one character", "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "watermark", "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 512, "height": 512, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": 123456,
                "steps": 4,
                "cfg": 1.0,
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "6": {
            "class_type": "SaveImage",
            "inputs": {"images": ["5", 0], "filename_prefix": "ComfyUI"},
        },
    }


class ComfyUIPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workflow = self.root / "workflow-api.json"
        self.workflow.write_text(json.dumps(workflow(), indent=2), encoding="utf-8")
        self.state = ServerState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.bundles = self.root / "bundles"
        self.manifest = self._bundle("approved", self.bundles)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def _bundle(self, state: str, output_root: Path, *, endpoint: str | None = None) -> Path:
        kwargs: dict[str, object] = {
            "profile_state": state,
            "review_date": "2026-08-05",
            "endpoint": endpoint or self.endpoint,
            "write": True,
        }
        if state == "approved":
            kwargs.update(
                {
                    "tool_evidence_url": "https://example.com/comfyui-license",
                    "model_evidence_url": "https://example.com/sdxl-turbo-license",
                    "confirm_tool_license": True,
                    "confirm_model_license": True,
                    "confirm_commercial_use": True,
                }
            )
        result = prepare_bundle(self.workflow, output_root, **kwargs)
        return output_root / str(result["bundle_path"]) / MANIFEST_FILE

    def run(self, *, timeout_seconds: float = 1.0) -> dict[str, object]:
        return run_preflight(
            self.manifest,
            self.bundles,
            timeout_seconds=timeout_seconds,
        )

    def test_success_uses_only_exact_get_routes_and_sanitizes_system_output(self) -> None:
        result = self.run()
        self.assertTrue(result["ok"])
        self.assertTrue(result["ready"])
        self.assertEqual(self.state.posts, 0)
        expected = [
            "/system_stats",
            "/models/checkpoints",
            "/object_info/CLIPTextEncode",
            "/object_info/CheckpointLoaderSimple",
            "/object_info/EmptyLatentImage",
            "/object_info/KSampler",
            "/object_info/SaveImage",
        ]
        self.assertEqual(self.state.requests, expected)
        self.assertEqual(result["requested_routes"], expected)
        rendered = json.dumps(result, sort_keys=True)
        self.assertNotIn("argv", rendered)
        self.assertNotIn("hidden", rendered)
        self.assertNotIn(r"C:\Users", rendered)
        self.assertFalse(result["prompt_queued"])
        self.assertFalse(result["filesystem_mutated"])
        self.assertFalse(result["external_process_started"])

    def test_proxy_environment_is_ignored(self) -> None:
        previous_http = os.environ.get("HTTP_PROXY")
        previous_https = os.environ.get("HTTPS_PROXY")
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:1"
        try:
            self.assertTrue(self.run()["ready"])
        finally:
            if previous_http is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = previous_http
            if previous_https is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = previous_https

    def test_reviewing_and_tampered_bundles_fail_before_network(self) -> None:
        reviewing_root = self.root / "reviewing"
        reviewing = self._bundle("reviewing", reviewing_root)
        self.state.requests.clear()
        result = run_preflight(reviewing, reviewing_root)
        self.assertFalse(result["ready"])
        self.assertEqual(result["diagnostics"][0]["code"], "BUNDLE_NOT_APPROVED")
        self.assertFalse(result["network_contacted"])
        self.assertEqual(self.state.requests, [])

        package = self.manifest.parent
        request = package / REQUEST_FILE
        request.write_bytes(request.read_bytes() + b" ")
        result = self.run()
        self.assertFalse(result["ready"])
        self.assertIn(result["diagnostics"][0]["code"], {"FILE_MISMATCH", "NONCANONICAL_JSON"})
        self.assertFalse(result["network_contacted"])
        self.assertEqual(self.state.requests, [])

    def test_missing_checkpoint_and_node_are_reported_together(self) -> None:
        self.state.checkpoints = ["other.safetensors"]
        self.state.missing_node = "KSampler"
        result = self.run()
        self.assertFalse(result["ready"])
        self.assertEqual(
            [item["code"] for item in result["diagnostics"]],
            ["CHECKPOINT_MISSING", "NODE_CLASSES_MISSING"],
        )
        self.assertEqual(result["workflow"]["missing_node_classes"], ["KSampler"])
        self.assertFalse(result["checkpoint"]["available"])
        self.assertEqual(self.state.posts, 0)

    def test_redirect_status_content_json_and_size_fail_closed(self) -> None:
        cases = {
            "redirect": "HTTP_REDIRECT",
            "status": "HTTP_STATUS",
            "wrong-content": "HTTP_CONTENT_TYPE",
            "malformed": "INVALID_HTTP_JSON",
            "duplicate": "DUPLICATE_JSON_KEY",
            "oversized": "HTTP_RESPONSE_TOO_LARGE",
            "system-schema": "SYSTEM_STATS_SCHEMA",
        }
        for mode, code in cases.items():
            with self.subTest(mode=mode):
                self.state.mode = mode
                self.state.requests.clear()
                result = self.run()
                self.assertFalse(result["ready"])
                self.assertEqual(result["diagnostics"][0]["code"], code)
                self.assertTrue(result["network_contacted"])
                self.assertEqual(self.state.posts, 0)
        self.state.mode = "success"

    def test_checkpoint_and_object_response_schemas_fail_closed(self) -> None:
        cases = {
            "models-schema": "CHECKPOINTS_SCHEMA",
            "models-duplicate": "CHECKPOINTS_SCHEMA",
            "object-schema": "OBJECT_INFO_SCHEMA",
            "object-extra": "OBJECT_INFO_SCHEMA",
        }
        for mode, code in cases.items():
            with self.subTest(mode=mode):
                self.state.mode = mode
                self.state.requests.clear()
                result = self.run()
                self.assertFalse(result["ready"])
                self.assertEqual(result["diagnostics"][0]["code"], code)
                self.assertEqual(self.state.posts, 0)
        self.state.mode = "success"

    def test_timeout_and_connection_failure_are_bounded(self) -> None:
        self.state.mode = "slow"
        result = self.run(timeout_seconds=0.05)
        self.assertFalse(result["ready"])
        self.assertEqual(result["diagnostics"][0]["code"], "HTTP_TIMEOUT")
        self.state.mode = "success"

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        unavailable_root = self.root / "unavailable"
        unavailable = self._bundle(
            "approved",
            unavailable_root,
            endpoint=f"http://127.0.0.1:{port}",
        )
        result = run_preflight(unavailable, unavailable_root, timeout_seconds=0.1)
        self.assertFalse(result["ready"])
        self.assertIn(result["diagnostics"][0]["code"], {"HTTP_ERROR", "HTTP_TIMEOUT"})
        self.assertTrue(result["network_contacted"])

    def test_unsafe_node_class_fails_before_network(self) -> None:
        unsafe_workflow = workflow()
        unsafe_workflow["7"] = {"class_type": "Unsafe/Node", "inputs": {}}
        unsafe_path = self.root / "unsafe-workflow.json"
        unsafe_path.write_text(json.dumps(unsafe_workflow), encoding="utf-8")
        unsafe_root = self.root / "unsafe-bundles"
        result = prepare_bundle(
            unsafe_path,
            unsafe_root,
            profile_state="approved",
            review_date="2026-08-05",
            tool_evidence_url="https://example.com/tool",
            model_evidence_url="https://example.com/model",
            confirm_tool_license=True,
            confirm_model_license=True,
            confirm_commercial_use=True,
            endpoint=self.endpoint,
            write=True,
        )
        manifest = unsafe_root / str(result["bundle_path"]) / MANIFEST_FILE
        self.state.requests.clear()
        checked = run_preflight(manifest, unsafe_root)
        self.assertFalse(checked["ready"])
        self.assertEqual(checked["diagnostics"][0]["code"], "NODE_CLASS_ROUTE")
        self.assertFalse(checked["network_contacted"])
        self.assertEqual(self.state.requests, [])

    def test_module_cli_emits_canonical_result_and_exit_status(self) -> None:
        stdout = _Stdout()
        stderr = io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            code = main(
                [
                    "run",
                    str(self.manifest),
                    "--bundle-root",
                    str(self.bundles),
                    "--timeout-seconds",
                    "1",
                ]
            )
        self.assertEqual(code, 0)
        value = json.loads(stdout.buffer.getvalue().decode("utf-8"))
        self.assertTrue(value["ready"])
        self.assertTrue(stdout.buffer.getvalue().endswith(b"\n"))
        self.assertIn("ready", stderr.getvalue())

        self.state.checkpoints = []
        stdout = _Stdout()
        with patch("sys.stdout", stdout), patch("sys.stderr", io.StringIO()):
            code = main(
                [
                    "run",
                    str(self.manifest),
                    "--bundle-root",
                    str(self.bundles),
                ]
            )
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.buffer.getvalue())["ready"])


if __name__ == "__main__":
    unittest.main()
