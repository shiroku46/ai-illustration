"""Fail-closed validation for the six MVP manifest types."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
import json
import re

from .models import Diagnostic, Manifest, load_manifest
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, export_paths, safe_relative_path

KINDS = {
    "character-spec",
    "style-profile",
    "generation-request",
    "candidate-asset",
    "review-decision",
    "export-manifest",
}
LICENSE_STATES = {"unreviewed", "reviewing", "approved", "rejected"}
REVIEW_STATES = {"shortlist", "accept", "reject", "needs_revision"}
ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")

REQUIRED: dict[str, tuple[str, ...]] = {
    "character-spec": (
        "kind", "schema_version", "id", "version", "role", "review_status",
        "identity_anchors", "license_status",
    ),
    "style-profile": (
        "kind", "schema_version", "id", "version", "line", "palette",
        "anti_ai_checks", "license_status",
    ),
    "generation-request": (
        "kind", "schema_version", "id", "character_ref", "style_ref", "pose",
        "expression", "crop", "facing", "tool_id", "model_id",
        "license_status", "config", "output_intent", "provenance",
    ),
    "candidate-asset": (
        "kind", "schema_version", "id", "request_ref", "path", "sha256",
        "width", "height", "color_space", "has_alpha", "media_type",
        "status", "provenance",
    ),
    "review-decision": (
        "kind", "schema_version", "id", "candidate_ref", "candidate_sha256",
        "decision", "reviewer", "timestamp", "categories",
    ),
    "export-manifest": (
        "kind", "schema_version", "id", "character_ref", "candidate_ref",
        "review_ref", "path", "sidecar_path", "sha256", "width", "height",
        "color_space", "has_alpha", "format", "license_status", "status",
        "crop", "facing", "pose", "expression", "version",
    ),
}
OPTIONAL = {"seed", "notes", "supersedes", "observed_sha256", "tool_version"}


def _diag(manifest: Manifest, code: str, message: str, field: str = "") -> Diagnostic:
    return Diagnostic(code, message, str(manifest.source), field)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _reference_id(value: str) -> tuple[str, str]:
    return tuple(value.rsplit("@", 1))  # type: ignore[return-value]


def validate_document(manifest: Manifest) -> list[Diagnostic]:
    data = manifest.data
    out: list[Diagnostic] = []
    kind = manifest.kind
    if kind not in KINDS:
        return [_diag(manifest, "UNKNOWN_KIND", f"unsupported kind: {kind!r}", "kind")]

    unknown = sorted(set(data) - set(REQUIRED[kind]) - OPTIONAL)
    if unknown:
        out.append(_diag(manifest, "UNKNOWN_FIELD", f"unknown fields are not accepted: {', '.join(unknown)}"))
    for field in REQUIRED[kind]:
        if field not in data:
            out.append(_diag(manifest, "MISSING_FIELD", "required field is missing", field))
    if data.get("schema_version") != "1.0":
        out.append(_diag(manifest, "SCHEMA_VERSION", "schema_version must be '1.0'", "schema_version"))
    if "id" in data and (not isinstance(data["id"], str) or not ID_RE.fullmatch(data["id"])):
        out.append(_diag(manifest, "INVALID_ID", "id must be lowercase ASCII with hyphens", "id"))
    for field in ("character_ref", "style_ref"):
        if field in data and (not isinstance(data[field], str) or not REF_RE.fullmatch(data[field])):
            out.append(_diag(manifest, "INVALID_REFERENCE", "reference must use id@vNNN", field))
    for field in ("request_ref", "candidate_ref", "review_ref"):
        if field in data and (not isinstance(data[field], str) or not ID_RE.fullmatch(data[field])):
            out.append(_diag(manifest, "INVALID_REFERENCE", "reference must be a manifest id", field))
    if "version" in data and (not isinstance(data["version"], str) or not VERSION_RE.fullmatch(data["version"])):
        out.append(_diag(manifest, "INVALID_VERSION", "version must use vNNN", "version"))
    if "license_status" in data and data["license_status"] not in LICENSE_STATES:
        out.append(_diag(manifest, "LICENSE_STATUS", "invalid license_status", "license_status"))

    if kind == "character-spec":
        if data.get("role") not in {"boke", "tsukkomi"}:
            out.append(_diag(manifest, "ROLE", "role must be boke or tsukkomi", "role"))
        if data.get("review_status") not in {"draft", "approved", "rejected"}:
            out.append(_diag(manifest, "REVIEW_STATUS", "invalid review_status", "review_status"))
        if not isinstance(data.get("identity_anchors"), list) or not data.get("identity_anchors"):
            out.append(_diag(manifest, "IDENTITY_ANCHORS", "at least one identity anchor is required", "identity_anchors"))

    if kind == "style-profile" and (not isinstance(data.get("anti_ai_checks"), list) or not data.get("anti_ai_checks")):
        out.append(_diag(manifest, "ANTI_AI_CHECKS", "anti_ai_checks must be a non-empty list", "anti_ai_checks"))

    if kind == "generation-request":
        for field in ("pose", "expression", "crop", "facing", "tool_id", "model_id"):
            value = data.get(field)
            if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
                out.append(_diag(manifest, "INVALID_TOKEN", "must be a lowercase ASCII token", field))
        if not isinstance(data.get("config"), dict):
            out.append(_diag(manifest, "CONFIG", "config must be an object", "config"))
        provenance = data.get("provenance")
        if not isinstance(provenance, dict) or not _nonempty_string(provenance.get("source")):
            out.append(_diag(manifest, "UNKNOWN_PROVENANCE", "provenance.source is required", "provenance"))
        seed = data.get("seed")
        if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or seed < 0):
            out.append(_diag(manifest, "SEED", "seed must be a non-negative integer", "seed"))

    if kind in {"candidate-asset", "export-manifest"}:
        for field in ("path", "sidecar_path"):
            if field in data:
                try:
                    safe_relative_path(data[field])
                except (TypeError, ValueError) as exc:
                    out.append(_diag(manifest, "UNSAFE_PATH", str(exc), field))
        if not isinstance(data.get("sha256"), str) or not SHA256_RE.fullmatch(data.get("sha256", "")):
            out.append(_diag(manifest, "CHECKSUM", "sha256 must be 64 lowercase hexadecimal characters", "sha256"))
        for field in ("width", "height"):
            value = data.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                out.append(_diag(manifest, "DIMENSION", "dimension must be a positive integer", field))
        if data.get("color_space") != "sRGB":
            out.append(_diag(manifest, "COLOR_SPACE", "color_space must be sRGB", "color_space"))
        if data.get("has_alpha") is not True:
            out.append(_diag(manifest, "ALPHA_REQUIRED", "transparent alpha is required", "has_alpha"))

    if kind == "candidate-asset":
        if data.get("media_type") != "image/png":
            out.append(_diag(manifest, "MEDIA_TYPE", "media_type must be image/png", "media_type"))
        if data.get("status") not in {"received", "technically_valid", "invalid"}:
            out.append(_diag(manifest, "CANDIDATE_STATUS", "invalid candidate status", "status"))
        provenance = data.get("provenance")
        if not isinstance(provenance, dict) or not _nonempty_string(provenance.get("source")):
            out.append(_diag(manifest, "UNKNOWN_PROVENANCE", "provenance.source is required", "provenance"))

    if kind == "review-decision":
        if data.get("decision") not in REVIEW_STATES:
            out.append(_diag(manifest, "DECISION", "invalid review decision", "decision"))
        if not _nonempty_string(data.get("reviewer")):
            out.append(_diag(manifest, "REVIEWER", "reviewer is required", "reviewer"))
        if not isinstance(data.get("timestamp"), str) or not ISO_UTC_RE.fullmatch(data.get("timestamp", "")):
            out.append(_diag(manifest, "TIMESTAMP", "timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ", "timestamp"))
        if not isinstance(data.get("categories"), list):
            out.append(_diag(manifest, "CATEGORIES", "categories must be a list", "categories"))
        if not isinstance(data.get("candidate_sha256"), str) or not SHA256_RE.fullmatch(data.get("candidate_sha256", "")):
            out.append(_diag(manifest, "CHECKSUM", "candidate_sha256 must be 64 lowercase hexadecimal characters", "candidate_sha256"))

    if kind == "export-manifest":
        if data.get("format") != "png":
            out.append(_diag(manifest, "FORMAT", "format must be png", "format"))
        if data.get("status") not in {"planned", "validated", "packaged", "verified"}:
            out.append(_diag(manifest, "EXPORT_STATUS", "invalid export status", "status"))
        if data.get("license_status") != "approved":
            out.append(_diag(manifest, "LICENSE_NOT_APPROVED", "exports require approved licensing", "license_status"))
        for field in ("crop", "facing", "pose", "expression"):
            value = data.get(field)
            if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
                out.append(_diag(manifest, "INVALID_TOKEN", "must be a lowercase ASCII token", field))
        needed = ("character_ref", "crop", "facing", "pose", "expression", "version", "sha256", "path", "sidecar_path")
        if all(field in data for field in needed):
            try:
                character_id, _ = _reference_id(data["character_ref"])
                expected_path, expected_sidecar = export_paths(
                    character_id=character_id,
                    crop=data["crop"],
                    facing=data["facing"],
                    pose=data["pose"],
                    expression=data["expression"],
                    version=data["version"],
                    sha256=data["sha256"],
                )
                if data["path"] != expected_path:
                    out.append(_diag(manifest, "NONDETERMINISTIC_PATH", f"expected {expected_path}", "path"))
                if data["sidecar_path"] != expected_sidecar:
                    out.append(_diag(manifest, "NONDETERMINISTIC_PATH", f"expected {expected_sidecar}", "sidecar_path"))
            except (ValueError, TypeError):
                pass
    return out


def load_path(path: Path) -> tuple[list[Manifest], list[Diagnostic]]:
    manifests: list[Manifest] = []
    diagnostics: list[Diagnostic] = []
    paths = [path] if path.is_file() else sorted(path.rglob("*.json"))
    if not paths:
        return manifests, [Diagnostic("NO_DOCUMENTS", "no JSON documents found", str(path))]
    for item in paths:
        try:
            manifests.append(load_manifest(item))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            diagnostics.append(Diagnostic("LOAD_ERROR", str(exc), str(item)))
    return manifests, diagnostics


def validate_set(manifests: Iterable[Manifest]) -> list[Diagnostic]:
    docs = list(manifests)
    diagnostics: list[Diagnostic] = []
    by_kind_id: dict[tuple[str, str], Manifest] = {}
    duplicate_ids: dict[str, list[Manifest]] = defaultdict(list)

    for manifest in docs:
        diagnostics.extend(validate_document(manifest))
        duplicate_ids[manifest.manifest_id].append(manifest)
        if manifest.kind and manifest.manifest_id:
            by_kind_id[(manifest.kind, manifest.manifest_id)] = manifest
    for manifest_id, matches in duplicate_ids.items():
        if manifest_id and len(matches) > 1:
            for manifest in matches:
                diagnostics.append(_diag(manifest, "DUPLICATE_ID", f"duplicate manifest id: {manifest_id}", "id"))

    def require(kind: str, identifier: str, source: Manifest, field: str) -> Manifest | None:
        target = by_kind_id.get((kind, identifier))
        if target is None:
            diagnostics.append(_diag(source, "UNRESOLVED_REFERENCE", f"{kind} {identifier!r} was not found", field))
        return target

    for manifest in docs:
        data = manifest.data
        if manifest.kind == "generation-request":
            for field, target_kind in (("character_ref", "character-spec"), ("style_ref", "style-profile")):
                value = data.get(field)
                if isinstance(value, str) and REF_RE.fullmatch(value):
                    identifier, version = _reference_id(value)
                    target = require(target_kind, identifier, manifest, field)
                    if target and target.data.get("version") != version:
                        diagnostics.append(_diag(manifest, "VERSION_MISMATCH", f"{field} version does not match target", field))
        elif manifest.kind == "candidate-asset":
            if isinstance(data.get("request_ref"), str):
                require("generation-request", data["request_ref"], manifest, "request_ref")
        elif manifest.kind == "review-decision":
            candidate = require("candidate-asset", data.get("candidate_ref", ""), manifest, "candidate_ref")
            if data.get("decision") in {"accept", "shortlist"} and candidate and candidate.data.get("status") != "technically_valid":
                diagnostics.append(_diag(manifest, "NOT_REVIEW_READY", "candidate must be technically_valid", "candidate_ref"))
            if candidate and data.get("candidate_sha256") != candidate.data.get("sha256"):
                diagnostics.append(_diag(manifest, "REVIEW_CHECKSUM_MISMATCH", "review checksum must match candidate", "candidate_sha256"))
        elif manifest.kind == "export-manifest":
            character_value = data.get("character_ref")
            if isinstance(character_value, str) and REF_RE.fullmatch(character_value):
                identifier, version = _reference_id(character_value)
                target = require("character-spec", identifier, manifest, "character_ref")
                if target and target.data.get("version") != version:
                    diagnostics.append(_diag(manifest, "VERSION_MISMATCH", "character version mismatch", "character_ref"))

            candidate = require("candidate-asset", data.get("candidate_ref", ""), manifest, "candidate_ref")
            review = require("review-decision", data.get("review_ref", ""), manifest, "review_ref")
            request = None
            if candidate and isinstance(candidate.data.get("request_ref"), str):
                request = require("generation-request", candidate.data["request_ref"], manifest, "candidate_ref")

            if review and review.data.get("decision") != "accept":
                diagnostics.append(_diag(manifest, "NOT_APPROVED", "export review must be accept", "review_ref"))
            if review and review.data.get("candidate_ref") != data.get("candidate_ref"):
                diagnostics.append(_diag(manifest, "REFERENCE_MISMATCH", "review does not approve this candidate", "review_ref"))
            if candidate:
                for field in ("sha256", "width", "height", "color_space", "has_alpha"):
                    if candidate.data.get(field) != data.get(field):
                        diagnostics.append(_diag(manifest, "EXPORT_MISMATCH", f"{field} differs from candidate", field))
                if candidate.data.get("status") != "technically_valid":
                    diagnostics.append(_diag(manifest, "NOT_REVIEW_READY", "candidate is not technically_valid", "candidate_ref"))
            if review and candidate:
                reviewed_sha = review.data.get("candidate_sha256")
                if reviewed_sha != candidate.data.get("sha256") or reviewed_sha != data.get("sha256"):
                    diagnostics.append(_diag(manifest, "REVIEW_CHECKSUM_MISMATCH", "review checksum must match candidate and export", "review_ref"))

            if request:
                for field in ("character_ref", "pose", "expression", "crop", "facing"):
                    if request.data.get(field) != data.get(field):
                        diagnostics.append(_diag(manifest, "SOURCE_METADATA_MISMATCH", f"{field} differs from source request", field))
                if request.data.get("license_status") != "approved":
                    diagnostics.append(_diag(manifest, "SOURCE_NOT_APPROVED", "source request licensing is not approved", "candidate_ref"))
                source_character = request.data.get("character_ref")
                if isinstance(source_character, str) and REF_RE.fullmatch(source_character):
                    character_id, character_version = _reference_id(source_character)
                    character = require("character-spec", character_id, manifest, "character_ref")
                    if character and (
                        character.data.get("version") != character_version
                        or character.data.get("review_status") != "approved"
                        or character.data.get("license_status") != "approved"
                    ):
                        diagnostics.append(_diag(manifest, "SOURCE_NOT_APPROVED", "source character is not approved", "character_ref"))
                source_style = request.data.get("style_ref")
                if isinstance(source_style, str) and REF_RE.fullmatch(source_style):
                    style_id, style_version = _reference_id(source_style)
                    style = require("style-profile", style_id, manifest, "candidate_ref")
                    if style and (style.data.get("version") != style_version or style.data.get("license_status") != "approved"):
                        diagnostics.append(_diag(manifest, "SOURCE_NOT_APPROVED", "source style licensing is not approved", "candidate_ref"))
    return diagnostics


def validate_path(path: Path) -> list[Diagnostic]:
    manifests, diagnostics = load_path(path)
    diagnostics.extend(validate_set(manifests))
    return diagnostics
