"""Read-only exact model-install manifest validation for local benchmark setup."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

from .catalog import evaluate_model_license_eligibility, validate_tool_profile
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, canonical_json, safe_relative_path

MANIFEST_KIND = "model-install-manifest"
SCHEMA_VERSION = "1.0"
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_PROFILE_BYTES = 4 * 1024 * 1024
MODEL_REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")
SETTING_TOKEN_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
BENCHMARK_SCOPES = frozenset({"production-candidate", "evaluation-only"})
COMPONENT_DESTINATIONS = {
    "checkpoint": "models/checkpoints",
    "diffusion-model": "models/diffusion_models",
    "text-encoder": "models/text_encoders",
    "vae": "models/vae",
}
PROMPT_FORMATS = frozenset({"tags", "natural-language", "hybrid"})
SETTINGS_BASES = frozenset({"official-model-card", "project-benchmark-baseline"})
OPERATING_SYSTEMS = frozenset({"windows", "linux", "macos"})

MANIFEST_FIELDS = frozenset({"kind", "schema_version", "id", "version", "target", "models"})
TARGET_FIELDS = frozenset(
    {"adapter_type", "runtime_type", "operating_system", "minimum_ram_gb", "minimum_vram_gb"}
)
MODEL_FIELDS = frozenset(
    {
        "family",
        "profile_path",
        "profile_ref",
        "profile_sha256",
        "benchmark_scope",
        "artifacts",
        "benchmark_settings",
        "workflow",
    }
)
ARTIFACT_FIELDS = frozenset(
    {"id", "component", "source_url", "filename", "destination", "size_bytes", "sha256", "required"}
)
SETTINGS_FIELDS = frozenset(
    {"width", "height", "steps", "cfg", "sampler", "scheduler", "prompt_format", "settings_basis"}
)
WORKFLOW_FIELDS = frozenset({"status", "expected_api_path"})


def _diag(code: str, message: str, field: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "field": field}


def _sorted(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique = {
        (item.get("field", ""), item.get("code", ""), item.get("message", "")): {
            "code": item.get("code", ""),
            "message": item.get("message", ""),
            "field": item.get("field", ""),
        }
        for item in values
    }
    return [unique[key] for key in sorted(unique)]


def _fields(value: Any, required: frozenset[str], field: str) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return [_diag("OBJECT_REQUIRED", "must be an object", field)]
    diagnostics: list[dict[str, str]] = []
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing:
        diagnostics.append(_diag("MISSING_FIELD", f"missing fields: {', '.join(missing)}", field))
    if unknown:
        diagnostics.append(_diag("UNKNOWN_FIELD", f"unknown fields: {', '.join(unknown)}", field))
    return diagnostics


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _canonical_sha256(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(document)).hexdigest()


def _resolve_root(path: Path) -> tuple[Path | None, list[dict[str, str]]]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        return None, [_diag("WORKSPACE_SYMLINK", "workspace root must not be a symlink", "workspace_root")]
    try:
        root = lexical.resolve(strict=True)
    except OSError as exc:
        return None, [_diag("WORKSPACE_MISSING", str(exc), "workspace_root")]
    if not root.is_dir():
        return None, [_diag("WORKSPACE_TYPE", "workspace root must be a directory", "workspace_root")]
    return root, []


def _has_symlink(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop:
            return False
        if current.parent == current:
            return True
        current = current.parent


def _load_relative_json(
    root: Path,
    relative: Any,
    *,
    field: str,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, Path | None, list[dict[str, str]]]:
    if not isinstance(relative, str):
        return None, None, [_diag("PATH_REQUIRED", "must be a relative path", field)]
    try:
        safe = safe_relative_path(relative)
        path = root.joinpath(*safe.parts)
        if _has_symlink(path, root):
            raise ValueError("path contains a symlink")
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError("path must identify a regular file")
        size = resolved.stat().st_size
        if size <= 0 or size > max_bytes:
            raise ValueError(f"file size must be 1..{max_bytes} bytes")
        payload = resolved.read_bytes()
        document = json.loads(payload.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("JSON root must be an object")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, None, [_diag("DOCUMENT_READ", str(exc), field)]
    return document, resolved, []


def load_manifest(path: Path) -> dict[str, Any]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        raise ValueError("manifest path must not be a symlink")
    resolved = lexical.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("manifest path must be a regular file")
    size = resolved.stat().st_size
    if size <= 0 or size > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest size must be 1..{MAX_MANIFEST_BYTES} bytes")
    document = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("manifest JSON root must be an object")
    return document


def _validate_target(target: Any) -> list[dict[str, str]]:
    diagnostics = _fields(target, TARGET_FIELDS, "target")
    if not isinstance(target, dict):
        return diagnostics
    for name in ("adapter_type", "runtime_type"):
        if not isinstance(target.get(name), str) or not TOKEN_RE.fullmatch(target.get(name, "")):
            diagnostics.append(_diag("INVALID_TOKEN", "must be a lowercase ASCII token", f"target.{name}"))
    if target.get("operating_system") not in OPERATING_SYSTEMS:
        diagnostics.append(_diag("OPERATING_SYSTEM", "unsupported operating system", "target.operating_system"))
    for name in ("minimum_ram_gb", "minimum_vram_gb"):
        value = target.get(name)
        if not _number(value) or value < 0:
            diagnostics.append(_diag("HARDWARE_VALUE", "must be a non-negative number", f"target.{name}"))
    return diagnostics


def _validate_settings(settings: Any, field: str) -> list[dict[str, str]]:
    diagnostics = _fields(settings, SETTINGS_FIELDS, field)
    if not isinstance(settings, dict):
        return diagnostics
    for name in ("width", "height"):
        value = settings.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or not 256 <= value <= 2048 or value % 64:
            diagnostics.append(_diag("IMAGE_DIMENSION", "must be a multiple of 64 in 256..2048", f"{field}.{name}"))
    steps = settings.get("steps")
    if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 100:
        diagnostics.append(_diag("STEPS", "must be an integer in 1..100", f"{field}.steps"))
    cfg = settings.get("cfg")
    if not _number(cfg) or not 0 <= cfg <= 30:
        diagnostics.append(_diag("CFG", "must be a number in 0..30", f"{field}.cfg"))
    for name in ("sampler", "scheduler"):
        value = settings.get(name)
        if not isinstance(value, str) or not SETTING_TOKEN_RE.fullmatch(value):
            diagnostics.append(_diag("SETTING_TOKEN", "invalid sampler or scheduler token", f"{field}.{name}"))
    if settings.get("prompt_format") not in PROMPT_FORMATS:
        diagnostics.append(_diag("PROMPT_FORMAT", "unsupported prompt format", f"{field}.prompt_format"))
    if settings.get("settings_basis") not in SETTINGS_BASES:
        diagnostics.append(_diag("SETTINGS_BASIS", "unsupported settings basis", f"{field}.settings_basis"))
    return diagnostics


def _validate_workflow(workflow: Any, field: str) -> tuple[list[dict[str, str]], str | None]:
    diagnostics = _fields(workflow, WORKFLOW_FIELDS, field)
    if not isinstance(workflow, dict):
        return diagnostics, None
    if workflow.get("status") != "human-export-required":
        diagnostics.append(_diag("WORKFLOW_STATUS", "status must be human-export-required", f"{field}.status"))
    path_value = workflow.get("expected_api_path")
    safe_path: str | None = None
    try:
        safe = safe_relative_path(path_value)
        safe_path = safe.as_posix()
        if not safe_path.startswith("local/benchmark-workflows/") or not safe_path.endswith(".json"):
            raise ValueError("workflow path must be local/benchmark-workflows/*.json")
    except (TypeError, ValueError) as exc:
        diagnostics.append(_diag("WORKFLOW_PATH", str(exc), f"{field}.expected_api_path"))
    return diagnostics, safe_path


def _validate_artifact(artifact: Any, field: str) -> tuple[list[dict[str, str]], tuple[str, str] | None, str | None]:
    diagnostics = _fields(artifact, ARTIFACT_FIELDS, field)
    if not isinstance(artifact, dict):
        return diagnostics, None, None
    artifact_id = artifact.get("id")
    if not isinstance(artifact_id, str) or not TOKEN_RE.fullmatch(artifact_id):
        diagnostics.append(_diag("INVALID_TOKEN", "id must be a lowercase ASCII token", f"{field}.id"))
    component = artifact.get("component")
    expected_destination = COMPONENT_DESTINATIONS.get(component)
    if expected_destination is None:
        diagnostics.append(_diag("COMPONENT", "unsupported model component", f"{field}.component"))
    source_url = artifact.get("source_url")
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        diagnostics.append(_diag("SOURCE_URL", "source URL must use https", f"{field}.source_url"))
    filename = artifact.get("filename")
    if not isinstance(filename, str) or not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
        diagnostics.append(_diag("FILENAME", "filename must be one basename", f"{field}.filename"))
    destination = artifact.get("destination")
    if destination != expected_destination:
        diagnostics.append(
            _diag(
                "DESTINATION",
                f"{component} must use {expected_destination}",
                f"{field}.destination",
            )
        )
    size = artifact.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        diagnostics.append(_diag("SIZE", "size_bytes must be a positive integer", f"{field}.size_bytes"))
    checksum = artifact.get("sha256")
    if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum):
        diagnostics.append(_diag("CHECKSUM", "sha256 must be lowercase hexadecimal", f"{field}.sha256"))
    if artifact.get("required") is not True:
        diagnostics.append(_diag("REQUIRED_ARTIFACT", "all manifest artifacts must be required", f"{field}.required"))
    install_key = (str(destination), str(filename)) if destination and filename else None
    return diagnostics, install_key, artifact_id if isinstance(artifact_id, str) else None


def validate_manifest(
    manifest: Any,
    *,
    workspace_root: Path,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    diagnostics = _fields(manifest, MANIFEST_FIELDS, "manifest")
    if not isinstance(manifest, dict):
        return _sorted(diagnostics), []
    if manifest.get("kind") != MANIFEST_KIND:
        diagnostics.append(_diag("KIND", f"kind must be {MANIFEST_KIND}", "kind"))
    if manifest.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(_diag("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version"))
    if not isinstance(manifest.get("id"), str) or not TOKEN_RE.fullmatch(manifest.get("id", "")):
        diagnostics.append(_diag("INVALID_ID", "id must be a lowercase ASCII token", "id"))
    if not isinstance(manifest.get("version"), str) or not VERSION_RE.fullmatch(manifest.get("version", "")):
        diagnostics.append(_diag("INVALID_VERSION", "version must use vNNN", "version"))
    diagnostics.extend(_validate_target(manifest.get("target")))
    root, root_diagnostics = _resolve_root(workspace_root)
    diagnostics.extend(root_diagnostics)

    models = manifest.get("models")
    if not isinstance(models, list) or len(models) < 3:
        diagnostics.append(_diag("MODELS", "at least three model entries are required", "models"))
        return _sorted(diagnostics), []

    summaries: list[dict[str, Any]] = []
    families: set[str] = set()
    refs: set[str] = set()
    profile_paths: set[str] = set()
    artifact_ids: set[str] = set()
    install_keys: set[tuple[str, str]] = set()
    workflow_paths: set[str] = set()
    target = manifest.get("target") if isinstance(manifest.get("target"), dict) else {}

    for index, model in enumerate(models):
        field = f"models[{index}]"
        diagnostics.extend(_fields(model, MODEL_FIELDS, field))
        if not isinstance(model, dict):
            continue
        family = model.get("family")
        if not isinstance(family, str) or not TOKEN_RE.fullmatch(family):
            diagnostics.append(_diag("FAMILY", "family must be a lowercase ASCII token", f"{field}.family"))
        elif family in families:
            diagnostics.append(_diag("DUPLICATE_FAMILY", f"duplicate family: {family}", f"{field}.family"))
        else:
            families.add(family)
        profile_ref = model.get("profile_ref")
        if not isinstance(profile_ref, str) or not MODEL_REF_RE.fullmatch(profile_ref):
            diagnostics.append(_diag("PROFILE_REF", "profile_ref must use id@vNNN", f"{field}.profile_ref"))
        elif profile_ref in refs:
            diagnostics.append(_diag("DUPLICATE_PROFILE", f"duplicate profile_ref: {profile_ref}", f"{field}.profile_ref"))
        else:
            refs.add(profile_ref)
        profile_sha = model.get("profile_sha256")
        if not isinstance(profile_sha, str) or not SHA256_RE.fullmatch(profile_sha):
            diagnostics.append(_diag("CHECKSUM", "profile_sha256 must be lowercase hexadecimal", f"{field}.profile_sha256"))
        scope = model.get("benchmark_scope")
        if scope not in BENCHMARK_SCOPES:
            diagnostics.append(_diag("BENCHMARK_SCOPE", "unsupported benchmark scope", f"{field}.benchmark_scope"))

        profile_path_value = model.get("profile_path")
        if isinstance(profile_path_value, str):
            if profile_path_value in profile_paths:
                diagnostics.append(_diag("DUPLICATE_PROFILE_PATH", "profile path is duplicated", f"{field}.profile_path"))
            profile_paths.add(profile_path_value)

        eligibility_dict: dict[str, Any] | None = None
        profile: dict[str, Any] | None = None
        if root is not None:
            profile, resolved_profile, read_diagnostics = _load_relative_json(
                root,
                profile_path_value,
                field=f"{field}.profile_path",
                max_bytes=MAX_PROFILE_BYTES,
            )
            diagnostics.extend(read_diagnostics)
            if profile is not None and resolved_profile is not None:
                diagnostics.extend(
                    {
                        "code": item.code,
                        "message": item.message,
                        "field": f"{field}.profile_path:{item.field}" if item.field else f"{field}.profile_path",
                    }
                    for item in validate_tool_profile(resolved_profile, profile)
                )
                expected_ref = f"{profile.get('id')}@{profile.get('version')}"
                if profile_ref != expected_ref:
                    diagnostics.append(_diag("PROFILE_REF_BINDING", "profile_ref does not match profile bytes", f"{field}.profile_ref"))
                actual_sha = _canonical_sha256(profile)
                if profile_sha != actual_sha:
                    diagnostics.append(_diag("PROFILE_SHA_BINDING", "profile_sha256 does not match profile bytes", f"{field}.profile_sha256"))
                eligibility = evaluate_model_license_eligibility(profile)
                eligibility_dict = eligibility.to_dict()
                if not eligibility.benchmark_eligible:
                    diagnostics.append(_diag("MODEL_NOT_BENCHMARK_ELIGIBLE", json.dumps(eligibility_dict, sort_keys=True, separators=(",", ":")), field))
                if scope == "production-candidate" and not eligibility.production_eligible:
                    diagnostics.append(_diag("SCOPE_PRODUCTION_MISMATCH", "production-candidate must be production eligible", f"{field}.benchmark_scope"))
                if scope == "evaluation-only" and eligibility.production_eligible:
                    diagnostics.append(_diag("SCOPE_EVALUATION_MISMATCH", "evaluation-only must not be production eligible", f"{field}.benchmark_scope"))
                if not eligibility.commercial_output_eligible:
                    diagnostics.append(_diag("OUTPUT_SCOPE", "benchmark profile requires approved commercial output use", field))
                if target:
                    if profile.get("adapter_type") != target.get("adapter_type"):
                        diagnostics.append(_diag("TARGET_ADAPTER", "profile adapter differs from manifest target", f"{field}.profile_path"))
                    if profile.get("runtime_type") != target.get("runtime_type"):
                        diagnostics.append(_diag("TARGET_RUNTIME", "profile runtime differs from manifest target", f"{field}.profile_path"))
                    if target.get("operating_system") not in profile.get("supported_operating_systems", []):
                        diagnostics.append(_diag("TARGET_OS", "profile does not support target operating system", f"{field}.profile_path"))
                    if _number(profile.get("minimum_ram_gb")) and _number(target.get("minimum_ram_gb")) and target["minimum_ram_gb"] < profile["minimum_ram_gb"]:
                        diagnostics.append(_diag("TARGET_RAM", "manifest target RAM is below profile minimum", f"{field}.profile_path"))
                    if _number(profile.get("minimum_vram_gb")) and _number(target.get("minimum_vram_gb")) and target["minimum_vram_gb"] < profile["minimum_vram_gb"]:
                        diagnostics.append(_diag("TARGET_VRAM", "manifest target VRAM is below profile minimum", f"{field}.profile_path"))

        artifacts = model.get("artifacts")
        artifact_summaries: list[dict[str, Any]] = []
        if not isinstance(artifacts, list) or not artifacts:
            diagnostics.append(_diag("ARTIFACTS", "at least one artifact is required", f"{field}.artifacts"))
        else:
            for artifact_index, artifact in enumerate(artifacts):
                artifact_field = f"{field}.artifacts[{artifact_index}]"
                artifact_diagnostics, install_key, artifact_id = _validate_artifact(artifact, artifact_field)
                diagnostics.extend(artifact_diagnostics)
                if artifact_id:
                    if artifact_id in artifact_ids:
                        diagnostics.append(_diag("DUPLICATE_ARTIFACT", f"duplicate artifact ID: {artifact_id}", f"{artifact_field}.id"))
                    artifact_ids.add(artifact_id)
                if install_key:
                    if install_key in install_keys:
                        diagnostics.append(_diag("DUPLICATE_INSTALL_TARGET", "destination and filename are duplicated", artifact_field))
                    install_keys.add(install_key)
                if isinstance(artifact, dict):
                    artifact_summaries.append(
                        {
                            "id": artifact.get("id"),
                            "filename": artifact.get("filename"),
                            "destination": artifact.get("destination"),
                            "size_bytes": artifact.get("size_bytes"),
                            "sha256": artifact.get("sha256"),
                        }
                    )

        diagnostics.extend(_validate_settings(model.get("benchmark_settings"), f"{field}.benchmark_settings"))
        workflow_diagnostics, workflow_path = _validate_workflow(model.get("workflow"), f"{field}.workflow")
        diagnostics.extend(workflow_diagnostics)
        if workflow_path:
            if workflow_path in workflow_paths:
                diagnostics.append(_diag("DUPLICATE_WORKFLOW", "workflow path is duplicated", f"{field}.workflow.expected_api_path"))
            workflow_paths.add(workflow_path)

        summaries.append(
            {
                "family": family,
                "profile_ref": profile_ref,
                "profile_sha256": profile_sha,
                "benchmark_scope": scope,
                "eligibility": eligibility_dict,
                "artifacts": sorted(artifact_summaries, key=lambda item: str(item.get("id", ""))),
                "workflow_status": model.get("workflow", {}).get("status") if isinstance(model.get("workflow"), dict) else None,
                "expected_api_path": workflow_path,
            }
        )

    return _sorted(diagnostics), sorted(summaries, key=lambda item: str(item.get("family", "")))


def result_document(manifest: dict[str, Any], diagnostics: list[dict[str, str]], summaries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": not diagnostics,
        "manifest_id": manifest.get("id"),
        "manifest_version": manifest.get("version"),
        "manifest_sha256": _canonical_sha256(manifest),
        "models": summaries,
        "diagnostics": _sorted(diagnostics),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate exact model installation evidence without mutation")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("check")
    command.add_argument("manifest", type=Path)
    command.add_argument("--workspace-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        diagnostics, summaries = validate_manifest(manifest, workspace_root=args.workspace_root)
        output = result_document(manifest, diagnostics, summaries)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        output = {
            "ok": False,
            "manifest_id": None,
            "manifest_version": None,
            "manifest_sha256": None,
            "models": [],
            "diagnostics": [_diag("ERROR", str(exc))],
        }
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
