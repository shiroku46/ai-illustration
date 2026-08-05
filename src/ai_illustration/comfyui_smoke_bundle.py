"""Content-addressed ComfyUI smoke bundle preparation."""

from __future__ import annotations

from datetime import date
from pathlib import Path
import shutil
from typing import Any

from .adapters.base import AdapterError
from .adapters.comfyui import _validated_bindings, sanitize_loopback_endpoint
from .adapters.comfyui_execution_common import validate_catalog_profile, validate_execution_profile
from .adapters.comfyui_execution_plan import prepare_execution
from .models import Manifest
from .naming import content_identifier, safe_relative_path
from .validation import validate_document
from .comfyui_smoke_common import (
    BINDINGS_FILE, DATE_RE, EXECUTION_FILE, MANIFEST_FILE, MODEL_FILE, PROFILE_STATES,
    REQUEST_FILE, TOOL_FILE, WORKFLOW_FILE, SmokeError, _json_bytes, _load_workflow,
    _reject_symlinks, _sha, _tokenize, _validate_token, inspect_workflow,
)


def _profile(
    identifier: str,
    profile_type: str,
    state: str,
    evidence_url: str,
    review_date: str,
    claim: str,
    capabilities: list[str],
    minimum_vram_gb: int,
    minimum_ram_gb: int,
) -> dict[str, Any]:
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
        "control_capabilities": sorted(set(capabilities)),
        "minimum_vram_gb": minimum_vram_gb,
        "minimum_ram_gb": minimum_ram_gb,
        "supported_operating_systems": ["windows"],
        "install_state": "installed",
        "evidence_references": [
            {"source_url": evidence_url, "retrieved_at": review_date, "claim": claim}
        ],
        "license_evidence_state": state,
        "commercial_use_review_state": state,
        "decision_state": state,
    }


def _approval_inputs(
    state: str,
    *,
    confirm_tool_license: bool,
    confirm_model_license: bool,
    confirm_commercial_use: bool,
    tool_evidence_url: str | None,
    model_evidence_url: str | None,
    review_date: str,
) -> tuple[str, str]:
    if state not in PROFILE_STATES:
        raise SmokeError("PROFILE_STATE", "profile state must be reviewing or approved", "profile_state")
    if not DATE_RE.fullmatch(review_date):
        raise SmokeError("REVIEW_DATE", "review_date must use YYYY-MM-DD", "review_date")
    try:
        date.fromisoformat(review_date)
    except ValueError as exc:
        raise SmokeError("REVIEW_DATE", "review_date is not a calendar date", "review_date") from exc
    if state == "approved":
        missing = [
            name
            for name, value in (
                ("confirm_tool_license", confirm_tool_license),
                ("confirm_model_license", confirm_model_license),
                ("confirm_commercial_use", confirm_commercial_use),
            )
            if not value
        ]
        if missing:
            raise SmokeError(
                "APPROVAL_ACKNOWLEDGEMENT",
                "approved state requires explicit acknowledgements: " + ", ".join(missing),
                "profile_state",
            )
        for url, field in (
            (tool_evidence_url, "tool_evidence_url"),
            (model_evidence_url, "model_evidence_url"),
        ):
            if not isinstance(url, str) or not url.startswith("https://") or "example.invalid" in url:
                raise SmokeError("EVIDENCE_URL", f"{field} must be a reviewed https URL", field)
    return (
        tool_evidence_url or "https://example.invalid/pending-owner-review/comfyui",
        model_evidence_url or "https://example.invalid/pending-owner-review/model",
    )


def _bundle_objects(
    workflow_path: Path,
    report: dict[str, Any],
    *,
    profile_state: str,
    review_date: str,
    tool_evidence_url: str,
    model_evidence_url: str,
    tool_id: str,
    model_id: str,
    request_id: str,
    endpoint: str,
    minimum_vram_gb: int,
    minimum_ram_gb: int,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    if not report["ok"]:
        raise SmokeError("INSPECTION_FAILED", "workflow inspection has unresolved diagnostics", "workflow")
    workflow, workflow_bytes, _resolved, _summary = _load_workflow(workflow_path)
    selection = report["selection"]
    values = report["values"]
    bindings = report["bindings"]
    config = report["config"]
    request = {
        "id": request_id,
        "kind": "generation-request",
        "schema_version": "1.0",
        "character_ref": "smoke-character@v001",
        "style_ref": "smoke-style@v001",
        "pose": "standing",
        "expression": "neutral",
        "crop": "full-body",
        "facing": "front",
        "tool_id": tool_id,
        "model_id": model_id,
        "seed": values["seed"],
        "license_status": profile_state,
        "config": config,
        "output_intent": "evaluation",
        "provenance": {"source": "owner-supplied-comfyui-api-workflow"},
    }
    capabilities = ["seed", "workflow"]
    if "positive_prompt" in bindings or "negative_prompt" in bindings:
        capabilities.append("text-prompt")
    tool = _profile(
        tool_id,
        "tool",
        profile_state,
        tool_evidence_url,
        review_date,
        "Owner review of the installed local ComfyUI runtime.",
        capabilities,
        minimum_vram_gb,
        minimum_ram_gb,
    )
    model = _profile(
        model_id,
        "model-configuration",
        profile_state,
        model_evidence_url,
        review_date,
        f"Owner review of local checkpoint {values['checkpoint_name']!r}.",
        capabilities,
        minimum_vram_gb,
        minimum_ram_gb,
    )
    output_nodes = selection["output_node_ids"]
    max_png = 64 * 1024 * 1024
    limits = {
        "max_images": len(output_nodes),
        "max_queue_response_bytes": 64 * 1024,
        "max_history_response_bytes": 4 * 1024 * 1024,
        "max_png_bytes": max_png,
        "max_total_png_bytes": min(512 * 1024 * 1024, max_png * len(output_nodes)),
        "request_timeout_seconds": 30,
        "poll_interval_ms": 250,
        "overall_timeout_seconds": 600,
    }
    execution_core = {
        "kind": "comfyui-execution-profile",
        "schema_version": "1.0",
        "workflow_sha256": _sha(workflow_bytes),
        "tool_profile_ref": tool_id,
        "model_profile_ref": model_id,
        "output_node_ids": output_nodes,
        "expected_width": values["width"],
        "expected_height": values["height"],
        "limits": limits,
    }
    execution = {
        "id": content_identifier("comfyui-execution-profile", execution_core, 20),
        **execution_core,
    }

    request_diagnostics = validate_document(Manifest(Path(REQUEST_FILE), request))
    if request_diagnostics:
        first = request_diagnostics[0]
        raise SmokeError("REQUEST_VALIDATION", f"{first.code}: {first.message}", first.field)
    for value, filename, expected_id, profile_type in (
        (tool, TOOL_FILE, tool_id, "tool"),
        (model, MODEL_FILE, model_id, "model-configuration"),
    ):
        try:
            validate_catalog_profile(
                value,
                source_path=Path(filename),
                expected_id=expected_id,
                profile_type=profile_type,
                field=profile_type,
            )
        except AdapterError as exc:
            if profile_state == "reviewing" and exc.code == "PROFILE_APPROVAL":
                pass
            else:
                raise SmokeError(exc.code, exc.message, exc.field) from exc
    try:
        validate_execution_profile(
            execution,
            workflow_sha=_sha(workflow_bytes),
            tool_id=tool_id,
            model_id=model_id,
        )
        _validated_bindings(request, workflow, bindings)
    except AdapterError as exc:
        raise SmokeError(exc.code, exc.message, exc.field) from exc

    generated = {
        WORKFLOW_FILE: workflow_bytes,
        REQUEST_FILE: _json_bytes(request),
        BINDINGS_FILE: _json_bytes(bindings),
        TOOL_FILE: _json_bytes(tool),
        MODEL_FILE: _json_bytes(model),
        EXECUTION_FILE: _json_bytes(execution),
    }
    files = [
        {"path": path, "sha256": _sha(payload), "size": len(payload)}
        for path, payload in sorted(generated.items())
    ]
    core = {
        "kind": "comfyui-smoke-bundle",
        "schema_version": "1.0",
        "approval_state": profile_state,
        "execution_ready": profile_state == "approved",
        "endpoint": endpoint,
        "workflow": {**report["workflow"], "name": WORKFLOW_FILE},
        "selection": selection,
        "request_ref": request_id,
        "tool_profile_ref": tool_id,
        "model_profile_ref": model_id,
        "execution_profile_ref": execution["id"],
        "expected_width": values["width"],
        "expected_height": values["height"],
        "files": files,
    }
    manifest = {"id": content_identifier("comfyui-smoke-bundle", core, 20), **core}
    generated[MANIFEST_FILE] = _json_bytes(manifest)
    return manifest, generated


def _root(path: Path, field: str, *, must_exist: bool) -> Path:
    raw = str(path)
    expanded = path.expanduser()
    if "\x00" in raw or ".." in expanded.parts:
        raise SmokeError("UNSAFE_PATH", f"{field} path is unsafe", field)
    _reject_symlinks(expanded, field)
    if must_exist and not expanded.is_dir():
        raise SmokeError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise SmokeError("ROOT_TYPE", f"{field} must be a directory", field)
    return expanded.resolve(strict=False)


def _output_location(output_root: Path, workflow_path: Path) -> Path:
    root = _root(output_root, "output_root", must_exist=False)
    source = workflow_path.expanduser().resolve(strict=True)
    source_root = source.parent
    for child, parent in ((root, source_root), (source, root)):
        try:
            child.relative_to(parent)
        except ValueError:
            continue
        raise SmokeError(
            "OUTPUT_OVERLAP",
            "output_root overlaps the workflow source directory",
            "output_root",
        )
    return root


def _write_bundle(root: Path, manifest: dict[str, Any], generated: dict[str, bytes]) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    destination = root / manifest["id"]
    expected = set(generated)
    if destination.is_symlink():
        raise SmokeError("OUTPUT_SYMLINK", "bundle destination is a symlink", "output_root")
    if destination.exists():
        if not destination.is_dir():
            raise SmokeError("OUTPUT_CONFLICT", "bundle destination is not a directory", "output_root")
        actual: set[str] = set()
        for candidate in destination.rglob("*"):
            if candidate.is_symlink():
                raise SmokeError("OUTPUT_SYMLINK", "existing bundle contains a symlink", str(candidate))
            if candidate.is_file():
                actual.add(candidate.relative_to(destination).as_posix())
        if actual != expected:
            raise SmokeError("OUTPUT_CONFLICT", "existing bundle file set differs", "output_root")
        for relative, payload in generated.items():
            if (destination / relative).read_bytes() != payload:
                raise SmokeError("OUTPUT_CONFLICT", f"existing bundle file differs: {relative}", relative)
        return False
    staging = root / f".{manifest['id']}.tmp"
    if staging.exists():
        raise SmokeError("STAGING_CONFLICT", "bundle staging path already exists", "output_root")
    try:
        staging.mkdir()
        for relative, payload in generated.items():
            safe_relative_path(relative)
            (staging / relative).write_bytes(payload)
        if manifest["approval_state"] == "approved":
            prepare_execution(
                staging / REQUEST_FILE,
                staging / WORKFLOW_FILE,
                staging / BINDINGS_FILE,
                staging / TOOL_FILE,
                staging / MODEL_FILE,
                staging / EXECUTION_FILE,
                endpoint=manifest["endpoint"],
            )
        staging.replace(destination)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    return True


def prepare_bundle(
    workflow_path: Path,
    output_root: Path,
    *,
    profile_state: str = "reviewing",
    review_date: str,
    tool_evidence_url: str | None = None,
    model_evidence_url: str | None = None,
    confirm_tool_license: bool = False,
    confirm_model_license: bool = False,
    confirm_commercial_use: bool = False,
    tool_id: str = "comfyui-local-api",
    model_id: str | None = None,
    request_id: str | None = None,
    endpoint: str = "http://127.0.0.1:8188",
    minimum_vram_gb: int = 0,
    minimum_ram_gb: int = 0,
    write: bool = False,
    **inspection_options: Any,
) -> dict[str, Any]:
    tool_url, model_url = _approval_inputs(
        profile_state,
        confirm_tool_license=confirm_tool_license,
        confirm_model_license=confirm_model_license,
        confirm_commercial_use=confirm_commercial_use,
        tool_evidence_url=tool_evidence_url,
        model_evidence_url=model_evidence_url,
        review_date=review_date,
    )
    tool_id = _validate_token(tool_id, "tool_id")
    endpoint = sanitize_loopback_endpoint(endpoint)
    report = inspect_workflow(workflow_path, **inspection_options)
    checkpoint_name = report["values"].get("checkpoint_name")
    if not isinstance(checkpoint_name, str):
        raise SmokeError("VALUE_REQUIRED", "checkpoint filename is required", "checkpoint_name")
    model_id = _validate_token(model_id or _tokenize(Path(checkpoint_name).stem, "model"), "model_id")
    request_id = _validate_token(
        request_id or f"smoke-{report['workflow']['canonical_sha256'][:12]}",
        "request_id",
    )
    for value, field in ((minimum_vram_gb, "minimum_vram_gb"), (minimum_ram_gb, "minimum_ram_gb")):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1024:
            raise SmokeError("INTEGER_RANGE", f"{field} must be from 0 to 1024", field)
    output_location = _output_location(output_root, workflow_path)
    manifest, generated = _bundle_objects(
        workflow_path,
        report,
        profile_state=profile_state,
        review_date=review_date,
        tool_evidence_url=tool_url,
        model_evidence_url=model_url,
        tool_id=tool_id,
        model_id=model_id,
        request_id=request_id,
        endpoint=endpoint,
        minimum_vram_gb=minimum_vram_gb,
        minimum_ram_gb=minimum_ram_gb,
    )
    written = _write_bundle(output_location, manifest, generated) if write else False
    return {
        "ok": True,
        "bundle": manifest,
        "bundle_path": manifest["id"],
        "written": written,
        "idempotent": write and not written,
        "execution_ready": manifest["execution_ready"],
        "network_contacted": False,
        "external_process_started": False,
    }
