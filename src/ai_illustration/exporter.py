"""Deterministic local export packages for reviewed variant PNGs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Any

from .naming import SHA256_RE, TOKEN_RE, canonical_json, content_identifier, safe_relative_path
from .validation import _parse_png
from .variants import check_variant_set

PACKAGE_MANIFEST = "package-manifest.json"
PAPER_THEATER_INDEX = "paper-theater-index.json"


@dataclass(frozen=True)
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


def _scan_source(root: Path, expected_names: set[str]) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ExportError("SOURCE_SYMLINK", "source tree must not contain symlinks", str(path))
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if "/" in relative or relative not in expected_names:
            raise ExportError("EXTRA_SOURCE_FILE", f"unexpected source file: {relative}", relative)
        if relative in found:
            raise ExportError("DUPLICATE_SOURCE", f"duplicate source file: {relative}", relative)
        found[relative] = path
    missing = sorted(expected_names - set(found))
    if missing:
        raise ExportError("SOURCE_MISSING", "missing source PNG(s): " + ", ".join(missing), "source_root")
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


def _inventory_files(root: Path) -> dict[str, bytes]:
    inventory: dict[str, bytes] = {}
    if root.is_symlink() or not root.is_dir():
        raise ExportError("PACKAGE_ROOT", "package directory is invalid", str(root))
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ExportError("PACKAGE_SYMLINK", "package must not contain symlinks", str(path))
        if path.is_file():
            inventory[path.relative_to(root).as_posix()] = path.read_bytes()
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
    write: bool = False,
) -> dict[str, Any]:
    variant_set = check_variant_set(variant_set_path, manifest_root)
    source = _resolved_directory(source_root, must_exist=True, field="source_root")
    output = _resolved_directory(output_root, must_exist=False, field="output_root")
    if source == output or _is_within(source, output) or _is_within(output, source):
        raise ExportError("ROOT_OVERLAP", "source and output roots must not overlap", "output_root")

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

    expected_source_names = {f"{identifier}.png" for identifier in variant_ids}
    source_files = _scan_source(source, expected_source_names)
    file_payloads: dict[str, bytes] = {}
    items: list[dict[str, Any]] = []
    index_entries: list[dict[str, Any]] = []

    for variant in sorted(variants, key=lambda item: str(item["id"])):
        identifier = variant["id"]
        source_name = f"{identifier}.png"
        png_payload, png_sha = _verify_supplied_png(source_files[source_name], variant)
        png_path = str(safe_relative_path(variant["path"]))
        sidecar_path = str(safe_relative_path(variant["sidecar_path"]))
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

    relative_files = sorted(file_payloads)
    result = {
        "ok": True,
        "write": write,
        "package_directory": package["id"],
        "package": package,
        "paper_theater_index": index,
        "files": relative_files,
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
    if package.get("kind") != "variant-export-package" or package.get("schema_version") != "1.0":
        raise ExportError("PACKAGE_SCHEMA", "invalid package kind or schema version", str(package_manifest_path))
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

    expected_files = {PACKAGE_MANIFEST, package.get("paper_theater_index_path", "")}
    index_path = package.get("paper_theater_index_path")
    index_sha = package.get("paper_theater_index_sha256")
    if not isinstance(index_path, str) or not isinstance(index_sha, str) or not SHA256_RE.fullmatch(index_sha):
        raise ExportError("INDEX_REFERENCE", "invalid paper-theater index reference", "paper_theater_index_path")
    index_payload = inventory.get(index_path)
    if index_payload is None or _sha(index_payload) != index_sha:
        raise ExportError("INDEX_CHECKSUM_MISMATCH", "paper-theater index is missing or modified", index_path)

    items = package.get("items")
    if not isinstance(items, list) or not items:
        raise ExportError("PACKAGE_ITEMS", "package items must be a non-empty list", "items")
    for item in items:
        if not isinstance(item, dict):
            raise ExportError("PACKAGE_ITEM", "package item must be an object", "items")
        for path_field, sha_field in (("png_path", "png_sha256"), ("sidecar_path", "sidecar_sha256")):
            path = item.get(path_field)
            expected_sha = item.get(sha_field)
            if not isinstance(path, str) or not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
                raise ExportError("PACKAGE_ITEM_REFERENCE", "invalid package item reference", path_field)
            safe_relative_path(path)
            payload = inventory.get(path)
            if payload is None or _sha(payload) != expected_sha:
                raise ExportError("PACKAGE_FILE_MISMATCH", f"missing or modified file: {path}", path)
            expected_files.add(path)
    extras = sorted(set(inventory) - expected_files)
    missing = sorted(expected_files - set(inventory))
    if extras or missing:
        raise ExportError("PACKAGE_INVENTORY_MISMATCH", f"missing={missing}; extra={extras}", str(package_root))
    return {"ok": True, "package": package, "file_count": len(inventory)}
