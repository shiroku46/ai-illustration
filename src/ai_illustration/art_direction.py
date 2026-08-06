"""Read-only, checksum-bound art-direction profile and owner-review validation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, canonical_json, safe_relative_path

PROFILE_KIND = "art-direction-profile"
REVIEW_KIND = "art-direction-review"
SCHEMA_VERSION = "1.0"
ROLE_NAMES = frozenset({"boke", "tsukkomi"})
PROFILE_STATES = frozenset({"draft", "reviewing"})
REVIEW_DECISIONS = frozenset({"approve", "reject", "needs_revision"})
SUPPORTED_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
MAX_REFERENCE_BYTES = 32 * 1024 * 1024
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SNAKE_TOKEN_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")

REQUIRED_GLOBAL_ANTI_GOALS = frozenset(
    {
        "uniform_polished_linework",
        "generic_mobile_game_face",
        "over_rendered_lighting",
        "accidental_architecture_or_background_objects",
        "anatomical_collapse",
        "fused_or_missing_hands_or_limbs",
        "incoherent_clothing",
        "unintended_2_5d_rendering",
    }
)

PROFILE_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "version",
        "status",
        "roles",
        "global_anti_goals",
        "visual_references",
        "notes",
    }
)
PROFILE_REQUIRED = PROFILE_FIELDS - {"notes"}
ROLE_FIELDS = frozenset(
    {
        "role",
        "silhouette",
        "body_ratio",
        "head_exaggeration",
        "hand_exaggeration",
        "foot_exaggeration",
        "costume_construction",
        "palette",
        "line_behavior",
        "eye_design",
        "shading_ceiling",
        "front_full_body_neutral_target",
        "background_isolation_target",
        "identity_anchors",
        "prohibited_ai_traits",
    }
)
REFERENCE_FIELDS = frozenset(
    {"id", "role", "path", "media_type", "sha256", "purpose"}
)
REVIEW_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "id",
        "profile_ref",
        "profile_version",
        "profile_sha256",
        "decision",
        "reviewer",
        "timestamp",
        "observations",
        "notes",
    }
)
REVIEW_REQUIRED = REVIEW_FIELDS - {"notes"}


class ArtDirectionError(ValueError):
    """One deterministic art-direction validation failure."""

    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _diagnostic(code: str, message: str, field: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "field": field}


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def document_bytes(value: Any) -> bytes:
    """Return canonical JSON plus one LF, matching persisted manifest bytes."""

    return canonical_json(value) + b"\n"


def profile_sha256(profile: dict[str, Any]) -> str:
    return hashlib.sha256(document_bytes(profile)).hexdigest()


def review_semantic_identity(review: dict[str, Any]) -> dict[str, Any]:
    """Return review identity fields without the self-referential id or free-form notes."""

    observations = review.get("observations")
    if isinstance(observations, list) and all(_nonempty_text(item) for item in observations):
        normalized_observations: Any = sorted(set(observations))
    else:
        normalized_observations = observations
    return {
        "kind": review.get("kind"),
        "schema_version": review.get("schema_version"),
        "profile_ref": review.get("profile_ref"),
        "profile_version": review.get("profile_version"),
        "profile_sha256": review.get("profile_sha256"),
        "decision": review.get("decision"),
        "reviewer": review.get("reviewer"),
        "timestamp": review.get("timestamp"),
        "observations": normalized_observations,
    }


def expected_review_id(review: dict[str, Any]) -> str:
    suffix = hashlib.sha256(canonical_json(review_semantic_identity(review))).hexdigest()[:16]
    return f"art-review-{suffix}"


def _check_fields(
    value: Any,
    *,
    required: frozenset[str],
    allowed: frozenset[str],
    field: str,
) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return [_diagnostic("OBJECT_REQUIRED", "must be an object", field)]
    diagnostics: list[dict[str, str]] = []
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        diagnostics.append(
            _diagnostic("MISSING_FIELD", f"missing fields: {', '.join(missing)}", field)
        )
    if unknown:
        diagnostics.append(
            _diagnostic("UNKNOWN_FIELD", f"unknown fields: {', '.join(unknown)}", field)
        )
    return diagnostics


def _validate_text_list(
    value: Any,
    *,
    field: str,
    allow_empty: bool = False,
    token_values: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or (not value and not allow_empty):
        return [_diagnostic("LIST_REQUIRED", "must be a non-empty list", field)]
    diagnostics: list[dict[str, str]] = []
    if any(not _nonempty_text(item) for item in value):
        diagnostics.append(_diagnostic("TEXT_REQUIRED", "items must be non-empty strings", field))
    elif len(value) != len(set(value)):
        diagnostics.append(_diagnostic("DUPLICATE_VALUE", "items must be unique", field))
    if token_values and all(isinstance(item, str) for item in value):
        invalid = sorted(item for item in value if not SNAKE_TOKEN_RE.fullmatch(item))
        if invalid:
            diagnostics.append(
                _diagnostic("INVALID_TOKEN", f"invalid tokens: {', '.join(invalid)}", field)
            )
    return diagnostics


def _validate_role(value: Any, index: int) -> list[dict[str, str]]:
    field = f"roles[{index}]"
    diagnostics = _check_fields(
        value, required=ROLE_FIELDS, allowed=ROLE_FIELDS, field=field
    )
    if not isinstance(value, dict):
        return diagnostics
    if value.get("role") not in ROLE_NAMES:
        diagnostics.append(
            _diagnostic("ROLE", "role must be boke or tsukkomi", f"{field}.role")
        )
    for name in ROLE_FIELDS - {
        "role",
        "palette",
        "identity_anchors",
        "prohibited_ai_traits",
    }:
        if not _nonempty_text(value.get(name)):
            diagnostics.append(
                _diagnostic("TEXT_REQUIRED", "must be a non-empty string", f"{field}.{name}")
            )
    diagnostics.extend(_validate_text_list(value.get("palette"), field=f"{field}.palette"))
    diagnostics.extend(
        _validate_text_list(value.get("identity_anchors"), field=f"{field}.identity_anchors")
    )
    diagnostics.extend(
        _validate_text_list(
            value.get("prohibited_ai_traits"), field=f"{field}.prohibited_ai_traits"
        )
    )
    return diagnostics


def _validate_reference(value: Any, index: int) -> list[dict[str, str]]:
    field = f"visual_references[{index}]"
    diagnostics = _check_fields(
        value,
        required=REFERENCE_FIELDS,
        allowed=REFERENCE_FIELDS,
        field=field,
    )
    if not isinstance(value, dict):
        return diagnostics
    if not isinstance(value.get("id"), str) or not TOKEN_RE.fullmatch(value.get("id", "")):
        diagnostics.append(
            _diagnostic("INVALID_ID", "id must be a lowercase token", f"{field}.id")
        )
    if value.get("role") not in ROLE_NAMES:
        diagnostics.append(
            _diagnostic("ROLE", "role must be boke or tsukkomi", f"{field}.role")
        )
    path = value.get("path")
    if not isinstance(path, str):
        diagnostics.append(
            _diagnostic("UNSAFE_PATH", "path must be a non-empty POSIX relative path", f"{field}.path")
        )
    else:
        try:
            safe_relative_path(path)
        except ValueError as exc:
            diagnostics.append(_diagnostic("UNSAFE_PATH", str(exc), f"{field}.path"))
    if value.get("media_type") not in SUPPORTED_MEDIA_TYPES:
        diagnostics.append(
            _diagnostic(
                "MEDIA_TYPE",
                "media_type must be image/png, image/jpeg, or image/webp",
                f"{field}.media_type",
            )
        )
    if not isinstance(value.get("sha256"), str) or not SHA256_RE.fullmatch(value.get("sha256", "")):
        diagnostics.append(
            _diagnostic(
                "CHECKSUM",
                "sha256 must be 64 lowercase hexadecimal characters",
                f"{field}.sha256",
            )
        )
    if not _nonempty_text(value.get("purpose")):
        diagnostics.append(
            _diagnostic("TEXT_REQUIRED", "purpose is required", f"{field}.purpose")
        )
    return diagnostics


def validate_profile(profile: Any) -> list[dict[str, str]]:
    diagnostics = _check_fields(
        profile,
        required=PROFILE_REQUIRED,
        allowed=PROFILE_FIELDS,
        field="profile",
    )
    if not isinstance(profile, dict):
        return diagnostics
    if profile.get("kind") != PROFILE_KIND:
        diagnostics.append(_diagnostic("KIND", f"kind must be {PROFILE_KIND}", "kind"))
    if profile.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(
            _diagnostic("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version")
        )
    if not isinstance(profile.get("id"), str) or not TOKEN_RE.fullmatch(profile.get("id", "")):
        diagnostics.append(_diagnostic("INVALID_ID", "id must be a lowercase token", "id"))
    if not isinstance(profile.get("version"), str) or not VERSION_RE.fullmatch(profile.get("version", "")):
        diagnostics.append(_diagnostic("VERSION", "version must use vNNN", "version"))
    if profile.get("status") not in PROFILE_STATES:
        diagnostics.append(_diagnostic("STATUS", "status must be draft or reviewing", "status"))
    if "notes" in profile and not _nonempty_text(profile.get("notes")):
        diagnostics.append(
            _diagnostic("TEXT_REQUIRED", "notes must be non-empty when present", "notes")
        )

    roles = profile.get("roles")
    if not isinstance(roles, list):
        diagnostics.append(_diagnostic("ROLES", "roles must be a list", "roles"))
    else:
        for index, role in enumerate(roles):
            diagnostics.extend(_validate_role(role, index))
        role_names = [
            item.get("role")
            for item in roles
            if isinstance(item, dict) and isinstance(item.get("role"), str)
        ]
        if (
            len(roles) != 2
            or set(role_names) != ROLE_NAMES
            or len(role_names) != len(set(role_names))
        ):
            diagnostics.append(
                _diagnostic(
                    "ROLE_COVERAGE",
                    "roles must contain exactly one boke and one tsukkomi",
                    "roles",
                )
            )

    anti_goals = profile.get("global_anti_goals")
    diagnostics.extend(
        _validate_text_list(anti_goals, field="global_anti_goals", token_values=True)
    )
    if isinstance(anti_goals, list) and all(isinstance(item, str) for item in anti_goals):
        missing = sorted(REQUIRED_GLOBAL_ANTI_GOALS - set(anti_goals))
        if missing:
            diagnostics.append(
                _diagnostic(
                    "ANTI_GOAL_COVERAGE",
                    f"missing required anti-goals: {', '.join(missing)}",
                    "global_anti_goals",
                )
            )

    references = profile.get("visual_references")
    if not isinstance(references, list) or not references:
        diagnostics.append(
            _diagnostic(
                "REFERENCES",
                "visual_references must be a non-empty list",
                "visual_references",
            )
        )
    else:
        for index, reference in enumerate(references):
            diagnostics.extend(_validate_reference(reference, index))
        reference_roles = {
            item.get("role")
            for item in references
            if isinstance(item, dict) and isinstance(item.get("role"), str)
        }
        missing_roles = sorted(ROLE_NAMES - reference_roles)
        if missing_roles:
            diagnostics.append(
                _diagnostic(
                    "REFERENCE_COVERAGE",
                    f"missing visual reference roles: {', '.join(missing_roles)}",
                    "visual_references",
                )
            )
        ids = [
            item.get("id")
            for item in references
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        paths = [
            item.get("path")
            for item in references
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        if len(ids) != len(set(ids)):
            diagnostics.append(
                _diagnostic(
                    "DUPLICATE_REFERENCE", "reference ids must be unique", "visual_references"
                )
            )
        if len(paths) != len(set(paths)):
            diagnostics.append(
                _diagnostic(
                    "DUPLICATE_REFERENCE", "reference paths must be unique", "visual_references"
                )
            )

    return _sorted_diagnostics(diagnostics)


def validate_review(review: Any) -> list[dict[str, str]]:
    diagnostics = _check_fields(
        review,
        required=REVIEW_REQUIRED,
        allowed=REVIEW_FIELDS,
        field="review",
    )
    if not isinstance(review, dict):
        return diagnostics
    if review.get("kind") != REVIEW_KIND:
        diagnostics.append(_diagnostic("KIND", f"kind must be {REVIEW_KIND}", "kind"))
    if review.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(
            _diagnostic("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version")
        )
    if not isinstance(review.get("id"), str) or not TOKEN_RE.fullmatch(review.get("id", "")):
        diagnostics.append(_diagnostic("INVALID_ID", "id must be a lowercase token", "id"))
    elif review.get("id") != expected_review_id(review):
        diagnostics.append(
            _diagnostic("REVIEW_ID", f"id must equal {expected_review_id(review)}", "id")
        )
    if not isinstance(review.get("profile_ref"), str) or not TOKEN_RE.fullmatch(review.get("profile_ref", "")):
        diagnostics.append(
            _diagnostic(
                "INVALID_REFERENCE", "profile_ref must be a lowercase token", "profile_ref"
            )
        )
    if not isinstance(review.get("profile_version"), str) or not VERSION_RE.fullmatch(review.get("profile_version", "")):
        diagnostics.append(
            _diagnostic("VERSION", "profile_version must use vNNN", "profile_version")
        )
    if not isinstance(review.get("profile_sha256"), str) or not SHA256_RE.fullmatch(review.get("profile_sha256", "")):
        diagnostics.append(
            _diagnostic(
                "CHECKSUM",
                "profile_sha256 must be 64 lowercase hexadecimal characters",
                "profile_sha256",
            )
        )
    if review.get("decision") not in REVIEW_DECISIONS:
        diagnostics.append(_diagnostic("DECISION", "invalid review decision", "decision"))
    if not _nonempty_text(review.get("reviewer")):
        diagnostics.append(_diagnostic("REVIEWER", "reviewer is required", "reviewer"))
    if not isinstance(review.get("timestamp"), str) or not UTC_RE.fullmatch(review.get("timestamp", "")):
        diagnostics.append(
            _diagnostic(
                "TIMESTAMP",
                "timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ",
                "timestamp",
            )
        )
    diagnostics.extend(
        _validate_text_list(
            review.get("observations"), field="observations", allow_empty=True
        )
    )
    if "notes" in review and not _nonempty_text(review.get("notes")):
        diagnostics.append(
            _diagnostic("TEXT_REQUIRED", "notes must be non-empty when present", "notes")
        )
    return _sorted_diagnostics(diagnostics)


def _has_symlink_component(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop:
            return False
        if current.parent == current:
            return True
        current = current.parent


def _media_matches(payload: bytes, media_type: str) -> bool:
    if media_type == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return (
            len(payload) >= 4
            and payload.startswith(b"\xff\xd8")
            and payload.endswith(b"\xff\xd9")
        )
    if media_type == "image/webp":
        return len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    return False


def validate_live_references(
    profile: Any, reference_root: Path
) -> list[dict[str, str]]:
    diagnostics = validate_profile(profile)
    if diagnostics:
        return diagnostics
    assert isinstance(profile, dict)

    expanded = reference_root.expanduser()
    lexical_root = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical_root.is_symlink():
        return [
            _diagnostic(
                "REFERENCE_ROOT",
                "reference_root must not be a symlink",
                "reference_root",
            )
        ]
    try:
        root = lexical_root.resolve(strict=True)
    except OSError as exc:
        return [_diagnostic("REFERENCE_ROOT", str(exc), "reference_root")]
    if not root.is_dir():
        return [
            _diagnostic(
                "REFERENCE_ROOT",
                "reference_root must be a directory",
                "reference_root",
            )
        ]

    for index, reference in enumerate(profile["visual_references"]):
        field = f"visual_references[{index}].path"
        relative = safe_relative_path(reference["path"])
        lexical = root.joinpath(*relative.parts)
        if _has_symlink_component(lexical, root):
            diagnostics.append(
                _diagnostic(
                    "REFERENCE_SYMLINK",
                    "reference path contains a symlink",
                    field,
                )
            )
            continue
        try:
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            diagnostics.append(_diagnostic("REFERENCE_MISSING", str(exc), field))
            continue
        if not resolved.is_file() or resolved.is_symlink():
            diagnostics.append(
                _diagnostic("REFERENCE_TYPE", "reference must be a regular file", field)
            )
            continue
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            diagnostics.append(_diagnostic("REFERENCE_READ", str(exc), field))
            continue
        if size <= 0 or size > MAX_REFERENCE_BYTES:
            diagnostics.append(
                _diagnostic(
                    "REFERENCE_SIZE",
                    f"reference size must be 1..{MAX_REFERENCE_BYTES} bytes",
                    field,
                )
            )
            continue
        try:
            payload = resolved.read_bytes()
        except OSError as exc:
            diagnostics.append(_diagnostic("REFERENCE_READ", str(exc), field))
            continue
        if not _media_matches(payload, reference["media_type"]):
            diagnostics.append(
                _diagnostic(
                    "REFERENCE_MEDIA",
                    "reference bytes do not match media_type",
                    field,
                )
            )
        if hashlib.sha256(payload).hexdigest() != reference["sha256"]:
            diagnostics.append(
                _diagnostic(
                    "REFERENCE_CHECKSUM", "reference SHA-256 does not match", field
                )
            )
    return _sorted_diagnostics(diagnostics)


def validate_approval(
    profile: Any, review: Any, reference_root: Path
) -> list[dict[str, str]]:
    diagnostics = validate_live_references(profile, reference_root)
    diagnostics.extend(validate_review(review))
    if not isinstance(profile, dict) or not isinstance(review, dict):
        return _sorted_diagnostics(diagnostics)
    if review.get("profile_ref") != profile.get("id"):
        diagnostics.append(
            _diagnostic(
                "PROFILE_BINDING",
                "review profile_ref is stale or mismatched",
                "profile_ref",
            )
        )
    if review.get("profile_version") != profile.get("version"):
        diagnostics.append(
            _diagnostic(
                "PROFILE_BINDING",
                "review profile_version is stale or mismatched",
                "profile_version",
            )
        )
    expected_sha = profile_sha256(profile)
    if review.get("profile_sha256") != expected_sha:
        diagnostics.append(
            _diagnostic(
                "PROFILE_BINDING",
                "review profile_sha256 is stale or mismatched",
                "profile_sha256",
            )
        )
    if review.get("decision") != "approve":
        diagnostics.append(
            _diagnostic(
                "NOT_APPROVED",
                "review decision does not authorize benchmarking",
                "decision",
            )
        )
    return _sorted_diagnostics(diagnostics)


def _sorted_diagnostics(
    diagnostics: list[dict[str, str]],
) -> list[dict[str, str]]:
    unique = {
        (item.get("field", ""), item.get("code", ""), item.get("message", "")): {
            "code": item.get("code", ""),
            "message": item.get("message", ""),
            "field": item.get("field", ""),
        }
        for item in diagnostics
    }
    return [unique[key] for key in sorted(unique)]


def _safe_document_path(path: Path) -> Path:
    expanded = path.expanduser()
    if "\x00" in str(expanded) or ".." in expanded.parts:
        raise ArtDirectionError("UNSAFE_PATH", "document path is unsafe", "document")
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    for candidate in (lexical, *lexical.parents):
        if candidate.exists() and candidate.is_symlink():
            raise ArtDirectionError(
                "DOCUMENT_SYMLINK", "document path contains a symlink", "document"
            )
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise ArtDirectionError("DOCUMENT_MISSING", str(exc), "document") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise ArtDirectionError(
            "DOCUMENT_TYPE", "document must be a regular file", "document"
        )
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ArtDirectionError("DOCUMENT_READ", str(exc), "document") from exc
    if size > MAX_DOCUMENT_BYTES:
        raise ArtDirectionError("DOCUMENT_SIZE", "document is too large", "document")
    return resolved


def load_document(path: Path) -> dict[str, Any]:
    resolved = _safe_document_path(path)
    try:
        raw = resolved.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtDirectionError("DOCUMENT_JSON", str(exc), "document") from exc
    if not isinstance(value, dict):
        raise ArtDirectionError(
            "DOCUMENT_OBJECT", "document root must be an object", "document"
        )
    return value


def _result(
    *,
    diagnostics: list[dict[str, str]],
    profile: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "ok": not diagnostics,
        "diagnostics": _sorted_diagnostics(diagnostics),
    }
    if profile is not None:
        output["profile_id"] = profile.get("id")
        output["profile_version"] = profile.get("version")
        output["profile_sha256"] = profile_sha256(profile)
    if review is not None:
        output["review_id"] = review.get("id")
        output["decision"] = review.get("decision")
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate local art-direction contracts without mutation"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    profile_check = sub.add_parser("profile-check")
    profile_check.add_argument("profile", type=Path)
    profile_check.add_argument("--reference-root", type=Path, required=True)
    approval_check = sub.add_parser("approval-check")
    approval_check.add_argument("profile", type=Path)
    approval_check.add_argument("review", type=Path)
    approval_check.add_argument("--reference-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        profile = load_document(args.profile)
        review = (
            load_document(args.review)
            if args.command == "approval-check"
            else None
        )
        diagnostics = (
            validate_approval(profile, review, args.reference_root)
            if review is not None
            else validate_live_references(profile, args.reference_root)
        )
        output = _result(
            diagnostics=diagnostics,
            profile=profile,
            review=review,
        )
    except ArtDirectionError as exc:
        output = _result(diagnostics=[exc.to_dict()])
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
