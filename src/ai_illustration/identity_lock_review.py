"""Read-only owner approval gate for identity-lock consistency evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

from .identity_lock import expand_matrix, load_plan, plan_sha256, validate_plan
from .identity_lock_results import (
    PACKAGE_MANIFEST,
    build_sheet_package,
    document_bytes,
    results_sha256,
    validate_results,
)
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, canonical_json, safe_relative_path
from .quality import HARD_FAIL_CATEGORIES

KIND = "identity-lock-review"
SCHEMA_VERSION = "1.0"
DECISIONS = frozenset({"approve_identity_lock", "reject", "needs_revision"})
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PROFILE_REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")
MAX_PACKAGE_FILE_BYTES = 256 * 1024 * 1024
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
        "similarity_confidence",
        "similarity_threshold",
        "automatic_approval",
        "inferred_strategy",
        "automatic_promotion",
        "variant_promotion",
    }
)

REVIEW_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "plan_ref",
        "plan_version",
        "plan_sha256",
        "results_ref",
        "results_version",
        "results_sha256",
        "package_ref",
        "package_sha256",
        "reviewer",
        "timestamp",
        "decision",
        "role_selections",
        "rejected_evidence",
        "observations",
        "notes",
    }
)
REVIEW_REQUIRED = REVIEW_FIELDS - {"notes"}
SELECTION_FIELDS = frozenset(
    {
        "role",
        "strategy_id",
        "candidate_id",
        "request_id",
        "identity_sha256",
        "model_family",
        "model_profile_ref",
        "model_profile_sha256",
        "workflow_sha256",
        "accepted_run_ids",
    }
)
REJECTION_FIELDS = frozenset({"run_id", "hard_fail_categories"})


class IdentityLockReviewError(ValueError):
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


def _scan_forbidden(value: Any, field: str = "review") -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{field}.{key}"
            if key in FORBIDDEN_KEYS:
                diagnostics.append(_diag("AUTOMATIC_APPROVAL_FORBIDDEN", f"field is forbidden: {key}", current))
            diagnostics.extend(_scan_forbidden(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            diagnostics.extend(_scan_forbidden(item, f"{field}[{index}]"))
    return diagnostics


def _string_list(value: Any, field: str, *, minimum: int = 0, tokens: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) < minimum:
        return [_diag("LIST_REQUIRED", f"requires at least {minimum} values", field)]
    diagnostics: list[dict[str, str]] = []
    if any(not isinstance(item, str) or not item.strip() for item in value):
        diagnostics.append(_diag("TEXT_REQUIRED", "items must be non-empty strings", field))
        return diagnostics
    if len(value) != len(set(value)):
        diagnostics.append(_diag("DUPLICATE_VALUE", "items must be unique", field))
    if tokens:
        invalid = sorted(item for item in value if not TOKEN_RE.fullmatch(item))
        if invalid:
            diagnostics.append(_diag("INVALID_TOKEN", f"invalid tokens: {', '.join(invalid)}", field))
    return diagnostics


def review_semantic_identity(review: dict[str, Any]) -> dict[str, Any]:
    selections: list[dict[str, Any]] = []
    for item in review.get("role_selections", []) if isinstance(review.get("role_selections"), list) else []:
        if not isinstance(item, dict):
            continue
        normalized = {key: item.get(key) for key in sorted(SELECTION_FIELDS - {"accepted_run_ids"})}
        runs = item.get("accepted_run_ids")
        normalized["accepted_run_ids"] = sorted(set(runs)) if isinstance(runs, list) and all(isinstance(v, str) for v in runs) else runs
        selections.append(normalized)
    selections.sort(key=lambda item: (str(item.get("role", "")), str(item.get("strategy_id", ""))))

    rejected: list[dict[str, Any]] = []
    for item in review.get("rejected_evidence", []) if isinstance(review.get("rejected_evidence"), list) else []:
        if not isinstance(item, dict):
            continue
        categories = item.get("hard_fail_categories")
        rejected.append(
            {
                "run_id": item.get("run_id"),
                "hard_fail_categories": sorted(set(categories)) if isinstance(categories, list) and all(isinstance(v, str) for v in categories) else categories,
            }
        )
    rejected.sort(key=lambda item: str(item.get("run_id", "")))
    observations = review.get("observations")
    normalized_observations = sorted(set(observations)) if isinstance(observations, list) and all(isinstance(v, str) for v in observations) else observations
    return {
        "kind": review.get("kind"),
        "schema_version": review.get("schema_version"),
        "plan_ref": review.get("plan_ref"),
        "plan_version": review.get("plan_version"),
        "plan_sha256": review.get("plan_sha256"),
        "results_ref": review.get("results_ref"),
        "results_version": review.get("results_version"),
        "results_sha256": review.get("results_sha256"),
        "package_ref": review.get("package_ref"),
        "package_sha256": review.get("package_sha256"),
        "reviewer": review.get("reviewer"),
        "timestamp": review.get("timestamp"),
        "decision": review.get("decision"),
        "role_selections": selections,
        "rejected_evidence": rejected,
        "observations": normalized_observations,
    }


def expected_review_id(review: dict[str, Any]) -> str:
    suffix = hashlib.sha256(canonical_json(review_semantic_identity(review))).hexdigest()[:16]
    return f"identity-review-{suffix}"


def review_sha256(review: dict[str, Any]) -> str:
    return hashlib.sha256(document_bytes(review)).hexdigest()


def validate_review_document(review: Any) -> list[dict[str, str]]:
    diagnostics = _check_fields(review, REVIEW_REQUIRED, REVIEW_FIELDS, "review")
    diagnostics.extend(_scan_forbidden(review))
    if not isinstance(review, dict):
        return _sorted(diagnostics)
    if review.get("kind") != KIND:
        diagnostics.append(_diag("KIND", f"kind must be {KIND}", "kind"))
    if review.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(_diag("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version"))
    review_id = review.get("id")
    if not isinstance(review_id, str) or not re.fullmatch(r"identity-review-[0-9a-f]{16}", review_id):
        diagnostics.append(_diag("REVIEW_ID", "id must use identity-review-<16hex>", "id"))
    elif review_id != expected_review_id(review):
        diagnostics.append(_diag("REVIEW_ID", f"id must equal {expected_review_id(review)}", "id"))
    for name in ("plan_ref", "results_ref", "package_ref"):
        diagnostics.extend(_token(review.get(name), name))
    for name in ("plan_version", "results_version"):
        if not isinstance(review.get(name), str) or not VERSION_RE.fullmatch(review.get(name, "")):
            diagnostics.append(_diag("VERSION", "must use vNNN", name))
    for name in ("plan_sha256", "results_sha256", "package_sha256"):
        diagnostics.extend(_checksum(review.get(name), name))
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        diagnostics.append(_diag("REVIEWER", "reviewer is required", "reviewer"))
    if not isinstance(review.get("timestamp"), str) or not UTC_RE.fullmatch(review.get("timestamp", "")):
        diagnostics.append(_diag("TIMESTAMP", "timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ", "timestamp"))
    decision = review.get("decision")
    if decision not in DECISIONS:
        diagnostics.append(_diag("DECISION", "unsupported owner decision", "decision"))
    diagnostics.extend(_string_list(review.get("observations"), "observations", minimum=1))
    if "notes" in review and (not isinstance(review.get("notes"), str) or not review["notes"].strip()):
        diagnostics.append(_diag("TEXT_REQUIRED", "notes must be non-empty when present", "notes"))

    selections = review.get("role_selections")
    if not isinstance(selections, list):
        diagnostics.append(_diag("ROLE_SELECTIONS", "role_selections must be a list", "role_selections"))
        selections = []
    roles: list[str] = []
    all_accepted: list[str] = []
    for index, item in enumerate(selections):
        field = f"role_selections[{index}]"
        diagnostics.extend(_check_fields(item, SELECTION_FIELDS, SELECTION_FIELDS, field))
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role not in {"boke", "tsukkomi"}:
            diagnostics.append(_diag("ROLE", "role must be boke or tsukkomi", f"{field}.role"))
        elif isinstance(role, str):
            roles.append(role)
        for name in ("strategy_id", "candidate_id", "request_id", "model_family"):
            diagnostics.extend(_token(item.get(name), f"{field}.{name}"))
        profile_ref = item.get("model_profile_ref")
        if not isinstance(profile_ref, str) or not PROFILE_REF_RE.fullmatch(profile_ref):
            diagnostics.append(_diag("MODEL_REFERENCE", "must use id@vNNN", f"{field}.model_profile_ref"))
        for name in ("identity_sha256", "model_profile_sha256", "workflow_sha256"):
            diagnostics.extend(_checksum(item.get(name), f"{field}.{name}"))
        accepted = item.get("accepted_run_ids")
        diagnostics.extend(_string_list(accepted, f"{field}.accepted_run_ids", minimum=1, tokens=True))
        if isinstance(accepted, list) and all(isinstance(v, str) for v in accepted):
            all_accepted.extend(accepted)

    if decision == "approve_identity_lock":
        if len(selections) != 2 or set(roles) != {"boke", "tsukkomi"} or len(roles) != len(set(roles)):
            diagnostics.append(_diag("ROLE_COVERAGE", "approval requires exactly one boke and one tsukkomi selection", "role_selections"))
    elif selections:
        diagnostics.append(_diag("ROLE_SELECTIONS_FORBIDDEN", f"{decision} must not contain role selections", "role_selections"))
    if len(all_accepted) != len(set(all_accepted)):
        diagnostics.append(_diag("ACCEPTED_RUN_OVERLAP", "accepted run IDs must not overlap between role selections", "role_selections"))

    rejected = review.get("rejected_evidence")
    if not isinstance(rejected, list):
        diagnostics.append(_diag("REJECTED_EVIDENCE", "rejected_evidence must be a list", "rejected_evidence"))
        rejected = []
    rejected_ids: list[str] = []
    for index, item in enumerate(rejected):
        field = f"rejected_evidence[{index}]"
        diagnostics.extend(_check_fields(item, REJECTION_FIELDS, REJECTION_FIELDS, field))
        if not isinstance(item, dict):
            continue
        diagnostics.extend(_token(item.get("run_id"), f"{field}.run_id"))
        if isinstance(item.get("run_id"), str):
            rejected_ids.append(item["run_id"])
        categories = item.get("hard_fail_categories")
        if not isinstance(categories, list) or not categories:
            diagnostics.append(_diag("HARD_FAILS", "hard_fail_categories must be a non-empty list", f"{field}.hard_fail_categories"))
        elif any(not isinstance(category, str) or category not in HARD_FAIL_CATEGORIES for category in categories):
            diagnostics.append(_diag("HARD_FAIL_CATEGORY", "unknown hard-fail category", f"{field}.hard_fail_categories"))
        elif len(categories) != len(set(categories)):
            diagnostics.append(_diag("DUPLICATE_VALUE", "hard-fail categories must be unique", f"{field}.hard_fail_categories"))
    if len(rejected_ids) != len(set(rejected_ids)):
        diagnostics.append(_diag("DUPLICATE_REJECTION", "rejected run IDs must be unique", "rejected_evidence"))
    overlap = sorted(set(all_accepted) & set(rejected_ids))
    if overlap:
        diagnostics.append(_diag("ACCEPT_REJECT_OVERLAP", f"accepted and rejected runs overlap: {', '.join(overlap)}", "rejected_evidence"))
    return _sorted(diagnostics)


def _package_root(path: Path) -> tuple[Path | None, list[dict[str, str]]]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        return None, [_diag("PACKAGE_ROOT_SYMLINK", "package root must not be a symlink", "package_root")]
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        return None, [_diag("PACKAGE_ROOT_MISSING", str(exc), "package_root")]
    if not resolved.is_dir():
        return None, [_diag("PACKAGE_ROOT_TYPE", "package root must be a directory", "package_root")]
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


def validate_package(package_root: Path, expected_files: dict[str, bytes]) -> list[dict[str, str]]:
    root, diagnostics = _package_root(package_root)
    if root is None:
        return diagnostics
    actual: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or _has_symlink(path, root):
            diagnostics.append(_diag("PACKAGE_SYMLINK", "package path contains a symlink", relative))
            continue
        if path.is_file():
            actual.add(relative)
        elif not path.is_dir():
            diagnostics.append(_diag("PACKAGE_TYPE", "package entry must be a regular file or directory", relative))
    expected = set(expected_files)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        diagnostics.append(_diag("PACKAGE_MISSING", f"missing files: {', '.join(missing)}", "package_root"))
    if extra:
        diagnostics.append(_diag("PACKAGE_EXTRA", f"unexpected files: {', '.join(extra)}", "package_root"))
    for relative in sorted(expected & actual):
        try:
            safe = safe_relative_path(relative)
            path = root.joinpath(*safe.parts)
            size = path.stat().st_size
            if size <= 0 or size > MAX_PACKAGE_FILE_BYTES:
                diagnostics.append(_diag("PACKAGE_SIZE", "package file size is out of range", relative))
                continue
            payload = path.read_bytes()
        except (OSError, TypeError, ValueError) as exc:
            diagnostics.append(_diag("PACKAGE_READ", str(exc), relative))
            continue
        if payload != expected_files[relative]:
            diagnostics.append(_diag("PACKAGE_BYTES", "package file bytes differ from deterministic reconstruction", relative))
    return _sorted(diagnostics)


def validate_review(
    review: Any,
    plan: Any,
    results: Any,
    *,
    result_root: Path,
    package_root: Path,
) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
    diagnostics = validate_review_document(review)
    diagnostics.extend(validate_plan(plan))
    result_diagnostics, images = validate_results(results, plan, result_root)
    diagnostics.extend(result_diagnostics)
    if diagnostics or not isinstance(review, dict) or not isinstance(plan, dict) or not isinstance(results, dict):
        return _sorted(diagnostics), {}

    expected_manifest, expected_files = build_sheet_package(results, plan, images)
    diagnostics.extend(validate_package(package_root, expected_files))
    expected_package_sha = hashlib.sha256(document_bytes(expected_manifest)).hexdigest()
    bindings = {
        "plan_ref": plan["id"],
        "plan_version": plan["version"],
        "plan_sha256": plan_sha256(plan),
        "results_ref": results["id"],
        "results_version": results["version"],
        "results_sha256": results_sha256(results),
        "package_ref": expected_manifest["id"],
        "package_sha256": expected_package_sha,
    }
    for field, expected in bindings.items():
        if review.get(field) != expected:
            diagnostics.append(_diag("EVIDENCE_BINDING", f"{field} does not match exact evidence", field))

    matrix = expand_matrix(plan)
    matrix_by_id = {row["run_id"]: row for row in matrix}
    result_by_id = {entry["run_id"]: entry for entry in results["results"]}
    role_plan = {item["role"]: item for item in plan["roles"]}
    model = plan["selected_model"]
    locks: dict[str, dict[str, Any]] = {}
    accepted_all: set[str] = set()

    if review.get("decision") == "approve_identity_lock":
        for index, selection in enumerate(review.get("role_selections", [])):
            if not isinstance(selection, dict):
                continue
            role = selection.get("role")
            if role not in role_plan:
                continue
            identity = role_plan[role]
            exact_fields = {
                "candidate_id": identity["candidate_id"],
                "request_id": identity["request_id"],
                "identity_sha256": identity["image_sha256"],
                "model_family": model["family"],
                "model_profile_ref": model["profile_ref"],
                "model_profile_sha256": model["profile_sha256"],
                "workflow_sha256": model["workflow_sha256"],
            }
            for field, expected in exact_fields.items():
                if selection.get(field) != expected:
                    diagnostics.append(_diag("ROLE_BINDING", f"{field} does not match exact identity/model lock", f"role_selections[{index}].{field}"))
            strategy_id = selection.get("strategy_id")
            strategy_ids = {item["id"] for item in plan["strategies"]}
            if strategy_id not in strategy_ids:
                diagnostics.append(_diag("STRATEGY_BINDING", "strategy_id is not present in the identity-lock plan", f"role_selections[{index}].strategy_id"))
                continue
            expected_runs = sorted(
                row["run_id"] for row in matrix
                if row["role"] == role and row["strategy_id"] == strategy_id
            )
            accepted = selection.get("accepted_run_ids") if isinstance(selection.get("accepted_run_ids"), list) else []
            if sorted(accepted) != expected_runs:
                diagnostics.append(_diag("GRID_COVERAGE", "accepted_run_ids must equal the complete role/strategy pose-expression grid", f"role_selections[{index}].accepted_run_ids"))
            poses: set[str] = set()
            expressions: set[str] = set()
            for run_id in accepted:
                row = matrix_by_id.get(run_id)
                entry = result_by_id.get(run_id)
                if row is None or entry is None:
                    diagnostics.append(_diag("ACCEPTED_RUN_UNKNOWN", "accepted run is not exact matrix evidence", f"role_selections[{index}].accepted_run_ids"))
                    continue
                if row["role"] != role or row["strategy_id"] != strategy_id:
                    diagnostics.append(_diag("ACCEPTED_RUN_BINDING", "accepted run belongs to another role or strategy", run_id))
                if entry.get("state") != "succeeded" or run_id not in images:
                    diagnostics.append(_diag("ACCEPTED_RUN_FAILED", "accepted run must be a checksum-verified successful image", run_id))
                poses.add(row["pose"])
                expressions.add(row["expression"])
                if run_id in accepted_all:
                    diagnostics.append(_diag("ACCEPTED_RUN_OVERLAP", "accepted run appears in more than one role selection", run_id))
                accepted_all.add(run_id)
            if len(poses) < 3 or len(expressions) < 3:
                diagnostics.append(_diag("GRID_DIVERSITY", "approved identity requires at least three poses and three expressions", f"role_selections[{index}].accepted_run_ids"))
            locks[role] = {
                "role": role,
                "candidate_id": identity["candidate_id"],
                "request_id": identity["request_id"],
                "identity_sha256": identity["image_sha256"],
                "strategy_id": strategy_id,
                "model_family": model["family"],
                "model_profile_ref": model["profile_ref"],
                "model_profile_sha256": model["profile_sha256"],
                "workflow_sha256": model["workflow_sha256"],
                "accepted_run_ids": expected_runs,
                "review_ref": review["id"],
                "review_sha256": review_sha256(review),
            }

    for index, rejected in enumerate(review.get("rejected_evidence", [])):
        if not isinstance(rejected, dict):
            continue
        run_id = rejected.get("run_id")
        if run_id not in result_by_id:
            diagnostics.append(_diag("REJECTED_RUN_UNKNOWN", "rejected run is not present in exact results", f"rejected_evidence[{index}].run_id"))
        if run_id in accepted_all:
            diagnostics.append(_diag("ACCEPT_REJECT_OVERLAP", "accepted run cannot carry rejected hard-fail evidence", f"rejected_evidence[{index}].run_id"))

    diagnostics = _sorted(diagnostics)
    if diagnostics or review.get("decision") != "approve_identity_lock" or set(locks) != {"boke", "tsukkomi"}:
        return diagnostics, {}
    return [], {role: locks[role] for role in sorted(locks)}


def _load_object(path: Path, field: str) -> dict[str, Any]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if "\x00" in str(expanded) or ".." in expanded.parts or lexical.is_symlink():
        raise IdentityLockReviewError("UNSAFE_PATH", f"{field} path is unsafe or symlinked", field)
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise IdentityLockReviewError("FILE_MISSING", str(exc), field) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise IdentityLockReviewError("FILE_TYPE", f"{field} must be a regular file", field)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityLockReviewError("JSON", str(exc), field) from exc
    if not isinstance(value, dict):
        raise IdentityLockReviewError("OBJECT_REQUIRED", f"{field} root must be an object", field)
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate owner identity-lock approval without mutation")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("review-check")
    check.add_argument("review", type=Path)
    check.add_argument("plan", type=Path)
    check.add_argument("results", type=Path)
    check.add_argument("--result-root", type=Path, required=True)
    check.add_argument("--package-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        review = _load_object(args.review, "review")
        plan = load_plan(args.plan)
        results = _load_object(args.results, "results")
        diagnostics, locks = validate_review(
            review,
            plan,
            results,
            result_root=args.result_root,
            package_root=args.package_root,
        )
        output: dict[str, Any] = {
            "ok": not diagnostics,
            "diagnostics": diagnostics,
            "review_id": review.get("id"),
            "decision": review.get("decision"),
        }
        if not diagnostics and locks:
            output["identity_locks"] = locks
    except (IdentityLockReviewError, ValueError, OSError) as exc:
        diagnostic = exc.to_dict() if isinstance(exc, IdentityLockReviewError) else _diag("ERROR", str(exc))
        output = {"ok": False, "diagnostics": [diagnostic]}
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
