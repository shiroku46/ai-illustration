"""Deterministic, non-executing expression and pose variant-set planning."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Any

from .models import Manifest
from .naming import SHA256_RE, TOKEN_RE, canonical_json, content_identifier, safe_relative_path
from .validation import _verify_png, load_path, validate_document

INTENTS = {"evaluation", "production"}
UNRESOLVED_FIELDS = (
    "stage_side", "canvas", "editable_source_format", "layer_strategy",
    "mouth_shape_granularity",
)
MATRIX_FIELDS = {"combinations", *UNRESOLVED_FIELDS}
COMBINATION_FIELDS = {"expression", "pose", "facing", "crop", "mouth_state"}
MAX_VARIANTS = 512


@dataclass(frozen=True)
class VariantError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VariantError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise VariantError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _token(value: Any, field: str) -> str:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        raise VariantError("INVALID_TOKEN", "must be lowercase ASCII with optional hyphens", field)
    return value


def _manifest_index(root: Path) -> dict[tuple[str, str], Manifest]:
    if not root.is_dir():
        raise VariantError("MANIFEST_ROOT", "manifest root must be a directory", str(root))
    manifests, load_diagnostics = load_path(root)
    diagnostics = list(load_diagnostics)
    for manifest in manifests:
        diagnostics.extend(validate_document(manifest))
    if diagnostics:
        first = diagnostics[0]
        raise VariantError(first.code, first.message, first.field or first.document)
    index: dict[tuple[str, str], Manifest] = {}
    ids: set[str] = set()
    for manifest in manifests:
        if not manifest.manifest_id:
            raise VariantError("MISSING_ID", "manifest id is required", str(manifest.source))
        if manifest.manifest_id in ids:
            raise VariantError("DUPLICATE_ID", f"duplicate manifest id: {manifest.manifest_id}", "id")
        ids.add(manifest.manifest_id)
        index[(manifest.kind, manifest.manifest_id)] = manifest
    return index


def _require(index: dict[tuple[str, str], Manifest], kind: str, identifier: Any, field: str) -> Manifest:
    if not isinstance(identifier, str):
        raise VariantError("INVALID_REFERENCE", f"{kind} reference must be a string", field)
    target = index.get((kind, identifier))
    if target is None:
        raise VariantError("UNRESOLVED_REFERENCE", f"{kind} {identifier!r} was not found", field)
    return target


def _split_ref(value: Any, field: str) -> tuple[str, str]:
    if not isinstance(value, str) or "@" not in value:
        raise VariantError("INVALID_REFERENCE", "reference must use id@vNNN", field)
    identifier, version = value.rsplit("@", 1)
    _token(identifier, field)
    if len(version) != 4 or not version.startswith("v") or not version[1:].isdigit():
        raise VariantError("INVALID_REFERENCE", "reference must use id@vNNN", field)
    return identifier, version


def _source_context(
    index: dict[tuple[str, str], Manifest],
    candidate_id: str,
    intent: str,
    root: Path,
) -> dict[str, Any]:
    if intent not in INTENTS:
        raise VariantError("INTENT", "intent must be evaluation or production", "intent")
    candidate = _require(index, "candidate-asset", candidate_id, "source_candidate")
    c = candidate.data
    if c.get("status") != "technically_valid":
        raise VariantError("SOURCE_NOT_VALID", "source candidate must be technically_valid", "source_candidate")
    if not isinstance(c.get("sha256"), str) or not SHA256_RE.fullmatch(c["sha256"]):
        raise VariantError("CHECKSUM", "source candidate checksum is invalid", "source_candidate")
    provenance = c.get("provenance")
    if not isinstance(provenance, dict) or not isinstance(provenance.get("source"), str) or not provenance["source"].strip():
        raise VariantError("UNKNOWN_PROVENANCE", "source candidate provenance is required", "source_candidate")
    asset_diagnostics = _verify_png(candidate, "path", root.resolve())
    if asset_diagnostics:
        first = asset_diagnostics[0]
        raise VariantError(first.code, first.message, first.field or first.document)

    request = _require(index, "generation-request", c.get("request_ref"), "request_ref")
    r = request.data
    character_id, character_version = _split_ref(r.get("character_ref"), "character_ref")
    style_id, style_version = _split_ref(r.get("style_ref"), "style_ref")
    character = _require(index, "character-spec", character_id, "character_ref")
    style = _require(index, "style-profile", style_id, "style_ref")
    if character.data.get("version") != character_version:
        raise VariantError("VERSION_MISMATCH", "character version does not match source request", "character_ref")
    if style.data.get("version") != style_version:
        raise VariantError("VERSION_MISMATCH", "style version does not match source request", "style_ref")
    if character.data.get("review_status") != "approved":
        raise VariantError("CHARACTER_NOT_APPROVED", "character specification must be approved", "character_ref")

    reviews = [
        manifest for (kind, _), manifest in index.items()
        if kind == "review-decision" and manifest.data.get("candidate_ref") == candidate_id
    ]
    if not reviews:
        raise VariantError("ACCEPT_REVIEW_REQUIRED", "an accept review is required", "source_candidate")
    reviews.sort(key=lambda item: (str(item.data.get("timestamp", "")), item.manifest_id))
    review = reviews[-1]
    rv = review.data
    if rv.get("decision") != "accept":
        raise VariantError("ACCEPT_REVIEW_REQUIRED", "latest review decision must be accept", "review_ref")
    if rv.get("candidate_request_ref") != request.manifest_id:
        raise VariantError("STALE_REVIEW", "review source request does not match candidate", "review_ref")
    if rv.get("candidate_sha256") != c.get("sha256"):
        raise VariantError("STALE_REVIEW", "review checksum does not match candidate", "review_ref")

    if intent == "production":
        approvals = {
            "generation-request": r.get("license_status"),
            "character-spec": character.data.get("license_status"),
            "style-profile": style.data.get("license_status"),
        }
        rejected = [name for name, value in approvals.items() if value != "approved"]
        if rejected:
            raise VariantError(
                "PRODUCTION_LICENSE_NOT_APPROVED",
                "production intent requires approved licensing: " + ", ".join(rejected),
                "intent",
            )

    return {
        "candidate": c,
        "request": r,
        "review": rv,
        "review_id": review.manifest_id,
        "character": character.data,
        "style": style.data,
        "character_id": character_id,
        "character_version": character_version,
        "style_id": style_id,
        "style_version": style_version,
    }


def _normalize_matrix(matrix: dict[str, Any], source: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    unknown = sorted(set(matrix) - MATRIX_FIELDS)
    if unknown:
        raise VariantError("UNKNOWN_FIELD", "unknown matrix fields: " + ", ".join(unknown), "matrix")
    combinations = matrix.get("combinations")
    if not isinstance(combinations, list) or not combinations:
        raise VariantError("MATRIX", "combinations must be a non-empty list", "combinations")
    if len(combinations) > MAX_VARIANTS:
        raise VariantError("MATRIX_LIMIT", f"at most {MAX_VARIANTS} combinations are allowed", "combinations")

    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for position, raw in enumerate(combinations):
        field = f"combinations[{position}]"
        if not isinstance(raw, dict):
            raise VariantError("MATRIX", "combination must be an object", field)
        extra = sorted(set(raw) - COMBINATION_FIELDS)
        if extra:
            raise VariantError("UNKNOWN_FIELD", "unknown combination fields: " + ", ".join(extra), field)
        item: dict[str, Any] = {
            "expression": _token(raw.get("expression"), f"{field}.expression"),
            "pose": _token(raw.get("pose"), f"{field}.pose"),
            "facing": _token(raw.get("facing"), f"{field}.facing"),
            "crop": _token(raw.get("crop"), f"{field}.crop"),
        }
        mouth = raw.get("mouth_state")
        if mouth is not None:
            item["mouth_state"] = _token(mouth, f"{field}.mouth_state")
        key = (item["expression"], item["pose"], item["facing"], item["crop"], item.get("mouth_state", ""))
        if key in seen:
            raise VariantError("DUPLICATE_COMBINATION", "duplicate variant combination", field)
        seen.add(key)
        normalized.append(item)
    normalized.sort(key=lambda item: (item["expression"], item["pose"], item["facing"], item["crop"], item.get("mouth_state", "")))

    decisions: dict[str, Any] = {name: matrix.get(name) for name in UNRESOLVED_FIELDS}
    if decisions["stage_side"] not in {None, "left", "right"}:
        raise VariantError("STAGE_SIDE", "stage_side must be left, right, or null", "stage_side")
    canvas = decisions["canvas"]
    width = source["candidate"].get("width")
    height = source["candidate"].get("height")
    if canvas is not None:
        if not isinstance(canvas, dict) or set(canvas) != {"width", "height"}:
            raise VariantError("CANVAS", "canvas must contain exactly width and height", "canvas")
        width, height = canvas.get("width"), canvas.get("height")
    for name, value in (("width", width), ("height", height)):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise VariantError("DIMENSION", "dimensions must be positive integers", f"canvas.{name}")
    decisions["canvas"] = None if canvas is None else {"width": width, "height": height}
    for name in ("editable_source_format", "layer_strategy", "mouth_shape_granularity"):
        value = decisions[name]
        if value is not None:
            decisions[name] = _token(value, name)
    return normalized, {"decisions": decisions, "width": width, "height": height}


def _variant_path(character_id: str, item: dict[str, Any], variant_id: str) -> tuple[str, str]:
    suffix = f"-{item['mouth_state']}" if item.get("mouth_state") else ""
    directory = PurePosixPath(
        "variants", "v1", character_id, item["crop"], item["facing"],
        f"{item['pose']}-{item['expression']}{suffix}",
    )
    png = str(directory / f"{variant_id}.png")
    sidecar = str(directory / f"{variant_id}.json")
    safe_relative_path(png)
    safe_relative_path(sidecar)
    return png, sidecar


def plan_variant_set(manifest_root: Path, candidate_id: str, matrix: dict[str, Any], intent: str) -> dict[str, Any]:
    candidate_id = _token(candidate_id, "source_candidate")
    index = _manifest_index(manifest_root)
    source = _source_context(index, candidate_id, intent, manifest_root)
    combinations, matrix_info = _normalize_matrix(matrix, source)
    variants: list[dict[str, Any]] = []
    used_paths: set[str] = set()
    for item in combinations:
        identity = {
            "source_candidate_ref": candidate_id,
            "source_candidate_sha256": source["candidate"]["sha256"],
            "character_ref": source["request"]["character_ref"],
            "style_ref": source["request"]["style_ref"],
            "combination": item,
            "width": matrix_info["width"],
            "height": matrix_info["height"],
        }
        variant_id = content_identifier("variant", identity, 20)
        path, sidecar = _variant_path(source["character_id"], item, variant_id)
        if path in used_paths or sidecar in used_paths:
            raise VariantError("PATH_COLLISION", "planned output path collision", "variants")
        used_paths.update({path, sidecar})
        key_parts = [source["character"]["role"], source["character_id"], item["expression"], item["pose"], item["facing"], item["crop"]]
        if item.get("mouth_state"):
            key_parts.append(item["mouth_state"])
        variant = {
            "id": variant_id,
            **item,
            "path": path,
            "sidecar_path": sidecar,
            "paper_theater_key": ".".join(key_parts),
            "format": "png",
            "media_type": "image/png",
            "color_space": "sRGB",
            "has_alpha": True,
            "width": matrix_info["width"],
            "height": matrix_info["height"],
        }
        variants.append(variant)

    core = {
        "kind": "variant-set",
        "schema_version": "1.0",
        "intent": intent,
        "source_candidate_ref": candidate_id,
        "source_request_ref": source["request"]["id"],
        "source_candidate_sha256": source["candidate"]["sha256"],
        "review_ref": source["review_id"],
        "character_ref": source["request"]["character_ref"],
        "style_ref": source["request"]["style_ref"],
        "role": source["character"]["role"],
        "tool_id": source["request"]["tool_id"],
        "model_id": source["request"]["model_id"],
        "license_status": source["request"].get("license_status"),
        **matrix_info["decisions"],
        "variants": variants,
    }
    return {"id": content_identifier("variant-set", core, 20), **core}


def validate_variant_set(data: dict[str, Any], manifest_root: Path) -> dict[str, Any]:
    required = {
        "id", "kind", "schema_version", "intent", "source_candidate_ref",
        "source_request_ref", "source_candidate_sha256", "review_ref", "character_ref",
        "style_ref", "role", "tool_id", "model_id", "license_status", *UNRESOLVED_FIELDS,
        "variants",
    }
    if set(data) != required:
        missing = sorted(required - set(data))
        extra = sorted(set(data) - required)
        raise VariantError("PLAN_FIELDS", f"missing={missing}; extra={extra}", "variant-set")
    if data.get("kind") != "variant-set" or data.get("schema_version") != "1.0":
        raise VariantError("PLAN_SCHEMA", "kind/schema_version is invalid", "variant-set")
    variants = data.get("variants")
    if not isinstance(variants, list) or not variants:
        raise VariantError("PLAN_VARIANTS", "variants must be a non-empty list", "variants")
    matrix = {
        "combinations": [
            {key: item[key] for key in ("expression", "pose", "facing", "crop", "mouth_state") if key in item}
            for item in variants if isinstance(item, dict)
        ],
        **{name: data.get(name) for name in UNRESOLVED_FIELDS},
    }
    expected = plan_variant_set(manifest_root, data.get("source_candidate_ref", ""), matrix, data.get("intent", ""))
    if canonical_json(data) != canonical_json(expected):
        raise VariantError("PLAN_MISMATCH", "variant-set is not the canonical plan for its bound inputs", "variant-set")
    return expected


def check_variant_set(path: Path, manifest_root: Path) -> dict[str, Any]:
    return validate_variant_set(load_json_object(path), manifest_root)
