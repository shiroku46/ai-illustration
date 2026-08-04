from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.naming import canonical_json, content_identifier
from ai_illustration.workspace import (
    DASHBOARD_MANIFEST,
    build_workspace_dashboard,
    check_workspace_dashboard,
    main,
    workspace_status,
)
from ai_illustration.workspace_checks import CHECKERS
from ai_illustration.workspace_common import CHECK_SPECS, WorkspaceError, load_workspace


def canonical(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def workspace_document(
    checks: list[dict[str, object]],
    project_name: str = "漫才キャラクターAI",
) -> dict[str, object]:
    core = {
        "kind": "ai-illustration-workspace",
        "schema_version": "1.0",
        "project_name": project_name,
        "checks": checks,
    }
    return {
        "id": content_identifier("ai-illustration-workspace", core, 20),
        **core,
    }


def manifest_check(
    identifier: str,
    artifact: str,
    dependencies: list[str],
    action: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": identifier,
        "kind": "manifest-set",
        "depends_on": dependencies,
        "arguments": {"path": artifact},
        "action": action,
    }


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace_path = self.root / "workspace.json"
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.dashboard_root = self.root / "dashboards"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_workspace(
        self, checks: list[dict[str, object]]
    ) -> dict[str, object]:
        value = workspace_document(checks)
        self.workspace_path.write_bytes(canonical(value))
        return value

    def complete_checker(self):
        return patch.dict(
            CHECKERS,
            {"manifest-set": lambda arguments: {"ok": True}},
            clear=False,
        )

    def test_status_states_counts_and_first_action(self) -> None:
        complete = self.artifacts / "complete.json"
        complete.write_text("{}", encoding="utf-8")
        self.write_workspace(
            [
                manifest_check("inputs", "artifacts/complete.json", []),
                manifest_check(
                    "generation",
                    "artifacts/missing.json",
                    ["inputs"],
                    {
                        "type": "human",
                        "label": "ComfyUIを起動して承認済みworkflowを実行する",
                        "argv": [],
                    },
                ),
                manifest_check(
                    "variants", "artifacts/variants.json", ["generation"]
                ),
            ]
        )
        with self.complete_checker():
            status = workspace_status(self.workspace_path)
        self.assertEqual(
            [item["status"] for item in status["checks"]],
            ["complete", "not-started", "blocked"],
        )
        self.assertEqual(
            status["counts"],
            {
                "total": 3,
                "complete": 1,
                "not_started": 1,
                "blocked": 1,
                "invalid": 0,
            },
        )
        self.assertEqual(status["next"]["check_id"], "generation")
        self.assertEqual(status["next"]["action"]["type"], "human")
        self.assertFalse(status["network_contacted"])
        self.assertFalse(status["external_process_started"])

    def test_invalid_diagnostic_is_machine_path_independent(self) -> None:
        artifact = self.artifacts / "bad.json"
        artifact.write_text("bad", encoding="utf-8")
        self.write_workspace(
            [manifest_check("inputs", "artifacts/bad.json", [])]
        )

        def fail(arguments):
            raise WorkspaceError(
                "BROKEN",
                f"invalid at {self.root}/artifacts/bad.json",
                str(self.root / "artifacts/bad.json"),
            )

        with patch.dict(
            CHECKERS, {"manifest-set": fail}, clear=False
        ):
            status = workspace_status(self.workspace_path)
        diagnostic = status["checks"][0]["diagnostics"][0]
        self.assertEqual(status["checks"][0]["status"], "invalid")
        self.assertNotIn(str(self.root), diagnostic["message"])
        self.assertNotIn(str(self.root), diagnostic["field"])
        self.assertEqual(status["next"]["status"], "invalid")

    def test_complete_workspace_and_module_check_exit(self) -> None:
        artifact = self.artifacts / "complete.json"
        artifact.write_text("{}", encoding="utf-8")
        self.write_workspace(
            [manifest_check("inputs", "artifacts/complete.json", [])]
        )
        stdout_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
        with self.complete_checker(), patch(
            "sys.stdout", stdout
        ), redirect_stderr(io.StringIO()):
            status = workspace_status(self.workspace_path)
            exit_code = main(["check", str(self.workspace_path)])
            stdout.flush()
        self.assertTrue(status["complete"])
        self.assertIsNone(status["next"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(stdout_bytes.getvalue())["complete"])

    def test_workspace_format_and_secret_failures(self) -> None:
        valid = manifest_check("inputs", "artifacts/one.json", [])
        cases: list[tuple[str, dict[str, object] | bytes, str]] = []

        wrong_id = workspace_document([valid])
        wrong_id["id"] = "ai-illustration-workspace-" + "0" * 20
        cases.append(("wrong-id", wrong_id, "WORKSPACE_ID"))
        cases.append(
            (
                "duplicate",
                workspace_document(
                    [
                        valid,
                        manifest_check(
                            "inputs", "artifacts/two.json", []
                        ),
                    ]
                ),
                "DUPLICATE_CHECK",
            )
        )
        cases.append(
            (
                "forward",
                workspace_document(
                    [
                        manifest_check(
                            "second",
                            "artifacts/two.json",
                            ["first"],
                        )
                    ]
                ),
                "FORWARD_DEPENDENCY",
            )
        )
        cases.append(
            (
                "arguments",
                workspace_document(
                    [
                        {
                            "id": "inputs",
                            "kind": "manifest-set",
                            "depends_on": [],
                            "arguments": {
                                "wrong": "artifacts/one.json"
                            },
                            "action": None,
                        }
                    ]
                ),
                "CHECK_ARGUMENTS",
            )
        )
        cases.append(
            (
                "unsafe",
                workspace_document(
                    [manifest_check("inputs", "/absolute/path", [])]
                ),
                "UNSAFE_PATH",
            )
        )
        cases.append(
            (
                "secret",
                workspace_document(
                    [
                        manifest_check(
                            "inputs",
                            "artifacts/one.json",
                            [],
                            {
                                "type": "command",
                                "label": "run",
                                "argv": [
                                    "tool",
                                    "--api-token",
                                    "value",
                                ],
                            },
                        )
                    ]
                ),
                "SECRET_LIKE_DATA",
            )
        )
        noncanonical = workspace_document([valid])
        cases.append(
            (
                "noncanonical",
                json.dumps(noncanonical, indent=2).encode(),
                "NONCANONICAL_JSON",
            )
        )

        for name, value, code in cases:
            with self.subTest(name=name):
                self.workspace_path.write_bytes(
                    value if isinstance(value, bytes) else canonical(value)
                )
                with self.assertRaises(Exception) as caught:
                    load_workspace(self.workspace_path)
                self.assertEqual(
                    getattr(caught.exception, "code", None), code
                )

    def test_symlink_argument_and_registry_coverage(self) -> None:
        self.assertEqual(set(CHECKERS), set(CHECK_SPECS))
        outside = self.root / "outside"
        outside.mkdir()
        link = self.artifacts / "link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        self.write_workspace(
            [manifest_check("inputs", "artifacts/link/data.json", [])]
        )
        with self.assertRaises(WorkspaceError) as caught:
            load_workspace(self.workspace_path)
        self.assertEqual(caught.exception.code, "PATH_SYMLINK")

    def test_dashboard_lifecycle_and_local_only_content(self) -> None:
        artifact = self.artifacts / "complete.json"
        artifact.write_text("{}", encoding="utf-8")
        self.write_workspace(
            [manifest_check("inputs", "artifacts/complete.json", [])]
        )
        with self.complete_checker():
            dry = build_workspace_dashboard(
                self.workspace_path, self.dashboard_root
            )
            self.assertFalse(dry["written"])
            self.assertFalse(self.dashboard_root.exists())
            first = build_workspace_dashboard(
                self.workspace_path, self.dashboard_root, write=True
            )
            second = build_workspace_dashboard(
                self.workspace_path, self.dashboard_root, write=True
            )
            package = self.dashboard_root / first["package_path"]
            checked = check_workspace_dashboard(
                package / DASHBOARD_MANIFEST,
                self.workspace_path,
                self.dashboard_root,
            )
        self.assertTrue(first["written"])
        self.assertFalse(second["written"])
        self.assertTrue(checked["ok"])
        combined = "".join(
            (package / name).read_text(encoding="utf-8")
            for name in ("index.html", "app.js")
        )
        for forbidden in (
            "http:",
            "https:",
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
            "localStorage",
        ):
            self.assertNotIn(forbidden, combined)
        self.assertIn(
            "connect-src 'none'",
            (package / "index.html").read_text(encoding="utf-8"),
        )

    def test_dashboard_tamper_extra_symlink_and_overlap_fail(self) -> None:
        artifact = self.artifacts / "complete.json"
        artifact.write_text("{}", encoding="utf-8")
        self.write_workspace(
            [manifest_check("inputs", "artifacts/complete.json", [])]
        )
        with self.complete_checker():
            result = build_workspace_dashboard(
                self.workspace_path, self.dashboard_root, write=True
            )
        package = self.dashboard_root / result["package_path"]
        app = package / "app.js"
        original = app.read_bytes()

        app.write_bytes(original + b"x")
        with self.complete_checker(), self.assertRaises(
            WorkspaceError
        ) as caught:
            check_workspace_dashboard(
                package / DASHBOARD_MANIFEST,
                self.workspace_path,
                self.dashboard_root,
            )
        self.assertEqual(caught.exception.code, "FILE_MISMATCH")

        app.write_bytes(original)
        (package / "extra.txt").write_text("extra", encoding="utf-8")
        with self.complete_checker(), self.assertRaises(
            WorkspaceError
        ) as caught:
            check_workspace_dashboard(
                package / DASHBOARD_MANIFEST,
                self.workspace_path,
                self.dashboard_root,
            )
        self.assertEqual(caught.exception.code, "FILE_SET")
        (package / "extra.txt").unlink()

        target = package / "style.css"
        original_css = target.read_bytes()
        target.unlink()
        try:
            target.symlink_to(self.root / "outside.css")
        except (OSError, NotImplementedError):
            target.write_bytes(original_css)
        else:
            with self.complete_checker(), self.assertRaises(
                WorkspaceError
            ) as caught:
                check_workspace_dashboard(
                    package / DASHBOARD_MANIFEST,
                    self.workspace_path,
                    self.dashboard_root,
                )
            self.assertEqual(
                caught.exception.code, "PACKAGE_SYMLINK"
            )

        source_directory = self.artifacts / "source"
        source_directory.mkdir()
        self.write_workspace(
            [manifest_check("inputs", "artifacts/source", [])]
        )
        with self.complete_checker(), self.assertRaises(
            WorkspaceError
        ) as caught:
            build_workspace_dashboard(
                self.workspace_path,
                source_directory / "dashboard",
                write=True,
            )
        self.assertEqual(caught.exception.code, "OUTPUT_OVERLAP")


if __name__ == "__main__":
    unittest.main()
