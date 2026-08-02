"""Deterministic local export packages for reviewed variant PNGs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .naming import SHA256_RE, TOKEN_RE, canonical_json, content_identifier, safe_relative_path
from .validation import _parse_png
from .variants import check_variant_set

PACKAGE_MANIFEST = "package-manifest.json"
PAPER_THEATER_INDEX = "paper-theater-index.json"
VARIANT_REVIEW_FIELDS = {
    "id",
    "kind",
    "schema_version",
    "variant_set_ref",
    "variant_id",
    "png_sha256",
    "decision",
    "reviewer",
}
VARIANT_REVIEW_BINDING_FIELDS = (
    "variant_review_ref",
    "variant_review_path",
    "variant_review_sha256",
)
VARIANT_REVIEW_SIDECAR_FIELDS = (*VARIANT_REVIEW_BINDING_FIELDS, "variant_reviewer")
LICENSE_STATUSES = {"unreviewed", "reviewing", "approved", "rejected"}
PACKAGE_FIELDS = {
    "id",
    "kind",
    "schema_version",
    "variant_set_ref",
    "intent",
    "source_candidate_ref",
    "source_candidate_sha256",
    "source_request_ref",
    "review_ref",
    "character_ref",
    "style_ref",
    "license_status",
    "paper_theater_index_path",
    "paper_theater_index_sha256",
    "items",
}
INDEX_FIELDS = {"id", "kind", "schema_version", "variant_set_ref", "intent", "entries"}
ITEM_BASE_FIELDS = {
    "variant_id",
    "paper_theater_key",
    "png_path",
    "png_sha256",
    "sidecar_path",
    "sidecar_sha256",
}
SIDECAR_BASE_FIELDS = {
    "kind",
    "schema_version",
    "variant_set_ref",
    "variant_id",
    "paper_theater_key",
    "intent",
    "source_candidate_ref",
    "source_request_ref",
    "review_ref",
    "character_ref",
    "style_ref",
    "source_file",
    "source_sha256",
    "output_path",
    "output_sha256",
    "width",
    "height",
    "format",
    "media_type",
    "color_space",
    "has_alpha",
    "license_status",
    "provenance",
}


@dataclass
class ExportError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise ExportError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _load_object_bytes(payload: bytes, field: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError("LOAD_ERROR", str(exc), field) from exc
    if not isinstance(value, dict):
        raise ExportError("ROOT_TYPE", "JSON root must be an object", field)
    return value


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _resolved_directory(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ExportError("SYMLINK_ROOT", "root directory must not be a symlink", field)
    if must_exist:
        if not expanded.is_dir():
            raise ExportError("ROOT_MISSING", "root directory does not exist", field)
        return expanded.resolve()
    if expanded.exists() and not expanded.is_dir():
        raise ExportError("ROOT_NOT_DIRECTORY", "root path must be a directory", field)
    return expanded.resolve(strict=False)


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _overlap(left: Path, right: Path) -> bool:
    return left == right or _is_within(left, right) or _is_within(right, left)


def _safe_join(root: Path, relative: str, field: str) -> Path:
    try:
        rel = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise ExportError("UNSAFE_PATH", str(exc), field) from exc
    candidate = root.joinpath(*rel.parts)
    resolved_parent = candidate.parent.resolve(strict=False)
    if not _is_within(root, resolved_parent):
        raise ExportError("PATH_ESCAPE", "path escapes configured root", field)
    return candidate


def _safe_relative(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ExportError("UNSAFE_PATH", "path must be a string", field)
    try:
        return str(safe_relative_path(value))
    except (TypeError, ValueError) as exc:
        raise ExportError("UNSAFE_PATH", str(exc), field) from exc


def _require_string(value: Any, field: str, code: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ExportError(code, f"{field} must be a non-empty bounded string", field)
    return value


def _require_sha256(value: Any, field: str, code: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ExportError(code, f"{field} must be a SHA-256 checksum", field)
    return value


def _require_exact_fields(value: dict[str, Any], expected: set[str], code: str, field: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise ExportError(code, f"missing={missing}; extra={extra}", field)


def _validate_package_schema(package: dict[str, Any], field: str) -> None:
    _require_exact_fields(package, PACKAGE_FIELDS, "PACKAGE_SCHEMA", field)
    if package.get("kind") != "variant-export-package" or package.get("schema_version") != "1.0":
        raise ExportError("PACKAGE_SCHEMA", "invalid package kind or schema version", field)
    for name in (
        "variant_set_ref",
        "source_candidate_ref",
        "source_request_ref",
        "review_ref",
        "character_ref",
        "style_ref",
    ):
        _require_string(package.get(name), name, "PACKAGE_SCHEMA")
    _require_sha256(package.get("source_candidate_sha256"), "source_candidate_sha256", "PACKAGE_SCHEMA")
    if package.get("intent") not in {"evaluation", "production"}:
        raise ExportError("PACKAGE_SCHEMA", "package intent must be evaluation or production", "intent")
    if package.get("license_status") not in LICENSE_STATUSES:
        raise ExportError("PACKAGE_SCHEMA", "invalid package license status", "license_status")
    _safe_relative(package.get("paper_theater_index_path"), "paper_theater_index_path")
    _require_sha256(
        package.get("paper_theater_index_sha256"),
        "paper_theater_index_sha256",
        "PACKAGE_SCHEMA",
    )
    if not isinstance(package.get("items"), list) or not package["items"]:
        raise ExportError("PACKAGE_SCHEMA", "package items must be a non-empty list", "items")


def _validate_sidecar_schema(sidecar: dict[str, Any], field: str) -> None:
    intent = sidecar.get("intent")
    review_fields = {name for name in VARIANT_REVIEW_SIDECAR_FIELDS if name in sidecar}
    if intent == "evaluation" and review_fields:
        raise ExportError(
            "EVALUATION_REVIEW_CLAIM",
            "evaluation sidecar must not contain variant approval fields",
            field,
        )
    expected_fields = SIDECAR_BASE_FIELDS | (
        set(VARIANT_REVIEW_SIDECAR_FIELDS) if intent == "production" else set()
    )
    _require_exact_fields(sidecar, expected_fields, "SIDECAR_SCHEMA", field)
    if sidecar.get("kind") != "variant-export-sidecar" or sidecar.get("schema_version") != "1.0":
        raise ExportError("SIDECAR_SCHEMA", "invalid sidecar kind or schema version", field)
    for name in (
        "variant_set_ref",
        "variant_id",
        "paper_theater_key",
        "source_candidate_ref",
        "source_request_ref",
        "review_ref",
        "character_ref",
        "style_ref",
        "source_file",
    ):
        _require_string(sidecar.get(name), name, "SIDECAR_SCHEMA")
    if not TOKEN_RE.fullmatch(sidecar["variant_id"]):
        raise ExportError("SIDECAR_SCHEMA", "invalid sidecar variant ID", field)
    if intent not in {"evaluation", "production"}:
        raise ExportError("SIDECAR_SCHEMA", "invalid sidecar intent", field)
    if sidecar.get("license_status") not in LICENSE_STATUSES:
        raise ExportError("SIDECAR_SCHEMA", "invalid sidecar license status", field)
    _require_sha256(sidecar.get("source_sha256"), "source_sha256", "SIDECAR_SCHEMA")
    _require_sha256(sidecar.get("output_sha256"), "output_sha256", "SIDECAR_SCHEMA")
    _safe_relative(sidecar.get("output_path"), "output_path")
    for name in ("width", "height"):
        value = sidecar.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ExportError("SIDECAR_SCHEMA", f"{name} must be a positive integer", field)
    if (
        sidecar.get("format") != "png"
        or sidecar.get("media_type") != "image/png"
        or sidecar.get("color_space") != "sRGB"
        or sidecar.get("has_alpha") is not True
    ):
        raise ExportError("SIDECAR_SCHEMA", "invalid sidecar PNG declarations", field)
    provenance = sidecar.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {"method", "source_candidate_sha256"}:
        raise ExportError("SIDECAR_SCHEMA", "invalid sidecar provenance fields", field)
    if provenance.get("method") != "verified-byte-copy":
        raise ExportError("SIDECAR_SCHEMA", "invalid sidecar provenance method", field)
    _require_sha256(
        provenance.get("source_candidate_sha256"),
        "provenance.source_candidate_sha256",
        "SIDECAR_SCHEMA",
    )


def _scan_flat_files(root: Path, expected_names: set[str], *, label: str) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ExportError(f"{label.upper()}_SYMLINK", f"{label} tree must not contain symlinks", str(path))
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if "/" in relative or relative not in expected_names:
            raise ExportError(f"EXTRA_{label.upper()}_FILE", f"unexpected {label} file: {relative}", relative)
        if relative in found:
            raise ExportError(f"DUPLICATE_{label.upper()}", f"duplicate {label} file: {relative}", relative)
        found[relative] = path
    missing = sorted(expected_names - set(found))
    if missing:
        raise ExportError(f"{label.upper()}_MISSING", f"missing {label} file(s): " + ", ".join(missing), f"{label}_root")
    return found


def _verify_supplied_png(path: Path, variant: dict[str, Any]) -> tuple[bytes, str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ExportError("SOURCE_READ_ERROR", str(exc), path.name) from exc
    actual_sha = _sha(payload)
    try:
        png = _parse_png(payload)
    except ValueError as exc:
        raise ExportError("PNG_STRUCTURE", str(exc), path.name) from exc
    if png.width != variant.get("width") or png.height != variant.get("height"):
        raise ExportError(
            "PNG_DIMENSION_MISMATCH",
            f"{path.name} is {png.width}x{png.height}, expected {variant.get('width')}x{variant.get('height')}",
            path.name,
        )
    if not png.has_alpha or variant.get("has_alpha") is not True:
        raise ExportError("PNG_ALPHA_REQUIRED", "supplied and planned PNG must support alpha", path.name)
    if not png.has_srgb or variant.get("color_space") != "sRGB":
        raise ExportError("PNG_SRGB_REQUIRED", "supplied and planned PNG must declare sRGB", path.name)
    if variant.get("format") != "png" or variant.get("media_type") != "image/png":
        raise ExportError("PNG_DECLARATION", "variant must declare PNG/image/png", path.name)
    return payload, actual_sha


def _validate_packaged_png(payload: bytes, sidecar: dict[str, Any], field: str) -> None:
    try:
        png = _parse_png(payload)
    except ValueError as exc:
        raise ExportError("PNG_STRUCTURE", str(exc), field) from exc
    if not png.has_alpha:
        raise ExportError("PNG_ALPHA_REQUIRED", "packaged PNG must support alpha", field)
    if not png.has_srgb:
        raise ExportError("PNG_SRGB_REQUIRED", "packaged PNG must declare sRGB", field)
    declarations = {
        "width": png.width,
        "height": png.height,
        "format": "png",
        "media_type": "image/png",
        "color_space": "sRGB",
        "has_alpha": True,
    }
    for name, expected in declarations.items():
        if sidecar.get(name) != expected:
            raise ExportError(
                "PNG_DECLARATION_MISMATCH",
                f"sidecar {name} does not match packaged PNG",
                field,
            )


def _validate_variant_review_object(
    review: dict[str, Any],
    *,
    field: str,
    variant_set_id: str,
    variant_id: str,
    png_sha256: str,
) -> tuple[dict[str, str], bytes]:
    if set(review) != VARIANT_REVIEW_FIELDS:
        missing = sorted(VARIANT_REVIEW_FIELDS - set(review))
        extra = sorted(set(review) - VARIANT_REVIEW_FIELDS)
        raise ExportError("VARIANT_REVIEW_FIELDS", f"missing={missing}; extra={extra}", field)
    if review.get("kind") != "variant-review-decision" or review.get("schema_version") != "1.0":
        raise ExportError("VARIANT_REVIEW_SCHEMA", "invalid variant review kind or schema version", field)
    if review.get("variant_set_ref") != variant_set_id or review.get("variant_id") != variant_id:
        raise ExportError("VARIANT_REVIEW_REFERENCE", "variant review does not bind this variant set and ID", field)
    if review.get("png_sha256") != png_sha256:
        raise ExportError("VARIANT_REVIEW_CHECKSUM", "variant review checksum does not bind supplied PNG bytes", field)
    if review.get("decision") != "accept":
        raise ExportError("VARIANT_REVIEW_NOT_ACCEPTED", "production variant review must be accept", field)
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip() or len(reviewer) > 200:
        raise ExportError("VARIANT_REVIEWER", "variant reviewer must be a non-empty bounded string", field)
    identifier = review.get("id")
    core = {key: value for key, value in review.items() if key != "id"}
    expected_id = content_identifier("variant-review", core, 20)
    if identifier != expected_id:
        raise ExportError("VARIANT_REVIEW_ID", f"expected canonical review ID {expected_id}", field)
    payload = _json_bytes(review)
    review_path = f"reviews/{variant_id}.json"
    return {
        "variant_review_ref": identifier,
        "variant_review_path": review_path,
        "variant_review_sha256": _sha(payload),
        "variant_reviewer": reviewer,
    }, payload


def _validate_variant_review_file(
    path: Path,
    *,
    variant_set_id: str,
    variant_id: str,
    png_sha256: str,
) -> tuple[dict[str, str], bytes]:
    return _validate_variant_review_object(
        _load_object(path),
        field=path.name,
        variant_set_id=variant_set_id,
        variant_id=variant_id,
        png_sha256=png_sha256,
    )


def _inventory_files(root: Path) -> dict[str, bytes]:
    inventory: dict[str, bytes] = {}
    if root.is_symlink() or not root.is_dir():
        raise ExportError("PACKAGE_ROOT", "package directory is invalid", str(root))
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ExportError("PACKAGE_SYMLINK", "package must not contain symlinks", str(path))
        if path.is_file():
            try:
                inventory[path.relative_to(root).as_posix()] = path.read_bytes()
            except OSError as exc:
                raise ExportError("PACKAGE_READ_ERROR", str(exc), str(path)) from exc
    return inventory


def _ensure_unique(values: list[str], code: str, field: str) -> None:
    if len(values) != len(set(values)):
        raise ExportError(code, f"duplicate {field}", field)


def build_export_package(
    variant_set_path: Path,
    manifest_root: Path,
    source_root: Path,
    output_root: Path,
    *,
    approval_root: Path | None = None,
    write: bool = False,
) -> dict[str, Any]:
    variant_set = check_variant_set(variant_set_path, manifest_root)
    manifests = _resolved_directory(manifest_root, must_exist=True, field="manifest_root")
    source = _resolved_directory(source_root, must_exist=True, field="source_root")
    output = _resolved_directory(output_root, must_exist=False, field="output_root")
    if _overlap(source, output) or _overlap(manifests, output):
        raise ExportError("ROOT_OVERLAP", "output root must not overlap source or manifest roots", "output_root")

    production = variant_set.get("intent") == "production"
    approvals: dict[str, Path] = {}
    approval_path: Path | None = None
    if production:
        if approval_root is None:
            raise ExportError(
                "PRODUCTION_VARIANT_REVIEW_REQUIRED",
                "production export requires an approval root with one exact byte-bound accept review per variant",
                "approval_root",
            )
        approval_path = _resolved_directory(approval_root, must_exist=True, field="approval_root")
        if _overlap(approval_path, output):
            raise ExportError("ROOT_OVERLAP", "approval and output roots must not overlap", "output_root")
    elif approval_root is not None:
        raise ExportError("APPROVAL_ROOT_NOT_ALLOWED", "approval root is accepted only for production exports", "approval_root")

    variants = variant_set.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ExportError("VARIANTS", "variant-set must contain variants", "variants")
    variant_ids = [str(item.get("id", "")) for item in variants if isinstance(item, dict)]
    if len(variant_ids) != len(variants) or any(not TOKEN_RE.fullmatch(value) for value in variant_ids):
        raise ExportError("VARIANT_ID", "every variant must have a valid ID", "variants")
    _ensure_unique(variant_ids, "DUPLICATE_VARIANT_ID", "variant IDs")
    keys = [str(item.get("paper_theater_key", "")) for item in variants]
    _ensure_unique(keys, "DUPLICATE_PAPER_THEATER_KEY", "paper-theater keys")
    png_paths = [str(item.get("path", "")) for item in variants]
    sidecar_paths = [str(item.get("sidecar_path", "")) for item in variants]
    _ensure_unique(png_paths + sidecar_paths, "OUTPUT_PATH_COLLISION", "output paths")

    source_files = _scan_flat_files(source, {f"{identifier}.png" for identifier in variant_ids}, label="source")
    if production and approval_path is not None:
        approvals = _scan_flat_files(
            approval_path,
            {f"{identifier}.json" for identifier in variant_ids},
            label="approval",
        )

    file_payloads: dict[str, bytes] = {}
    items: list[dict[str, Any]] = []
    index_entries: list[dict[str, Any]] = []

    for variant in sorted(variants, key=lambda item: str(item["id"])):
        identifier = variant["id"]
        source_name = f"{identifier}.png"
        png_payload, png_sha = _verify_supplied_png(source_files[source_name], variant)
        png_path = _safe_relative(variant["path"], "path")
        sidecar_path = _safe_relative(variant["sidecar_path"], "sidecar_path")
        review_binding: dict[str, str] = {}
        if production:
            review_binding, review_payload = _validate_variant_review_file(
                approvals[f"{identifier}.json"],
                variant_set_id=variant_set["id"],
                variant_id=identifier,
                png_sha256=png_sha,
            )
            file_payloads[review_binding["variant_review_path"]] = review_payload
        sidecar = {
            "kind": "variant-export-sidecar",
            "schema_version": "1.0",
            "variant_set_ref": variant_set["id"],
            "variant_id": identifier,
            "paper_theater_key": variant["paper_theater_key"],
            "intent": variant_set["intent"],
            "source_candidate_ref": variant_set["source_candidate_ref"],
            "source_request_ref": variant_set["source_request_ref"],
            "review_ref": variant_set["review_ref"],
            "character_ref": variant_set["character_ref"],
            "style_ref": variant_set["style_ref"],
            "source_file": source_name,
            "source_sha256": png_sha,
            "output_path": png_path,
            "output_sha256": png_sha,
            "width": variant["width"],
            "height": variant["height"],
            "format": "png",
            "media_type": "image/png",
            "color_space": "sRGB",
            "has_alpha": True,
            "license_status": variant_set["license_status"],
            "provenance": {
                "method": "verified-byte-copy",
                "source_candidate_sha256": variant_set["source_candidate_sha256"],
            },
            **review_binding,
        }
        sidecar_payload = _json_bytes(sidecar)
        file_payloads[png_path] = png_payload
        file_payloads[sidecar_path] = sidecar_payload
        item = {
            "variant_id": identifier,
            "paper_theater_key": variant["paper_theater_key"],
            "png_path": png_path,
            "png_sha256": png_sha,
            "sidecar_path": sidecar_path,
            "sidecar_sha256": _sha(sidecar_payload),
            **{key: review_binding[key] for key in VARIANT_REVIEW_BINDING_FIELDS if key in review_binding},
        }
        items.append(item)
        index_entries.append({
            "key": variant["paper_theater_key"],
            "variant_id": identifier,
            "png_path": png_path,
            "sidecar_path": sidecar_path,
            "sha256": png_sha,
        })

    items.sort(key=lambda item: item["variant_id"])
    index_entries.sort(key=lambda item: item["key"])
    index_core = {
        "kind": "paper-theater-index",
        "schema_version": "1.0",
        "variant_set_ref": variant_set["id"],
        "intent": variant_set["intent"],
        "entries": index_entries,
    }
    index = {"id": content_identifier("paper-theater-index", index_core, 20), **index_core}
    index_payload = _json_bytes(index)
    file_payloads[PAPER_THEATER_INDEX] = index_payload

    package_core = {
        "kind": "variant-export-package",
        "schema_version": "1.0",
        "variant_set_ref": variant_set["id"],
        "intent": variant_set["intent"],
        "source_candidate_ref": variant_set["source_candidate_ref"],
        "source_candidate_sha256": variant_set["source_candidate_sha256"],
        "source_request_ref": variant_set["source_request_ref"],
        "review_ref": variant_set["review_ref"],
        "character_ref": variant_set["character_ref"],
        "style_ref": variant_set["style_ref"],
        "license_status": variant_set["license_status"],
        "paper_theater_index_path": PAPER_THEATER_INDEX,
        "paper_theater_index_sha256": _sha(index_payload),
        "items": items,
    }
    package = {"id": content_identifier("variant-export-package", package_core, 20), **package_core}
    package_payload = _json_bytes(package)
    file_payloads[PACKAGE_MANIFEST] = package_payload

    result = {
        "ok": True,
        "write": write,
        "package_directory": package["id"],
        "package": package,
        "paper_theater_index": index,
        "files": sorted(file_payloads),
        "published": False,
        "idempotent": False,
    }
    if not write:
        return result

    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve()
    final = output / package["id"]
    if final.exists():
        existing = _inventory_files(final)
        if existing != file_payloads:
            raise ExportError("OUTPUT_CONFLICT", "existing package differs from requested package", str(final))
        result["idempotent"] = True
        return result

    staging_path: Path | None = Path(tempfile.mkdtemp(prefix=f".{package['id']}.", dir=output))
    try:
        for relative, payload in file_payloads.items():
            destination = _safe_join(staging_path, relative, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        if _inventory_files(staging_path) != file_payloads:
            raise ExportError("STAGING_MISMATCH", "staged package differs before publication", str(staging_path))
        os.replace(staging_path, final)
        staging_path = None
        result["published"] = True
        return result
    finally:
        if staging_path is not None and staging_path.exists():
            shutil.rmtree(staging_path, ignore_errors=True)


def check_export_package(package_manifest_path: Path, output_root: Path) -> dict[str, Any]:
    package = _load_object(package_manifest_path)
    _validate_package_schema(package, str(package_manifest_path))
    identifier = package.get("id")
    if not isinstance(identifier, str) or not TOKEN_RE.fullmatch(identifier):
        raise ExportError("PACKAGE_ID", "invalid package ID", "id")
    core = {key: value for key, value in package.items() if key != "id"}
    if content_identifier("variant-export-package", core, 20) != identifier:
        raise ExportError("PACKAGE_ID_MISMATCH", "package ID is not canonical", "id")

    output = _resolved_directory(output_root, must_exist=True, field="output_root")
    package_root = output / identifier
    expected_manifest = package_root / PACKAGE_MANIFEST
    if package_manifest_path.resolve() != expected_manifest.resolve():
        raise ExportError("PACKAGE_LOCATION", "manifest is not in its canonical package directory", str(package_manifest_path))
    inventory = _inventory_files(package_root)
    if inventory.get(PACKAGE_MANIFEST) != _json_bytes(package):
        raise ExportError("PACKAGE_CANONICAL", "package manifest is not canonical", PACKAGE_MANIFEST)

    expected_files = {PACKAGE_MANIFEST, package["paper_theater_index_path"]}
    index_path = package["paper_theater_index_path"]
    index_sha = package["paper_theater_index_sha256"]
    index_payload = inventory.get(index_path)
    if index_payload is None or _sha(index_payload) != index_sha:
        raise ExportError("INDEX_CHECKSUM_MISMATCH", "paper-theater index is missing or modified", index_path)
    index = _load_object_bytes(index_payload, index_path)
    _require_exact_fields(index, INDEX_FIELDS, "INDEX_SCHEMA", index_path)
    if index.get("kind") != "paper-theater-index" or index.get("schema_version") != "1.0":
        raise ExportError("INDEX_SCHEMA", "invalid paper-theater index kind or schema version", index_path)
    index_identifier = index.get("id")
    if not isinstance(index_identifier, str) or not TOKEN_RE.fullmatch(index_identifier):
        raise ExportError("INDEX_ID", "invalid paper-theater index ID", index_path)
    index_core = {key: value for key, value in index.items() if key != "id"}
    if content_identifier("paper-theater-index", index_core, 20) != index_identifier:
        raise ExportError("INDEX_ID_MISMATCH", "paper-theater index ID is not canonical", index_path)
    if _json_bytes(index) != index_payload:
        raise ExportError("INDEX_CANONICAL", "paper-theater index JSON is not canonical", index_path)
    if index.get("variant_set_ref") != package.get("variant_set_ref"):
        raise ExportError("INDEX_BINDING_MISMATCH", "paper-theater index variant set differs from package", index_path)
    if index.get("intent") != package.get("intent"):
        raise ExportError("INDEX_BINDING_MISMATCH", "paper-theater index intent differs from package", index_path)

    items = package["items"]
    production = package["intent"] == "production"
    if production and package["license_status"] != "approved":
        raise ExportError(
            "PRODUCTION_LICENSE_NOT_APPROVED",
            "production package licensing must remain approved",
            "license_status",
        )
    expected_index_entries: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            raise ExportError("PACKAGE_ITEM", "package item must be an object", "items")
        present_review_fields = {field for field in VARIANT_REVIEW_SIDECAR_FIELDS if field in item}
        if production and present_review_fields != set(VARIANT_REVIEW_BINDING_FIELDS):
            raise ExportError("PRODUCTION_VARIANT_REVIEW_REQUIRED", "production package item lacks complete variant review binding", "items")
        if not production and present_review_fields:
            raise ExportError("EVALUATION_REVIEW_CLAIM", "evaluation package must not contain variant approval fields", "items")
        expected_item_fields = ITEM_BASE_FIELDS | (
            set(VARIANT_REVIEW_BINDING_FIELDS) if production else set()
        )
        _require_exact_fields(item, expected_item_fields, "PACKAGE_ITEM", "items")

        item_payloads: dict[str, bytes] = {}
        normalized_paths: dict[str, str] = {}
        for path_field, sha_field in (("png_path", "png_sha256"), ("sidecar_path", "sidecar_sha256")):
            path = _safe_relative(item.get(path_field), path_field)
            expected_sha = item.get(sha_field)
            if not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
                raise ExportError("PACKAGE_ITEM_REFERENCE", "invalid package item checksum", sha_field)
            payload = inventory.get(path)
            if payload is None or _sha(payload) != expected_sha:
                raise ExportError("PACKAGE_FILE_MISMATCH", f"missing or modified file: {path}", path)
            expected_files.add(path)
            item_payloads[path_field] = payload
            normalized_paths[path_field] = path

        variant_id = item.get("variant_id")
        if not isinstance(variant_id, str) or not TOKEN_RE.fullmatch(variant_id):
            raise ExportError("VARIANT_ID", "package item has invalid variant ID", "variant_id")
        paper_theater_key = item.get("paper_theater_key")
        if not isinstance(paper_theater_key, str) or not paper_theater_key:
            raise ExportError("PAPER_THEATER_KEY", "package item has invalid paper-theater key", "paper_theater_key")
        expected_index_entries.append({
            "key": paper_theater_key,
            "variant_id": variant_id,
            "png_path": normalized_paths["png_path"],
            "sidecar_path": normalized_paths["sidecar_path"],
            "sha256": item["png_sha256"],
        })
        sidecar_path = normalized_paths["sidecar_path"]
        sidecar = _load_object_bytes(item_payloads["sidecar_path"], sidecar_path)
        _validate_sidecar_schema(sidecar, sidecar_path)
        sidecar_checks = {
            "variant_set_ref": package["variant_set_ref"],
            "variant_id": variant_id,
            "paper_theater_key": paper_theater_key,
            "intent": package["intent"],
            "source_candidate_ref": package["source_candidate_ref"],
            "source_request_ref": package["source_request_ref"],
            "review_ref": package["review_ref"],
            "character_ref": package["character_ref"],
            "style_ref": package["style_ref"],
            "source_file": f"{variant_id}.png",
            "source_sha256": item.get("png_sha256"),
            "output_path": normalized_paths["png_path"],
            "output_sha256": item.get("png_sha256"),
            "license_status": package["license_status"],
        }
        for field, expected in sidecar_checks.items():
            if sidecar.get(field) != expected:
                raise ExportError("SIDECAR_BINDING_MISMATCH", f"sidecar {field} does not match package item", sidecar_path)
        expected_provenance = {
            "method": "verified-byte-copy",
            "source_candidate_sha256": package["source_candidate_sha256"],
        }
        if sidecar.get("provenance") != expected_provenance:
            raise ExportError(
                "SIDECAR_BINDING_MISMATCH",
                "sidecar provenance does not match package",
                sidecar_path,
            )
        _validate_packaged_png(item_payloads["png_path"], sidecar, normalized_paths["png_path"])

        if production:
            review_path = _safe_relative(item.get("variant_review_path"), "variant_review_path")
            review_sha = item.get("variant_review_sha256")
            if not isinstance(review_sha, str) or not SHA256_RE.fullmatch(review_sha):
                raise ExportError("PRODUCTION_VARIANT_REVIEW_REQUIRED", "invalid variant review checksum", "variant_review_sha256")
            review_payload = inventory.get(review_path)
            if review_payload is None or _sha(review_payload) != review_sha:
                raise ExportError("VARIANT_REVIEW_FILE_MISMATCH", "variant review file is missing or modified", review_path)
            review_object = _load_object_bytes(review_payload, review_path)
            binding, canonical_payload = _validate_variant_review_object(
                review_object,
                field=review_path,
                variant_set_id=package["variant_set_ref"],
                variant_id=variant_id,
                png_sha256=item["png_sha256"],
            )
            if canonical_payload != review_payload:
                raise ExportError("VARIANT_REVIEW_CANONICAL", "variant review file is not canonical", review_path)
            for field in VARIANT_REVIEW_BINDING_FIELDS:
                if item.get(field) != binding.get(field) or sidecar.get(field) != binding.get(field):
                    raise ExportError("VARIANT_REVIEW_BINDING_MISMATCH", f"{field} is not bound consistently", review_path)
            if sidecar.get("variant_reviewer") != binding.get("variant_reviewer"):
                raise ExportError("VARIANT_REVIEW_BINDING_MISMATCH", "reviewer is not bound consistently", review_path)
            expected_files.add(review_path)

    _ensure_unique(
        [entry["key"] for entry in expected_index_entries],
        "DUPLICATE_PAPER_THEATER_KEY",
        "paper-theater keys",
    )
    _ensure_unique(
        [entry["variant_id"] for entry in expected_index_entries],
        "DUPLICATE_VARIANT_ID",
        "variant IDs",
    )
    _ensure_unique(
        [entry["png_path"] for entry in expected_index_entries]
        + [entry["sidecar_path"] for entry in expected_index_entries],
        "OUTPUT_PATH_COLLISION",
        "output paths",
    )
    expected_index_entries.sort(key=lambda entry: entry["key"])
    if index.get("entries") != expected_index_entries:
        raise ExportError(
            "INDEX_BINDING_MISMATCH",
            "paper-theater index entries are not the exact package-item projection",
            index_path,
        )

    extras = sorted(set(inventory) - expected_files)
    missing = sorted(expected_files - set(inventory))
    if extras or missing:
        raise ExportError("PACKAGE_INVENTORY_MISMATCH", f"missing={missing}; extra={extras}", str(package_root))
    return {"ok": True, "package": package, "file_count": len(inventory)}
