from __future__ import annotations

import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_illustration.adapters import AdapterError, ComfyUIAdapter, load_json_object, sanitize_loopback_endpoint
from ai_illustration.adapters.comfyui import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class ComfyUIAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ComfyUIAdapter()
        self.workflow = load_json_object(FIXTURES / "comfyui" / "workflow-api.json")
        self.bindings = load_json_object(FIXTURES / "comfyui" / "bindings.json")
        self.request = load_json_object(FIXTURES / "valid" / "generation-request.json")

    def test_plan_is_byte_deterministic(self) -> None:
        first = canonical_json_bytes(self.adapter.plan(self.request, self.workflow, self.bindings).to_dict())
        second = canonical_json_bytes(self.adapter.plan(self.request, self.workflow, self.bindings).to_dict())
        self.assertEqual(first, second)

    def test_loopback_endpoints_are_accepted_and_canonicalized(self) -> None:
        self.assertEqual("http://localhost:8188", sanitize_loopback_endpoint("http://localhost:8188/"))
        self.assertEqual("http://127.0.0.1:8188", sanitize_loopback_endpoint("http://127.0.0.1:8188"))
        self.assertEqual("http://[::1]:8188", sanitize_loopback_endpoint("http://[::1]:8188"))

    def test_unsafe_endpoints_fail_closed(self) -> None:
        unsafe = [
            "https://localhost:8188",
            "http://example.com:8188",
            "http://user:pass@localhost:8188",
            "http://localhost:8188?token=x",
            "http://localhost:8188/#fragment",
            "http://localhost:8188/../admin",
            "file:///tmp/socket",
        ]
        for endpoint in unsafe:
            with self.subTest(endpoint=endpoint), self.assertRaises(AdapterError):
                sanitize_loopback_endpoint(endpoint)

    def test_unknown_node_and_input_bindings_are_rejected(self) -> None:
        unknown_node = copy.deepcopy(self.bindings)
        unknown_node["seed"]["node_id"] = "999"
        with self.assertRaisesRegex(AdapterError, "binding node"):
            self.adapter.plan(self.request, self.workflow, unknown_node)
        unknown_input = copy.deepcopy(self.bindings)
        unknown_input["seed"]["input"] = "missing"
        with self.assertRaisesRegex(AdapterError, "binding input"):
            self.adapter.plan(self.request, self.workflow, unknown_input)

    def test_workflow_checksum_changes_with_content(self) -> None:
        first = self.adapter.check_workflow(self.workflow)["workflow_sha256"]
        changed = copy.deepcopy(self.workflow)
        changed["2"]["inputs"]["steps"] = 21
        second = self.adapter.check_workflow(changed)["workflow_sha256"]
        self.assertNotEqual(first, second)

    def test_secret_like_keys_and_values_are_rejected(self) -> None:
        secret_binding = copy.deepcopy(self.bindings)
        secret_binding["api_token"] = secret_binding.pop("seed")
        with self.assertRaisesRegex(AdapterError, "secret-like"):
            self.adapter.plan(self.request, self.workflow, secret_binding)
        secret_request = copy.deepcopy(self.request)
        secret_request["output_intent"] = "Bearer hidden"
        with self.assertRaisesRegex(AdapterError, "secret-like"):
            self.adapter.plan(secret_request, self.workflow, self.bindings)

    def test_license_and_model_state_control_readiness_not_dry_run(self) -> None:
        plan = self.adapter.plan(self.request, self.workflow, self.bindings)
        self.assertTrue(plan.dry_run)
        self.assertTrue(plan.executable_ready)
        unreviewed = {**self.request, "license_status": "unreviewed"}
        unreviewed_plan = self.adapter.plan(unreviewed, self.workflow, self.bindings)
        self.assertTrue(unreviewed_plan.dry_run)
        self.assertFalse(unreviewed_plan.executable_ready)
        self.assertIn("model-license-not-approved", unreviewed_plan.readiness_reasons)
        unresolved = {**self.request, "model_id": "unresolved"}
        unresolved_plan = self.adapter.plan(unresolved, self.workflow, self.bindings)
        self.assertFalse(unresolved_plan.executable_ready)
        self.assertIn("model-identifier-unresolved", unresolved_plan.readiness_reasons)

    def test_planning_never_opens_socket_or_subprocess(self) -> None:
        with mock.patch.object(socket, "socket") as socket_mock, mock.patch.object(subprocess, "run") as run_mock:
            self.adapter.plan(self.request, self.workflow, self.bindings)
        socket_mock.assert_not_called()
        run_mock.assert_not_called()

    def test_execution_is_explicitly_disabled(self) -> None:
        with self.assertRaisesRegex(AdapterError, "not authorized"):
            self.adapter.execute()


if __name__ == "__main__":
    unittest.main()
