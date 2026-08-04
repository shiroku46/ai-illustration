"""Canonical workspace validation and path resolution for the owner dashboard."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .adapters.base import AdapterError
from .adapters.comfyui import _scan_for_secrets, sanitize_loopback_endpoint
from .naming import canonical_json, content_identifier, safe_relative_path

WORKSPACE_KIND = "ai-illustration-workspace"
WORKSPACE_VERSION = "1.0"
DASHBOARD_MANIFEST = "workspace-dashboard-manifest.json"
DASHBOARD_DATA = "workspace-data.js"
DASHBOARD_HTML = "index.html"
DASHBOARD_CSS = "style.css"
DASHBOARD_JS = "app.js"
MAX_WORKSPACE_BYTES = 4 * 1024 * 1024
MAX_CHECKS = 128
MAX_ARGUMENTS = 32
MAX_ACTION_ARGUMENTS = 128
SECRET_MARKERS = (
    "api-key",
    "api_key",
    "apikey",
    "api-token",
    "api_token",
    "authorization",
    "bearer",
    "credential",
    "password",
    "passwd",
    "secret",
    "token",
)


@dataclass
class WorkspaceError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


@dataclass(frozen=True)
class CheckSpec:
    required: frozenset[str]
    path_arguments: frozenset[str]
    primary: str


CHECK_SPECS: dict[str, CheckSpec] = {
    "manifest-set": CheckSpec(frozenset({"path"}), frozenset({"path"}), "path"),
    "comfyui-execution": CheckSpec(
        frozenset(
            {
                "manifest",
                "output_root",
                "request",
                "workflow",
                "bindings",
                "tool_profile",
                "model_profile",
                "execution_profile",
                "endpoint",
            }
        ),
        frozenset(
            {
                "manifest",
                "output_root",
                "request",
                "workflow",
                "bindings",
                "tool_profile",
                "model_profile",
                "execution_profile",
            }
        ),
        "manifest",
    ),
    "variant-set": CheckSpec(
        frozenset({"variant_set", "manifest_root"}),
        frozenset({"variant_set", "manifest_root"}),
        "variant_set",
    ),
    "export-package": CheckSpec(
        frozenset({"package_manifest", "output_root"}),
        frozenset({"package_manifest", "output_root"}),
        "package_manifest",
    ),
    "scene-plan": CheckSpec(
        frozenset({"scene_plan", "package_root"}),
        frozenset({"scene_plan", "package_root"}),
        "scene_plan",
    ),
    "preview-package": CheckSpec(
        frozenset({"preview_manifest", "output_root", "package_root"}),
        frozenset({"preview_manifest", "output_root", "package_root"}),
        "preview_manifest",
    ),
    "audio-preview": CheckSpec(
        frozenset(
            {
                "audio_preview_manifest",
                "output_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        frozenset(
            {
                "audio_preview_manifest",
                "output_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        "audio_preview_manifest",
    ),
    "render-plan": CheckSpec(
        frozenset(
            {
                "render_plan_manifest",
                "output_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        frozenset(
            {
                "render_plan_manifest",
                "output_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        "render_plan_manifest",
    ),
    "renderer-job": CheckSpec(
        frozenset(
            {
                "renderer_job_manifest",
                "output_root",
                "render_plan_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        frozenset(
            {
                "renderer_job_manifest",
                "output_root",
                "render_plan_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        "renderer_job_manifest",
    ),
    "frame-render": CheckSpec(
        frozenset(
            {
                "frame_render_manifest",
                "output_root",
                "renderer_job_root",
                "render_plan_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        frozenset(
            {
                "frame_render_manifest",
                "output_root",
                "renderer_job_root",
                "render_plan_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        "frame_render_manifest",
    ),
    "frame-preview": CheckSpec(
        frozenset(
            {
                "frame_preview_manifest",
                "output_root",
                "frame_render_root",
                "renderer_job_root",
                "render_plan_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        frozenset(
            {
                "frame_preview_manifest",
                "output_root",
                "frame_render_root",
                "renderer_job_root",
                "render_plan_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
            }
        ),
        "frame_preview_manifest",
    ),
    "video-export": CheckSpec(
        frozenset(
            {
                "video_export_manifest",
                "profile",
                "ffmpeg",
                "output_root",
                "frame_preview_root",
                "frame_render_root",
                "renderer_job_root",
                "render_plan_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
                "profile_root",
            }
        ),
        frozenset(
            {
                "video_export_manifest",
                "profile",
                "ffmpeg",
                "output_root",
                "frame_preview_root",
                "frame_render_root",
                "renderer_job_root",
                "render_plan_root",
                "audio_preview_root",
                "preview_root",
                "package_root",
                "audio_root",
                "profile_root",
            }
        ),
        "video_export_manifest",
    ),
}


def json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _token(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise WorkspaceError("TOKEN", f"{field} must be a bounded token", field)
    if any(
        not (character.islower() or character.isdigit() or character == "-")
        for character in value
    ):
        raise WorkspaceError(
            "TOKEN",
            f"{field} must use lowercase ASCII, digits, and hyphens",
            field,
        )
    if value.startswith("-") or value.endswith("-") or "--" in value:
        raise WorkspaceError("TOKEN", f"{field} token form is invalid", field)
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise WorkspaceError(
            "TEXT",
            f"{field} must be non-empty and at most {maximum} characters",
            field,
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise WorkspaceError(
            "TEXT", f"{field} contains a control character", field
        )
    return value


def _reject_symlinks(path: Path, field: str) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    for candidate in (lexical, *lexical.parents):
        try:
            if candidate.exists() and candidate.is_symlink():
                raise WorkspaceError(
                    "PATH_SYMLINK",
                    f"{field} contains a symlink component",
                    field,
                )
        except OSError as exc:
            raise WorkspaceError("PATH_ERROR", str(exc), field) from exc


def _relative_path(value: Any, base: Path, field: str) -> Path:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise WorkspaceError(
            "UNSAFE_PATH", f"{field} must be a POSIX relative path", field
        )
    try:
        safe = safe_relative_path(value)
    except (TypeError, ValueError) as exc:
        raise WorkspaceError("UNSAFE_PATH", str(exc), field) from exc
    candidate = base.joinpath(*safe.parts)
    current = base
    for part in safe.parts:
        current /= part
        if current.exists() and current.is_symlink():
            raise WorkspaceError(
                "PATH_SYMLINK",
                f"{field} contains a symlink component",
                field,
            )
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(base)
    except (OSError, ValueError) as exc:
        raise WorkspaceError(
            "PATH_ESCAPE",
            f"{field} escapes the workspace directory",
            field,
        ) from exc
    return resolved


def _load_object(path: Path) -> tuple[dict[str, Any], bytes, Path]:
    raw = str(path)
    if "\x00" in raw or ".." in path.expanduser().parts:
        raise WorkspaceError(
            "UNSAFE_PATH", "workspace path is unsafe", "workspace"
        )
    expanded = path.expanduser()
    _reject_symlinks(expanded, "workspace")
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise WorkspaceError(
            "WORKSPACE_MISSING", str(exc), "workspace"
        ) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise WorkspaceError(
            "WORKSPACE_TYPE",
            "workspace must be a regular file",
            "workspace",
        )
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_WORKSPACE_BYTES:
        raise WorkspaceError(
            "WORKSPACE_SIZE",
            "workspace exceeds the JSON size limit",
            "workspace",
        )
    with resolved.open("rb") as handle:
        payload = handle.read(MAX_WORKSPACE_BYTES + 1)
    if len(payload) != size or len(payload) > MAX_WORKSPACE_BYTES:
        raise WorkspaceError(
            "WORKSPACE_SIZE_CHANGED",
            "workspace size changed during bounded read",
            "workspace",
        )

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise WorkspaceError(
                    "DUPLICATE_JSON_KEY",
                    f"duplicate JSON key: {key}",
                    "workspace",
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=pairs
        )
    except WorkspaceError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError(
            "INVALID_JSON", str(exc), "workspace"
        ) from exc
    if not isinstance(value, dict):
        raise WorkspaceError(
            "INVALID_JSON_ROOT",
            "workspace root must be an object",
            "workspace",
        )
    if payload != json_bytes(value):
        raise WorkspaceError(
            "NONCANONICAL_JSON",
            "workspace must use canonical JSON plus newline",
            "workspace",
        )
    return value, payload, resolved


def _validate_action(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "type",
        "label",
        "argv",
    }:
        raise WorkspaceError(
            "ACTION_SCHEMA", f"{field} fields are invalid", field
        )
    action_type = value.get("type")
    if action_type not in {"command", "human"}:
        raise WorkspaceError(
            "ACTION_TYPE", f"{field}.type is invalid", f"{field}.type"
        )
    label = _text(value.get("label"), f"{field}.label", 240)
    argv = value.get("argv")
    if not isinstance(argv, list) or len(argv) > MAX_ACTION_ARGUMENTS:
        raise WorkspaceError(
            "ACTION_ARGV", f"{field}.argv is invalid", f"{field}.argv"
        )
    normalized = [
        _text(item, f"{field}.argv[{index}]", 4096)
        for index, item in enumerate(argv)
    ]
    for index, argument in enumerate(normalized):
        lowered = argument.lower()
        if any(marker in lowered for marker in SECRET_MARKERS):
            raise WorkspaceError(
                "SECRET_LIKE_DATA",
                "secret-like action argument is forbidden",
                f"{field}.argv[{index}]",
            )
    if action_type == "command" and not normalized:
        raise WorkspaceError(
            "ACTION_ARGV",
            "command actions require at least one argument",
            f"{field}.argv",
        )
    if action_type == "human" and normalized:
        raise WorkspaceError(
            "ACTION_ARGV",
            "human actions must not contain executable arguments",
            f"{field}.argv",
        )
    return {"type": action_type, "label": label, "argv": normalized}


def load_workspace(
    path: Path,
) -> tuple[dict[str, Any], bytes, Path, list[dict[str, Any]]]:
    value, payload, resolved = _load_object(path)
    if set(value) != {
        "id",
        "kind",
        "schema_version",
        "project_name",
        "checks",
    }:
        raise WorkspaceError(
            "WORKSPACE_SCHEMA", "workspace fields are invalid", "workspace"
        )
    if (
        value.get("kind") != WORKSPACE_KIND
        or value.get("schema_version") != WORKSPACE_VERSION
    ):
        raise WorkspaceError(
            "WORKSPACE_SCHEMA",
            "workspace kind/version is invalid",
            "workspace",
        )
    _text(value.get("project_name"), "project_name", 200)
    checks = value.get("checks")
    if (
        not isinstance(checks, list)
        or not checks
        or len(checks) > MAX_CHECKS
    ):
        raise WorkspaceError(
            "CHECKS",
            "checks must be a bounded non-empty list",
            "checks",
        )
    try:
        _scan_for_secrets(value, "workspace")
    except AdapterError as exc:
        raise WorkspaceError(
            exc.code, exc.message, exc.field
        ) from exc
    core = {key: value[key] for key in value if key != "id"}
    if value.get("id") != content_identifier(
        "ai-illustration-workspace", core, 20
    ):
        raise WorkspaceError(
            "WORKSPACE_ID",
            "workspace ID is not content-derived",
            "id",
        )

    base = resolved.parent.resolve()
    seen: set[str] = set()
    resolved_checks: list[dict[str, Any]] = []
    for index, raw in enumerate(checks):
        field = f"checks[{index}]"
        if not isinstance(raw, dict) or set(raw) != {
            "id",
            "kind",
            "depends_on",
            "arguments",
            "action",
        }:
            raise WorkspaceError(
                "CHECK_SCHEMA", f"{field} fields are invalid", field
            )
        identifier = _token(raw.get("id"), f"{field}.id")
        if identifier in seen:
            raise WorkspaceError(
                "DUPLICATE_CHECK",
                f"duplicate check ID: {identifier}",
                f"{field}.id",
            )
        kind = raw.get("kind")
        if kind not in CHECK_SPECS:
            raise WorkspaceError(
                "CHECK_KIND",
                f"unsupported check kind: {kind}",
                f"{field}.kind",
            )
        depends = raw.get("depends_on")
        if not isinstance(depends, list) or len(depends) > MAX_CHECKS:
            raise WorkspaceError(
                "DEPENDENCIES",
                f"{field}.depends_on is invalid",
                f"{field}.depends_on",
            )
        normalized_depends = [
            _token(item, f"{field}.depends_on[{position}]")
            for position, item in enumerate(depends)
        ]
        if normalized_depends != sorted(set(normalized_depends)):
            raise WorkspaceError(
                "DEPENDENCIES",
                "dependencies must be sorted and unique",
                f"{field}.depends_on",
            )
        missing = [
            item for item in normalized_depends if item not in seen
        ]
        if missing:
            raise WorkspaceError(
                "FORWARD_DEPENDENCY",
                f"dependencies must reference earlier checks: {missing}",
                f"{field}.depends_on",
            )

        arguments = raw.get("arguments")
        spec = CHECK_SPECS[kind]
        if (
            not isinstance(arguments, dict)
            or len(arguments) > MAX_ARGUMENTS
            or set(arguments) != set(spec.required)
        ):
            raise WorkspaceError(
                "CHECK_ARGUMENTS",
                f"{field}.arguments do not match {kind}",
                f"{field}.arguments",
            )
        resolved_arguments: dict[str, str | Path] = {}
        stored_arguments: dict[str, str] = {}
        for name in sorted(arguments):
            argument_field = f"{field}.arguments.{name}"
            raw_value = arguments[name]
            if name == "endpoint":
                if not isinstance(raw_value, str):
                    raise WorkspaceError(
                        "CHECK_ARGUMENTS",
                        "endpoint must be text",
                        argument_field,
                    )
                try:
                    endpoint = sanitize_loopback_endpoint(raw_value)
                except ValueError as exc:
                    raise WorkspaceError(
                        "ENDPOINT", str(exc), argument_field
                    ) from exc
                stored_arguments[name] = endpoint
                resolved_arguments[name] = endpoint
            else:
                if not isinstance(raw_value, str):
                    raise WorkspaceError(
                        "CHECK_ARGUMENTS",
                        f"{argument_field} must be text",
                        argument_field,
                    )
                resolved_path = _relative_path(
                    raw_value, base, argument_field
                )
                stored_arguments[name] = safe_relative_path(
                    raw_value
                ).as_posix()
                resolved_arguments[name] = resolved_path

        action = _validate_action(raw.get("action"), f"{field}.action")
        resolved_checks.append(
            {
                "id": identifier,
                "kind": kind,
                "depends_on": normalized_depends,
                "arguments": stored_arguments,
                "resolved_arguments": resolved_arguments,
                "action": action,
                "primary": spec.primary,
            }
        )
        seen.add(identifier)
    return value, payload, resolved, resolved_checks
