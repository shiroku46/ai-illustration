"""Read-only owner review and selected-model lock for benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

from .art_direction import load_document
from .benchmark_results import (
    PACKAGE_MANIFEST,
    build_contact_sheet_package,
    document_bytes,
    result_set_sha256,
    validate_results,
)
from .catalog import evaluate_model_license_eligibility
from .model_benchmark import canonical_sha256
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, canonical_json, safe_relative_path
from .quality import HARD_FAIL_CATEGORIES

REVIEW_KIND = "model-benchmark-review"
SCHEMA_VERSION = "1.0"
DECISIONS = frozenset({"select_model", "reject_all", "needs_revision"})
MIN_ACCEPTED_RUNS = 4
MIN_ACCEPTED_SEEDS = 3
MIN_ACCEPTED_CASES = 3
REQUIRED_ACCEPTED_CASES = frozenset(
    {"front-full-body-neutral", "three-quarter-readable-hands"}
)
MAX_PACKAGE_FILE_BYTES = 256 * 1024 * 1024
MAX_MODEL_PROFILE_BYTES = 4 * 1024 * 1024
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
MODEL_REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")

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
        "selected_model",
        "accepted_run_ids",
        "rejected_run_ids",
        "hard_fail_categories",
        "observations",
        "notes",
    }
)
REVIEW_REQUIRED = REVIEW_FIELDS - {"selected_model", "notes"}
SELECTED_MODEL_FIELDS = frozenset(
    {"family", "profile_ref", "profile_sha256", "workflow_sha256"}
)
FORBIDDEN_DECISION_FIELDS = frozenset(
    {
        "score",
        "scores",
        "aesthetic_score",
        "rank",
        "ranking",
        "winner",
        "recommendation",
        "recommended",
        "confidence",
        "automatic_approval",
        "derived_selection",
    }
)


class BenchmarkReviewError(ValueError):
    """One deterministic benchmark-review validation failure."""

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


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _fields(
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
    forbidden = sorted(set(value) & FORBIDDEN_DECISION_FIELDS)
    if missing:
        diagnostics.append(
            _diag("MISSING_FIELD", f"missing fields: {', '.join(missing)}", field)
        )
    if unknown:
        diagnostics.append(
            _diag("UNKNOWN_FIELD", f"unknown fields: {', '.join(unknown)}", field)
        )
    if forbidden:
        diagnostics.append(
            _diag(
                "AUTOMATIC_SELECTION_FORBIDDEN",
                f"automatic decision fields are forbidden: {', '.join(forbidden)}",
                field,
            )
        )
    return diagnostics


def _token(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        return [_diag("INVALID_TOKEN", "must be a lowercase ASCII token", field)]
    return []


def _version(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        return [_diag("VERSION", "must use vNNN", field)]
    return []


def _checksum(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        return [_diag("CHECKSUM", "must be 64 lowercase hexadecimal characters", field)]
    return []


def _string_list(
    value: Any,
    *,
    field: str,
    allow_empty: bool = True,
    token_items: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or (not allow_empty and not value):
        return [
            _diag(
                "LIST_REQUIRED",
                "must be a list" if allow_empty else "must be a non-empty list",
                field,
            )
        ]
    diagnostics: list[dict[str, str]] = []
    if any(not _nonempty(item) for item in value):
        diagnostics.append(
            _diag("TEXT_REQUIRED", "items must be non-empty strings", field)
        )
        return diagnostics
    if len(value) != len(set(value)):
        diagnostics.append(_diag("DUPLICATE_VALUE", "items must be unique", field))
    if token_items:
        invalid = sorted(item for item in value if not TOKEN_RE.fullmatch(item))
        if invalid:
            diagnostics.append(
                _diag("INVALID_TOKEN", f"invalid tokens: {', '.join(invalid)}", field)
            )
    return diagnostics


def _normalized_list(review: dict[str, Any], name: str) -> Any:
    value = review.get(name)
    if isinstance(value, list) and all(_nonempty(item) for item in value):
        return sorted(set(value))
    return value


def review_semantic_identity(review: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic decision identity, excluding ID and optional notes."""

    selected = review.get("selected_model")
    selected_identity = (
        {key: selected.get(key) for key in sorted(SELECTED_MODEL_FIELDS)}
        if isinstance(selected, dict)
        else None
    )
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
        "selected_model": selected_identity,
        "accepted_run_ids": _normalized_list(review, "accepted_run_ids"),
        "rejected_run_ids": _normalized_list(review, "rejected_run_ids"),
        "hard_fail_categories": _normalized_list(review, "hard_fail_categories"),
        "observations": _normalized_list(review, "observations"),
    }


def expected_review_id(review: dict[str, Any]) -> str:
    suffix = hashlib.sha256(
        canonical_json(review_semantic_identity(review))
    ).hexdigest()[:16]
    return f"benchmark-review-{suffix}"


def _validate_selected_model(selected: Any) -> list[dict[str, str]]:
    diagnostics = _fields(
        selected,
        required=SELECTED_MODEL_FIELDS,
        allowed=SELECTED_MODEL_FIELDS,
        field="selected_model",
    )
    if not isinstance(selected, dict):
        return diagnostics
    diagnostics.extend(_token(selected.get("family"), "selected_model.family"))
    profile_ref = selected.get("profile_ref")
    if not isinstance(profile_ref, str) or not MODEL_REF_RE.fullmatch(profile_ref):
        diagnostics.append(
            _diag(
                "MODEL_REFERENCE",
                "profile_ref must use id@vNNN",
                "selected_model.profile_ref",
            )
        )
    diagnostics.extend(
        _checksum(selected.get("profile_sha256"), "selected_model.profile_sha256")
    )
    diagnostics.extend(
        _checksum(selected.get("workflow_sha256"), "selected_model.workflow_sha256")
    )
    return diagnostics


def validate_review_document(review: Any) -> list[dict[str, str]]:
    diagnostics = _fields(
        review,
        required=REVIEW_REQUIRED,
        allowed=REVIEW_FIELDS,
        field="review",
    )
    if not isinstance(review, dict):
        return diagnostics
    if review.get("kind") != REVIEW_KIND:
        diagnostics.append(_diag("KIND", f"kind must be {REVIEW_KIND}", "kind"))
    if review.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(
            _diag("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version")
        )
    diagnostics.extend(_token(review.get("id"), "id"))
    if isinstance(review.get("id"), str) and TOKEN_RE.fullmatch(review["id"]):
        expected = expected_review_id(review)
        if review["id"] != expected:
            diagnostics.append(
                _diag("REVIEW_ID", f"id must equal {expected}", "id")
            )
    for name in ("plan_ref", "results_ref", "package_ref"):
        diagnostics.extend(_token(review.get(name), name))
    for name in ("plan_version", "results_version"):
        diagnostics.extend(_version(review.get(name), name))
    for name in ("plan_sha256", "results_sha256", "package_sha256"):
        diagnostics.extend(_checksum(review.get(name), name))
    if not _nonempty(review.get("reviewer")):
        diagnostics.append(_diag("REVIEWER", "reviewer is required", "reviewer"))
    if not isinstance(review.get("timestamp"), str) or not UTC_RE.fullmatch(
        review.get("timestamp", "")
    ):
        diagnostics.append(
            _diag(
                "TIMESTAMP",
                "timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ",
                "timestamp",
            )
        )
    decision = review.get("decision")
    if decision not in DECISIONS:
        diagnostics.append(_diag("DECISION", "unsupported owner decision", "decision"))

    diagnostics.extend(
        _string_list(
            review.get("accepted_run_ids"),
            field="accepted_run_ids",
            token_items=True,
        )
    )
    diagnostics.extend(
        _string_list(
            review.get("rejected_run_ids"),
            field="rejected_run_ids",
            token_items=True,
        )
    )
    diagnostics.extend(
        _string_list(
            review.get("hard_fail_categories"),
            field="hard_fail_categories",
        )
    )
    diagnostics.extend(
        _string_list(
            review.get("observations"),
            field="observations",
            allow_empty=False,
        )
    )
    if "notes" in review and not _nonempty(review.get("notes")):
        diagnostics.append(
            _diag("TEXT_REQUIRED", "notes must be non-empty when present", "notes")
        )

    hard_fails = review.get("hard_fail_categories")
    if isinstance(hard_fails, list) and all(isinstance(item, str) for item in hard_fails):
        unknown = sorted(set(hard_fails) - set(HARD_FAIL_CATEGORIES))
        if unknown:
            diagnostics.append(
                _diag(
                    "HARD_FAIL_CATEGORY",
                    f"unknown hard-fail categories: {', '.join(unknown)}",
                    "hard_fail_categories",
                )
            )

    accepted = review.get("accepted_run_ids")
    rejected = review.get("rejected_run_ids")
    if isinstance(accepted, list) and isinstance(rejected, list):
        overlap = sorted(set(accepted) & set(rejected))
        if overlap:
            diagnostics.append(
                _diag(
                    "RUN_OVERLAP",
                    f"accepted and rejected runs overlap: {', '.join(overlap)}",
                    "accepted_run_ids",
                )
            )

    selected = review.get("selected_model")
    if decision == "select_model":
        diagnostics.extend(_validate_selected_model(selected))
        if not isinstance(accepted, list) or len(accepted) < MIN_ACCEPTED_RUNS:
            diagnostics.append(
                _diag(
                    "ACCEPTED_RUN_COUNT",
                    f"select_model requires at least {MIN_ACCEPTED_RUNS} accepted runs",
                    "accepted_run_ids",
                )
            )
    elif decision in {"reject_all", "needs_revision"}:
        if selected is not None:
            diagnostics.append(
                _diag(
                    "SELECTED_MODEL_FORBIDDEN",
                    f"{decision} must not contain selected_model",
                    "selected_model",
                )
            )
        if isinstance(accepted, list) and accepted:
            diagnostics.append(
                _diag(
                    "ACCEPTED_RUNS_FORBIDDEN",
                    f"{decision} must not accept runs",
                    "accepted_run_ids",
                )
            )
    return _sorted(diagnostics)


def _package_root(path: Path) -> tuple[Path | None, list[dict[str, str]]]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        return None, [
            _diag(
                "PACKAGE_ROOT_SYMLINK",
                "package root must not be a symlink",
                "package_root",
            )
        ]
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        return None, [_diag("PACKAGE_ROOT_MISSING", str(exc), "package_root")]
    if not resolved.is_dir():
        return None, [
            _diag(
                "PACKAGE_ROOT_TYPE",
                "package root must be a directory",
                "package_root",
            )
        ]
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


def validate_package(
    package_root: Path,
    expected_manifest: dict[str, Any],
    expected_files: dict[str, bytes],
) -> list[dict[str, str]]:
    root, diagnostics = _package_root(package_root)
    if root is None:
        return diagnostics
    expected_paths = set(expected_files)
    actual_paths: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or _has_symlink(path, root):
            diagnostics.append(
                _diag(
                    "PACKAGE_SYMLINK",
                    "package path contains a symlink",
                    relative,
                )
            )
            continue
        if path.is_file():
            actual_paths.add(relative)
        elif not path.is_dir():
            diagnostics.append(
                _diag(
                    "PACKAGE_TYPE",
                    "package entry must be a regular file or directory",
                    relative,
                )
            )
    missing = sorted(expected_paths - actual_paths)
    extra = sorted(actual_paths - expected_paths)
    if missing:
        diagnostics.append(
            _diag(
                "PACKAGE_MISSING",
                f"missing files: {', '.join(missing)}",
                "package_root",
            )
        )
    if extra:
        diagnostics.append(
            _diag(
                "PACKAGE_EXTRA",
                f"unexpected files: {', '.join(extra)}",
                "package_root",
            )
        )

    for relative in sorted(expected_paths & actual_paths):
        try:
            safe = safe_relative_path(relative)
            path = root.joinpath(*safe.parts)
            if _has_symlink(path, root) or not path.is_file():
                diagnostics.append(
                    _diag(
                        "PACKAGE_TYPE",
                        "expected regular non-symlink file",
                        relative,
                    )
                )
                continue
            size = path.stat().st_size
            if size <= 0 or size > MAX_PACKAGE_FILE_BYTES:
                diagnostics.append(
                    _diag(
                        "PACKAGE_SIZE",
                        f"file size must be 1..{MAX_PACKAGE_FILE_BYTES} bytes",
                        relative,
                    )
                )
                continue
            payload = path.read_bytes()
        except (OSError, ValueError) as exc:
            diagnostics.append(_diag("PACKAGE_READ", str(exc), relative))
            continue
        expected = expected_files[relative]
        if payload != expected:
            diagnostics.append(
                _diag(
                    "PACKAGE_BYTES",
                    "file bytes do not match deterministic package",
                    relative,
                )
            )
        if len(payload) != len(expected):
            diagnostics.append(
                _diag("PACKAGE_SIZE", "file size does not match manifest", relative)
            )
        if hashlib.sha256(payload).hexdigest() != hashlib.sha256(expected).hexdigest():
            diagnostics.append(
                _diag(
                    "PACKAGE_CHECKSUM",
                    "file checksum does not match manifest",
                    relative,
                )
            )

    if PACKAGE_MANIFEST in actual_paths:
        try:
            actual_manifest = json.loads(
                (root / PACKAGE_MANIFEST).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            diagnostics.append(
                _diag("PACKAGE_MANIFEST_JSON", str(exc), PACKAGE_MANIFEST)
            )
        else:
            if actual_manifest != expected_manifest:
                diagnostics.append(
                    _diag(
                        "PACKAGE_MANIFEST_BINDING",
                        "manifest object does not match expected package",
                        PACKAGE_MANIFEST,
                    )
                )
    return _sorted(diagnostics)


def _selected_lock(review: dict[str, Any]) -> dict[str, Any] | None:
    selected = review.get("selected_model")
    if review.get("decision") != "select_model" or not isinstance(selected, dict):
        return None
    return {
        "family": selected["family"],
        "profile_ref": selected["profile_ref"],
        "profile_sha256": selected["profile_sha256"],
        "workflow_sha256": selected["workflow_sha256"],
        "benchmark_review_id": review["id"],
    }


def _append_binding_diagnostics(
    diagnostics: list[dict[str, str]],
    review: dict[str, Any],
    plan: dict[str, Any],
    results: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    bindings = (
        ("plan_ref", plan.get("id"), "PLAN_BINDING"),
        ("plan_version", plan.get("version"), "PLAN_BINDING"),
        ("plan_sha256", canonical_sha256(plan), "PLAN_BINDING"),
        ("results_ref", results.get("id"), "RESULTS_BINDING"),
        ("results_version", results.get("version"), "RESULTS_BINDING"),
        ("results_sha256", result_set_sha256(results), "RESULTS_BINDING"),
        ("package_ref", manifest.get("id"), "PACKAGE_BINDING"),
        (
            "package_sha256",
            hashlib.sha256(document_bytes(manifest)).hexdigest(),
            "PACKAGE_BINDING",
        ),
    )
    for field, expected, code in bindings:
        if review.get(field) != expected:
            diagnostics.append(
                _diag(code, f"{field} does not match exact evidence", field)
            )


def selected_model_production_diagnostics(
    model: dict[str, Any],
    *,
    workspace_root: Path,
) -> list[dict[str, str]]:
    """Fail closed unless the exact selected model profile is production eligible."""

    try:
        expanded = workspace_root.expanduser()
        lexical_root = expanded if expanded.is_absolute() else Path.cwd() / expanded
        if lexical_root.is_symlink():
            raise ValueError("workspace root must not be a symlink")
        root = lexical_root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace root must be a directory")
        relative = safe_relative_path(str(model.get("profile_path", "")))
        profile_path = root.joinpath(*relative.parts)
        if _has_symlink(profile_path, root):
            raise ValueError("model profile path contains a symlink")
        resolved = profile_path.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file():
            raise ValueError("model profile must be a regular file")
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_MODEL_PROFILE_BYTES:
            raise ValueError(
                f"model profile size must be 1..{MAX_MODEL_PROFILE_BYTES} bytes"
            )
        profile = load_document(resolved)
    except (OSError, ValueError) as exc:
        return [
            _diag(
                "SELECTED_MODEL_PROFILE_READ",
                str(exc),
                "selected_model.profile_ref",
            )
        ]

    expected_sha = model.get("profile_sha256")
    actual_sha = canonical_sha256(profile)
    if expected_sha != actual_sha:
        return [
            _diag(
                "SELECTED_MODEL_PROFILE_BINDING",
                "selected model profile bytes do not match the benchmark plan",
                "selected_model.profile_sha256",
            )
        ]
    eligibility = evaluate_model_license_eligibility(profile)
    if not eligibility.production_eligible:
        return [
            _diag(
                "SELECTED_MODEL_NOT_PRODUCTION_ELIGIBLE",
                json.dumps(
                    eligibility.to_dict(),
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "selected_model",
            )
        ]
    return []


def _append_run_diagnostics(
    diagnostics: list[dict[str, str]],
    review: dict[str, Any],
    results: dict[str, Any],
    plan: dict[str, Any],
    *,
    workspace_root: Path,
) -> None:
    entries = {
        entry["run_id"]: entry
        for entry in results["results"]
        if isinstance(entry, dict) and isinstance(entry.get("run_id"), str)
    }
    for field in ("accepted_run_ids", "rejected_run_ids"):
        for run_id in review.get(field, []):
            if run_id not in entries:
                diagnostics.append(
                    _diag("UNKNOWN_RUN", f"unknown run ID: {run_id}", field)
                )

    if review.get("decision") != "select_model" or not isinstance(
        review.get("selected_model"), dict
    ):
        return
    selected = review["selected_model"]
    models = [
        model
        for model in plan["models"]
        if model["family"] == selected.get("family")
    ]
    if len(models) != 1:
        diagnostics.append(
            _diag(
                "SELECTED_MODEL",
                "selected family is not unique in the plan",
                "selected_model.family",
            )
        )
    else:
        model = models[0]
        expected_selected = {
            "family": model["family"],
            "profile_ref": f"{model['profile_id']}@{model['profile_version']}",
            "profile_sha256": model["profile_sha256"],
            "workflow_sha256": model["workflow_sha256"],
        }
        for key, expected in expected_selected.items():
            if selected.get(key) != expected:
                diagnostics.append(
                    _diag(
                        "SELECTED_MODEL_BINDING",
                        f"{key} does not match the plan",
                        f"selected_model.{key}",
                    )
                )
        diagnostics.extend(
            selected_model_production_diagnostics(
                model,
                workspace_root=workspace_root,
            )
        )

    accepted_entries: list[dict[str, Any]] = []
    for run_id in review["accepted_run_ids"]:
        entry = entries.get(run_id)
        if entry is None:
            continue
        accepted_entries.append(entry)
        if entry.get("state") != "succeeded":
            diagnostics.append(
                _diag(
                    "ACCEPTED_RUN_FAILED",
                    f"accepted run is not successful: {run_id}",
                    "accepted_run_ids",
                )
            )
        if entry.get("model_family") != selected.get("family"):
            diagnostics.append(
                _diag(
                    "ACCEPTED_RUN_FAMILY",
                    f"accepted run belongs to another family: {run_id}",
                    "accepted_run_ids",
                )
            )
    seeds = {entry.get("seed") for entry in accepted_entries}
    cases = {entry.get("prompt_case_id") for entry in accepted_entries}
    if len(seeds) < MIN_ACCEPTED_SEEDS:
        diagnostics.append(
            _diag(
                "ACCEPTED_SEED_DIVERSITY",
                f"at least {MIN_ACCEPTED_SEEDS} distinct accepted seeds are required",
                "accepted_run_ids",
            )
        )
    if len(cases) < MIN_ACCEPTED_CASES:
        diagnostics.append(
            _diag(
                "ACCEPTED_CASE_DIVERSITY",
                f"at least {MIN_ACCEPTED_CASES} distinct accepted prompt cases are required",
                "accepted_run_ids",
            )
        )
    missing_cases = sorted(REQUIRED_ACCEPTED_CASES - cases)
    if missing_cases:
        diagnostics.append(
            _diag(
                "REQUIRED_ACCEPTED_CASE",
                f"missing required accepted cases: {', '.join(missing_cases)}",
                "accepted_run_ids",
            )
        )


def validate_review(
    review: Any,
    results: Any,
    plan: Any,
    *,
    workspace_root: Path,
    reference_root: Path,
    result_root: Path,
    package_root: Path,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    diagnostics = validate_review_document(review)
    result_diagnostics, images = validate_results(
        results,
        plan,
        workspace_root=workspace_root,
        reference_root=reference_root,
        result_root=result_root,
    )
    diagnostics.extend(result_diagnostics)
    if diagnostics or not isinstance(review, dict) or not isinstance(
        results, dict
    ) or not isinstance(plan, dict):
        return _sorted(diagnostics), None

    manifest, files = build_contact_sheet_package(results, images)
    diagnostics.extend(validate_package(package_root, manifest, files))
    _append_binding_diagnostics(diagnostics, review, plan, results, manifest)
    _append_run_diagnostics(
        diagnostics,
        review,
        results,
        plan,
        workspace_root=workspace_root,
    )
    diagnostics = _sorted(diagnostics)
    return diagnostics, _selected_lock(review) if not diagnostics else None


def _result(
    diagnostics: list[dict[str, str]],
    *,
    review: dict[str, Any] | None = None,
    selected_model_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "ok": not diagnostics,
        "diagnostics": _sorted(diagnostics),
    }
    if review is not None:
        output["review_id"] = review.get("id")
        output["decision"] = review.get("decision")
    if selected_model_lock is not None:
        output["selected_model_lock"] = selected_model_lock
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate owner benchmark review without mutation"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("review-check")
    command.add_argument("review", type=Path)
    command.add_argument("results", type=Path)
    command.add_argument("plan", type=Path)
    command.add_argument("--workspace-root", type=Path, required=True)
    command.add_argument("--reference-root", type=Path, required=True)
    command.add_argument("--result-root", type=Path, required=True)
    command.add_argument("--package-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        review = load_document(args.review)
        results = load_document(args.results)
        plan = load_document(args.plan)
        diagnostics, selected = validate_review(
            review,
            results,
            plan,
            workspace_root=args.workspace_root,
            reference_root=args.reference_root,
            result_root=args.result_root,
            package_root=args.package_root,
        )
        output = _result(
            diagnostics,
            review=review,
            selected_model_lock=selected,
        )
    except (BenchmarkReviewError, ValueError, OSError) as exc:
        diagnostic = (
            exc.to_dict()
            if isinstance(exc, BenchmarkReviewError)
            else _diag("ERROR", str(exc))
        )
        output = _result([diagnostic])
    print(
        json.dumps(
            output,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
