"""Read-only validation and expansion for reproducible model benchmark plans."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

from .art_direction import load_document, validate_approval
from .catalog import evaluate_compatibility, validate_hardware_profile, validate_tool_profile
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, canonical_json, safe_relative_path

PLAN_KIND = "model-benchmark-plan"
SCHEMA_VERSION = "1.0"
PLAN_STATUS = "prepared"
SELECTION_POLICY = "owner-only"
MINIMUM_MODEL_FAMILIES = 3
MINIMUM_SEEDS = 8
MAX_DEPENDENCY_BYTES = 4 * 1024 * 1024
MAX_WORKFLOW_BYTES = 16 * 1024 * 1024

REQUIRED_PROMPT_CASES = frozenset(
    {
        "front-full-body-neutral",
        "three-quarter-readable-hands",
        "expressive-face-close-up",
        "seated-or-asymmetric-pose",
        "clothing-detail-stress",
        "two-character-secondary-stress",
    }
)
SECONDARY_CASE = "two-character-secondary-stress"
ROLE_SCOPES = frozenset({"single-role", "two-character-secondary"})

PLAN_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "version",
        "status",
        "art_direction",
        "hardware",
        "models",
        "seeds",
        "prompt_cases",
        "output_root",
        "selection_policy",
        "notes",
    }
)
PLAN_REQUIRED = PLAN_FIELDS - {"notes"}
ART_FIELDS = frozenset(
    {
        "profile_path",
        "profile_id",
        "profile_version",
        "profile_sha256",
        "review_path",
        "review_id",
        "review_sha256",
    }
)
HARDWARE_FIELDS = frozenset({"path", "id", "sha256"})
MODEL_FIELDS = frozenset(
    {
        "family",
        "profile_path",
        "profile_id",
        "profile_version",
        "profile_sha256",
        "workflow_path",
        "workflow_sha256",
        "native_width",
        "native_height",
        "sampler",
        "scheduler",
        "steps",
        "cfg",
        "prompt_format",
        "evidence_note",
    }
)
PROMPT_FIELDS = frozenset(
    {
        "id",
        "role_scope",
        "positive_contract",
        "negative_contract",
        "crop",
        "pose",
        "expression",
    }
)
SECRET_KEY_RE = re.compile(
    r"^(?:api[_-]?key|access[_-]?token|auth(?:orization)?|cookie|password|secret)$",
    re.IGNORECASE,
)
SECRET_VALUE_RE = re.compile(
    r"(?:\bBearer\s+[A-Za-z0-9._~+/=-]{8,}|\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


class BenchmarkError(ValueError):
    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _diag(code: str, message: str, field: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "field": field}


def _sorted_diagnostics(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique = {
        (item.get("field", ""), item.get("code", ""), item.get("message", "")): {
            "code": item.get("code", ""),
            "message": item.get("message", ""),
            "field": item.get("field", ""),
        }
        for item in values
    }
    return [unique[key] for key in sorted(unique)]


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def document_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(document_bytes(value)).hexdigest()


def _check_fields(
    value: Any,
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    field: str,
) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return [_diag("OBJECT_REQUIRED", "must be an object", field)]
    diagnostics: list[dict[str, str]] = []
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        diagnostics.append(_diag("MISSING_FIELD", f"missing fields: {', '.join(missing)}", field))
    if unknown:
        diagnostics.append(_diag("UNKNOWN_FIELD", f"unknown fields: {', '.join(unknown)}", field))
    return diagnostics


def _token(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        return [_diag("INVALID_TOKEN", "must be a lowercase ASCII token", field)]
    return []


def _checksum(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        return [_diag("CHECKSUM", "must be 64 lowercase hexadecimal characters", field)]
    return []


def _relative_path(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str):
        return [_diag("UNSAFE_PATH", "must be a POSIX relative path", field)]
    try:
        safe_relative_path(value)
    except ValueError as exc:
        return [_diag("UNSAFE_PATH", str(exc), field)]
    return []


def _validate_art_binding(value: Any) -> list[dict[str, str]]:
    field = "art_direction"
    diagnostics = _check_fields(value, required=ART_FIELDS, allowed=ART_FIELDS, field=field)
    if not isinstance(value, dict):
        return diagnostics
    diagnostics.extend(_relative_path(value.get("profile_path"), f"{field}.profile_path"))
    diagnostics.extend(_token(value.get("profile_id"), f"{field}.profile_id"))
    if not isinstance(value.get("profile_version"), str) or not VERSION_RE.fullmatch(value.get("profile_version", "")):
        diagnostics.append(_diag("VERSION", "must use vNNN", f"{field}.profile_version"))
    diagnostics.extend(_checksum(value.get("profile_sha256"), f"{field}.profile_sha256"))
    diagnostics.extend(_relative_path(value.get("review_path"), f"{field}.review_path"))
    diagnostics.extend(_token(value.get("review_id"), f"{field}.review_id"))
    diagnostics.extend(_checksum(value.get("review_sha256"), f"{field}.review_sha256"))
    return diagnostics


def _validate_hardware_binding(value: Any) -> list[dict[str, str]]:
    field = "hardware"
    diagnostics = _check_fields(
        value, required=HARDWARE_FIELDS, allowed=HARDWARE_FIELDS, field=field
    )
    if not isinstance(value, dict):
        return diagnostics
    diagnostics.extend(_relative_path(value.get("path"), f"{field}.path"))
    diagnostics.extend(_token(value.get("id"), f"{field}.id"))
    diagnostics.extend(_checksum(value.get("sha256"), f"{field}.sha256"))
    return diagnostics


def _validate_model(value: Any, index: int) -> list[dict[str, str]]:
    field = f"models[{index}]"
    diagnostics = _check_fields(
        value, required=MODEL_FIELDS, allowed=MODEL_FIELDS, field=field
    )
    if not isinstance(value, dict):
        return diagnostics
    for name in ("family", "profile_id", "sampler", "scheduler"):
        diagnostics.extend(_token(value.get(name), f"{field}.{name}"))
    if not isinstance(value.get("profile_version"), str) or not VERSION_RE.fullmatch(value.get("profile_version", "")):
        diagnostics.append(_diag("VERSION", "must use vNNN", f"{field}.profile_version"))
    for name in ("profile_path", "workflow_path"):
        diagnostics.extend(_relative_path(value.get(name), f"{field}.{name}"))
    for name in ("profile_sha256", "workflow_sha256"):
        diagnostics.extend(_checksum(value.get(name), f"{field}.{name}"))
    for name in ("native_width", "native_height", "steps"):
        number = value.get(name)
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            diagnostics.append(_diag("POSITIVE_INTEGER", "must be a positive integer", f"{field}.{name}"))
    cfg = value.get("cfg")
    if not isinstance(cfg, (int, float)) or isinstance(cfg, bool) or cfg <= 0:
        diagnostics.append(_diag("POSITIVE_NUMBER", "must be a positive number", f"{field}.cfg"))
    for name in ("prompt_format", "evidence_note"):
        if not _nonempty_text(value.get(name)):
            diagnostics.append(_diag("TEXT_REQUIRED", "must be non-empty", f"{field}.{name}"))
    return diagnostics


def _validate_prompt_case(value: Any, index: int) -> list[dict[str, str]]:
    field = f"prompt_cases[{index}]"
    diagnostics = _check_fields(
        value, required=PROMPT_FIELDS, allowed=PROMPT_FIELDS, field=field
    )
    if not isinstance(value, dict):
        return diagnostics
    diagnostics.extend(_token(value.get("id"), f"{field}.id"))
    if value.get("role_scope") not in ROLE_SCOPES:
        diagnostics.append(_diag("ROLE_SCOPE", "invalid role_scope", f"{field}.role_scope"))
    for name in ("positive_contract", "negative_contract"):
        if not _nonempty_text(value.get(name)):
            diagnostics.append(_diag("TEXT_REQUIRED", "must be non-empty", f"{field}.{name}"))
    for name in ("crop", "pose", "expression"):
        diagnostics.extend(_token(value.get(name), f"{field}.{name}"))
    case_id = value.get("id")
    expected_scope = "two-character-secondary" if case_id == SECONDARY_CASE else "single-role"
    if case_id in REQUIRED_PROMPT_CASES and value.get("role_scope") != expected_scope:
        diagnostics.append(
            _diag(
                "ROLE_SCOPE",
                f"{case_id} must use role_scope={expected_scope}",
                f"{field}.role_scope",
            )
        )
    return diagnostics


def validate_plan(plan: Any) -> list[dict[str, str]]:
    diagnostics = _check_fields(
        plan, required=PLAN_REQUIRED, allowed=PLAN_FIELDS, field="plan"
    )
    if not isinstance(plan, dict):
        return diagnostics
    if plan.get("kind") != PLAN_KIND:
        diagnostics.append(_diag("KIND", f"kind must be {PLAN_KIND}", "kind"))
    if plan.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(_diag("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version"))
    diagnostics.extend(_token(plan.get("id"), "id"))
    if not isinstance(plan.get("version"), str) or not VERSION_RE.fullmatch(plan.get("version", "")):
        diagnostics.append(_diag("VERSION", "version must use vNNN", "version"))
    if plan.get("status") != PLAN_STATUS:
        diagnostics.append(_diag("STATUS", f"status must be {PLAN_STATUS}", "status"))
    if plan.get("selection_policy") != SELECTION_POLICY:
        diagnostics.append(
            _diag(
                "SELECTION_POLICY",
                "selection_policy must be owner-only; automatic ranking is forbidden",
                "selection_policy",
            )
        )
    diagnostics.extend(_relative_path(plan.get("output_root"), "output_root"))
    if "notes" in plan and not _nonempty_text(plan.get("notes")):
        diagnostics.append(_diag("TEXT_REQUIRED", "notes must be non-empty when present", "notes"))
    diagnostics.extend(_validate_art_binding(plan.get("art_direction")))
    diagnostics.extend(_validate_hardware_binding(plan.get("hardware")))

    seeds = plan.get("seeds")
    if not isinstance(seeds, list) or len(seeds) < MINIMUM_SEEDS:
        diagnostics.append(_diag("SEEDS", f"at least {MINIMUM_SEEDS} seeds are required", "seeds"))
    else:
        for index, seed in enumerate(seeds):
            if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
                diagnostics.append(_diag("SEED", "must be a non-negative integer", f"seeds[{index}]"))
        if all(isinstance(seed, int) and not isinstance(seed, bool) for seed in seeds) and len(seeds) != len(set(seeds)):
            diagnostics.append(_diag("DUPLICATE_SEED", "seeds must be unique", "seeds"))

    models = plan.get("models")
    if not isinstance(models, list) or len(models) < MINIMUM_MODEL_FAMILIES:
        diagnostics.append(
            _diag(
                "MODEL_COUNT",
                f"at least {MINIMUM_MODEL_FAMILIES} model families are required",
                "models",
            )
        )
    else:
        for index, model in enumerate(models):
            diagnostics.extend(_validate_model(model, index))
        families = [
            item.get("family")
            for item in models
            if isinstance(item, dict) and isinstance(item.get("family"), str)
        ]
        profile_ids = [
            item.get("profile_id")
            for item in models
            if isinstance(item, dict) and isinstance(item.get("profile_id"), str)
        ]
        if len(families) != len(set(families)):
            diagnostics.append(_diag("DUPLICATE_FAMILY", "model families must be unique", "models"))
        if len(profile_ids) != len(set(profile_ids)):
            diagnostics.append(_diag("DUPLICATE_MODEL", "model profile IDs must be unique", "models"))

    prompt_cases = plan.get("prompt_cases")
    if not isinstance(prompt_cases, list):
        diagnostics.append(_diag("PROMPT_CASES", "prompt_cases must be a list", "prompt_cases"))
    else:
        for index, prompt_case in enumerate(prompt_cases):
            diagnostics.extend(_validate_prompt_case(prompt_case, index))
        ids = [
            item.get("id")
            for item in prompt_cases
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        if len(ids) != len(set(ids)):
            diagnostics.append(_diag("DUPLICATE_CASE", "prompt case IDs must be unique", "prompt_cases"))
        if set(ids) != REQUIRED_PROMPT_CASES or len(prompt_cases) != len(REQUIRED_PROMPT_CASES):
            missing = sorted(REQUIRED_PROMPT_CASES - set(ids))
            extra = sorted(set(ids) - REQUIRED_PROMPT_CASES)
            diagnostics.append(
                _diag(
                    "PROMPT_COVERAGE",
                    f"prompt cases must match required set; missing={missing}, extra={extra}",
                    "prompt_cases",
                )
            )
    return _sorted_diagnostics(diagnostics)


def _root(path: Path) -> tuple[Path | None, list[dict[str, str]]]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        return None, [_diag("ROOT_SYMLINK", "workspace root must not be a symlink", "workspace_root")]
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        return None, [_diag("ROOT_MISSING", str(exc), "workspace_root")]
    if not resolved.is_dir():
        return None, [_diag("ROOT_TYPE", "workspace root must be a directory", "workspace_root")]
    return resolved, []


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


def _read_under_root(
    root: Path,
    relative: str,
    *,
    maximum: int,
    field: str,
) -> tuple[Path | None, bytes | None, list[dict[str, str]]]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        return None, None, [_diag("UNSAFE_PATH", str(exc), field)]
    lexical = root.joinpath(*safe.parts)
    if _has_symlink(lexical, root):
        return None, None, [_diag("PATH_SYMLINK", "path contains a symlink", field)]
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        return None, None, [_diag("FILE_MISSING", str(exc), field)]
    if not resolved.is_file() or resolved.is_symlink():
        return None, None, [_diag("FILE_TYPE", "must be a regular file", field)]
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return None, None, [_diag("FILE_READ", str(exc), field)]
    if size <= 0 or size > maximum:
        return None, None, [_diag("FILE_SIZE", f"size must be 1..{maximum} bytes", field)]
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        return None, None, [_diag("FILE_READ", str(exc), field)]
    if len(payload) != size or len(payload) > maximum:
        return None, None, [_diag("FILE_SIZE", "file changed or exceeded size while reading", field)]
    return resolved, payload, []


def _json_object(payload: bytes, field: str) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        return None, [_diag("JSON", str(exc), field)]
    if not isinstance(value, dict):
        return None, [_diag("JSON_OBJECT", "JSON root must be an object", field)]
    return value, []


def _dependency_object(
    root: Path,
    relative: str,
    *,
    field: str,
) -> tuple[Path | None, dict[str, Any] | None, list[dict[str, str]]]:
    path, payload, diagnostics = _read_under_root(
        root, relative, maximum=MAX_DEPENDENCY_BYTES, field=field
    )
    if diagnostics or path is None or payload is None:
        return path, None, diagnostics
    value, json_diagnostics = _json_object(payload, field)
    return path, value, json_diagnostics


def _catalog_diagnostics(values: Iterable[Any], prefix: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for item in values:
        field = str(getattr(item, "field", ""))
        result.append(
            _diag(
                str(getattr(item, "code", "CATALOG")),
                str(getattr(item, "message", item)),
                f"{prefix}.{field}" if field else prefix,
            )
        )
    return result


def _scan_secrets(value: Any, field: str = "workflow") -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{field}.{key}"
            if isinstance(key, str) and SECRET_KEY_RE.fullmatch(key):
                diagnostics.append(_diag("WORKFLOW_SECRET", "credential-like key is forbidden", child))
            diagnostics.extend(_scan_secrets(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            diagnostics.extend(_scan_secrets(item, f"{field}[{index}]"))
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value):
        diagnostics.append(_diag("WORKFLOW_SECRET", "credential-like value is forbidden", field))
    return diagnostics


def validate_dependencies(
    plan: Any,
    workspace_root: Path,
    reference_root: Path,
) -> list[dict[str, str]]:
    diagnostics = validate_plan(plan)
    if diagnostics or not isinstance(plan, dict):
        return diagnostics
    root, root_diagnostics = _root(workspace_root)
    if root is None:
        return root_diagnostics

    art = plan["art_direction"]
    profile_path, profile, values = _dependency_object(
        root, art["profile_path"], field="art_direction.profile_path"
    )
    diagnostics.extend(values)
    review_path, review, values = _dependency_object(
        root, art["review_path"], field="art_direction.review_path"
    )
    diagnostics.extend(values)
    if profile is not None:
        if profile.get("id") != art["profile_id"] or profile.get("version") != art["profile_version"]:
            diagnostics.append(_diag("ART_BINDING", "art profile ID/version mismatch", "art_direction"))
        if canonical_sha256(profile) != art["profile_sha256"]:
            diagnostics.append(_diag("ART_BINDING", "art profile SHA-256 mismatch", "art_direction.profile_sha256"))
    if review is not None:
        if review.get("id") != art["review_id"]:
            diagnostics.append(_diag("ART_BINDING", "art review ID mismatch", "art_direction.review_id"))
        if canonical_sha256(review) != art["review_sha256"]:
            diagnostics.append(_diag("ART_BINDING", "art review SHA-256 mismatch", "art_direction.review_sha256"))
    if profile is not None and review is not None:
        diagnostics.extend(
            _diag(item["code"], item["message"], f"art_approval.{item['field']}" if item["field"] else "art_approval")
            for item in validate_approval(profile, review, reference_root)
        )

    hardware_binding = plan["hardware"]
    hardware_path, hardware, values = _dependency_object(
        root, hardware_binding["path"], field="hardware.path"
    )
    diagnostics.extend(values)
    if hardware is not None and hardware_path is not None:
        diagnostics.extend(_catalog_diagnostics(validate_hardware_profile(hardware_path, hardware), "hardware"))
        if hardware.get("id") != hardware_binding["id"]:
            diagnostics.append(_diag("HARDWARE_BINDING", "hardware ID mismatch", "hardware.id"))
        if canonical_sha256(hardware) != hardware_binding["sha256"]:
            diagnostics.append(_diag("HARDWARE_BINDING", "hardware SHA-256 mismatch", "hardware.sha256"))

    for index, model in enumerate(plan["models"]):
        prefix = f"models[{index}]"
        model_path, model_profile, values = _dependency_object(
            root, model["profile_path"], field=f"{prefix}.profile_path"
        )
        diagnostics.extend(values)
        if model_profile is not None and model_path is not None:
            diagnostics.extend(
                _catalog_diagnostics(
                    validate_tool_profile(model_path, model_profile),
                    f"{prefix}.profile",
                )
            )
            if model_profile.get("id") != model["profile_id"] or model_profile.get("version") != model["profile_version"]:
                diagnostics.append(_diag("MODEL_BINDING", "model profile ID/version mismatch", prefix))
            if canonical_sha256(model_profile) != model["profile_sha256"]:
                diagnostics.append(_diag("MODEL_BINDING", "model profile SHA-256 mismatch", f"{prefix}.profile_sha256"))
            if model_profile.get("profile_type") != "model-configuration":
                diagnostics.append(_diag("MODEL_PROFILE", "profile_type must be model-configuration", f"{prefix}.profile"))
            for field_name in (
                "license_evidence_state",
                "commercial_use_review_state",
                "decision_state",
            ):
                if model_profile.get(field_name) != "approved":
                    diagnostics.append(_diag("MODEL_APPROVAL", f"{field_name} must be approved", f"{prefix}.profile.{field_name}"))
            if model_profile.get("offline_capability") != "yes":
                diagnostics.append(_diag("MODEL_OFFLINE", "offline_capability must be yes", f"{prefix}.profile.offline_capability"))
            if model_profile.get("deterministic_seed_support") is not True:
                diagnostics.append(_diag("MODEL_SEED", "deterministic seed support is required", f"{prefix}.profile.deterministic_seed_support"))
            if hardware is not None:
                compatibility = evaluate_compatibility(model_profile, hardware)
                if compatibility.status != "compatible-by-declaration":
                    diagnostics.append(
                        _diag(
                            "MODEL_COMPATIBILITY",
                            json.dumps(compatibility.to_dict(), sort_keys=True, separators=(",", ":")),
                            f"{prefix}.profile",
                        )
                    )

        workflow_path, workflow_payload, values = _read_under_root(
            root,
            model["workflow_path"],
            maximum=MAX_WORKFLOW_BYTES,
            field=f"{prefix}.workflow_path",
        )
        diagnostics.extend(values)
        if workflow_payload is not None:
            if hashlib.sha256(workflow_payload).hexdigest() != model["workflow_sha256"]:
                diagnostics.append(_diag("WORKFLOW_BINDING", "workflow SHA-256 mismatch", f"{prefix}.workflow_sha256"))
            workflow, values = _json_object(workflow_payload, f"{prefix}.workflow")
            diagnostics.extend(values)
            if workflow is not None:
                diagnostics.extend(_scan_secrets(workflow, f"{prefix}.workflow"))
    return _sorted_diagnostics(diagnostics)


def expand_matrix(plan: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = validate_plan(plan)
    if diagnostics:
        raise BenchmarkError("PLAN_INVALID", json.dumps(diagnostics, sort_keys=True, separators=(",", ":")), "plan")
    output_root = str(safe_relative_path(plan["output_root"]))
    rows: list[dict[str, Any]] = []
    models = sorted(plan["models"], key=lambda item: (item["family"], item["profile_id"], item["profile_version"]))
    cases = sorted(plan["prompt_cases"], key=lambda item: item["id"])
    for model in models:
        settings = {
            "width": model["native_width"],
            "height": model["native_height"],
            "sampler": model["sampler"],
            "scheduler": model["scheduler"],
            "steps": model["steps"],
            "cfg": model["cfg"],
            "prompt_format": model["prompt_format"],
        }
        for seed in sorted(plan["seeds"]):
            for prompt_case in cases:
                identity = {
                    "plan_id": plan["id"],
                    "plan_version": plan["version"],
                    "family": model["family"],
                    "profile_id": model["profile_id"],
                    "profile_version": model["profile_version"],
                    "profile_sha256": model["profile_sha256"],
                    "workflow_sha256": model["workflow_sha256"],
                    "seed": seed,
                    "prompt_case": prompt_case,
                    "settings": settings,
                }
                run_id = f"bench-{hashlib.sha256(canonical_json(identity)).hexdigest()[:16]}"
                directory = f"{output_root}/{plan['id']}/{model['family']}/{prompt_case['id']}/{seed}"
                rows.append(
                    {
                        "run_id": run_id,
                        "model_family": model["family"],
                        "model_profile_ref": f"{model['profile_id']}@{model['profile_version']}",
                        "model_profile_sha256": model["profile_sha256"],
                        "workflow_sha256": model["workflow_sha256"],
                        "seed": seed,
                        "prompt_case_id": prompt_case["id"],
                        "role_scope": prompt_case["role_scope"],
                        "settings": settings,
                        "image_path": f"{directory}/{run_id}.png",
                        "metadata_path": f"{directory}/{run_id}.json",
                    }
                )
    return rows


def _result(
    *,
    diagnostics: list[dict[str, str]],
    plan: dict[str, Any] | None = None,
    matrix: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": not diagnostics, "diagnostics": _sorted_diagnostics(diagnostics)}
    if plan is not None:
        result["plan_id"] = plan.get("id")
        result["plan_version"] = plan.get("version")
        result["plan_sha256"] = canonical_sha256(plan)
    if matrix is not None:
        result["matrix_count"] = len(matrix)
        result["matrix"] = matrix
    elif plan is not None and not diagnostics:
        result["matrix_count"] = len(plan["models"]) * len(plan["seeds"]) * len(plan["prompt_cases"])
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and expand local model benchmark plans")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan-check", "matrix"):
        command = sub.add_parser(name)
        command.add_argument("plan", type=Path)
        command.add_argument("--workspace-root", type=Path, required=True)
        command.add_argument("--reference-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = load_document(args.plan)
        diagnostics = validate_dependencies(plan, args.workspace_root, args.reference_root)
        matrix = expand_matrix(plan) if not diagnostics and args.command == "matrix" else None
        result = _result(diagnostics=diagnostics, plan=plan, matrix=matrix)
    except (BenchmarkError, ValueError) as exc:
        error = exc.to_dict() if isinstance(exc, BenchmarkError) else _diag("LOAD_ERROR", str(exc), "plan")
        result = _result(diagnostics=[error])
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
