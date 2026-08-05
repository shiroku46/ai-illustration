"""Fail-closed validation for the six MVP manifest types."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable
import zlib

from .models import Diagnostic, Manifest, load_manifest
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, export_paths, safe_relative_path
from .quality import (
    CREATIVE_CANDIDATE,
    PACKAGED_QUALITY_STAGES,
    QUALITY_STAGES,
    REVIEW_SCOPES,
    QualityGateError,
    normalized_hard_fail_categories,
)

KINDS = {
    "character-spec", "style-profile", "generation-request",
    "candidate-asset", "review-decision", "export-manifest",
}
LICENSE_STATES = {"unreviewed", "reviewing", "approved", "rejected"}
REVIEW_STATES = {"shortlist", "accept", "reject", "needs_revision"}
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_PNG_DECOMPRESSED_BYTES = 128 * 1024 * 1024

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
        "kind", "schema_version", "id", "candidate_ref",
        "candidate_request_ref", "candidate_sha256", "decision", "reviewer",
        "timestamp", "categories",
    ),
    "export-manifest": (
        "kind", "schema_version", "id", "character_ref", "candidate_ref",
        "review_ref", "path", "sidecar_path", "sha256", "width", "height",
        "color_space", "has_alpha", "format", "license_status", "status",
        "crop", "facing", "pose", "expression", "version",
    ),
}
OPTIONAL = {"seed", "notes", "supersedes", "observed_sha256", "tool_version"}
QUALITY_OPTIONAL: dict[str, set[str]] = {
    "candidate-asset": {"quality_stage"},
    "review-decision": {"review_scope", "resulting_quality_stage", "hard_fail_categories"},
}


@dataclass(frozen=True)
class PngInfo:
    width: int
    height: int
    has_alpha: bool
    has_srgb: bool


def _diagnostic(manifest: Manifest, code: str, message: str, field: str = "") -> Diagnostic:
    return Diagnostic(code, message, str(manifest.source), field)


def _split_versioned_ref(value: str) -> tuple[str, str]:
    identifier, version = value.rsplit("@", 1)
    return identifier, version


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_document(manifest: Manifest) -> list[Diagnostic]:
    data = manifest.data
    diagnostics: list[Diagnostic] = []
    kind = manifest.kind
    if kind not in KINDS:
        return [_diagnostic(manifest, "UNKNOWN_KIND", f"unsupported kind: {kind!r}", "kind")]

    optional = OPTIONAL | QUALITY_OPTIONAL.get(kind, set())
    unknown = sorted(set(data) - set(REQUIRED[kind]) - optional)
    if unknown:
        diagnostics.append(_diagnostic(
            manifest, "UNKNOWN_FIELD",
            f"unknown fields are not accepted: {', '.join(unknown)}",
        ))
    for field in REQUIRED[kind]:
        if field not in data:
            diagnostics.append(_diagnostic(manifest, "MISSING_FIELD", "required field is missing", field))

    if data.get("schema_version") != "1.0":
        diagnostics.append(_diagnostic(manifest, "SCHEMA_VERSION", "schema_version must be '1.0'", "schema_version"))
    if "id" in data and (not isinstance(data["id"], str) or not ID_RE.fullmatch(data["id"])):
        diagnostics.append(_diagnostic(manifest, "INVALID_ID", "id must be lowercase ASCII with hyphens", "id"))
    for field in ("character_ref", "style_ref"):
        if field in data and (not isinstance(data[field], str) or not REF_RE.fullmatch(data[field])):
            diagnostics.append(_diagnostic(manifest, "INVALID_REFERENCE", "reference must use id@vNNN", field))
    for field in ("request_ref", "candidate_ref", "candidate_request_ref", "review_ref"):
        if field in data and (not isinstance(data[field], str) or not ID_RE.fullmatch(data[field])):
            diagnostics.append(_diagnostic(manifest, "INVALID_REFERENCE", "reference must be a manifest id", field))
    if "version" in data and (not isinstance(data["version"], str) or not VERSION_RE.fullmatch(data["version"])):
        diagnostics.append(_diagnostic(manifest, "INVALID_VERSION", "version must use vNNN", "version"))
    if "license_status" in data and data["license_status"] not in LICENSE_STATES:
        diagnostics.append(_diagnostic(manifest, "LICENSE_STATUS", "invalid license_status", "license_status"))

    if kind == "character-spec":
        if data.get("role") not in {"boke", "tsukkomi"}:
            diagnostics.append(_diagnostic(manifest, "ROLE", "role must be boke or tsukkomi", "role"))
        if data.get("review_status") not in {"draft", "approved", "rejected"}:
            diagnostics.append(_diagnostic(manifest, "REVIEW_STATUS", "invalid review_status", "review_status"))
        if not isinstance(data.get("identity_anchors"), list) or not data.get("identity_anchors"):
            diagnostics.append(_diagnostic(manifest, "IDENTITY_ANCHORS", "at least one identity anchor is required", "identity_anchors"))

    if kind == "style-profile" and (
        not isinstance(data.get("anti_ai_checks"), list) or not data.get("anti_ai_checks")
    ):
        diagnostics.append(_diagnostic(manifest, "ANTI_AI_CHECKS", "anti_ai_checks must be a non-empty list", "anti_ai_checks"))

    if kind == "generation-request":
        for field in ("pose", "expression", "crop", "facing", "tool_id", "model_id"):
            value = data.get(field)
            if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
                diagnostics.append(_diagnostic(manifest, "INVALID_TOKEN", "must be a lowercase ASCII token", field))
        if not isinstance(data.get("config"), dict):
            diagnostics.append(_diagnostic(manifest, "CONFIG", "config must be an object", "config"))
        provenance = data.get("provenance")
        if not isinstance(provenance, dict) or not _nonempty_text(provenance.get("source")):
            diagnostics.append(_diagnostic(manifest, "UNKNOWN_PROVENANCE", "provenance.source is required", "provenance"))
        seed = data.get("seed")
        if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or seed < 0):
            diagnostics.append(_diagnostic(manifest, "SEED", "seed must be a non-negative integer", "seed"))

    if kind in {"candidate-asset", "export-manifest"}:
        for field in ("path", "sidecar_path"):
            if field in data:
                try:
                    safe_relative_path(data[field])
                except (TypeError, ValueError) as exc:
                    diagnostics.append(_diagnostic(manifest, "UNSAFE_PATH", str(exc), field))
        if not isinstance(data.get("sha256"), str) or not SHA256_RE.fullmatch(data.get("sha256", "")):
            diagnostics.append(_diagnostic(manifest, "CHECKSUM", "sha256 must be 64 lowercase hexadecimal characters", "sha256"))
        for field in ("width", "height"):
            value = data.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                diagnostics.append(_diagnostic(manifest, "DIMENSION", "dimension must be a positive integer", field))
        if data.get("color_space") != "sRGB":
            diagnostics.append(_diagnostic(manifest, "COLOR_SPACE", "color_space must be sRGB", "color_space"))
        if data.get("has_alpha") is not True:
            diagnostics.append(_diagnostic(manifest, "ALPHA_REQUIRED", "transparent alpha is required", "has_alpha"))

    if kind == "candidate-asset":
        if data.get("media_type") != "image/png":
            diagnostics.append(_diagnostic(manifest, "MEDIA_TYPE", "media_type must be image/png", "media_type"))
        if data.get("status") not in {"received", "technically_valid", "invalid"}:
            diagnostics.append(_diagnostic(manifest, "CANDIDATE_STATUS", "invalid candidate status", "status"))
        quality_stage = data.get("quality_stage")
        if quality_stage is not None and quality_stage not in PACKAGED_QUALITY_STAGES:
            diagnostics.append(_diagnostic(
                manifest,
                "QUALITY_STAGE",
                "candidate quality_stage must be transport_smoke_output or technical_candidate",
                "quality_stage",
            ))
        provenance = data.get("provenance")
        if not isinstance(provenance, dict) or not _nonempty_text(provenance.get("source")):
            diagnostics.append(_diagnostic(manifest, "UNKNOWN_PROVENANCE", "provenance.source is required", "provenance"))

    if kind == "review-decision":
        if data.get("decision") not in REVIEW_STATES:
            diagnostics.append(_diagnostic(manifest, "DECISION", "invalid review decision", "decision"))
        if not _nonempty_text(data.get("reviewer")):
            diagnostics.append(_diagnostic(manifest, "REVIEWER", "reviewer is required", "reviewer"))
        if not isinstance(data.get("timestamp"), str) or not UTC_RE.fullmatch(data.get("timestamp", "")):
            diagnostics.append(_diagnostic(manifest, "TIMESTAMP", "timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ", "timestamp"))
        if not isinstance(data.get("categories"), list):
            diagnostics.append(_diagnostic(manifest, "CATEGORIES", "categories must be a list", "categories"))
        if not isinstance(data.get("candidate_sha256"), str) or not SHA256_RE.fullmatch(data.get("candidate_sha256", "")):
            diagnostics.append(_diagnostic(manifest, "CHECKSUM", "candidate_sha256 must be 64 lowercase hexadecimal characters", "candidate_sha256"))
        quality_fields = {"review_scope", "resulting_quality_stage", "hard_fail_categories"}
        present_quality_fields = quality_fields & set(data)
        if present_quality_fields and present_quality_fields != quality_fields:
            diagnostics.append(_diagnostic(
                manifest,
                "QUALITY_REVIEW_FIELDS",
                "quality-aware review fields must be supplied together",
            ))
        elif present_quality_fields:
            if data.get("review_scope") not in REVIEW_SCOPES:
                diagnostics.append(_diagnostic(manifest, "REVIEW_SCOPE", "invalid review_scope", "review_scope"))
            if data.get("resulting_quality_stage") not in QUALITY_STAGES:
                diagnostics.append(_diagnostic(
                    manifest,
                    "QUALITY_STAGE",
                    "invalid resulting_quality_stage",
                    "resulting_quality_stage",
                ))
            try:
                hard_fails = normalized_hard_fail_categories(data.get("hard_fail_categories"))
            except QualityGateError as exc:
                diagnostics.append(_diagnostic(manifest, exc.code, exc.message, exc.field))
            else:
                if data.get("decision") == "accept" and data.get("review_scope") == "creative":
                    if data.get("resulting_quality_stage") != CREATIVE_CANDIDATE:
                        diagnostics.append(_diagnostic(
                            manifest,
                            "QUALITY_STAGE",
                            "creative accept must result in creative_candidate",
                            "resulting_quality_stage",
                        ))
                    if hard_fails:
                        diagnostics.append(_diagnostic(
                            manifest,
                            "CREATIVE_HARD_FAIL",
                            "creative accept cannot contain hard-fail categories",
                            "hard_fail_categories",
                        ))
                elif data.get("resulting_quality_stage") == CREATIVE_CANDIDATE:
                    diagnostics.append(_diagnostic(
                        manifest,
                        "QUALITY_STAGE",
                        "creative_candidate requires an explicit creative accept",
                        "resulting_quality_stage",
                    ))

    if kind == "export-manifest":
        if data.get("format") != "png":
            diagnostics.append(_diagnostic(manifest, "FORMAT", "format must be png", "format"))
        if data.get("status") not in {"planned", "validated", "packaged", "verified"}:
            diagnostics.append(_diagnostic(manifest, "EXPORT_STATUS", "invalid export status", "status"))
        if data.get("license_status") != "approved":
            diagnostics.append(_diagnostic(manifest, "LICENSE_NOT_APPROVED", "exports require approved licensing", "license_status"))
        for field in ("crop", "facing", "pose", "expression"):
            value = data.get(field)
            if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
                diagnostics.append(_diagnostic(manifest, "INVALID_TOKEN", "must be lowercase ASCII token", field))
        needed = ("character_ref", "crop", "facing", "pose", "expression", "version", "sha256", "path", "sidecar_path")
        if all(field in data for field in needed):
            try:
                character_id, _ = _split_versioned_ref(data["character_ref"])
                expected_path, expected_sidecar = export_paths(
                    character_id=character_id, crop=data["crop"], facing=data["facing"],
                    pose=data["pose"], expression=data["expression"],
                    version=data["version"], sha256=data["sha256"],
                )
                if data["path"] != expected_path:
                    diagnostics.append(_diagnostic(manifest, "NONDETERMINISTIC_PATH", f"expected {expected_path}", "path"))
                if data["sidecar_path"] != expected_sidecar:
                    diagnostics.append(_diagnostic(manifest, "NONDETERMINISTIC_PATH", f"expected {expected_sidecar}", "sidecar_path"))
            except (TypeError, ValueError):
                pass
    return diagnostics


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


def _parse_png(data: bytes) -> PngInfo:
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("invalid PNG signature")
    offset = len(PNG_SIGNATURE)
    width = height = channels = -1
    color_type = -1
    seen_ihdr = seen_plte = seen_idat = seen_iend = seen_srgb = False
    idat_closed = False
    idat = bytearray()
    chunk_index = 0

    while offset < len(data):
        if len(data) - offset < 12:
            raise ValueError("truncated PNG chunk")
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            raise ValueError("PNG chunk length exceeds file size")
        chunk_data = data[offset + 8:offset + 8 + length]
        stored_crc = int.from_bytes(data[offset + 8 + length:chunk_end], "big")
        if stored_crc != (zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF):
            raise ValueError(f"invalid CRC for {chunk_type!r}")
        if not re.fullmatch(rb"[A-Za-z]{4}", chunk_type):
            raise ValueError("invalid PNG chunk type")
        if chunk_index == 0 and chunk_type != b"IHDR":
            raise ValueError("IHDR must be the first chunk")
        if seen_iend:
            raise ValueError("data appears after IEND")

        if seen_idat and chunk_type not in {b"IDAT", b"IEND"}:
            idat_closed = True

        if chunk_type == b"IHDR":
            if seen_ihdr or length != 13:
                raise ValueError("PNG must contain exactly one 13-byte IHDR")
            seen_ihdr = True
            width = int.from_bytes(chunk_data[0:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            bit_depth, color_type = chunk_data[8], chunk_data[9]
            compression, filter_method, interlace = chunk_data[10], chunk_data[11], chunk_data[12]
            if width <= 0 or height <= 0:
                raise ValueError("PNG dimensions must be positive")
            if bit_depth != 8 or color_type not in {4, 6}:
                raise ValueError("production PNG must be 8-bit grayscale-alpha or RGBA")
            if compression != 0 or filter_method != 0 or interlace != 0:
                raise ValueError("unsupported PNG compression, filter, or interlace method")
            channels = 2 if color_type == 4 else 4
        elif chunk_type == b"PLTE":
            if seen_plte or seen_idat:
                raise ValueError("PLTE must be unique and precede IDAT")
            if color_type == 4:
                raise ValueError("PLTE is forbidden for grayscale-alpha PNG")
            if length == 0 or length % 3 != 0 or length > 256 * 3:
                raise ValueError("invalid PLTE length")
            seen_plte = True
        elif chunk_type == b"sRGB":
            if seen_srgb or seen_plte or seen_idat:
                raise ValueError("sRGB must be unique and precede PLTE and IDAT")
            if length != 1 or chunk_data[0] > 3:
                raise ValueError("invalid sRGB chunk")
            seen_srgb = True
        elif chunk_type == b"tRNS":
            raise ValueError("tRNS is forbidden for alpha PNG")
        elif chunk_type == b"IDAT":
            if not seen_ihdr:
                raise ValueError("IDAT precedes IHDR")
            if idat_closed:
                raise ValueError("IDAT chunks must be consecutive")
            seen_idat = True
            idat.extend(chunk_data)
        elif chunk_type == b"IEND":
            if length != 0 or not seen_idat:
                raise ValueError("invalid IEND placement or length")
            seen_iend = True
            if chunk_end != len(data):
                raise ValueError("trailing bytes after IEND")
        else:
            raise ValueError(f"unsupported PNG chunk {chunk_type!r}")

        offset = chunk_end
        chunk_index += 1
        if seen_iend:
            break

    if not (seen_ihdr and seen_idat and seen_iend):
        raise ValueError("PNG requires IHDR, IDAT, and IEND chunks")
    row_size = 1 + width * channels
    expected_size = row_size * height
    if expected_size > MAX_PNG_DECOMPRESSED_BYTES:
        raise ValueError("decompressed PNG exceeds the configured safety limit")

    decompressor = zlib.decompressobj()
    output_limit = expected_size + 1
    try:
        raw = bytearray(decompressor.decompress(bytes(idat), output_limit))
        if len(raw) > expected_size or decompressor.unconsumed_tail:
            raise ValueError("decompressed pixel data exceeds IHDR-derived size")
        remaining = output_limit - len(raw)
        if remaining > 0:
            raw.extend(decompressor.flush(remaining))
    except zlib.error as exc:
        raise ValueError(f"IDAT decompression failed: {exc}") from exc
    if len(raw) > expected_size:
        raise ValueError("decompressed pixel data exceeds IHDR-derived size")
    if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
        raise ValueError("IDAT stream is incomplete or contains trailing compressed data")
    if len(raw) != expected_size:
        raise ValueError("decompressed pixel data length is inconsistent with IHDR")
    for row in range(height):
        if raw[row * row_size] > 4:
            raise ValueError("invalid PNG scanline filter type")
    return PngInfo(width=width, height=height, has_alpha=True, has_srgb=seen_srgb)


def _resolve_asset_path(root: Path, relative: Path) -> Path:
    candidate = root.joinpath(*relative.parts)
    if candidate.is_file():
        return candidate
    return Path.cwd().joinpath(*relative.parts)


def _verify_png(manifest: Manifest, field: str, root: Path) -> list[Diagnostic]:
    data = manifest.data
    try:
        relative = safe_relative_path(data.get(field, ""))
    except (TypeError, ValueError) as exc:
        return [_diagnostic(manifest, "UNSAFE_PATH", str(exc), field)]
    path = _resolve_asset_path(root, Path(*relative.parts))
    if not path.is_file():
        return [_diagnostic(manifest, "ASSET_MISSING", f"asset file not found: {relative}", field)]
    try:
        payload = path.read_bytes()
    except OSError as exc:
        return [_diagnostic(manifest, "ASSET_READ_ERROR", str(exc), field)]

    diagnostics: list[Diagnostic] = []
    if path.suffix.lower() != ".png":
        return [_diagnostic(manifest, "ASSET_NOT_PNG", "asset path must end in .png", field)]
    actual_sha = hashlib.sha256(payload).hexdigest()
    if actual_sha != data.get("sha256"):
        diagnostics.append(_diagnostic(manifest, "ASSET_CHECKSUM_MISMATCH", f"actual SHA-256 is {actual_sha}", "sha256"))
    try:
        png = _parse_png(payload)
    except ValueError as exc:
        diagnostics.append(_diagnostic(manifest, "PNG_STRUCTURE", str(exc), field))
        return diagnostics
    if png.width != data.get("width") or png.height != data.get("height"):
        diagnostics.append(_diagnostic(manifest, "ASSET_DIMENSION_MISMATCH", f"actual dimensions are {png.width}x{png.height}", field))
    if png.has_alpha != (data.get("has_alpha") is True):
        diagnostics.append(_diagnostic(manifest, "ASSET_ALPHA_MISMATCH", "PNG alpha capability differs from manifest", field))
    if data.get("color_space") == "sRGB" and not png.has_srgb:
        diagnostics.append(_diagnostic(manifest, "ASSET_COLOR_SPACE_UNVERIFIED", "PNG lacks a valid sRGB chunk", field))
    return diagnostics


def validate_set(manifests: Iterable[Manifest], root: Path | None = None) -> list[Diagnostic]:
    root = (root or Path.cwd()).resolve()
    documents = list(manifests)
    diagnostics: list[Diagnostic] = []
    index: dict[tuple[str, str], Manifest] = {}
    duplicates: dict[str, list[Manifest]] = defaultdict(list)
    for manifest in documents:
        diagnostics.extend(validate_document(manifest))
        duplicates[manifest.manifest_id].append(manifest)
        if manifest.kind and manifest.manifest_id:
            index[(manifest.kind, manifest.manifest_id)] = manifest
    for identifier, matches in duplicates.items():
        if identifier and len(matches) > 1:
            for manifest in matches:
                diagnostics.append(_diagnostic(manifest, "DUPLICATE_ID", f"duplicate manifest id: {identifier}", "id"))

    def require(kind: str, identifier: str, source: Manifest, field: str) -> Manifest | None:
        target = index.get((kind, identifier))
        if target is None:
            diagnostics.append(_diagnostic(source, "UNRESOLVED_REFERENCE", f"{kind} {identifier!r} was not found", field))
        return target

    for manifest in documents:
        data = manifest.data
        if manifest.kind == "generation-request":
            for field, kind in (("character_ref", "character-spec"), ("style_ref", "style-profile")):
                value = data.get(field)
                if isinstance(value, str) and REF_RE.fullmatch(value):
                    identifier, version = _split_versioned_ref(value)
                    target = require(kind, identifier, manifest, field)
                    if target and target.data.get("version") != version:
                        diagnostics.append(_diagnostic(manifest, "VERSION_MISMATCH", f"{field} version does not match target", field))
        elif manifest.kind == "candidate-asset":
            if isinstance(data.get("request_ref"), str):
                require("generation-request", data["request_ref"], manifest, "request_ref")
            if data.get("status") == "technically_valid":
                diagnostics.extend(_verify_png(manifest, "path", root))
        elif manifest.kind == "review-decision":
            candidate = require("candidate-asset", data.get("candidate_ref", ""), manifest, "candidate_ref")
            if candidate:
                if data.get("decision") in {"accept", "shortlist"} and candidate.data.get("status") != "technically_valid":
                    diagnostics.append(_diagnostic(manifest, "NOT_REVIEW_READY", "candidate must be technically_valid", "candidate_ref"))
                if data.get("candidate_request_ref") != candidate.data.get("request_ref"):
                    diagnostics.append(_diagnostic(manifest, "REVIEW_SOURCE_MISMATCH", "review request must match candidate source request", "candidate_request_ref"))
                if data.get("candidate_sha256") != candidate.data.get("sha256"):
                    diagnostics.append(_diagnostic(manifest, "REVIEW_CHECKSUM_MISMATCH", "review checksum must match candidate", "candidate_sha256"))
        elif manifest.kind == "export-manifest":
            candidate = require("candidate-asset", data.get("candidate_ref", ""), manifest, "candidate_ref")
            review = require("review-decision", data.get("review_ref", ""), manifest, "review_ref")
            request = None
            if candidate and isinstance(candidate.data.get("request_ref"), str):
                request = require("generation-request", candidate.data["request_ref"], manifest, "candidate_ref")
            production = data.get("status") in {"validated", "packaged", "verified"}
            if production and review and review.data.get("decision") != "accept":
                diagnostics.append(_diagnostic(manifest, "NOT_APPROVED", "production export review must be accept", "review_ref"))
            if review and review.data.get("candidate_ref") != data.get("candidate_ref"):
                diagnostics.append(_diagnostic(manifest, "REFERENCE_MISMATCH", "review does not approve this candidate", "review_ref"))
            if candidate:
                for field in ("sha256", "width", "height", "color_space", "has_alpha"):
                    if candidate.data.get(field) != data.get(field):
                        diagnostics.append(_diagnostic(manifest, "EXPORT_MISMATCH", f"{field} differs from candidate", field))
                if production and candidate.data.get("status") != "technically_valid":
                    diagnostics.append(_diagnostic(manifest, "NOT_REVIEW_READY", "candidate is not technically_valid", "candidate_ref"))
            if review and candidate:
                if review.data.get("candidate_request_ref") != candidate.data.get("request_ref"):
                    diagnostics.append(_diagnostic(manifest, "REVIEW_SOURCE_MISMATCH", "review request must match candidate source request", "review_ref"))
                reviewed_sha = review.data.get("candidate_sha256")
                if reviewed_sha != candidate.data.get("sha256") or reviewed_sha != data.get("sha256"):
                    diagnostics.append(_diagnostic(manifest, "REVIEW_CHECKSUM_MISMATCH", "review checksum must match candidate and export", "review_ref"))
            if production:
                diagnostics.extend(_verify_png(manifest, "path", root))
            if request:
                for field in ("character_ref", "pose", "expression", "crop", "facing"):
                    if request.data.get(field) != data.get(field):
                        diagnostics.append(_diagnostic(manifest, "SOURCE_METADATA_MISMATCH", f"{field} differs from source request", field))
                if request.data.get("license_status") != "approved":
                    diagnostics.append(_diagnostic(manifest, "SOURCE_NOT_APPROVED", "source request licensing is not approved", "candidate_ref"))
                for field, kind in (("character_ref", "character-spec"), ("style_ref", "style-profile")):
                    value = request.data.get(field)
                    if isinstance(value, str) and REF_RE.fullmatch(value):
                        identifier, version = _split_versioned_ref(value)
                        source = require(kind, identifier, manifest, field)
                        if source and (
                            source.data.get("version") != version
                            or source.data.get("license_status") != "approved"
                            or (kind == "character-spec" and source.data.get("review_status") != "approved")
                        ):
                            diagnostics.append(_diagnostic(manifest, "SOURCE_NOT_APPROVED", f"source {kind} is not approved", field))
    return diagnostics


def validate_path(path: Path) -> list[Diagnostic]:
    manifests, diagnostics = load_path(path)
    root = path if path.is_dir() else path.parent
    diagnostics.extend(validate_set(manifests, root))
    return diagnostics
