"""Deterministic read-only planning for identity-lock experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, canonical_json, content_identifier, safe_relative_path

KIND = "identity-lock-plan"
SCHEMA_VERSION = "1.0"
ROLE_NAMES = frozenset({"boke", "tsukkomi"})
REQUIRED_STRATEGY_TYPES = frozenset({"reference-only", "reference-plus-pose"})
STRATEGY_TYPES = REQUIRED_STRATEGY_TYPES | {"character-lora"}
CONTROL_METHODS = frozenset({"openpose", "lineart", "depth", "t2i-adapter"})
FORBIDDEN_KEYS = frozenset(
    {
        "identity_score",
        "aesthetic_score",
        "score",
        "rank",
        "ranking",
        "winner",
        "recommendation",
        "recommended",
        "automatic_approval",
        "automatic_promotion",
        "variant_promotion",
        "model_override",
        "prompt_only_identity",
    }
)
PROFILE_REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")

TOP_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "version",
        "status",
        "selected_model",
        "roles",
        "pose_targets",
        "expression_targets",
        "strategies",
        "notes",
    }
)
TOP_REQUIRED = TOP_FIELDS - {"notes"}
MODEL_FIELDS = frozenset(
    {
        "family",
        "profile_ref",
        "profile_sha256",
        "workflow_sha256",
        "benchmark_review_ref",
        "benchmark_review_sha256",
        "production_eligible",
    }
)
ROLE_FIELDS = frozenset(
    {"role", "candidate_id", "request_id", "image_sha256", "reference_path"}
)
CONTROL_ASSET_FIELDS = frozenset({"pose", "path", "sha256"})
REFERENCE_ONLY_FIELDS = frozenset({"id", "type"})
REFERENCE_POSE_FIELDS = frozenset({"id", "type", "control_method", "control_assets"})
LORA_FIELDS = frozenset(
    {
        "id",
        "type",
        "dataset_manifest_sha256",
        "training_artifact_sha256",
        "training_config_sha256",
        "license_status",
        "provenance_status",
    }
)


class IdentityLockError(ValueError):
    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


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


def _check_fields(value: Any, required: frozenset[str], allowed: frozenset[str], field: str) -> list[dict[str, str]]:
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


def _safe_path(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str):
        return [_diag("UNSAFE_PATH", "must be a POSIX relative path", field)]
    try:
        safe_relative_path(value)
    except (TypeError, ValueError) as exc:
        return [_diag("UNSAFE_PATH", str(exc), field)]
    return []


def _token_list(value: Any, field: str, minimum: int) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) < minimum:
        return [_diag("TARGET_COUNT", f"requires at least {minimum} values", field)]
    diagnostics: list[dict[str, str]] = []
    if len(value) != len(set(item for item in value if isinstance(item, str))):
        diagnostics.append(_diag("DUPLICATE_VALUE", "values must be unique", field))
    for index, item in enumerate(value):
        diagnostics.extend(_token(item, f"{field}[{index}]"))
    return diagnostics


def _scan_forbidden(value: Any, field: str = "plan") -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{field}.{key}"
            if key in FORBIDDEN_KEYS:
                diagnostics.append(_diag("AUTOMATIC_SELECTION_FORBIDDEN", f"field is forbidden: {key}", current))
            diagnostics.extend(_scan_forbidden(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            diagnostics.extend(_scan_forbidden(item, f"{field}[{index}]"))
    return diagnostics


def _validate_model(value: Any) -> list[dict[str, str]]:
    diagnostics = _check_fields(value, MODEL_FIELDS, MODEL_FIELDS, "selected_model")
    if not isinstance(value, dict):
        return diagnostics
    diagnostics.extend(_token(value.get("family"), "selected_model.family"))
    profile_ref = value.get("profile_ref")
    if not isinstance(profile_ref, str) or not PROFILE_REF_RE.fullmatch(profile_ref):
        diagnostics.append(_diag("MODEL_REFERENCE", "profile_ref must use id@vNNN", "selected_model.profile_ref"))
    diagnostics.extend(_checksum(value.get("profile_sha256"), "selected_model.profile_sha256"))
    diagnostics.extend(_checksum(value.get("workflow_sha256"), "selected_model.workflow_sha256"))
    diagnostics.extend(_token(value.get("benchmark_review_ref"), "selected_model.benchmark_review_ref"))
    diagnostics.extend(_checksum(value.get("benchmark_review_sha256"), "selected_model.benchmark_review_sha256"))
    if value.get("production_eligible") is not True:
        diagnostics.append(_diag("PRODUCTION_ELIGIBILITY", "selected model must be explicitly production eligible", "selected_model.production_eligible"))
    return diagnostics


def _validate_roles(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) != 2:
        return [_diag("ROLE_COVERAGE", "roles must contain exactly boke and tsukkomi", "roles")]
    diagnostics: list[dict[str, str]] = []
    roles: list[str] = []
    for index, item in enumerate(value):
        field = f"roles[{index}]"
        diagnostics.extend(_check_fields(item, ROLE_FIELDS, ROLE_FIELDS, field))
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in ROLE_NAMES:
            diagnostics.append(_diag("ROLE", "role must be boke or tsukkomi", f"{field}.role"))
        elif isinstance(role, str):
            roles.append(role)
        diagnostics.extend(_token(item.get("candidate_id"), f"{field}.candidate_id"))
        diagnostics.extend(_token(item.get("request_id"), f"{field}.request_id"))
        diagnostics.extend(_checksum(item.get("image_sha256"), f"{field}.image_sha256"))
        diagnostics.extend(_safe_path(item.get("reference_path"), f"{field}.reference_path"))
    if set(roles) != ROLE_NAMES or len(roles) != len(set(roles)):
        diagnostics.append(_diag("ROLE_COVERAGE", "roles must contain one boke and one tsukkomi", "roles"))
    return diagnostics


def _validate_control_assets(value: Any, poses: list[str], field: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return [_diag("CONTROL_ASSETS", "control_assets must be a list", field)]
    diagnostics: list[dict[str, str]] = []
    found: list[str] = []
    for index, item in enumerate(value):
        current = f"{field}[{index}]"
        diagnostics.extend(_check_fields(item, CONTROL_ASSET_FIELDS, CONTROL_ASSET_FIELDS, current))
        if not isinstance(item, dict):
            continue
        diagnostics.extend(_token(item.get("pose"), f"{current}.pose"))
        if isinstance(item.get("pose"), str):
            found.append(item["pose"])
        diagnostics.extend(_safe_path(item.get("path"), f"{current}.path"))
        diagnostics.extend(_checksum(item.get("sha256"), f"{current}.sha256"))
    if len(found) != len(set(found)):
        diagnostics.append(_diag("CONTROL_DUPLICATE", "control poses must be unique", field))
    if set(found) != set(poses):
        diagnostics.append(_diag("CONTROL_COVERAGE", "control assets must cover every pose target exactly once", field))
    return diagnostics


def _validate_strategies(value: Any, poses: list[str]) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) < 2:
        return [_diag("STRATEGIES", "at least two strategies are required", "strategies")]
    diagnostics: list[dict[str, str]] = []
    ids: list[str] = []
    types: list[str] = []
    for index, item in enumerate(value):
        field = f"strategies[{index}]"
        if not isinstance(item, dict):
            diagnostics.append(_diag("OBJECT_REQUIRED", "strategy must be an object", field))
            continue
        strategy_type = item.get("type")
        allowed = (
            REFERENCE_ONLY_FIELDS if strategy_type == "reference-only"
            else REFERENCE_POSE_FIELDS if strategy_type == "reference-plus-pose"
            else LORA_FIELDS if strategy_type == "character-lora"
            else frozenset({"id", "type"})
        )
        diagnostics.extend(_check_fields(item, allowed, allowed, field))
        diagnostics.extend(_token(item.get("id"), f"{field}.id"))
        if isinstance(item.get("id"), str):
            ids.append(item["id"])
        if strategy_type not in STRATEGY_TYPES:
            diagnostics.append(_diag("STRATEGY_TYPE", "unsupported strategy type", f"{field}.type"))
            continue
        types.append(strategy_type)
        if strategy_type == "reference-plus-pose":
            if item.get("control_method") not in CONTROL_METHODS:
                diagnostics.append(_diag("CONTROL_METHOD", "unsupported structural control method", f"{field}.control_method"))
            diagnostics.extend(_validate_control_assets(item.get("control_assets"), poses, f"{field}.control_assets"))
        elif strategy_type == "character-lora":
            for name in ("dataset_manifest_sha256", "training_artifact_sha256", "training_config_sha256"):
                diagnostics.extend(_checksum(item.get(name), f"{field}.{name}"))
            if item.get("license_status") != "approved":
                diagnostics.append(_diag("LORA_LICENSE", "character LoRA requires approved license status", f"{field}.license_status"))
            if item.get("provenance_status") != "approved":
                diagnostics.append(_diag("LORA_PROVENANCE", "character LoRA requires approved provenance status", f"{field}.provenance_status"))
    if len(ids) != len(set(ids)):
        diagnostics.append(_diag("DUPLICATE_STRATEGY", "strategy IDs must be unique", "strategies"))
    missing = sorted(REQUIRED_STRATEGY_TYPES - set(types))
    if missing:
        diagnostics.append(_diag("REQUIRED_STRATEGY", f"missing required strategy types: {', '.join(missing)}", "strategies"))
    return diagnostics


def validate_plan(plan: Any) -> list[dict[str, str]]:
    diagnostics = _check_fields(plan, TOP_REQUIRED, TOP_FIELDS, "plan")
    diagnostics.extend(_scan_forbidden(plan))
    if not isinstance(plan, dict):
        return _sorted(diagnostics)
    if plan.get("kind") != KIND:
        diagnostics.append(_diag("KIND", f"kind must be {KIND}", "kind"))
    if plan.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(_diag("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version"))
    diagnostics.extend(_token(plan.get("id"), "id"))
    if not isinstance(plan.get("version"), str) or not VERSION_RE.fullmatch(plan.get("version", "")):
        diagnostics.append(_diag("VERSION", "version must use vNNN", "version"))
    if plan.get("status") != "prepared":
        diagnostics.append(_diag("STATUS", "status must be prepared", "status"))
    if "notes" in plan and (not isinstance(plan.get("notes"), str) or not plan["notes"].strip()):
        diagnostics.append(_diag("TEXT_REQUIRED", "notes must be non-empty when present", "notes"))
    diagnostics.extend(_validate_model(plan.get("selected_model")))
    diagnostics.extend(_validate_roles(plan.get("roles")))
    diagnostics.extend(_token_list(plan.get("pose_targets"), "pose_targets", 3))
    diagnostics.extend(_token_list(plan.get("expression_targets"), "expression_targets", 3))
    poses = plan.get("pose_targets") if isinstance(plan.get("pose_targets"), list) and all(isinstance(item, str) for item in plan["pose_targets"]) else []
    diagnostics.extend(_validate_strategies(plan.get("strategies"), poses))
    return _sorted(diagnostics)


def plan_sha256(plan: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(plan)).hexdigest()


def expand_matrix(plan: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = validate_plan(plan)
    if diagnostics:
        first = diagnostics[0]
        raise IdentityLockError(first["code"], first["message"], first["field"])
    model = plan["selected_model"]
    role_map = {item["role"]: item for item in plan["roles"]}
    strategy_map = {item["id"]: item for item in plan["strategies"]}
    rows: list[dict[str, Any]] = []
    for role in sorted(role_map):
        identity = role_map[role]
        for strategy_id in sorted(strategy_map):
            strategy = strategy_map[strategy_id]
            controls = {
                item["pose"]: item["sha256"]
                for item in strategy.get("control_assets", [])
            }
            for pose in sorted(plan["pose_targets"]):
                for expression in sorted(plan["expression_targets"]):
                    core = {
                        "plan_ref": plan["id"],
                        "plan_version": plan["version"],
                        "model_family": model["family"],
                        "model_profile_ref": model["profile_ref"],
                        "model_profile_sha256": model["profile_sha256"],
                        "workflow_sha256": model["workflow_sha256"],
                        "role": role,
                        "candidate_id": identity["candidate_id"],
                        "request_id": identity["request_id"],
                        "identity_sha256": identity["image_sha256"],
                        "strategy_id": strategy_id,
                        "strategy_type": strategy["type"],
                        "pose": pose,
                        "expression": expression,
                        "control_sha256": controls.get(pose),
                    }
                    run_id = content_identifier("identity-run", core)
                    rows.append(
                        {
                            "run_id": run_id,
                            **core,
                            "output_path": f"identity-lock/{role}/{strategy_id}/{pose}/{expression}/{run_id}.png",
                        }
                    )
    return rows


def _safe_document(path: Path) -> Path:
    expanded = path.expanduser()
    if "\x00" in str(expanded) or ".." in expanded.parts:
        raise IdentityLockError("UNSAFE_PATH", "plan path is unsafe", "plan")
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        raise IdentityLockError("PLAN_SYMLINK", "plan path must not be a symlink", "plan")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise IdentityLockError("PLAN_MISSING", str(exc), "plan") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise IdentityLockError("PLAN_TYPE", "plan must be a regular file", "plan")
    return resolved


def load_plan(path: Path) -> dict[str, Any]:
    resolved = _safe_document(path)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityLockError("PLAN_JSON", str(exc), "plan") from exc
    if not isinstance(value, dict):
        raise IdentityLockError("PLAN_OBJECT", "plan root must be an object", "plan")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and expand identity-lock experiment plans")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan-check", "matrix"):
        command = sub.add_parser(name)
        command.add_argument("plan", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = load_plan(args.plan)
        diagnostics = validate_plan(plan)
        output: dict[str, Any] = {
            "ok": not diagnostics,
            "diagnostics": diagnostics,
            "plan_id": plan.get("id"),
            "plan_version": plan.get("version"),
            "plan_sha256": plan_sha256(plan),
        }
        if not diagnostics and args.command == "matrix":
            rows = expand_matrix(plan)
            output["run_count"] = len(rows)
            output["runs"] = rows
    except IdentityLockError as exc:
        output = {"ok": False, "diagnostics": [exc.to_dict()]}
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
