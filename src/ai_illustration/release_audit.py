"""Deterministic read-only audit for the software MVP 1.0 release contract."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any, Sequence

from . import __version__
from .adapters.base import AdapterError
from .adapters.comfyui import ComfyUIAdapter, _scan_for_secrets
from .cli import build_parser
from .frame_preview import _parser as frame_preview_parser
from .naming import canonical_json, content_identifier, safe_relative_path
from .video_export import _parser as video_export_parser
from .workspace import _parser as workspace_parser
from .workspace_checks import CHECKERS

RELEASE_KIND = "ai-illustration-mvp-release"
RELEASE_VERSION = "1.0.0"
MAX_CONTRACT_BYTES = 4 * 1024 * 1024
MAX_CRITICAL_FILE_BYTES = 16 * 1024 * 1024
SOFTWARE_COMPLETION_DEFINITION = (
    "Software MVP is complete when the release audit, repository validation, "
    "and full automated test suite succeed for all declared local generation, "
    "review, packaging, paper-theater, rendering, video-export, and workspace capabilities."
)
CONTENT_COMPLETION_DEFINITION = (
    "Project content is complete only after the owner supplies approved local tools, "
    "models, workflows, source audio, human character and variant reviews, licensing "
    "decisions, and any desired final encoded media."
)

EXPECTED_WORKSPACE_KINDS = frozenset(
    {
        "manifest-set",
        "comfyui-execution",
        "variant-set",
        "export-package",
        "scene-plan",
        "preview-package",
        "audio-preview",
        "render-plan",
        "renderer-job",
        "frame-render",
        "frame-preview",
        "video-export",
    }
)
EXPECTED_MAIN_COMMANDS = frozenset(
    {
        "validate",
        "catalog-validate",
        "catalog-list",
        "catalog-compat",
        "adapter-check",
        "adapter-plan",
        "adapter-run",
        "adapter-run-check",
        "review-ui",
        "variant-plan",
        "variant-check",
        "variant-export",
        "export-check",
        "paper-plan",
        "paper-check",
        "preview-plan",
        "preview-check",
        "audio-preview-plan",
        "audio-preview-check",
        "render-plan",
        "render-plan-check",
        "composition-job",
        "composition-job-check",
        "frame-render",
        "frame-render-check",
    }
)
EXPECTED_DEDICATED_COMMANDS = {
    "workspace": frozenset({"status", "check", "build", "dashboard-check"}),
    "frame_preview": frozenset({"build", "check"}),
    "video_export": frozenset({"plan", "run", "check"}),
}
EXPECTED_ENTRY_POINTS = {
    "ai-illustration": "ai_illustration.cli:main",
    "ai-illustration-manifest": "ai_illustration.cli:main",
    "ai-illustration-workspace": "ai_illustration.workspace:main",
    "ai-illustration-frame-preview": "ai_illustration.frame_preview:main",
    "ai-illustration-video-export": "ai_illustration.video_export:main",
    "ai-illustration-release-audit": "ai_illustration.release_audit:main",
}
MINIMUM_CRITICAL_PATHS = frozenset(
    {
        ".github/workflows/ci.yml",
        ".github/workflows/unit-tests.yml",
        "README.md",
        "pyproject.toml",
        "docs/ARCHITECTURE.md",
        "docs/ASSET_SPECIFICATION.md",
        "docs/COMFYUI_EXECUTION.md",
        "docs/FRAME_PREVIEW.md",
        "docs/IMAGE_STYLE_GUIDE.md",
        "docs/MVP_COMPLETION.md",
        "docs/PRODUCT_REQUIREMENTS.md",
        "docs/VIDEO_EXPORT.md",
        "docs/WORKSPACE.md",
        "schemas/ai-illustration-mvp-release.schema.json",
        "schemas/ai-illustration-workspace-dashboard.schema.json",
        "schemas/ai-illustration-workspace.schema.json",
        "schemas/comfyui-execution-package.schema.json",
        "schemas/comfyui-execution-profile.schema.json",
        "schemas/paper-theater-frame-preview-package.schema.json",
        "schemas/paper-theater-frame-render-package.schema.json",
        "schemas/paper-theater-video-export-package.schema.json",
        "schemas/paper-theater-video-export-profile.schema.json",
        "src/ai_illustration/__init__.py",
        "src/ai_illustration/adapters/comfyui.py",
        "src/ai_illustration/adapters/comfyui_execute.py",
        "src/ai_illustration/adapters/comfyui_http.py",
        "src/ai_illustration/audio_preview.py",
        "src/ai_illustration/catalog.py",
        "src/ai_illustration/cli.py",
        "src/ai_illustration/composition.py",
        "src/ai_illustration/exporter.py",
        "src/ai_illustration/frame_preview.py",
        "src/ai_illustration/frame_renderer.py",
        "src/ai_illustration/naming.py",
        "src/ai_illustration/paper_theater.py",
        "src/ai_illustration/preview.py",
        "src/ai_illustration/release_audit.py",
        "src/ai_illustration/render_plan.py",
        "src/ai_illustration/review_ui.py",
        "src/ai_illustration/validation.py",
        "src/ai_illustration/variants.py",
        "src/ai_illustration/video_export.py",
        "src/ai_illustration/video_export_execute.py",
        "src/ai_illustration/video_export_process.py",
        "src/ai_illustration/workspace.py",
        "src/ai_illustration/workspace_checks.py",
    }
)
MINIMUM_PROTECTED_SOURCES = frozenset(
    {
        "src/ai_illustration/adapters/comfyui_execute.py",
        "src/ai_illustration/adapters/comfyui_http.py",
        "src/ai_illustration/video_export_execute.py",
        "src/ai_illustration/video_export_process.py",
        "src/ai_illustration/workspace.py",
    }
)
MINIMUM_EXTERNAL_PREREQUISITES = frozenset(
    {
        "approved-local-comfyui-installation",
        "approved-local-model-and-workflow",
        "caller-recorded-wav-audio",
        "checksum-pinned-local-ffmpeg",
    }
)
MINIMUM_HUMAN_DECISIONS = frozenset(
    {
        "character-identity-selection",
        "commercial-and-license-approval",
        "final-aesthetic-review",
        "variant-acceptance",
    }
)
MINIMUM_PROHIBITED_EFFECTS = frozenset(
    {
        "automatic-commercial-approval",
        "automatic-final-character-selection",
        "credential-access",
        "external-network-from-audit",
        "media-publication",
        "subprocess-from-audit",
    }
)
URL_RE = re.compile(
    r"https?://(?P<host>\[[^\]]+\]|[^/'\":\s]+)", re.IGNORECASE
)
ALLOWED_SOURCE_URL_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "[::1]", "example.invalid"}
)


class ReleaseAuditError(ValueError):
    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _diagnostic(code: str, message: str, field: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "field": field}


def _load_contract(
    path: Path,
) -> tuple[dict[str, Any], bytes, Path, Path]:
    expanded = path.expanduser()
    if "\x00" in str(expanded) or ".." in expanded.parts:
        raise ReleaseAuditError(
            "UNSAFE_PATH", "release contract path is unsafe", "contract"
        )
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    for candidate in (lexical, *lexical.parents):
        if candidate.exists() and candidate.is_symlink():
            raise ReleaseAuditError(
                "PATH_SYMLINK",
                "release contract contains a symlink component",
                "contract",
            )
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise ReleaseAuditError(
            "CONTRACT_MISSING", str(exc), "contract"
        ) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise ReleaseAuditError(
            "CONTRACT_TYPE",
            "release contract must be a regular file",
            "contract",
        )
    if resolved.parent.name != "release":
        raise ReleaseAuditError(
            "CONTRACT_LOCATION",
            "release contract must be beneath release/",
            "contract",
        )
    root = resolved.parent.parent.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseAuditError(
            "CONTRACT_LOCATION",
            "release contract escapes repository root",
            "contract",
        ) from exc
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_CONTRACT_BYTES:
        raise ReleaseAuditError(
            "CONTRACT_SIZE",
            "release contract exceeds the JSON size limit",
            "contract",
        )
    with resolved.open("rb") as handle:
        payload = handle.read(MAX_CONTRACT_BYTES + 1)
    if len(payload) != size or len(payload) > MAX_CONTRACT_BYTES:
        raise ReleaseAuditError(
            "CONTRACT_SIZE_CHANGED",
            "release contract changed during bounded read",
            "contract",
        )

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ReleaseAuditError(
                    "DUPLICATE_JSON_KEY",
                    f"duplicate JSON key: {key}",
                    "contract",
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=pairs
        )
    except ReleaseAuditError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAuditError(
            "INVALID_JSON", str(exc), "contract"
        ) from exc
    if not isinstance(value, dict):
        raise ReleaseAuditError(
            "INVALID_JSON_ROOT",
            "release contract root must be an object",
            "contract",
        )
    if payload != json_bytes(value):
        raise ReleaseAuditError(
            "NONCANONICAL_JSON",
            "release contract must use canonical JSON plus newline",
            "contract",
        )
    try:
        _scan_for_secrets(value, "release")
    except AdapterError as exc:
        raise ReleaseAuditError(exc.code, exc.message, exc.field) from exc
    return value, payload, resolved, root


def _sorted_strings(
    value: Any, field: str, *, allow_empty: bool = False
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or len(value) > 256
    ):
        raise ReleaseAuditError(
            "CONTRACT_SCHEMA", f"{field} must be a bounded list", field
        )
    if any(
        not isinstance(item, str) or not item or len(item) > 4096
        for item in value
    ):
        raise ReleaseAuditError(
            "CONTRACT_SCHEMA", f"{field} contains invalid text", field
        )
    if value != sorted(set(value)):
        raise ReleaseAuditError(
            "CONTRACT_ORDER", f"{field} must be sorted and unique", field
        )
    return value


def _safe_path(value: str, root: Path, field: str) -> Path:
    try:
        relative = safe_relative_path(value)
    except (TypeError, ValueError) as exc:
        raise ReleaseAuditError("UNSAFE_PATH", str(exc), field) from exc
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise ReleaseAuditError(
                "PATH_SYMLINK",
                f"{field} contains a symlink component",
                field,
            )
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReleaseAuditError(
            "PATH_ESCAPE", f"{field} escapes repository root", field
        ) from exc
    return resolved


def _validate_contract(value: dict[str, Any], root: Path) -> None:
    expected_fields = {
        "id",
        "kind",
        "schema_version",
        "release_name",
        "version",
        "python_requires",
        "runtime_dependencies",
        "workspace_check_kinds",
        "main_cli_commands",
        "dedicated_cli_commands",
        "entry_points",
        "critical_paths",
        "protected_sources",
        "external_prerequisites",
        "human_decisions",
        "prohibited_effects",
        "software_completion_definition",
        "content_completion_definition",
    }
    if set(value) != expected_fields:
        raise ReleaseAuditError(
            "CONTRACT_SCHEMA",
            "release contract fields are invalid",
            "contract",
        )
    if (
        value.get("kind") != RELEASE_KIND
        or value.get("schema_version") != "1.0"
    ):
        raise ReleaseAuditError(
            "CONTRACT_SCHEMA",
            "release contract kind/version is invalid",
            "contract",
        )
    if value.get("release_name") != "AI Illustration Pipeline Software MVP":
        raise ReleaseAuditError(
            "RELEASE_NAME", "release name is invalid", "release_name"
        )
    if (
        value.get("version") != RELEASE_VERSION
        or value.get("python_requires") != ">=3.11"
    ):
        raise ReleaseAuditError(
            "RELEASE_VERSION",
            "release version or Python requirement is invalid",
            "version",
        )
    if value.get("runtime_dependencies") != []:
        raise ReleaseAuditError(
            "RUNTIME_DEPENDENCIES",
            "software MVP must have zero runtime dependencies",
            "runtime_dependencies",
        )
    if (
        value.get("software_completion_definition")
        != SOFTWARE_COMPLETION_DEFINITION
        or value.get("content_completion_definition")
        != CONTENT_COMPLETION_DEFINITION
    ):
        raise ReleaseAuditError(
            "COMPLETION_DEFINITION",
            "completion definition changed",
            "software_completion_definition",
        )

    if frozenset(
        _sorted_strings(
            value.get("workspace_check_kinds"), "workspace_check_kinds"
        )
    ) != EXPECTED_WORKSPACE_KINDS:
        raise ReleaseAuditError(
            "WORKSPACE_KINDS",
            "workspace check kinds differ from the release surface",
            "workspace_check_kinds",
        )
    if frozenset(
        _sorted_strings(
            value.get("main_cli_commands"), "main_cli_commands"
        )
    ) != EXPECTED_MAIN_COMMANDS:
        raise ReleaseAuditError(
            "MAIN_COMMANDS",
            "main CLI command set differs from the release surface",
            "main_cli_commands",
        )
    dedicated = value.get("dedicated_cli_commands")
    if (
        not isinstance(dedicated, dict)
        or set(dedicated) != set(EXPECTED_DEDICATED_COMMANDS)
    ):
        raise ReleaseAuditError(
            "DEDICATED_COMMANDS",
            "dedicated CLI groups are invalid",
            "dedicated_cli_commands",
        )
    for name, expected in EXPECTED_DEDICATED_COMMANDS.items():
        actual = frozenset(
            _sorted_strings(
                dedicated.get(name), f"dedicated_cli_commands.{name}"
            )
        )
        if actual != expected:
            raise ReleaseAuditError(
                "DEDICATED_COMMANDS",
                f"{name} command set differs",
                f"dedicated_cli_commands.{name}",
            )
    if value.get("entry_points") != EXPECTED_ENTRY_POINTS:
        raise ReleaseAuditError(
            "ENTRY_POINTS",
            "entry point contract differs from the release surface",
            "entry_points",
        )

    critical = frozenset(
        _sorted_strings(value.get("critical_paths"), "critical_paths")
    )
    protected = frozenset(
        _sorted_strings(
            value.get("protected_sources"), "protected_sources"
        )
    )
    if not MINIMUM_CRITICAL_PATHS.issubset(critical):
        raise ReleaseAuditError(
            "CRITICAL_PATHS",
            f"required critical paths are missing: "
            f"{sorted(MINIMUM_CRITICAL_PATHS - critical)}",
            "critical_paths",
        )
    if (
        not MINIMUM_PROTECTED_SOURCES.issubset(protected)
        or not protected.issubset(critical)
    ):
        raise ReleaseAuditError(
            "PROTECTED_SOURCES",
            "protected sources are incomplete or outside critical paths",
            "protected_sources",
        )
    for index, relative in enumerate(value["critical_paths"]):
        _safe_path(relative, root, f"critical_paths[{index}]")
    external = frozenset(
        _sorted_strings(
            value.get("external_prerequisites"),
            "external_prerequisites",
        )
    )
    human = frozenset(
        _sorted_strings(value.get("human_decisions"), "human_decisions")
    )
    prohibited = frozenset(
        _sorted_strings(
            value.get("prohibited_effects"), "prohibited_effects"
        )
    )
    if not MINIMUM_EXTERNAL_PREREQUISITES.issubset(external):
        raise ReleaseAuditError(
            "EXTERNAL_PREREQUISITES",
            "external prerequisites are incomplete",
            "external_prerequisites",
        )
    if not MINIMUM_HUMAN_DECISIONS.issubset(human):
        raise ReleaseAuditError(
            "HUMAN_DECISIONS",
            "human decisions are incomplete",
            "human_decisions",
        )
    if not MINIMUM_PROHIBITED_EFFECTS.issubset(prohibited):
        raise ReleaseAuditError(
            "PROHIBITED_EFFECTS",
            "prohibited effects are incomplete",
            "prohibited_effects",
        )
    core = {key: value[key] for key in value if key != "id"}
    if value.get("id") != content_identifier(
        "ai-illustration-mvp-release", core, 20
    ):
        raise ReleaseAuditError(
            "RELEASE_ID",
            "release contract ID is not content-derived",
            "id",
        )


def _subcommands(parser: argparse.ArgumentParser) -> frozenset[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


def _bounded_file(path: Path, field: str) -> tuple[bytes, int]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseAuditError(
            "CRITICAL_FILE_TYPE",
            "critical path must be a regular file",
            field,
        )
    size = path.stat().st_size
    if size <= 0 or size > MAX_CRITICAL_FILE_BYTES:
        raise ReleaseAuditError(
            "CRITICAL_FILE_SIZE",
            "critical file exceeds the size limit",
            field,
        )
    with path.open("rb") as handle:
        payload = handle.read(MAX_CRITICAL_FILE_BYTES + 1)
    if len(payload) != size or len(payload) > MAX_CRITICAL_FILE_BYTES:
        raise ReleaseAuditError(
            "CRITICAL_FILE_SIZE_CHANGED",
            "critical file changed during bounded read",
            field,
        )
    return payload, size


def _critical_inventory(
    root: Path,
    paths: list[str],
    diagnostics: list[dict[str, str]],
) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for index, relative in enumerate(paths):
        field = f"critical_paths[{index}]"
        try:
            payload, size = _bounded_file(
                _safe_path(relative, root, field), field
            )
        except ReleaseAuditError as exc:
            diagnostics.append(exc.to_dict())
            continue
        inventory.append(
            {"path": relative, "sha256": sha256(payload), "size": size}
        )
    return sorted(inventory, key=lambda item: item["path"])


def _scan_protected_sources(
    root: Path,
    paths: list[str],
    diagnostics: list[dict[str, str]],
) -> None:
    forbidden = (
        "shell" + "=True",
        "shell " + "= True",
        "os." + "system(",
        "ev" + "al(",
        "ex" + "ec(",
    )
    for index, relative in enumerate(paths):
        field = f"protected_sources[{index}]"
        try:
            payload, _size = _bounded_file(
                _safe_path(relative, root, field), field
            )
            text = payload.decode("utf-8")
        except (ReleaseAuditError, UnicodeError) as exc:
            if isinstance(exc, ReleaseAuditError):
                diagnostics.append(exc.to_dict())
            else:
                diagnostics.append(
                    _diagnostic("PROTECTED_SOURCE_READ", str(exc), field)
                )
            continue
        for pattern in forbidden:
            if pattern in text:
                diagnostics.append(
                    _diagnostic(
                        "FORBIDDEN_SOURCE_PATTERN",
                        f"{relative} contains {pattern}",
                        field,
                    )
                )
        for match in URL_RE.finditer(text):
            raw_host = match.group("host").lower()
            host = (
                raw_host
                if raw_host.startswith("[")
                else raw_host.split(":", 1)[0]
            )
            if host not in ALLOWED_SOURCE_URL_HOSTS:
                diagnostics.append(
                    _diagnostic(
                        "EXTERNAL_URL_LITERAL",
                        f"{relative} contains non-loopback URL host {host}",
                        field,
                    )
                )


def audit_release(contract_path: Path) -> dict[str, Any]:
    contract, contract_bytes, resolved, root = _load_contract(contract_path)
    _validate_contract(contract, root)
    diagnostics: list[dict[str, str]] = []

    try:
        pyproject_payload, _ = _bounded_file(
            root / "pyproject.toml", "pyproject.toml"
        )
        project = tomllib.loads(pyproject_payload.decode("utf-8")).get(
            "project", {}
        )
    except (
        ReleaseAuditError,
        UnicodeError,
        tomllib.TOMLDecodeError,
    ) as exc:
        diagnostics.append(
            _diagnostic("PYPROJECT", str(exc), "pyproject.toml")
        )
        project = {}
    if project.get("version") != RELEASE_VERSION:
        diagnostics.append(
            _diagnostic(
                "PYPROJECT_VERSION",
                "pyproject version must be 1.0.0",
                "project.version",
            )
        )
    if project.get("requires-python") != ">=3.11":
        diagnostics.append(
            _diagnostic(
                "PYTHON_REQUIRES",
                "requires-python must be >=3.11",
                "project.requires-python",
            )
        )
    if project.get("dependencies") != []:
        diagnostics.append(
            _diagnostic(
                "RUNTIME_DEPENDENCIES",
                "runtime dependencies must be empty",
                "project.dependencies",
            )
        )
    if project.get("scripts") != EXPECTED_ENTRY_POINTS:
        diagnostics.append(
            _diagnostic(
                "ENTRY_POINTS",
                "installed entry points differ from the release contract",
                "project.scripts",
            )
        )
    if __version__ != RELEASE_VERSION:
        diagnostics.append(
            _diagnostic(
                "PACKAGE_VERSION",
                "ai_illustration.__version__ must be 1.0.0",
                "__version__",
            )
        )

    if frozenset(CHECKERS) != EXPECTED_WORKSPACE_KINDS:
        diagnostics.append(
            _diagnostic(
                "WORKSPACE_REGISTRY",
                "workspace checker registry differs from release contract",
                "CHECKERS",
            )
        )
    try:
        schema_payload, _ = _bounded_file(
            root / "schemas/ai-illustration-workspace.schema.json",
            "workspace schema",
        )
        schema = json.loads(schema_payload.decode("utf-8"))
        schema_kinds = frozenset(
            schema["$defs"]["check"]["properties"]["kind"]["enum"]
        )
    except (
        ReleaseAuditError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
    ) as exc:
        diagnostics.append(
            _diagnostic("WORKSPACE_SCHEMA", str(exc), "workspace schema")
        )
    else:
        if schema_kinds != EXPECTED_WORKSPACE_KINDS:
            diagnostics.append(
                _diagnostic(
                    "WORKSPACE_SCHEMA",
                    "workspace schema kinds differ from release contract",
                    "workspace schema",
                )
            )

    if _subcommands(build_parser()) != EXPECTED_MAIN_COMMANDS:
        diagnostics.append(
            _diagnostic(
                "MAIN_COMMANDS",
                "main CLI commands differ from release contract",
                "main CLI",
            )
        )
    dedicated_actual = {
        "workspace": _subcommands(workspace_parser()),
        "frame_preview": _subcommands(frame_preview_parser()),
        "video_export": _subcommands(video_export_parser()),
    }
    for name, expected in EXPECTED_DEDICATED_COMMANDS.items():
        if dedicated_actual[name] != expected:
            diagnostics.append(
                _diagnostic(
                    "DEDICATED_COMMANDS",
                    f"{name} commands differ from release contract",
                    name,
                )
            )

    try:
        ComfyUIAdapter().execute(None)  # type: ignore[arg-type]
    except AdapterError as exc:
        if exc.code != "EXECUTION_DISABLED":
            diagnostics.append(
                _diagnostic(
                    "LEGACY_EXECUTION_BOUNDARY",
                    f"unexpected adapter error: {exc.code}",
                    "ComfyUIAdapter.execute",
                )
            )
    except Exception as exc:
        diagnostics.append(
            _diagnostic(
                "LEGACY_EXECUTION_BOUNDARY",
                str(exc),
                "ComfyUIAdapter.execute",
            )
        )
    else:
        diagnostics.append(
            _diagnostic(
                "LEGACY_EXECUTION_BOUNDARY",
                "legacy adapter execute boundary is enabled",
                "ComfyUIAdapter.execute",
            )
        )

    inventory = _critical_inventory(
        root, contract["critical_paths"], diagnostics
    )
    _scan_protected_sources(
        root, contract["protected_sources"], diagnostics
    )
    diagnostics = sorted(
        diagnostics,
        key=lambda item: (
            item["code"],
            item["field"],
            item["message"],
        ),
    )
    return {
        "ok": not diagnostics,
        "complete": not diagnostics,
        "release": {
            "id": contract["id"],
            "name": contract["release_name"],
            "version": contract["version"],
            "contract_name": resolved.name,
            "contract_sha256": sha256(contract_bytes),
        },
        "diagnostics": diagnostics,
        "critical_file_count": len(inventory),
        "critical_files": inventory,
        "network_contacted": False,
        "external_process_started": False,
        "filesystem_mutated": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_illustration.release_audit"
    )
    parser.add_argument("contract", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = audit_release(args.contract)
    except ReleaseAuditError as exc:
        result = {
            "ok": False,
            "complete": False,
            "release": None,
            "diagnostics": [exc.to_dict()],
            "critical_file_count": 0,
            "critical_files": [],
            "network_contacted": False,
            "external_process_started": False,
            "filesystem_mutated": False,
        }
    sys.stdout.buffer.write(json_bytes(result))
    print(
        f"software MVP audit: "
        f"{'complete' if result['complete'] else 'incomplete'} "
        f"({len(result['diagnostics'])} diagnostic(s))",
        file=sys.stderr,
    )
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
