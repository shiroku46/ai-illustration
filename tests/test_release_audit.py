from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_illustration.naming import canonical_json, content_identifier
import ai_illustration.release_audit as release_audit
from ai_illustration.release_audit import (
    CONTENT_COMPLETION_DEFINITION,
    EXPECTED_DEDICATED_COMMANDS,
    EXPECTED_ENTRY_POINTS,
    EXPECTED_MAIN_COMMANDS,
    EXPECTED_WORKSPACE_KINDS,
    MINIMUM_CRITICAL_PATHS,
    MINIMUM_EXTERNAL_PREREQUISITES,
    MINIMUM_HUMAN_DECISIONS,
    MINIMUM_PROHIBITED_EFFECTS,
    MINIMUM_PROTECTED_SOURCES,
    RELEASE_VERSION,
    SOFTWARE_COMPLETION_DEFINITION,
    ReleaseAuditError,
    audit_release,
    main,
)


def canonical(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def contract_document() -> dict[str, object]:
    core: dict[str, object] = {
        "kind": "ai-illustration-mvp-release",
        "schema_version": "1.0",
        "release_name": "AI Illustration Pipeline Software MVP",
        "version": RELEASE_VERSION,
        "python_requires": ">=3.11",
        "runtime_dependencies": [],
        "workspace_check_kinds": sorted(EXPECTED_WORKSPACE_KINDS),
        "main_cli_commands": sorted(EXPECTED_MAIN_COMMANDS),
        "dedicated_cli_commands": {
            name: sorted(commands)
            for name, commands in sorted(EXPECTED_DEDICATED_COMMANDS.items())
        },
        "entry_points": dict(sorted(EXPECTED_ENTRY_POINTS.items())),
        "critical_paths": sorted(MINIMUM_CRITICAL_PATHS),
        "protected_sources": sorted(MINIMUM_PROTECTED_SOURCES),
        "external_prerequisites": sorted(MINIMUM_EXTERNAL_PREREQUISITES),
        "human_decisions": sorted(MINIMUM_HUMAN_DECISIONS),
        "prohibited_effects": sorted(MINIMUM_PROHIBITED_EFFECTS),
        "software_completion_definition": SOFTWARE_COMPLETION_DEFINITION,
        "content_completion_definition": CONTENT_COMPLETION_DEFINITION,
    }
    return {
        "id": content_identifier("ai-illustration-mvp-release", core, 20),
        **core,
    }


class ReleaseAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.release_dir = self.root / "release"
        self.release_dir.mkdir()
        self.contract = self.release_dir / "mvp-v1.json"
        self._write_repository()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_repository(self) -> None:
        for relative in MINIMUM_CRITICAL_PATHS:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture: {relative}\n", encoding="utf-8")
        workspace_schema = {
            "$defs": {
                "check": {
                    "properties": {
                        "kind": {"enum": sorted(EXPECTED_WORKSPACE_KINDS)}
                    }
                }
            }
        }
        (self.root / "schemas/ai-illustration-workspace.schema.json").write_text(
            json.dumps(workspace_schema, sort_keys=True),
            encoding="utf-8",
        )
        script_lines = "\n".join(
            f'{name} = "{target}"'
            for name, target in EXPECTED_ENTRY_POINTS.items()
        )
        (self.root / "pyproject.toml").write_text(
            "[project]\n"
            'name = "fixture"\n'
            f'version = "{RELEASE_VERSION}"\n'
            'requires-python = ">=3.11"\n'
            "dependencies = []\n\n"
            "[project.scripts]\n"
            f"{script_lines}\n",
            encoding="utf-8",
        )
        self.contract.write_bytes(canonical(contract_document()))

    def test_complete_audit_is_deterministic_and_non_mutating(self) -> None:
        before = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
        )
        first = audit_release(self.contract)
        second = audit_release(self.contract)
        after = sorted(
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
        )
        self.assertEqual(first, second)
        self.assertTrue(first["complete"])
        self.assertEqual(first["diagnostics"], [])
        self.assertEqual(first["critical_file_count"], len(MINIMUM_CRITICAL_PATHS))
        self.assertEqual(
            [item["path"] for item in first["critical_files"]],
            sorted(MINIMUM_CRITICAL_PATHS),
        )
        self.assertEqual(before, after)
        self.assertFalse(first["network_contacted"])
        self.assertFalse(first["external_process_started"])
        self.assertFalse(first["filesystem_mutated"])

    def test_version_scripts_dependencies_and_registry_mismatches(self) -> None:
        pyproject = self.root / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        pyproject.write_text(text.replace('version = "1.0.0"', 'version = "0.9.0"'), encoding="utf-8")
        result = audit_release(self.contract)
        self.assertIn("PYPROJECT_VERSION", {item["code"] for item in result["diagnostics"]})

        self._write_repository()
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace("dependencies = []", 'dependencies = ["requests"]'),
            encoding="utf-8",
        )
        result = audit_release(self.contract)
        self.assertIn("RUNTIME_DEPENDENCIES", {item["code"] for item in result["diagnostics"]})

        self._write_repository()
        pyproject.write_text(
            pyproject.read_text(encoding="utf-8").replace("ai-illustration-workspace", "ai-illustration-workspace-broken", 1),
            encoding="utf-8",
        )
        result = audit_release(self.contract)
        self.assertIn("ENTRY_POINTS", {item["code"] for item in result["diagnostics"]})

        with patch.object(release_audit, "CHECKERS", {key: value for key, value in release_audit.CHECKERS.items() if key != "video-export"}):
            result = audit_release(self.contract)
        self.assertIn("WORKSPACE_REGISTRY", {item["code"] for item in result["diagnostics"]})

    def test_missing_symlink_oversized_and_protected_tamper_fail(self) -> None:
        critical = self.root / sorted(MINIMUM_CRITICAL_PATHS)[0]
        critical.unlink()
        result = audit_release(self.contract)
        self.assertIn("CRITICAL_FILE_TYPE", {item["code"] for item in result["diagnostics"]})

        self._write_repository()
        critical = self.root / sorted(MINIMUM_CRITICAL_PATHS)[0]
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        critical.unlink()
        try:
            critical.symlink_to(outside)
        except (OSError, NotImplementedError):
            pass
        else:
            result = audit_release(self.contract)
            self.assertIn("PATH_SYMLINK", {item["code"] for item in result["diagnostics"]})

        self._write_repository()
        critical = self.root / sorted(MINIMUM_CRITICAL_PATHS)[0]
        critical.write_bytes(b"x" * 17)
        with patch.object(release_audit, "MAX_CRITICAL_FILE_BYTES", 16):
            result = audit_release(self.contract)
        self.assertIn("CRITICAL_FILE_SIZE", {item["code"] for item in result["diagnostics"]})

        self._write_repository()
        protected = self.root / sorted(MINIMUM_PROTECTED_SOURCES)[0]
        protected.write_text("def bad():\n    return shell=True\n", encoding="utf-8")
        result = audit_release(self.contract)
        self.assertIn("FORBIDDEN_SOURCE_PATTERN", {item["code"] for item in result["diagnostics"]})

    def test_contract_format_and_binding_failures(self) -> None:
        cases: list[tuple[str, bytes, str]] = []
        wrong_id = contract_document()
        wrong_id["id"] = "ai-illustration-mvp-release-" + "0" * 20
        cases.append(("wrong-id", canonical(wrong_id), "RELEASE_ID"))

        unsafe = contract_document()
        unsafe["critical_paths"] = sorted([*unsafe["critical_paths"], "../escape"])
        unsafe_core = {key: unsafe[key] for key in unsafe if key != "id"}
        unsafe["id"] = content_identifier("ai-illustration-mvp-release", unsafe_core, 20)
        cases.append(("unsafe", canonical(unsafe), "UNSAFE_PATH"))

        secret = contract_document()
        secret["api_token"] = "secret"
        cases.append(("secret", canonical(secret), "SECRET_LIKE_DATA"))

        noncanonical = contract_document()
        cases.append(("noncanonical", json.dumps(noncanonical, indent=2).encode("utf-8"), "NONCANONICAL_JSON"))
        cases.append(("duplicate", b'{"id":"a","id":"b"}\n', "DUPLICATE_JSON_KEY"))

        for name, payload, code in cases:
            with self.subTest(name=name):
                self.contract.write_bytes(payload)
                with self.assertRaises(ReleaseAuditError) as caught:
                    audit_release(self.contract)
                self.assertEqual(caught.exception.code, code)

    def test_module_cli_outputs_canonical_result(self) -> None:
        stdout_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="utf-8")
        with patch("sys.stdout", stdout), redirect_stderr(io.StringIO()):
            exit_code = main([str(self.contract)])
            stdout.flush()
        payload = stdout_bytes.getvalue()
        result = json.loads(payload)
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["complete"])
        self.assertEqual(payload, canonical(result))


if __name__ == "__main__":
    unittest.main()
