from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from ai_illustration.adapters.base import AdapterError
from ai_illustration.adapters.comfyui_execute import (
    MANIFEST_FILE,
    check_comfyui_execution,
    prepare_execution,
    run_comfyui_execution,
)
from ai_illustration.frame_renderer import RGBAImage, encode_rgba_png
from ai_illustration.naming import canonical_json, content_identifier


def canonical(value: object) -> bytes:
    return canonical_json(value) + b"\n"


class ServerState:
    def __init__(self, png: bytes) -> None:
        self.png = png
        self.posts = 0
        self.post_payloads: list[bytes] = []
        self.history_calls = 0
        self.mode = "success"


class Handler(BaseHTTPRequestHandler):
    server_version = "test"

    def log_message(self, *args: object) -> None:
        return None

    @property
    def state(self) -> ServerState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, code: int, content_type: str, payload: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if self.path != "/prompt":
            self._send(404, "application/json", b"{}")
            return
        if self.state.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/evil")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        self.state.posts += 1
        self.state.post_payloads.append(payload)
        if self.state.mode == "duplicate-queue":
            self._send(200, "application/json", b'{"prompt_id":"a","prompt_id":"b"}')
            return
        if self.state.mode == "unknown-queue":
            self._send(200, "application/json", b'{"evil":true,"prompt_id":"prompt-1"}')
            return
        self._send(
            200,
            "application/json",
            b'{"node_errors":{},"number":1,"prompt_id":"prompt-1"}',
        )

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/history/prompt-1":
            self.state.history_calls += 1
            if self.state.mode == "pending":
                self._send(200, "application/json", b"{}")
                return
            if self.state.mode == "wrong-prompt":
                self._send(200, "application/json", b'{"other":{}}')
                return
            if self.state.mode == "execution-error":
                value = {"prompt-1": {"status": {"status_str": "error"}}}
            elif self.state.mode == "wrong-node":
                value = {
                    "prompt-1": {
                        "outputs": {
                            "10": {
                                "images": [
                                    {"filename": "x.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        }
                    }
                }
            elif self.state.mode == "unsafe-name":
                value = {
                    "prompt-1": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "../evil.png", "subfolder": "", "type": "output"}
                                ]
                            }
                        }
                    }
                }
            elif self.state.mode == "wrong-type":
                value = {
                    "prompt-1": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "x.png", "subfolder": "", "type": "temp"}
                                ]
                            }
                        }
                    }
                }
            elif self.state.mode == "two-images":
                value = {
                    "prompt-1": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "a.png", "subfolder": "", "type": "output"},
                                    {"filename": "b.png", "subfolder": "", "type": "output"},
                                ]
                            }
                        }
                    }
                }
            else:
                value = {
                    "prompt-1": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {"filename": "x.png", "subfolder": "batch", "type": "output"}
                                ]
                            }
                        }
                    }
                }
            self._send(
                200,
                "application/json",
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            )
            return
        if parsed.path == "/view":
            query = parse_qs(parsed.query)
            if query.get("type") != ["output"]:
                self._send(400, "application/json", b"{}")
                return
            self._send(200, "image/png", self.state.png)
            return
        self._send(404, "application/json", b"{}")


class Fixture:
    def __init__(self, root: Path) -> None:
        self.request = root / "request.json"
        self.workflow = root / "workflow.json"
        self.bindings = root / "bindings.json"
        self.tool = root / "tool.json"
        self.model = root / "model.json"
        self.execution = root / "execution.json"

        request = {
            "id": "request-demo",
            "kind": "generation-request",
            "schema_version": "1.0",
            "character_ref": "character-demo@v001",
            "style_ref": "style-demo@v001",
            "pose": "standing",
            "expression": "neutral",
            "crop": "full-body",
            "facing": "front",
            "tool_id": "tool-approved",
            "model_id": "model-approved",
            "seed": 7,
            "license_status": "approved",
            "config": {"steps": 1},
            "output_intent": "evaluation",
            "provenance": {"source": "fixture"},
        }
        workflow = {
            "1": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 1}},
            "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        }
        bindings = {
            "seed": {"node_id": "1", "input": "seed", "source": "seed"},
            "steps": {"node_id": "1", "input": "steps", "source": "config.steps"},
        }

        def profile(identifier: str, profile_type: str) -> dict[str, object]:
            return {
                "kind": "tool-profile",
                "schema_version": "1.0",
                "id": identifier,
                "version": "v001",
                "profile_type": profile_type,
                "adapter_type": "comfyui-local-api",
                "runtime_type": "python",
                "offline_capability": "yes",
                "deterministic_seed_support": True,
                "control_capabilities": ["seed", "workflow"],
                "minimum_vram_gb": 0,
                "minimum_ram_gb": 0,
                "supported_operating_systems": ["linux"],
                "install_state": "installed",
                "evidence_references": [
                    {
                        "source_url": "https://example.invalid/evidence",
                        "retrieved_at": "2026-08-04",
                        "claim": "fixture",
                    }
                ],
                "license_evidence_state": "approved",
                "commercial_use_review_state": "approved",
                "decision_state": "approved",
            }

        self.request.write_bytes(canonical(request))
        self.workflow.write_text(json.dumps(workflow, indent=2), encoding="utf-8")
        self.bindings.write_text(json.dumps(bindings, indent=2), encoding="utf-8")
        self.tool.write_bytes(canonical(profile("tool-approved", "tool")))
        self.model.write_bytes(canonical(profile("model-approved", "model-configuration")))

        import hashlib

        limits = {
            "max_images": 2,
            "max_queue_response_bytes": 4096,
            "max_history_response_bytes": 65536,
            "max_png_bytes": 1048576,
            "max_total_png_bytes": 2097152,
            "request_timeout_seconds": 5,
            "poll_interval_ms": 50,
            "overall_timeout_seconds": 3,
        }
        core = {
            "kind": "comfyui-execution-profile",
            "schema_version": "1.0",
            "workflow_sha256": hashlib.sha256(self.workflow.read_bytes()).hexdigest(),
            "tool_profile_ref": "tool-approved",
            "model_profile_ref": "model-approved",
            "output_node_ids": ["9"],
            "expected_width": 2,
            "expected_height": 2,
            "limits": limits,
        }
        execution = {
            "id": content_identifier("comfyui-execution-profile", core, 20),
            **core,
        }
        self.execution.write_bytes(canonical(execution))


class ComfyUIExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = Fixture(self.root)
        png = encode_rgba_png(RGBAImage(2, 2, bytes([255, 0, 0, 255] * 4)))
        self.state = ServerState(png)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.state = self.state  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.output = self.root / "output"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary.cleanup()

    def execute(self, **kwargs: object) -> dict[str, object]:
        return run_comfyui_execution(
            self.fixture.request,
            self.fixture.workflow,
            self.fixture.bindings,
            self.fixture.tool,
            self.fixture.model,
            self.fixture.execution,
            self.output,
            endpoint=self.endpoint,
            execute=True,
            **kwargs,
        )

    def check(self, manifest: Path) -> dict[str, object]:
        return check_comfyui_execution(
            manifest,
            self.output,
            self.fixture.request,
            self.fixture.workflow,
            self.fixture.bindings,
            self.fixture.tool,
            self.fixture.model,
            self.fixture.execution,
            endpoint=self.endpoint,
        )

    def test_plan_success_offline_check_and_idempotency(self) -> None:
        first_plan = prepare_execution(
            self.fixture.request,
            self.fixture.workflow,
            self.fixture.bindings,
            self.fixture.tool,
            self.fixture.model,
            self.fixture.execution,
            endpoint=self.endpoint,
        )[0]
        second_plan = prepare_execution(
            self.fixture.request,
            self.fixture.workflow,
            self.fixture.bindings,
            self.fixture.tool,
            self.fixture.model,
            self.fixture.execution,
            endpoint=self.endpoint,
        )[0]
        self.assertEqual(first_plan, second_plan)
        self.assertEqual(first_plan["bound_values"], {"seed": 7, "steps": 1})
        self.assertNotIn(str(self.root), json.dumps(first_plan))

        first = self.execute()
        package = self.output / str(first["package_path"])
        checked = self.check(package / MANIFEST_FILE)
        second = self.execute()
        self.assertTrue(first["written"])
        self.assertEqual(checked["candidate_count"], 1)
        self.assertFalse(checked["network_contacted"])
        self.assertTrue(second["reused"])
        self.assertEqual(self.state.posts, 1)
        sent = json.loads(self.state.post_payloads[0])
        self.assertEqual(sent["prompt"]["1"]["inputs"]["seed"], 7)

    def test_acknowledgement_source_overlap_and_nonloopback_fail_before_network(self) -> None:
        with self.assertRaises(AdapterError) as caught:
            run_comfyui_execution(
                self.fixture.request,
                self.fixture.workflow,
                self.fixture.bindings,
                self.fixture.tool,
                self.fixture.model,
                self.fixture.execution,
                self.output,
                endpoint=self.endpoint,
                execute=False,
            )
        self.assertEqual(caught.exception.code, "EXECUTE_ACKNOWLEDGEMENT")

        with self.assertRaises(AdapterError) as caught:
            run_comfyui_execution(
                self.fixture.request,
                self.fixture.workflow,
                self.fixture.bindings,
                self.fixture.tool,
                self.fixture.model,
                self.fixture.execution,
                self.root,
                endpoint=self.endpoint,
                execute=True,
            )
        self.assertEqual(caught.exception.code, "OUTPUT_OVERLAP")

        with self.assertRaises(AdapterError) as caught:
            prepare_execution(
                self.fixture.request,
                self.fixture.workflow,
                self.fixture.bindings,
                self.fixture.tool,
                self.fixture.model,
                self.fixture.execution,
                endpoint="http://192.168.1.1:8188",
            )
        self.assertEqual(caught.exception.code, "UNSAFE_ENDPOINT")
        self.assertEqual(self.state.posts, 0)

    def test_proxy_is_ignored_and_redirect_is_rejected_with_cleanup(self) -> None:
        previous = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        try:
            self.assertTrue(self.execute()["written"])
        finally:
            if previous is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = previous

        self.output = self.root / "redirect-output"
        self.state.mode = "redirect"
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "HTTP_REDIRECT")
        self.assertFalse(any(self.output.glob(".*.tmp")) if self.output.exists() else False)

    def test_queue_history_and_output_schemas_fail_closed(self) -> None:
        cases = (
            ("duplicate-queue", "DUPLICATE_JSON_KEY"),
            ("unknown-queue", "QUEUE_RESPONSE_SCHEMA"),
            ("wrong-prompt", "HISTORY_PROMPT_ID"),
            ("wrong-node", "OUTPUT_NODES"),
            ("unsafe-name", "OUTPUT_DESCRIPTOR"),
            ("wrong-type", "OUTPUT_TYPE"),
            ("execution-error", "EXECUTION_ERROR"),
        )
        for mode, code in cases:
            with self.subTest(mode=mode):
                self.state.mode = mode
                with self.assertRaises(AdapterError) as caught:
                    self.execute()
                self.assertEqual(caught.exception.code, code)
                self.state.posts = 0

    def test_count_timeout_and_png_validation_limits(self) -> None:
        profile = json.loads(self.fixture.execution.read_text(encoding="utf-8"))
        profile["limits"]["max_images"] = 1
        core = {key: value for key, value in profile.items() if key != "id"}
        profile["id"] = content_identifier("comfyui-execution-profile", core, 20)
        self.fixture.execution.write_bytes(canonical(profile))
        self.state.mode = "two-images"
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "IMAGE_COUNT")

        self.fixture = Fixture(self.root)
        self.state.mode = "pending"
        values = iter([0.0, 0.0, 4.0])
        with self.assertRaises(AdapterError) as caught:
            self.execute(clock=lambda: next(values), sleeper=lambda _: None)
        self.assertEqual(caught.exception.code, "OVERALL_TIMEOUT")

        self.state.mode = "success"
        self.state.png = encode_rgba_png(RGBAImage(1, 1, bytes([0, 0, 0, 255])))
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "PNG_INVALID")

        self.state.png = encode_rgba_png(RGBAImage(2, 2, bytes([0, 0, 0, 255] * 4))).replace(
            b"sRGB", b"tEXt", 1
        )
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "PNG_INVALID")

    def test_input_validation_rejects_secret_incomplete_request_and_bad_profile(self) -> None:
        request = json.loads(self.fixture.request.read_text(encoding="utf-8"))
        request["api_token"] = "secret"
        self.fixture.request.write_bytes(canonical(request))
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "SECRET_LIKE_DATA")
        self.assertEqual(self.state.posts, 0)

        self.fixture = Fixture(self.root)
        request = json.loads(self.fixture.request.read_text(encoding="utf-8"))
        request.pop("character_ref")
        self.fixture.request.write_bytes(canonical(request))
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "REQUEST_VALIDATION")

        self.fixture = Fixture(self.root)
        tool = json.loads(self.fixture.tool.read_text(encoding="utf-8"))
        tool["version"] = "v1"
        self.fixture.tool.write_bytes(canonical(tool))
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "PROFILE_VALIDATION")

        self.fixture = Fixture(self.root)
        model = json.loads(self.fixture.model.read_text(encoding="utf-8"))
        model["decision_state"] = "reviewing"
        self.fixture.model.write_bytes(canonical(model))
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "PROFILE_APPROVAL")

    def test_noncanonical_and_duplicate_source_json_fail_closed(self) -> None:
        request = json.loads(self.fixture.request.read_text(encoding="utf-8"))
        self.fixture.request.write_text(json.dumps(request, indent=2), encoding="utf-8")
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "NONCANONICAL_JSON")

        self.fixture.request.write_text('{"id":"a","id":"b"}', encoding="utf-8")
        with self.assertRaises(AdapterError) as caught:
            self.execute()
        self.assertEqual(caught.exception.code, "DUPLICATE_JSON_KEY")

    def test_offline_checker_detects_tamper_extra_and_oversized_png(self) -> None:
        result = self.execute()
        package = self.output / str(result["package_path"])
        manifest = json.loads((package / MANIFEST_FILE).read_text(encoding="utf-8"))
        png = package / manifest["candidates"][0]["path"]
        original = png.read_bytes()

        png.write_bytes(original + b"x")
        with self.assertRaises(AdapterError) as caught:
            self.check(package / MANIFEST_FILE)
        self.assertEqual(caught.exception.code, "CANDIDATE_BYTES")

        png.write_bytes(b"x" * (1048576 + 1))
        with self.assertRaises(AdapterError) as caught:
            self.check(package / MANIFEST_FILE)
        self.assertEqual(caught.exception.code, "CANDIDATE_BYTES")

        png.write_bytes(original)
        (package / "extra.txt").write_text("extra", encoding="utf-8")
        with self.assertRaises(AdapterError) as caught:
            self.check(package / MANIFEST_FILE)
        self.assertEqual(caught.exception.code, "FILE_SET")


if __name__ == "__main__":
    unittest.main()
