"""Deterministic technical preparation of owner-approved production variant PNGs.

This module performs only transparent-margin cropping and pixel-exact placement on a
caller-authored transparent canvas. It never resynthesizes or aesthetically repairs
artwork.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
from typing import Any, Sequence
import zlib

from .exporter import PACKAGE_MANIFEST, check_export_package
from .naming import SHA256_RE, VERSION_RE, canonical_json, content_identifier, safe_relative_path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_DIMENSION = 8192
MAX_PIXELS = 64 * 1024 * 1024
MAX_PNG_BYTES = 128 * 1024 * 1024
PROFILE_FIELDS = {"kind", "schema_version", "id", "version", "roles"}
ROLE_FIELDS = {
    "role",
    "target_canvas_width",
    "target_canvas_height",
    "target_anchor_x",
    "target_anchor_y",
    "source_anchor_policy",
    "transparent_border_inset",
    "max_border_alpha_pixels",
    "min_visible_pixels",
    "max_semitransparent_fraction",
}
FRACTION_FIELDS = {"numerator", "denominator"}
IDENTITY_FIELDS = (
    "identity_gate",
    "identity_review_ref",
    "identity_review_sha256",
    "identity_strategy_id",
    "identity_evidence_run_ids",
    "identity_model",
)
ROLES = ("boke", "tsukkomi")


@dataclass(frozen=True)
class AssetPrepError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


@dataclass(frozen=True)
class DecodedPng:
    width: int
    height: int
    rgba: bytes


@dataclass(frozen=True)
class PreparedItem:
    manifest_item: dict[str, Any]
    output_path: str
    output_bytes: bytes


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _load_json_file(path: Path, field: str) -> tuple[dict[str, Any], bytes]:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise AssetPrepError("SYMLINK_FILE", f"{field} must not be a symlink", field)
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise AssetPrepError("FILE_MISSING", str(exc), field) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise AssetPrepError("FILE_TYPE", f"{field} must be a regular file", field)
    try:
        payload = resolved.read_bytes()
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetPrepError("JSON", str(exc), field) from exc
    if not isinstance(value, dict):
        raise AssetPrepError("OBJECT_REQUIRED", f"{field} root must be an object", field)
    return value, payload


def _exact_fields(value: dict[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise AssetPrepError("FIELDS", f"missing={missing}; extra={extra}", field)


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise AssetPrepError("INTEGER", f"{field} must be an integer in {minimum}..{maximum}", field)
    return value


def _profile_core(profile: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in profile.items() if key != "id"}


def expected_profile_id(profile: dict[str, Any]) -> str:
    return content_identifier("asset-prep-profile", _profile_core(profile), 16)


def profile_sha256(profile: dict[str, Any]) -> str:
    return _sha(canonical_json(profile))


def validate_profile(profile: dict[str, Any]) -> dict[str, dict[str, Any]]:
    _exact_fields(profile, PROFILE_FIELDS, "profile")
    if profile.get("kind") != "asset-prep-profile" or profile.get("schema_version") != "1.0":
        raise AssetPrepError("PROFILE_SCHEMA", "invalid profile kind/schema_version", "profile")
    if not isinstance(profile.get("version"), str) or not VERSION_RE.fullmatch(profile["version"]):
        raise AssetPrepError("PROFILE_VERSION", "profile version must use vNNN", "version")
    if profile.get("id") != expected_profile_id(profile):
        raise AssetPrepError("PROFILE_ID", "profile ID is not content-derived from the canonical profile", "id")
    roles = profile.get("roles")
    if not isinstance(roles, list) or len(roles) != 2:
        raise AssetPrepError("PROFILE_ROLES", "profile must contain exactly two role configurations", "roles")
    by_role: dict[str, dict[str, Any]] = {}
    for index, config in enumerate(roles):
        field = f"roles[{index}]"
        if not isinstance(config, dict):
            raise AssetPrepError("PROFILE_ROLE", "role configuration must be an object", field)
        _exact_fields(config, ROLE_FIELDS, field)
        role = config.get("role")
        if role not in ROLES or role in by_role:
            raise AssetPrepError("PROFILE_ROLE", "roles must contain boke and tsukkomi exactly once", f"{field}.role")
        width = _bounded_int(config.get("target_canvas_width"), f"{field}.target_canvas_width", minimum=1, maximum=MAX_DIMENSION)
        height = _bounded_int(config.get("target_canvas_height"), f"{field}.target_canvas_height", minimum=1, maximum=MAX_DIMENSION)
        if width * height > MAX_PIXELS:
            raise AssetPrepError("CANVAS_SIZE", "target canvas exceeds bounded pixel count", field)
        _bounded_int(config.get("target_anchor_x"), f"{field}.target_anchor_x", minimum=0, maximum=width - 1)
        _bounded_int(config.get("target_anchor_y"), f"{field}.target_anchor_y", minimum=0, maximum=height - 1)
        if config.get("source_anchor_policy") != "bottom-center-visible-bounds":
            raise AssetPrepError("ANCHOR_POLICY", "only bottom-center-visible-bounds is supported", f"{field}.source_anchor_policy")
        _bounded_int(config.get("transparent_border_inset"), f"{field}.transparent_border_inset", minimum=0, maximum=min(width, height))
        _bounded_int(config.get("max_border_alpha_pixels"), f"{field}.max_border_alpha_pixels", minimum=0, maximum=MAX_PIXELS)
        _bounded_int(config.get("min_visible_pixels"), f"{field}.min_visible_pixels", minimum=1, maximum=MAX_PIXELS)
        ratio = config.get("max_semitransparent_fraction")
        if not isinstance(ratio, dict):
            raise AssetPrepError("FRACTION", "max_semitransparent_fraction must be an object", f"{field}.max_semitransparent_fraction")
        _exact_fields(ratio, FRACTION_FIELDS, f"{field}.max_semitransparent_fraction")
        numerator = _bounded_int(ratio.get("numerator"), f"{field}.max_semitransparent_fraction.numerator", minimum=0, maximum=10**9)
        denominator = _bounded_int(ratio.get("denominator"), f"{field}.max_semitransparent_fraction.denominator", minimum=1, maximum=10**9)
        if numerator > denominator:
            raise AssetPrepError("FRACTION", "maximum semitransparent fraction must be between zero and one", f"{field}.max_semitransparent_fraction")
        by_role[role] = config
    if set(by_role) != set(ROLES):
        raise AssetPrepError("PROFILE_ROLES", "profile must contain boke and tsukkomi", "roles")
    return by_role


def load_profile(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    profile, _ = _load_json_file(path, "profile")
    return profile, validate_profile(profile)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _decode_scanlines(raw: bytes, width: int, height: int, channels: int) -> bytes:
    stride = width * channels
    expected = (stride + 1) * height
    if len(raw) != expected:
        raise AssetPrepError("PNG_DECOMPRESSED_SIZE", f"expected {expected} decompressed bytes, got {len(raw)}", "png")
    rows: list[bytes] = []
    previous = bytearray(stride)
    offset = 0
    for row_index in range(height):
        filter_type = raw[offset]
        offset += 1
        scan = raw[offset:offset + stride]
        offset += stride
        recon = bytearray(stride)
        if filter_type not in {0, 1, 2, 3, 4}:
            raise AssetPrepError("PNG_FILTER", f"unsupported PNG filter {filter_type}", f"row[{row_index}]")
        for index, value in enumerate(scan):
            left = recon[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            else:
                predictor = _paeth(left, up, upper_left)
            recon[index] = (value + predictor) & 0xFF
        rows.append(bytes(recon))
        previous = recon
    return b"".join(rows)


def decode_png(payload: bytes) -> DecodedPng:
    if not isinstance(payload, bytes) or len(payload) < 8 or len(payload) > MAX_PNG_BYTES:
        raise AssetPrepError("PNG_SIZE", f"PNG size must be 8..{MAX_PNG_BYTES} bytes", "png")
    if payload[:8] != PNG_SIGNATURE:
        raise AssetPrepError("PNG_SIGNATURE", "invalid PNG signature", "png")
    offset = 8
    ihdr: tuple[int, int, int] | None = None
    saw_srgb = False
    saw_iend = False
    saw_idat = False
    idat_ended = False
    compressed_parts: list[bytes] = []
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise AssetPrepError("PNG_STRUCTURE", "truncated PNG chunk", "png")
        length = struct.unpack(">I", payload[offset:offset + 4])[0]
        chunk_type = payload[offset + 4:offset + 8]
        end = offset + 12 + length
        if length > MAX_PNG_BYTES or end > len(payload):
            raise AssetPrepError("PNG_STRUCTURE", "invalid PNG chunk length", "png")
        data = payload[offset + 8:offset + 8 + length]
        stored_crc = struct.unpack(">I", payload[offset + 8 + length:end])[0]
        actual_crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        if stored_crc != actual_crc:
            raise AssetPrepError("PNG_CRC", f"CRC mismatch for {chunk_type!r}", "png")
        if chunk_type in {b"acTL", b"fcTL", b"fdAT"}:
            raise AssetPrepError("PNG_ANIMATION", "animated PNG is not supported", "png")
        if chunk_type == b"IHDR":
            if ihdr is not None or offset != 8 or length != 13:
                raise AssetPrepError("PNG_IHDR", "IHDR must be the first unique 13-byte chunk", "png")
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", data)
            if width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
                raise AssetPrepError("PNG_DIMENSIONS", "PNG dimensions exceed bounded limits", "png")
            if bit_depth != 8 or color_type not in {4, 6}:
                raise AssetPrepError("PNG_COLOR", "only 8-bit grayscale-alpha or RGBA PNG is supported", "png")
            if compression != 0 or filter_method != 0 or interlace != 0:
                raise AssetPrepError("PNG_ENCODING", "only standard non-interlaced PNG encoding is supported", "png")
            ihdr = (width, height, color_type)
        elif chunk_type == b"sRGB":
            if ihdr is None or saw_idat or saw_srgb or length != 1:
                raise AssetPrepError("PNG_SRGB", "sRGB must be one pre-IDAT one-byte chunk", "png")
            saw_srgb = True
        elif chunk_type == b"IDAT":
            if ihdr is None or idat_ended:
                raise AssetPrepError("PNG_IDAT", "IDAT chunks must be contiguous after IHDR", "png")
            saw_idat = True
            compressed_parts.append(data)
        elif chunk_type == b"IEND":
            if length != 0 or ihdr is None or not saw_idat or saw_iend:
                raise AssetPrepError("PNG_IEND", "invalid IEND chunk", "png")
            saw_iend = True
            offset = end
            if offset != len(payload):
                raise AssetPrepError("PNG_TRAILING_DATA", "PNG contains trailing bytes after IEND", "png")
            break
        else:
            if saw_idat:
                idat_ended = True
            if chunk_type and 65 <= chunk_type[0] <= 90:
                raise AssetPrepError("PNG_CRITICAL_CHUNK", f"unsupported critical chunk {chunk_type!r}", "png")
        offset = end
    if ihdr is None or not saw_srgb or not saw_idat or not saw_iend:
        raise AssetPrepError("PNG_STRUCTURE", "PNG requires IHDR, sRGB, IDAT, and IEND", "png")
    width, height, color_type = ihdr
    channels = 4 if color_type == 6 else 2
    expected = (width * channels + 1) * height
    if expected > MAX_PNG_BYTES:
        raise AssetPrepError("PNG_DECOMPRESSED_SIZE", "decompressed PNG exceeds bounded limit", "png")
    compressed = b"".join(compressed_parts)
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, expected + 1)
        if len(raw) > expected or decompressor.unconsumed_tail:
            raise AssetPrepError("PNG_DECOMPRESSED_SIZE", "PNG decompresses beyond expected size", "png")
        raw += decompressor.flush()
    except zlib.error as exc:
        raise AssetPrepError("PNG_ZLIB", str(exc), "png") from exc
    if not decompressor.eof or decompressor.unused_data or len(raw) != expected:
        raise AssetPrepError("PNG_ZLIB", "compressed stream is incomplete, concatenated, or wrong-sized", "png")
    pixels = _decode_scanlines(raw, width, height, channels)
    if color_type == 6:
        rgba = pixels
    else:
        expanded = bytearray(width * height * 4)
        for index in range(width * height):
            gray = pixels[index * 2]
            alpha = pixels[index * 2 + 1]
            out = index * 4
            expanded[out:out + 4] = bytes((gray, gray, gray, alpha))
        rgba = bytes(expanded)
    return DecodedPng(width=width, height=height, rgba=rgba)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_rgba_png(width: int, height: int, rgba: bytes) -> bytes:
    if width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION or width * height > MAX_PIXELS:
        raise AssetPrepError("PNG_DIMENSIONS", "output PNG dimensions exceed bounded limits", "output")
    if len(rgba) != width * height * 4:
        raise AssetPrepError("PIXEL_COUNT", "RGBA byte count does not match output dimensions", "output")
    rows = bytearray()
    stride = width * 4
    for y in range(height):
        rows.append(0)
        start = y * stride
        rows.extend(rgba[start:start + stride])
    compressed = zlib.compress(bytes(rows), level=9)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"sRGB", b"\x00") + _png_chunk(b"IDAT", compressed) + _png_chunk(b"IEND", b"")


def _pixel(rgba: bytes, width: int, x: int, y: int) -> bytes:
    offset = (y * width + x) * 4
    return rgba[offset:offset + 4]


def _metrics(image: DecodedPng, config: dict[str, Any]) -> tuple[dict[str, Any], tuple[int, int, int, int]]:
    visible = 0
    semi = 0
    border = 0
    min_x = image.width
    min_y = image.height
    max_x = -1
    max_y = -1
    inset = config["transparent_border_inset"]
    for y in range(image.height):
        for x in range(image.width):
            alpha = image.rgba[(y * image.width + x) * 4 + 3]
            if alpha == 0:
                continue
            visible += 1
            if alpha < 255:
                semi += 1
            min_x = min(min_x, x)
            min_y = min(min_y, y)
            max_x = max(max_x, x)
            max_y = max(max_y, y)
            if inset > 0 and (x < inset or y < inset or x >= image.width - inset or y >= image.height - inset):
                border += 1
    if visible == 0:
        raise AssetPrepError("NO_VISIBLE_CONTENT", "source PNG has no alpha>0 pixels", "png")
    if visible < config["min_visible_pixels"]:
        raise AssetPrepError("VISIBLE_PIXEL_MINIMUM", "visible pixel count is below profile minimum", "png")
    if border > config["max_border_alpha_pixels"]:
        raise AssetPrepError("BORDER_ALPHA", "nonzero alpha in the configured border band exceeds the profile limit", "png")
    maximum = config["max_semitransparent_fraction"]
    if semi * maximum["denominator"] > maximum["numerator"] * visible:
        raise AssetPrepError("SEMITRANSPARENT_FRACTION", "semitransparent visible pixels exceed the profile limit", "png")
    fraction = Fraction(semi, visible)
    bbox = (min_x, min_y, max_x, max_y)
    return {
        "visible_bbox": {"x": min_x, "y": min_y, "width": max_x - min_x + 1, "height": max_y - min_y + 1},
        "visible_pixels": visible,
        "semitransparent_pixels": semi,
        "semitransparent_fraction": {"numerator": fraction.numerator, "denominator": fraction.denominator},
        "border_alpha_pixels": border,
    }, bbox


def _normalize(image: DecodedPng, bbox: tuple[int, int, int, int], config: dict[str, Any]) -> tuple[bytes, dict[str, int], dict[str, int]]:
    min_x, min_y, max_x, max_y = bbox
    crop_width = max_x - min_x + 1
    crop_height = max_y - min_y + 1
    source_anchor_x = (crop_width - 1) // 2
    source_anchor_y = crop_height - 1
    destination_x = config["target_anchor_x"] - source_anchor_x
    destination_y = config["target_anchor_y"] - source_anchor_y
    target_width = config["target_canvas_width"]
    target_height = config["target_canvas_height"]
    if destination_x < 0 or destination_y < 0 or destination_x + crop_width > target_width or destination_y + crop_height > target_height:
        raise AssetPrepError("CROP_FIT", "visible crop cannot fit target canvas at the requested anchor", "target_anchor")
    output = bytearray(target_width * target_height * 4)
    for crop_y in range(crop_height):
        source_y = min_y + crop_y
        for crop_x in range(crop_width):
            source_x = min_x + crop_x
            source_offset = (source_y * image.width + source_x) * 4
            target_x = destination_x + crop_x
            target_y = destination_y + crop_y
            target_offset = (target_y * target_width + target_x) * 4
            output[target_offset:target_offset + 4] = image.rgba[source_offset:source_offset + 4]
    encoded = encode_rgba_png(target_width, target_height, bytes(output))
    decoded = decode_png(encoded)
    source_visible = 0
    output_visible = 0
    for y in range(image.height):
        for x in range(image.width):
            source_pixel = _pixel(image.rgba, image.width, x, y)
            if source_pixel[3] == 0:
                continue
            source_visible += 1
            target_x = destination_x + (x - min_x)
            target_y = destination_y + (y - min_y)
            if _pixel(decoded.rgba, decoded.width, target_x, target_y) != source_pixel:
                raise AssetPrepError("PIXEL_PRESERVATION", "visible source pixel changed during normalization", "output")
    for index in range(decoded.width * decoded.height):
        if decoded.rgba[index * 4 + 3] > 0:
            output_visible += 1
    if output_visible != source_visible:
        raise AssetPrepError("PIXEL_PRESERVATION", "visible pixel count changed during normalization", "output")
    return encoded, {"x": min_x, "y": min_y, "width": crop_width, "height": crop_height}, {"x": destination_x, "y": destination_y}


def _resolved_root(path: Path, field: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise AssetPrepError("SYMLINK_ROOT", f"{field} must not be a symlink", field)
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise AssetPrepError("ROOT_MISSING", str(exc), field) from exc
    if not resolved.is_dir():
        raise AssetPrepError("ROOT_TYPE", f"{field} must be a directory", field)
    return resolved


def _resolve_under(root: Path, relative: Any, field: str) -> Path:
    if not isinstance(relative, str):
        raise AssetPrepError("UNSAFE_PATH", "relative path must be a string", field)
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise AssetPrepError("UNSAFE_PATH", str(exc), field) from exc
    lexical = root.joinpath(*safe.parts)
    current = lexical
    while current != root:
        if current.is_symlink():
            raise AssetPrepError("SYMLINK_PATH", f"{field} contains a symlink", field)
        current = current.parent
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AssetPrepError("PATH_MISSING", str(exc), field) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise AssetPrepError("FILE_TYPE", f"{field} must be a regular file", field)
    return resolved


def _package_role(package: dict[str, Any]) -> str:
    items = package.get("items")
    if not isinstance(items, list) or not items:
        raise AssetPrepError("PACKAGE_ITEMS", "verified production package has no items", "items")
    roles: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise AssetPrepError("PACKAGE_ITEMS", "package item must be an object", "items")
        key = item.get("paper_theater_key")
        if not isinstance(key, str) or "." not in key:
            raise AssetPrepError("PACKAGE_ROLE", "paper-theater key does not expose a role", "paper_theater_key")
        roles.add(key.split(".", 1)[0])
    if len(roles) != 1 or next(iter(roles)) not in ROLES:
        raise AssetPrepError("PACKAGE_ROLE", "package must contain exactly one boke or tsukkomi role", "items")
    return next(iter(roles))


def _identity_projection(package: dict[str, Any]) -> dict[str, Any]:
    return {field: copy_value(package.get(field)) for field in IDENTITY_FIELDS}


def copy_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: copy_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [copy_value(item) for item in value]
    return value


def _verify_package_manifest_location(package_manifest: Path, package_root: Path) -> tuple[Path, bytes]:
    root = _resolved_root(package_root, "package_root")
    expanded = package_manifest.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        raise AssetPrepError("PACKAGE_MANIFEST_SYMLINK", "package manifest must not be a symlink", "package_manifest")
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AssetPrepError("PACKAGE_MANIFEST_PATH", str(exc), "package_manifest") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise AssetPrepError("PACKAGE_MANIFEST_TYPE", "package manifest must be a regular file", "package_manifest")
    try:
        payload = resolved.read_bytes()
    except OSError as exc:
        raise AssetPrepError("PACKAGE_MANIFEST_READ", str(exc), "package_manifest") from exc
    return resolved, payload


def _prepare(profile_path: Path, package_manifest_path: Path, package_root: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    profile, role_configs = load_profile(profile_path)
    package_manifest, package_manifest_bytes = _verify_package_manifest_location(package_manifest_path, package_root)
    root = _resolved_root(package_root, "package_root")
    try:
        checked = check_export_package(package_manifest, root)
    except Exception as exc:
        if isinstance(exc, AssetPrepError):
            raise
        code = getattr(exc, "code", "PACKAGE_CHECK")
        message = getattr(exc, "message", str(exc))
        raise AssetPrepError(f"PACKAGE_{code}", message, "package_manifest") from exc
    package = checked.get("package")
    if not isinstance(package, dict):
        raise AssetPrepError("PACKAGE_RESULT", "package checker returned no package object", "package_manifest")
    if package.get("intent") != "production" or package.get("license_status") != "approved":
        raise AssetPrepError("PRODUCTION_PACKAGE_REQUIRED", "asset preparation requires a production package with approved licensing", "package_manifest")
    if package.get("identity_gate") != "owner-approved":
        raise AssetPrepError("IDENTITY_GATE_REQUIRED", "asset preparation requires owner-approved identity gate", "identity_gate")
    for field in ("identity_review_ref", "identity_review_sha256", "identity_strategy_id", "identity_model"):
        value = package.get(field)
        if value is None or value == "":
            raise AssetPrepError("IDENTITY_BINDING_REQUIRED", f"missing production identity binding: {field}", field)
    if not isinstance(package.get("identity_evidence_run_ids"), list) or not package["identity_evidence_run_ids"]:
        raise AssetPrepError("IDENTITY_BINDING_REQUIRED", "production identity evidence runs are required", "identity_evidence_run_ids")
    role = _package_role(package)
    config = role_configs[role]
    package_dir = package_manifest.parent
    prepared_items: list[PreparedItem] = []
    output_paths: set[str] = set()
    for item in sorted(package["items"], key=lambda entry: str(entry.get("variant_id", ""))):
        variant_id = item.get("variant_id")
        if not isinstance(variant_id, str) or not variant_id:
            raise AssetPrepError("VARIANT_ID", "package item lacks variant_id", "items")
        for field in ("variant_review_ref", "variant_review_path", "variant_review_sha256"):
            if not isinstance(item.get(field), str) or not item[field]:
                raise AssetPrepError("VARIANT_REVIEW_REQUIRED", f"production package item lacks {field}", field)
        png_path = _resolve_under(package_dir, item.get("png_path"), "png_path")
        sidecar_path = _resolve_under(package_dir, item.get("sidecar_path"), "sidecar_path")
        try:
            png_payload = png_path.read_bytes()
            sidecar_payload = sidecar_path.read_bytes()
        except OSError as exc:
            raise AssetPrepError("SOURCE_READ", str(exc), variant_id) from exc
        if item.get("png_sha256") != _sha(png_payload):
            raise AssetPrepError("SOURCE_CHECKSUM", "package PNG checksum changed", variant_id)
        if item.get("sidecar_sha256") != _sha(sidecar_payload):
            raise AssetPrepError("SIDECAR_CHECKSUM", "package sidecar checksum changed", variant_id)
        try:
            sidecar = json.loads(sidecar_payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AssetPrepError("SIDECAR_JSON", str(exc), variant_id) from exc
        if not isinstance(sidecar, dict):
            raise AssetPrepError("SIDECAR_JSON", "sidecar root must be an object", variant_id)
        decoded = decode_png(png_payload)
        if sidecar.get("width") != decoded.width or sidecar.get("height") != decoded.height:
            raise AssetPrepError("SOURCE_DIMENSIONS", "PNG dimensions no longer match verified sidecar", variant_id)
        metrics, bbox = _metrics(decoded, config)
        output_payload, crop_box, translation = _normalize(decoded, bbox, config)
        output_relative = f"prepared/{role}/{variant_id}.png"
        if output_relative in output_paths:
            raise AssetPrepError("OUTPUT_COLLISION", "duplicate prepared output path", output_relative)
        output_paths.add(output_relative)
        manifest_item = {
            "variant_id": variant_id,
            "paper_theater_key": item["paper_theater_key"],
            "variant_review_ref": item["variant_review_ref"],
            "variant_review_sha256": item["variant_review_sha256"],
            "source_path": item["png_path"],
            "source_sha256": item["png_sha256"],
            "source_width": decoded.width,
            "source_height": decoded.height,
            "output_path": output_relative,
            "output_sha256": _sha(output_payload),
            "output_width": config["target_canvas_width"],
            "output_height": config["target_canvas_height"],
            "metrics": metrics,
            "crop_box": crop_box,
            "translation": translation,
        }
        prepared_items.append(PreparedItem(manifest_item=manifest_item, output_path=output_relative, output_bytes=output_payload))
    manifest_core = {
        "kind": "asset-prep-manifest",
        "schema_version": "1.0",
        "source_package_ref": package["id"],
        "source_package_sha256": _sha(package_manifest_bytes),
        "source_variant_set_ref": package["variant_set_ref"],
        "source_variant_set_sha256": package["variant_set_sha256"],
        "profile_ref": profile["id"],
        "profile_version": profile["version"],
        "profile_sha256": profile_sha256(profile),
        "role": role,
        **_identity_projection(package),
        "items": [item.manifest_item for item in prepared_items],
    }
    manifest = {"id": content_identifier("asset-prep-manifest", manifest_core, 20), **manifest_core}
    payloads = {item.output_path: item.output_bytes for item in prepared_items}
    payloads["asset-prep-manifest.json"] = _json_bytes(manifest)
    return {
        "ok": True,
        "profile_id": profile["id"],
        "source_package_id": package["id"],
        "role": role,
        "manifest": manifest,
        "files": sorted(payloads),
    }, payloads


def check_asset_prep(profile_path: Path, package_manifest_path: Path, package_root: Path) -> dict[str, Any]:
    result, _ = _prepare(profile_path, package_manifest_path, package_root)
    return result


def _inventory(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise AssetPrepError("OUTPUT_SYMLINK", "prepared output must not contain symlinks", str(path))
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def build_asset_prep(profile_path: Path, package_manifest_path: Path, package_root: Path, output_dir: Path) -> dict[str, Any]:
    result, payloads = _prepare(profile_path, package_manifest_path, package_root)
    package_root_resolved = _resolved_root(package_root, "package_root")
    output = output_dir.expanduser()
    if output.is_symlink() or output.exists():
        raise AssetPrepError("OUTPUT_NOT_FRESH", "output directory must not already exist or be a symlink", "output_dir")
    parent = output.parent.resolve(strict=False)
    if not parent.exists() or not parent.is_dir() or parent.is_symlink():
        raise AssetPrepError("OUTPUT_PARENT", "output parent must be an existing non-symlink directory", "output_dir")
    resolved_output = output.resolve(strict=False)
    try:
        resolved_output.relative_to(package_root_resolved)
    except ValueError:
        pass
    else:
        raise AssetPrepError("ROOT_OVERLAP", "prepared output must not be inside the source package root", "output_dir")
    staging: Path | None = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=parent))
    try:
        for relative, payload in payloads.items():
            safe = safe_relative_path(relative)
            destination = staging.joinpath(*safe.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        if _inventory(staging) != payloads:
            raise AssetPrepError("STAGING_MISMATCH", "staged asset-prep package differs from planned bytes", "output_dir")
        for relative, payload in payloads.items():
            if relative.endswith(".png"):
                decoded = decode_png(payload)
                if decoded.width <= 0 or decoded.height <= 0:
                    raise AssetPrepError("OUTPUT_VERIFY", "prepared PNG failed post-encode verification", relative)
        os.replace(staging, output)
        staging = None
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {**result, "output_dir": str(output), "published": True}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic non-resynthesizing asset preparation")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "build"):
        item = sub.add_parser(command)
        item.add_argument("profile", type=Path)
        item.add_argument("package_manifest", type=Path)
        item.add_argument("--package-root", type=Path, required=True)
        if command == "build":
            item.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            result = check_asset_prep(args.profile, args.package_manifest, args.package_root)
        else:
            result = build_asset_prep(args.profile, args.package_manifest, args.package_root, args.output_dir)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 0
    except (AssetPrepError, OSError, ValueError) as exc:
        diagnostic = exc.to_dict() if isinstance(exc, AssetPrepError) else {"code": "ASSET_PREP_ERROR", "message": str(exc), "field": ""}
        print(json.dumps({"ok": False, "diagnostics": [diagnostic]}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
