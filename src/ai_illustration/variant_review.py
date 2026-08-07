"""Read-only owner review validation for exact generated variant PNGs."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

from .naming import SHA256_RE, TOKEN_RE, canonical_json, safe_relative_path
from .quality import HARD_FAIL_CATEGORIES
from .validation import _parse_png
from .variants import IdentityEvidence, VariantError, check_variant_set

KIND = "variant-review-decision"
SCHEMA_VERSION = "1.0"
DECISIONS = frozenset({"accept", "reject", "needs_revision"})
RESULT_STATES = frozenset(
    {"evaluation-accepted", "production-variant-approved", "rejected", "needs-revision"}
)
MAX_PNG_BYTES = 128 * 1024 * 1024
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
VARIANT_SET_RE = re.compile(r"^variant-set-[0-9a-f]{20}$")
VARIANT_RE = re.compile(r"^variant-[0-9a-f]{20}$")
REVIEW_RE = re.compile(r"^variant-review-[0-9a-f]{20}$")
IDENTITY_REVIEW_RE = re.compile(r"^identity-review-[0-9a-f]{16}$")
PROFILE_REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")

REVIEW_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "variant_set_ref",
        "variant_set_sha256",
        "variant_id",
        "png_sha256",
        "source_candidate_ref",
        "source_request_ref",
        "source_candidate_sha256",
        "identity_gate",
        "identity_review_ref",
        "identity_review_sha256",
        "identity_strategy_id",
        "identity_evidence_run_ids",
        "identity_model",
        "decision",
        "result_state",
        "reviewer",
        "timestamp",
        "hard_fail_categories",
        "observations",
        "notes",
    }
)
REVIEW_REQUIRED = REVIEW_FIELDS - {"notes"}
IDENTITY_MODEL_FIELDS = frozenset(
    {"family", "profile_ref", "profile_sha256", "workflow_sha256"}
)
FORBIDDEN_KEYS = frozenset(
    {
        "score",
        "scores",
        "rank",
        "ranking",
        "winner",
        "recommendation",
        "recommended",
        "confidence",
        "similarity",
        "similarity_score",
        "similarity_threshold",
        "automatic_approval",
        "automatic_promotion",
        "variant_promotion",
        "selected",
        "selection",
    }
)


class VariantReviewError(ValueError):
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


def _checksum(value: Any, field: str, *, nullable: bool = False) -> list[dict[str, str]]:
    if nullable and value is None:
        return []
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        return [_diag("CHECKSUM", "must be 64 lowercase hexadecimal characters", field)]
    return []


def _scan_forbidden(value: Any, field: str = "review") -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{field}.{key}"
            if key in FORBIDDEN_KEYS:
                diagnostics.append(_diag("AUTOMATIC_DECISION_FORBIDDEN", f"field is forbidden: {key}", current))
            diagnostics.extend(_scan_forbidden(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            diagnostics.extend(_scan_forbidden(item, f"{field}[{index}]"))
    return diagnostics


def _nonempty_list(value: Any, field: str, *, tokens: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        return [_diag("LIST_REQUIRED", "must be a non-empty list", field)]
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


def _expected_result_state(intent: str, decision: str) -> str:
    if decision == "accept":
        return "production-variant-approved" if intent == "production" else "evaluation-accepted"
    return "rejected" if decision == "reject" else "needs-revision"


def variant_set_sha256(variant_set: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(variant_set)).hexdigest()


def review_semantic_identity(review: dict[str, Any]) -> dict[str, Any]:
    runs = review.get("identity_evidence_run_ids")
    normalized_runs = sorted(set(runs)) if isinstance(runs, list) and all(isinstance(item, str) for item in runs) else runs
    hard_fails = review.get("hard_fail_categories")
    normalized_hard_fails = sorted(set(hard_fails)) if isinstance(hard_fails, list) and all(isinstance(item, str) for item in hard_fails) else hard_fails
    observations = review.get("observations")
    normalized_observations = sorted(set(observations)) if isinstance(observations, list) and all(isinstance(item, str) for item in observations) else observations
    model = review.get("identity_model")
    normalized_model = (
        {key: model.get(key) for key in sorted(IDENTITY_MODEL_FIELDS)}
        if isinstance(model, dict)
        else model
    )
    return {
        "kind": review.get("kind"),
        "schema_version": review.get("schema_version"),
        "variant_set_ref": review.get("variant_set_ref"),
        "variant_set_sha256": review.get("variant_set_sha256"),
        "variant_id": review.get("variant_id"),
        "png_sha256": review.get("png_sha256"),
        "source_candidate_ref": review.get("source_candidate_ref"),
        "source_request_ref": review.get("source_request_ref"),
        "source_candidate_sha256": review.get("source_candidate_sha256"),
        "identity_gate": review.get("identity_gate"),
        "identity_review_ref": review.get("identity_review_ref"),
        "identity_review_sha256": review.get("identity_review_sha256"),
        "identity_strategy_id": review.get("identity_strategy_id"),
        "identity_evidence_run_ids": normalized_runs,
        "identity_model": normalized_model,
        "decision": review.get("decision"),
        "result_state": review.get("result_state"),
        "reviewer": review.get("reviewer"),
        "timestamp": review.get("timestamp"),
        "hard_fail_categories": normalized_hard_fails,
        "observations": normalized_observations,
    }


def expected_review_id(review: dict[str, Any]) -> str:
    suffix = hashlib.sha256(canonical_json(review_semantic_identity(review))).hexdigest()[:20]
    return f"variant-review-{suffix}"


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
    if not isinstance(review_id, str) or not REVIEW_RE.fullmatch(review_id):
        diagnostics.append(_diag("REVIEW_ID", "id must use variant-review-<20hex>", "id"))
    elif review_id != expected_review_id(review):
        diagnostics.append(_diag("REVIEW_ID", f"id must equal {expected_review_id(review)}", "id"))
    if not isinstance(review.get("variant_set_ref"), str) or not VARIANT_SET_RE.fullmatch(review.get("variant_set_ref", "")):
        diagnostics.append(_diag("VARIANT_SET_REF", "variant_set_ref has invalid format", "variant_set_ref"))
    diagnostics.extend(_checksum(review.get("variant_set_sha256"), "variant_set_sha256"))
    if not isinstance(review.get("variant_id"), str) or not VARIANT_RE.fullmatch(review.get("variant_id", "")):
        diagnostics.append(_diag("VARIANT_ID", "variant_id has invalid format", "variant_id"))
    diagnostics.extend(_checksum(review.get("png_sha256"), "png_sha256"))
    for name in ("source_candidate_ref", "source_request_ref"):
        diagnostics.extend(_token(review.get(name), name))
    diagnostics.extend(_checksum(review.get("source_candidate_sha256"), "source_candidate_sha256"))
    if review.get("identity_gate") not in {"evaluation-unlocked", "owner-approved"}:
        diagnostics.append(_diag("IDENTITY_GATE", "invalid identity_gate", "identity_gate"))
    identity_review_ref = review.get("identity_review_ref")
    if identity_review_ref is not None and (not isinstance(identity_review_ref, str) or not IDENTITY_REVIEW_RE.fullmatch(identity_review_ref)):
        diagnostics.append(_diag("IDENTITY_REVIEW_REF", "identity_review_ref has invalid format", "identity_review_ref"))
    diagnostics.extend(_checksum(review.get("identity_review_sha256"), "identity_review_sha256", nullable=True))
    identity_strategy = review.get("identity_strategy_id")
    if identity_strategy is not None:
        diagnostics.extend(_token(identity_strategy, "identity_strategy_id"))
    runs = review.get("identity_evidence_run_ids")
    if not isinstance(runs, list):
        diagnostics.append(_diag("IDENTITY_RUNS", "identity_evidence_run_ids must be a list", "identity_evidence_run_ids"))
    else:
        if len(runs) != len(set(item for item in runs if isinstance(item, str))):
            diagnostics.append(_diag("DUPLICATE_VALUE", "identity evidence run IDs must be unique", "identity_evidence_run_ids"))
        for index, item in enumerate(runs):
            diagnostics.extend(_token(item, f"identity_evidence_run_ids[{index}]"))
    model = review.get("identity_model")
    if model is not None:
        diagnostics.extend(_check_fields(model, IDENTITY_MODEL_FIELDS, IDENTITY_MODEL_FIELDS, "identity_model"))
        if isinstance(model, dict):
            diagnostics.extend(_token(model.get("family"), "identity_model.family"))
            ref = model.get("profile_ref")
            if not isinstance(ref, str) or not PROFILE_REF_RE.fullmatch(ref):
                diagnostics.append(_diag("MODEL_REFERENCE", "profile_ref must use id@vNNN", "identity_model.profile_ref"))
            diagnostics.extend(_checksum(model.get("profile_sha256"), "identity_model.profile_sha256"))
            diagnostics.extend(_checksum(model.get("workflow_sha256"), "identity_model.workflow_sha256"))
    decision = review.get("decision")
    if decision not in DECISIONS:
        diagnostics.append(_diag("DECISION", "decision must be accept, reject, or needs_revision", "decision"))
    if review.get("result_state") not in RESULT_STATES:
        diagnostics.append(_diag("RESULT_STATE", "invalid result_state", "result_state"))
    elif decision in DECISIONS:
        intent = "production" if review.get("identity_gate") == "owner-approved" else "evaluation"
        expected = _expected_result_state(intent, decision)
        if review.get("result_state") != expected:
            diagnostics.append(_diag("RESULT_STATE", f"result_state must equal {expected}", "result_state"))
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200:
        diagnostics.append(_diag("REVIEWER", "reviewer must be a non-empty bounded string", "reviewer"))
    if not isinstance(review.get("timestamp"), str) or not UTC_RE.fullmatch(review.get("timestamp", "")):
        diagnostics.append(_diag("TIMESTAMP", "timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ", "timestamp"))
    hard_fails = review.get("hard_fail_categories")
    if not isinstance(hard_fails, list):
        diagnostics.append(_diag("HARD_FAILS", "hard_fail_categories must be a list", "hard_fail_categories"))
    else:
        if len(hard_fails) != len(set(item for item in hard_fails if isinstance(item, str))):
            diagnostics.append(_diag("DUPLICATE_VALUE", "hard-fail categories must be unique", "hard_fail_categories"))
        unknown = sorted(item for item in hard_fails if not isinstance(item, str) or item not in HARD_FAIL_CATEGORIES)
        if unknown:
            diagnostics.append(_diag("HARD_FAIL_CATEGORY", "unknown hard-fail category", "hard_fail_categories"))
    diagnostics.extend(_nonempty_list(review.get("observations"), "observations"))
    if "notes" in review and (not isinstance(review.get("notes"), str) or not review["notes"].strip()):
        diagnostics.append(_diag("TEXT_REQUIRED", "notes must be non-empty when present", "notes"))
    return _sorted(diagnostics)


def _root(path: Path) -> tuple[Path | None, list[dict[str, str]]]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        return None, [_diag("RESULT_ROOT_SYMLINK", "result root must not be a symlink", "result_root")]
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        return None, [_diag("RESULT_ROOT_MISSING", str(exc), "result_root")]
    if not resolved.is_dir():
        return None, [_diag("RESULT_ROOT_TYPE", "result root must be a directory", "result_root")]
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


def _read_variant_png(root: Path, relative: str, field: str) -> tuple[bytes | None, list[dict[str, str]]]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        return None, [_diag("UNSAFE_PATH", str(exc), field)]
    lexical = root.joinpath(*safe.parts)
    if _has_symlink(lexical, root):
        return None, [_diag("PNG_SYMLINK", "variant PNG path contains a symlink", field)]
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        return None, [_diag("PNG_MISSING", str(exc), field)]
    if not resolved.is_file() or resolved.is_symlink():
        return None, [_diag("PNG_TYPE", "variant PNG must be a regular file", field)]
    try:
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_PNG_BYTES:
            return None, [_diag("PNG_SIZE", f"PNG size must be 1..{MAX_PNG_BYTES} bytes", field)]
        payload = resolved.read_bytes()
    except OSError as exc:
        return None, [_diag("PNG_READ", str(exc), field)]
    if len(payload) != size:
        return None, [_diag("PNG_CHANGED", "PNG changed while being read", field)]
    return payload, []


def _identity_projection(variant_set: dict[str, Any]) -> dict[str, Any]:
    return {
        "identity_gate": variant_set.get("identity_gate"),
        "identity_review_ref": variant_set.get("identity_review_ref"),
        "identity_review_sha256": variant_set.get("identity_review_sha256"),
        "identity_strategy_id": variant_set.get("identity_strategy_id"),
        "identity_evidence_run_ids": variant_set.get("identity_evidence_run_ids"),
        "identity_model": variant_set.get("identity_model"),
    }


def validate_review(
    review: Any,
    variant_set_path: Path,
    manifest_root: Path,
    result_root: Path,
    *,
    identity_evidence: IdentityEvidence | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    diagnostics = validate_review_document(review)
    try:
        variant_set = check_variant_set(
            variant_set_path,
            manifest_root,
            identity_evidence=identity_evidence,
        )
    except VariantError as exc:
        diagnostics.append(_diag(f"VARIANT_SET_{exc.code}", exc.message, exc.field))
        return _sorted(diagnostics), None
    if not isinstance(review, dict):
        return _sorted(diagnostics), None

    bindings = {
        "variant_set_ref": variant_set["id"],
        "variant_set_sha256": variant_set_sha256(variant_set),
        "source_candidate_ref": variant_set["source_candidate_ref"],
        "source_request_ref": variant_set["source_request_ref"],
        "source_candidate_sha256": variant_set["source_candidate_sha256"],
        **_identity_projection(variant_set),
    }
    for field, expected in bindings.items():
        if review.get(field) != expected:
            diagnostics.append(_diag("REVIEW_BINDING", f"{field} does not match exact variant-set evidence", field))

    variants = [item for item in variant_set["variants"] if item.get("id") == review.get("variant_id")]
    if len(variants) != 1:
        diagnostics.append(_diag("VARIANT_LOOKUP", "review variant_id must identify exactly one planned variant", "variant_id"))
        return _sorted(diagnostics), None
    variant = variants[0]
    root, root_diagnostics = _root(result_root)
    diagnostics.extend(root_diagnostics)
    payload: bytes | None = None
    if root is not None:
        payload, read_diagnostics = _read_variant_png(root, variant["path"], "variant_png")
        diagnostics.extend(read_diagnostics)
    if payload is not None:
        actual_sha = hashlib.sha256(payload).hexdigest()
        if review.get("png_sha256") != actual_sha:
            diagnostics.append(_diag("PNG_CHECKSUM", "review png_sha256 does not match live PNG bytes", "png_sha256"))
        try:
            info = _parse_png(payload)
        except ValueError as exc:
            diagnostics.append(_diag("PNG_STRUCTURE", str(exc), "variant_png"))
        else:
            if info.width != variant.get("width") or info.height != variant.get("height"):
                diagnostics.append(_diag("PNG_DIMENSIONS", f"PNG dimensions are {info.width}x{info.height}", "variant_png"))
            if not info.has_alpha or variant.get("has_alpha") is not True:
                diagnostics.append(_diag("PNG_ALPHA", "variant PNG and plan must preserve alpha", "variant_png"))
            if not info.has_srgb or variant.get("color_space") != "sRGB":
                diagnostics.append(_diag("PNG_SRGB", "variant PNG and plan must declare sRGB", "variant_png"))

    intent = variant_set["intent"]
    decision = review.get("decision")
    hard_fails = review.get("hard_fail_categories") if isinstance(review.get("hard_fail_categories"), list) else []
    expected_state = _expected_result_state(intent, decision) if decision in DECISIONS else None
    if expected_state is not None and review.get("result_state") != expected_state:
        diagnostics.append(_diag("RESULT_STATE", f"result_state must equal {expected_state}", "result_state"))
    if intent == "production":
        if variant_set.get("identity_gate") != "owner-approved":
            diagnostics.append(_diag("PRODUCTION_IDENTITY_GATE", "production variant set must be owner-approved", "identity_gate"))
        if decision == "accept" and hard_fails:
            diagnostics.append(_diag("PRODUCTION_HARD_FAIL", "production accept cannot contain hard-fail categories", "hard_fail_categories"))
    elif review.get("result_state") == "production-variant-approved":
        diagnostics.append(_diag("EVALUATION_NOT_PRODUCTION", "evaluation review cannot produce production approval", "result_state"))
    return _sorted(diagnostics), variant


def _load_object(path: Path, field: str) -> dict[str, Any]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if "\x00" in str(expanded) or ".." in expanded.parts or lexical.is_symlink():
        raise VariantReviewError("UNSAFE_PATH", f"{field} path is unsafe or symlinked", field)
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise VariantReviewError("FILE_MISSING", str(exc), field) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise VariantReviewError("FILE_TYPE", f"{field} must be a regular file", field)
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VariantReviewError("JSON", str(exc), field) from exc
    if not isinstance(value, dict):
        raise VariantReviewError("OBJECT_REQUIRED", f"{field} root must be an object", field)
    return value


def _identity_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--identity-review", type=Path)
    parser.add_argument("--identity-plan", type=Path)
    parser.add_argument("--identity-results", type=Path)
    parser.add_argument("--identity-result-root", type=Path)
    parser.add_argument("--identity-package-root", type=Path)


def _identity_evidence(args: argparse.Namespace) -> IdentityEvidence | None:
    values = [
        args.identity_review,
        args.identity_plan,
        args.identity_results,
        args.identity_result_root,
        args.identity_package_root,
    ]
    if not any(value is not None for value in values):
        return None
    if not all(value is not None for value in values):
        raise VariantReviewError("IDENTITY_EVIDENCE_ARGS", "all five identity evidence options must be supplied together", "identity_evidence")
    return IdentityEvidence(
        review=args.identity_review,
        plan=args.identity_plan,
        results=args.identity_results,
        result_root=args.identity_result_root,
        package_root=args.identity_package_root,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate exact owner review of one generated variant PNG")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("review-check")
    check.add_argument("review", type=Path)
    check.add_argument("variant_set", type=Path)
    check.add_argument("--manifest-root", type=Path, required=True)
    check.add_argument("--result-root", type=Path, required=True)
    _identity_args(check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        review = _load_object(args.review, "review")
        identity = _identity_evidence(args)
        diagnostics, variant = validate_review(
            review,
            args.variant_set,
            args.manifest_root,
            args.result_root,
            identity_evidence=identity,
        )
        output: dict[str, Any] = {
            "ok": not diagnostics,
            "diagnostics": diagnostics,
            "review_id": review.get("id"),
            "decision": review.get("decision"),
            "result_state": review.get("result_state"),
        }
        if not diagnostics and variant is not None:
            output["variant_id"] = variant["id"]
            output["variant_path"] = variant["path"]
    except (VariantReviewError, OSError, ValueError) as exc:
        diagnostic = exc.to_dict() if isinstance(exc, VariantReviewError) else _diag("ERROR", str(exc))
        output = {"ok": False, "diagnostics": [diagnostic]}
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
