from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.comfyui_smoke import (
    BINDINGS_FILE,
    EXECUTION_FILE,
    MANIFEST_FILE,
    MODEL_FILE,
    REQUEST_FILE,
    TOOL_FILE,
    WORKFLOW_FILE,
    SmokeError,
    check_bundle,
    inspect_workflow,
    main,
    prepare_bundle,
)
from ai_illustration.naming import canonical_json


def workflow_value(*, second_sampler: bool = False) -> dict[str, object]:
    workflow: dict[str, object] = {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "sd_xl_turbo_1.0_fp16.safetensors"},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["1", 1], "text": "1girl, standing, simple background"},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"clip": ["1", 1], "text": "blurry, watermark"},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"batch_size": 1, "height": 512, "width": 512},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "cfg": 1.0,
                "denoise": 1.0,
                "latent_image": ["4", 0],
                "model": ["1", 0],
                "negative": ["3", 0],
                "positive": ["2", 0],
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
                "seed": 123456,
                "steps": 4,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "ComfyUI", "images": ["6", 0]},
        },
    }
    if second_sampler:
        workflow["15"] = {
            "class_type": "KSampler",
            "inputs": dict(workflow["5"]["inputs"]),  # type: ignore[index]
        }
    return workflow


class ComfyUISmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input_root = self.root / "input"
        self.output_root = self.root / "bundles"
        self.input_root.mkdir()
        self.workflow_path = self.input_root / "owner-workflow.json"
        self.workflow_path.write_text(
            json.dumps(workflow_value(), indent=2), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def approved(self, *, write: bool = False, **kwargs: object) -> dict[str, object]:
        return prepare_bundle(
            self.workflow_path,
            self.output_root,
            profile_state="approved",
            review_date="2026-08-05",
            tool_evidence_url="https://github.com/comfyanonymous/ComfyUI",
            model_evidence_url="https://huggingface.co/stabilityai/sdxl-turbo",
            confirm_tool_license=True,
            confirm_model_license=True,
            confirm_commercial_use=True,
            write=write,
            **kwargs,
        )

    def test_inspect_traces_graph_and_builds_scalar_bindings(self) -> None:
        first = inspect_workflow(self.workflow_path)
        second = inspect_workflow(self.workflow_path)
        self.assertEqual(first, second)
        self.assertTrue(first["ok"])
        self.assertEqual(first["selection"]["sampler_node_id"], "5")
        self.assertEqual(first["selection"]["checkpoint_node_id"], "1")
        self.assertEqual(first["selection"]["size_node_id"], "4")
        self.assertEqual(first["selection"]["positive_node_id"], "2")
        self.assertEqual(first["selection"]["negative_node_id"], "3")
        self.assertEqual(first["selection"]["output_node_ids"], ["9"])
        self.assertEqual(first["values"]["seed"], 123456)
        self.assertEqual(first["values"]["steps"], 4)
        self.assertEqual(first["values"]["width"], 512)
        self.assertEqual(first["values"]["height"], 512)
        self.assertEqual(
            first["bindings"]["checkpoint_name"]["source"],
            "config.checkpoint_name",
        )
        self.assertEqual(first["bindings"]["seed"]["source"], "seed")
        self.assertFalse(first["filesystem_mutated"])
        self.assertFalse(first["network_contacted"])

    def test_explicit_override_resolves_ambiguous_sampler(self) -> None:
        self.workflow_path.write_text(
            json.dumps(workflow_value(second_sampler=True)), encoding="utf-8"
        )
        ambiguous = inspect_workflow(self.workflow_path)
        self.assertFalse(ambiguous["ok"])
        self.assertIn(
            "NODE_AMBIGUOUS", {item["code"] for item in ambiguous["diagnostics"]}
        )
        selected = inspect_workflow(self.workflow_path, sampler_node="5")
        self.assertTrue(selected["ok"])
        self.assertEqual(selected["selection"]["sampler_node_id"], "5")

    def test_duplicate_secret_and_missing_output_fail_closed(self) -> None:
        self.workflow_path.write_text(
            '{"1":{"class_type":"KSampler","inputs":{}},"1":{}}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(SmokeError, "DUPLICATE_JSON_KEY"):
            inspect_workflow(self.workflow_path)

        secret = workflow_value()
        secret["1"]["inputs"]["api_token"] = "secret"  # type: ignore[index]
        self.workflow_path.write_text(json.dumps(secret), encoding="utf-8")
        with self.assertRaisesRegex(SmokeError, "SECRET_LIKE_DATA"):
            inspect_workflow(self.workflow_path)

        missing = workflow_value()
        missing.pop("9")
        self.workflow_path.write_text(json.dumps(missing), encoding="utf-8")
        report = inspect_workflow(self.workflow_path)
        self.assertFalse(report["ok"])
        self.assertIn("NODE_MISSING", {item["code"] for item in report["diagnostics"]})

    def test_reviewing_bundle_is_dry_by_default_and_preserves_checkpoint_identity(self) -> None:
        result = prepare_bundle(
            self.workflow_path,
            self.output_root,
            review_date="2026-08-05",
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["written"])
        self.assertFalse(result["execution_ready"])
        self.assertFalse(self.output_root.exists())

        written = prepare_bundle(
            self.workflow_path,
            self.output_root,
            review_date="2026-08-05",
            write=True,
        )
        package = self.output_root / str(written["bundle_path"])
        request = json.loads((package / REQUEST_FILE).read_text(encoding="utf-8"))
        model = json.loads((package / MODEL_FILE).read_text(encoding="utf-8"))
        bindings = json.loads((package / BINDINGS_FILE).read_text(encoding="utf-8"))
        self.assertNotEqual(request["model_id"], request["config"]["checkpoint_name"])
        self.assertEqual(model["id"], request["model_id"])
        self.assertEqual(
            bindings["checkpoint_name"]["source"], "config.checkpoint_name"
        )
        self.assertEqual(request["license_status"], "reviewing")
        self.assertTrue(check_bundle(package / MANIFEST_FILE, self.output_root)["ok"])

    def test_approved_state_requires_all_human_acknowledgements_and_evidence(self) -> None:
        with self.assertRaisesRegex(SmokeError, "APPROVAL_ACKNOWLEDGEMENT"):
            prepare_bundle(
                self.workflow_path,
                self.output_root,
                profile_state="approved",
                review_date="2026-08-05",
            )
        with self.assertRaisesRegex(SmokeError, "EVIDENCE_URL"):
            prepare_bundle(
                self.workflow_path,
                self.output_root,
                profile_state="approved",
                review_date="2026-08-05",
                confirm_tool_license=True,
                confirm_model_license=True,
                confirm_commercial_use=True,
            )
        with self.assertRaisesRegex(SmokeError, "REVIEW_DATE"):
            prepare_bundle(
                self.workflow_path,
                self.output_root,
                review_date="2026-99-99",
            )

    def test_approved_write_is_offline_valid_idempotent_and_checkable(self) -> None:
        first = self.approved(write=True)
        self.assertTrue(first["written"])
        self.assertTrue(first["execution_ready"])
        package = self.output_root / str(first["bundle_path"])
        self.assertEqual(
            {path.name for path in package.iterdir()},
            {
                WORKFLOW_FILE,
                REQUEST_FILE,
                BINDINGS_FILE,
                TOOL_FILE,
                MODEL_FILE,
                EXECUTION_FILE,
                MANIFEST_FILE,
            },
        )
        manifest_payload = (package / MANIFEST_FILE).read_bytes()
        manifest = json.loads(manifest_payload)
        self.assertEqual(manifest_payload, canonical_json(manifest) + b"\n")
        checked = check_bundle(package / MANIFEST_FILE, self.output_root)
        self.assertTrue(checked["ok"])
        self.assertTrue(checked["execution_ready"])
        self.assertFalse(checked["network_contacted"])
        second = self.approved(write=True)
        self.assertFalse(second["written"])
        self.assertTrue(second["idempotent"])

    def test_checker_rejects_tamper_extra_and_conflicting_existing_output(self) -> None:
        result = self.approved(write=True)
        package = self.output_root / str(result["bundle_path"])
        request_path = package / REQUEST_FILE
        original = request_path.read_bytes()
        request_path.write_bytes(original + b" ")
        with self.assertRaisesRegex(SmokeError, "FILE_MISMATCH"):
            check_bundle(package / MANIFEST_FILE, self.output_root)
        with self.assertRaisesRegex(SmokeError, "OUTPUT_CONFLICT"):
            self.approved(write=True)
        request_path.write_bytes(original)
        extra = package / "extra.txt"
        extra.write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(SmokeError, "FILE_SET_MISMATCH"):
            check_bundle(package / MANIFEST_FILE, self.output_root)

    def test_output_overlap_and_staging_conflict_are_non_destructive(self) -> None:
        with self.assertRaisesRegex(SmokeError, "OUTPUT_OVERLAP"):
            prepare_bundle(
                self.workflow_path,
                self.input_root,
                review_date="2026-08-05",
                write=True,
            )
        dry = self.approved()
        staging = self.output_root / f".{dry['bundle_path']}.tmp"
        staging.mkdir(parents=True)
        marker = staging / "keep.txt"
        marker.write_text("owned", encoding="utf-8")
        with self.assertRaisesRegex(SmokeError, "STAGING_CONFLICT"):
            self.approved(write=True)
        self.assertEqual(marker.read_text(encoding="utf-8"), "owned")

    def test_symlinked_workflow_and_packaged_file_fail_closed(self) -> None:
        alias = self.root / "workflow-link.json"
        try:
            alias.symlink_to(self.workflow_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(SmokeError, "PATH_SYMLINK"):
            inspect_workflow(alias)

        result = self.approved(write=True)
        package = self.output_root / str(result["bundle_path"])
        request = package / REQUEST_FILE
        outside = self.root / "outside.json"
        outside.write_bytes(request.read_bytes())
        request.unlink()
        try:
            request.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("package symlinks unavailable")
        with self.assertRaisesRegex(SmokeError, "FILE_TYPE"):
            check_bundle(package / MANIFEST_FILE, self.output_root)

    def test_duplicate_manifest_and_cli_failure_are_structured(self) -> None:
        result = self.approved(write=True)
        package = self.output_root / str(result["bundle_path"])
        manifest_path = package / MANIFEST_FILE
        manifest_path.write_text('{"id":"a","id":"b"}\n', encoding="utf-8")
        with self.assertRaisesRegex(SmokeError, "DUPLICATE_JSON_KEY"):
            check_bundle(manifest_path, self.output_root)

        missing = self.root / "missing.json"
        stdout_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
        with patch("sys.stdout", stdout), redirect_stderr(io.StringIO()):
            code = main(["inspect", str(missing)])
            stdout.flush()
        payload = json.loads(stdout_bytes.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["diagnostics"][0]["code"], "WORKFLOW_MISSING")

    def test_module_cli_outputs_canonical_json(self) -> None:
        stdout_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
        stderr = io.StringIO()
        with patch("sys.stdout", stdout), redirect_stderr(stderr):
            code = main(["inspect", str(self.workflow_path)])
            stdout.flush()
        payload = stdout_bytes.getvalue()
        result = json.loads(payload)
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(payload, canonical_json(result) + b"\n")
        self.assertIn("ComfyUI smoke inspect: ok", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
