"""Deterministic local RGBA frame rendering from verified renderer jobs."""

from __future__ import annotations

from dataclasses import dataclass
import binascii
import hashlib
import json
from pathlib import Path
import shutil
import struct
from typing import Any
import zlib

from .composition import (
    SOURCE_BINDINGS,
    SPAN_TRANSFORMS,
    CompositionError,
    check_composition_job_package,
)
from .naming import SHA256_RE, canonical_json, content_identifier, safe_relative_path

FRAME_INVENTORY = "frame-inventory.json"
FRAME_RENDER_MANIFEST = "frame-render-manifest.json"
FRAMES_DIR = "frames"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CANVAS = 8192
MAX_FRAME_COUNT = 100_000
MAX_TOTAL_OUTPUT_PIXELS = 64_000_000
MAX_TOTAL_DECODED_BYTES = 256 * 1024 * 1024
MAX_PNG_BYTES = 128 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024 * 1024


@dataclass
class FrameRenderError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


@dataclass(frozen=True)
class RGBAImage:
    width: int
    height: int
    pixels: bytes


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise FrameRenderError("DUPLICATE_KEY", f"duplicate JSON key: {key}", str(path))
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except FrameRenderError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrameRenderError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise FrameRenderError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _reject_lexical(path: Path, field: str) -> Path:
    raw = str(path)
    if "\x00" in raw or "\\" in raw:
        raise FrameRenderError("UNSAFE_PATH", f"{field} contains a forbidden path character", field)
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise FrameRenderError("UNSAFE_PATH", f"{field} must not contain parent traversal", field)
    return expanded


def _reject_symlink_components(path: Path, field: str) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    for candidate in (lexical, *lexical.parents):
        try:
            if candidate.exists() and candidate.is_symlink():
                raise FrameRenderError("PATH_SYMLINK", f"{field} contains a symlink component", field)
        except OSError as exc:
            raise FrameRenderError("PATH_ERROR", str(exc), field) from exc


def _root(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = _reject_lexical(path, field)
    _reject_symlink_components(expanded, field)
    if must_exist and not expanded.is_dir():
        raise FrameRenderError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise FrameRenderError("ROOT_TYPE", f"{field} must be a directory", field)
    try:
        return expanded.resolve()
    except OSError as exc:
        raise FrameRenderError("PATH_ERROR", str(exc), field) from exc


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_file(root: Path, relative: str, field: str) -> tuple[str, Path]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise FrameRenderError("UNSAFE_PATH", str(exc), field) from exc
    candidate = root.joinpath(*safe.parts)
    current = root
    for part in safe.parts:
        current = current / part
        if current.is_symlink():
            raise FrameRenderError("PATH_SYMLINK", f"{field} contains a symlink component", field)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FrameRenderError("FILE_MISSING", str(exc), field) from exc
    if not _within(root, resolved):
        raise FrameRenderError("PATH_ESCAPE", f"{field} escapes configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise FrameRenderError("FILE_TYPE", f"{field} must be a regular file", field)
    return safe.as_posix(), resolved


def _relative_file(path: Path, root: Path, field: str) -> tuple[str, Path]:
    expanded = _reject_lexical(path, field)
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        relative = lexical.relative_to(root).as_posix()
    except ValueError as exc:
        raise FrameRenderError("PATH_ESCAPE", f"{field} must be beneath its configured root", field) from exc
    return _safe_file(root, relative, field)


def _bounded_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise FrameRenderError("INTEGER_RANGE", f"{field} must be from {minimum} to {maximum}", field)
    return value


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)


def encode_rgba_png(image: RGBAImage) -> bytes:
    if len(image.pixels) != image.width * image.height * 4:
        raise FrameRenderError("PIXEL_LENGTH", "RGBA pixel buffer length is invalid", "pixels")
    rows = bytearray()
    stride = image.width * 4
    for y in range(image.height):
        rows.append(0)
        start = y * stride
        rows.extend(image.pixels[start : start + stride])
    ihdr = struct.pack(">IIBBBBB", image.width, image.height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"sRGB", b"\x00") + _chunk(b"IDAT", zlib.compress(bytes(rows), level=9)) + _chunk(b"IEND", b"")


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    da = abs(estimate - left)
    db = abs(estimate - up)
    dc = abs(estimate - up_left)
    if da <= db and da <= dc:
        return left
    if db <= dc:
        return up
    return up_left


def decode_rgba_png(payload: bytes, *, expected_width: int | None = None, expected_height: int | None = None) -> RGBAImage:
    if len(payload) > MAX_PNG_BYTES:
        raise FrameRenderError("PNG_TOO_LARGE", "PNG exceeds the configured byte limit", "png")
    if not payload.startswith(PNG_SIGNATURE):
        raise FrameRenderError("PNG_SIGNATURE", "invalid PNG signature", "png")
    offset = len(PNG_SIGNATURE)
    width = height = None
    seen_ihdr = seen_srgb = seen_iend = False
    seen_idat = False
    idat_closed = False
    compressed = bytearray()
    while offset < len(payload):
        if len(payload) - offset < 12:
            raise FrameRenderError("PNG_STRUCTURE", "truncated PNG chunk", "png")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        if length > MAX_CHUNK_BYTES:
            raise FrameRenderError("PNG_CHUNK_LIMIT", "PNG chunk exceeds configured limit", "png")
        end = offset + 12 + length
        if end > len(payload):
            raise FrameRenderError("PNG_STRUCTURE", "PNG chunk exceeds file length", "png")
        data = payload[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        actual_crc = binascii.crc32(kind + data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise FrameRenderError("PNG_CRC", f"invalid CRC for {kind.decode('latin1')}", "png")
        if not seen_ihdr and kind != b"IHDR":
            raise FrameRenderError("PNG_STRUCTURE", "IHDR must be the first chunk", "png")
        if kind == b"IHDR":
            if seen_ihdr or length != 13:
                raise FrameRenderError("PNG_IHDR", "invalid or duplicate IHDR", "png")
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if width < 1 or height < 1 or width > MAX_CANVAS or height > MAX_CANVAS:
                raise FrameRenderError("PNG_DIMENSIONS", "PNG dimensions are out of range", "png")
            if (bit_depth, color_type, compression, filtering, interlace) != (8, 6, 0, 0, 0):
                raise FrameRenderError("PNG_FORMAT", "only non-interlaced 8-bit RGBA PNG is supported", "png")
            seen_ihdr = True
        elif kind == b"sRGB":
            if seen_srgb or seen_idat or length != 1 or data[0] > 3:
                raise FrameRenderError("PNG_SRGB", "invalid, duplicate, or late sRGB chunk", "png")
            seen_srgb = True
        elif kind == b"IDAT":
            if idat_closed:
                raise FrameRenderError("PNG_STRUCTURE", "IDAT chunks must be consecutive", "png")
            seen_idat = True
            compressed.extend(data)
            if len(compressed) > MAX_PNG_BYTES:
                raise FrameRenderError("PNG_TOO_LARGE", "compressed PNG stream exceeds limit", "png")
        elif kind == b"IEND":
            if length != 0 or seen_iend or not seen_idat:
                raise FrameRenderError("PNG_IEND", "invalid IEND", "png")
            seen_iend = True
            offset = end
            break
        else:
            if seen_idat:
                idat_closed = True
            if kind == b"PLTE":
                raise FrameRenderError("PNG_FORMAT", "PLTE is forbidden for RGBA input", "png")
            if kind and 65 <= kind[0] <= 90:
                raise FrameRenderError("PNG_CRITICAL_CHUNK", f"unsupported critical chunk {kind!r}", "png")
        offset = end
    if not (seen_ihdr and seen_srgb and seen_idat and seen_iend):
        raise FrameRenderError("PNG_STRUCTURE", "PNG is missing IHDR, sRGB, IDAT, or IEND", "png")
    if offset != len(payload):
        raise FrameRenderError("PNG_TRAILING_DATA", "bytes follow IEND", "png")
    assert width is not None and height is not None
    if expected_width is not None and width != expected_width:
        raise FrameRenderError("PNG_DIMENSION_MISMATCH", f"PNG width {width} != {expected_width}", "png")
    if expected_height is not None and height != expected_height:
        raise FrameRenderError("PNG_DIMENSION_MISMATCH", f"PNG height {height} != {expected_height}", "png")
    expected = height * (1 + width * 4)
    if expected > MAX_TOTAL_DECODED_BYTES:
        raise FrameRenderError("PNG_DECODE_LIMIT", "decoded PNG exceeds configured limit", "png")
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(bytes(compressed), expected + 1)
        raw += decompressor.flush()
    except zlib.error as exc:
        raise FrameRenderError("PNG_ZLIB", str(exc), "png") from exc
    if len(raw) != expected or not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
        raise FrameRenderError("PNG_DECODE_LENGTH", "decoded PNG byte length is invalid", "png")
    stride = width * 4
    pixels = bytearray(height * stride)
    previous = bytearray(stride)
    source_offset = 0
    for y in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        filtered = raw[source_offset : source_offset + stride]
        source_offset += stride
        row = bytearray(stride)
        for x, value in enumerate(filtered):
            left = row[x - 4] if x >= 4 else 0
            up = previous[x]
            up_left = previous[x - 4] if x >= 4 else 0
            if filter_type == 0:
                reconstructed = value
            elif filter_type == 1:
                reconstructed = (value + left) & 0xFF
            elif filter_type == 2:
                reconstructed = (value + up) & 0xFF
            elif filter_type == 3:
                reconstructed = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                reconstructed = (value + _paeth(left, up, up_left)) & 0xFF
            else:
                raise FrameRenderError("PNG_FILTER", f"unsupported PNG row filter {filter_type}", "png")
            row[x] = reconstructed
        pixels[y * stride : (y + 1) * stride] = row
        previous = row
    return RGBAImage(width, height, bytes(pixels))


def _source_over(dst: bytearray, offset: int, src: bytes, src_offset: int) -> None:
    sr, sg, sb, sa = src[src_offset : src_offset + 4]
    if sa == 0:
        return
    dr, dg, db, da = dst[offset : offset + 4]
    inv = 255 - sa
    out_a = sa + (da * inv + 127) // 255
    if out_a == 0:
        dst[offset : offset + 4] = b"\x00\x00\x00\x00"
        return
    channels = []
    for sc, dc in ((sr, dr), (sg, dg), (sb, db)):
        premul = sc * sa + (dc * da * inv + 127) // 255
        channels.append(min(255, (premul + out_a // 2) // out_a))
    dst[offset : offset + 4] = bytes((*channels, out_a))


def _render_frame(canvas: dict[str, Any], placements: list[dict[str, Any]], assets: dict[tuple[str, str, str], RGBAImage]) -> RGBAImage:
    width = _bounded_integer(canvas.get("width"), "canvas.width", 1, MAX_CANVAS)
    height = _bounded_integer(canvas.get("height"), "canvas.height", 1, MAX_CANVAS)
    background = canvas.get("background_rgba")
    if not isinstance(background, list) or len(background) != 4 or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 255 for value in background):
        raise FrameRenderError("CANVAS_SCHEMA", "background_rgba must contain four byte values", "canvas.background_rgba")
    pixels = bytearray(bytes(background) * (width * height))
    for placement in sorted(placements, key=lambda item: (item.get("z_order"), item.get("role"))):
        asset = placement.get("asset")
        if not isinstance(asset, dict):
            raise FrameRenderError("PLACEMENT_SCHEMA", "placement asset is missing", "placements")
        role = placement.get("role")
        path = asset.get("asset_path")
        sha = asset.get("png_sha256")
        image = assets.get((str(role), str(path), str(sha)))
        if image is None:
            raise FrameRenderError("ASSET_BINDING", "decoded source asset is missing", str(path))
        for point_name in ("source_anchor", "target_anchor", "translation"):
            point = placement.get(point_name)
            if not isinstance(point, dict):
                raise FrameRenderError("PLACEMENT_SCHEMA", f"{point_name} is missing", point_name)
            _bounded_integer(point.get("x"), f"{point_name}.x", -1_000_000, 1_000_000)
            _bounded_integer(point.get("y"), f"{point_name}.y", -1_000_000, 1_000_000)
        scale = placement.get("scale")
        if not isinstance(scale, dict):
            raise FrameRenderError("PLACEMENT_SCHEMA", "scale is missing", "scale")
        numerator = _bounded_integer(scale.get("numerator"), "scale.numerator", 1, 1_000_000)
        denominator = _bounded_integer(scale.get("denominator"), "scale.denominator", 1, 1_000_000)
        source_anchor = placement["source_anchor"]
        target_anchor = placement["target_anchor"]
        translation = placement["translation"]
        target_x = target_anchor["x"] + translation["x"]
        target_y = target_anchor["y"] + translation["y"]
        for dy in range(height):
            sy = (2 * source_anchor["y"] * numerator + (2 * (dy - target_y) + 1) * denominator) // (2 * numerator)
            if sy < 0 or sy >= image.height:
                continue
            for dx in range(width):
                sx = (2 * source_anchor["x"] * numerator + (2 * (dx - target_x) + 1) * denominator) // (2 * numerator)
                if sx < 0 or sx >= image.width:
                    continue
                _source_over(pixels, (dy * width + dx) * 4, image.pixels, (sy * image.width + sx) * 4)
    return RGBAImage(width, height, bytes(pixels))


def _package_item_dimensions(package_dir: Path, asset_path: str, expected_sha: str) -> tuple[int | None, int | None]:
    manifest_path = package_dir / "package-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return None, None
    manifest = _load_object(manifest_path)
    items = manifest.get("items")
    if not isinstance(items, list):
        return None, None
    matches = [item for item in items if isinstance(item, dict) and item.get("png_path") == asset_path]
    if len(matches) != 1 or matches[0].get("png_sha256") != expected_sha:
        raise FrameRenderError("ASSET_BINDING", "package manifest does not bind the requested PNG", asset_path)
    sidecar_path = matches[0].get("sidecar_path")
    if not isinstance(sidecar_path, str):
        return None, None
    _relative, resolved = _safe_file(package_dir, sidecar_path, "sidecar_path")
    sidecar = _load_object(resolved)
    width = sidecar.get("width")
    height = sidecar.get("height")
    if isinstance(width, bool) or not isinstance(width, int) or width < 1:
        raise FrameRenderError("SIDECAR_DIMENSIONS", "sidecar width is invalid", sidecar_path)
    if isinstance(height, bool) or not isinstance(height, int) or height < 1:
        raise FrameRenderError("SIDECAR_DIMENSIONS", "sidecar height is invalid", sidecar_path)
    return width, height


def _source_role_packages(source_bindings: dict[str, Any]) -> dict[str, str]:
    upstream = source_bindings.get("upstream")
    roles = upstream.get("roles") if isinstance(upstream, dict) else None
    if not isinstance(roles, dict):
        raise FrameRenderError("SOURCE_BINDINGS", "upstream role bindings are missing", SOURCE_BINDINGS)
    result: dict[str, str] = {}
    for role in ("boke", "tsukkomi"):
        item = roles.get(role)
        if not isinstance(item, dict) or not isinstance(item.get("package_id"), str):
            raise FrameRenderError("SOURCE_BINDINGS", f"{role} package binding is missing", role)
        result[role] = item["package_id"]
    return result


def _validate_renderer_job(renderer_job_manifest: Path, renderer_job_root: Path, render_plan_root: Path, audio_preview_root: Path, preview_root: Path, package_root: Path, audio_root: Path) -> tuple[dict[str, Any], str, Path]:
    root = _root(renderer_job_root, must_exist=True, field="renderer_job_root")
    relative, resolved = _relative_file(renderer_job_manifest, root, "renderer_job_manifest")
    try:
        checked = check_composition_job_package(resolved, root, render_plan_root, audio_preview_root, preview_root, package_root, audio_root)
    except CompositionError as exc:
        raise FrameRenderError(f"RENDERER_JOB_{exc.code}", exc.message, exc.field or "renderer_job_manifest") from exc
    renderer_job = checked.get("renderer_job")
    if not isinstance(renderer_job, dict):
        raise FrameRenderError("RENDERER_JOB_RESULT", "renderer-job checker result is malformed", "renderer_job_manifest")
    return renderer_job, relative, resolved


def _build_expected(renderer_job_manifest: Path, renderer_job_root: Path, render_plan_root: Path, audio_preview_root: Path, preview_root: Path, package_root: Path, audio_root: Path) -> tuple[dict[str, Any], dict[str, bytes], set[Path]]:
    renderer_job, renderer_relative, renderer_path = _validate_renderer_job(renderer_job_manifest, renderer_job_root, render_plan_root, audio_preview_root, preview_root, package_root, audio_root)
    renderer_dir = renderer_path.parent
    transforms_path = renderer_dir / SPAN_TRANSFORMS
    sources_path = renderer_dir / SOURCE_BINDINGS
    transforms = _load_object(transforms_path)
    source_bindings = _load_object(sources_path)
    if transforms_path.read_bytes() != _json_bytes(transforms) or sources_path.read_bytes() != _json_bytes(source_bindings):
        raise FrameRenderError("RENDERER_JOB_CANONICAL", "renderer-job inventories are not canonical", str(renderer_dir))
    spans = transforms.get("spans")
    if not isinstance(spans, list) or not spans:
        raise FrameRenderError("SPAN_SCHEMA", "renderer-job span inventory is missing", SPAN_TRANSFORMS)
    frame_count = _bounded_integer(renderer_job.get("frame_count"), "frame_count", 1, MAX_FRAME_COUNT)
    canvas = renderer_job.get("canvas")
    if not isinstance(canvas, dict):
        raise FrameRenderError("CANVAS_SCHEMA", "renderer-job canvas is missing", "canvas")
    width = _bounded_integer(canvas.get("width"), "canvas.width", 1, MAX_CANVAS)
    height = _bounded_integer(canvas.get("height"), "canvas.height", 1, MAX_CANVAS)
    if width * height * frame_count > MAX_TOTAL_OUTPUT_PIXELS:
        raise FrameRenderError("OUTPUT_LIMIT", "requested frame package exceeds the total pixel limit", "frame_count")
    role_packages = _source_role_packages(source_bindings)
    package_base = _root(package_root, must_exist=True, field="package_root")
    assets: dict[tuple[str, str, str], RGBAImage] = {}
    source_dirs: set[Path] = {renderer_dir.resolve()}
    decoded_total = 0
    for span_index, span in enumerate(spans):
        if not isinstance(span, dict):
            raise FrameRenderError("SPAN_SCHEMA", "span entry must be an object", f"spans[{span_index}]")
        placements = span.get("placements")
        if not isinstance(placements, list) or len(placements) != 2:
            raise FrameRenderError("SPAN_SCHEMA", "each span requires two placements", f"spans[{span_index}].placements")
        for placement in placements:
            if not isinstance(placement, dict):
                raise FrameRenderError("PLACEMENT_SCHEMA", "placement must be an object", f"spans[{span_index}]")
            role = placement.get("role")
            asset = placement.get("asset")
            if role not in role_packages or not isinstance(asset, dict):
                raise FrameRenderError("ASSET_BINDING", "placement role or asset is invalid", f"spans[{span_index}]")
            asset_path = asset.get("asset_path")
            png_sha = asset.get("png_sha256")
            if not isinstance(asset_path, str) or not isinstance(png_sha, str) or not SHA256_RE.fullmatch(png_sha):
                raise FrameRenderError("ASSET_BINDING", "asset path or checksum is invalid", f"spans[{span_index}]")
            key = (role, asset_path, png_sha)
            if key in assets:
                continue
            package_id = role_packages[role]
            try:
                package_component = safe_relative_path(package_id)
            except ValueError as exc:
                raise FrameRenderError("UNSAFE_PATH", str(exc), f"{role}.package_id") from exc
            if len(package_component.parts) != 1:
                raise FrameRenderError("UNSAFE_PATH", "package ID must be one path component", f"{role}.package_id")
            package_dir = package_base / package_component.parts[0]
            if package_dir.is_symlink() or not package_dir.is_dir():
                raise FrameRenderError("PACKAGE_MISSING", f"package directory missing for {role}", package_id)
            package_dir = package_dir.resolve()
            if not _within(package_base, package_dir):
                raise FrameRenderError("PATH_ESCAPE", "package directory escapes package_root", package_id)
            source_dirs.add(package_dir)
            _normalized, png_path = _safe_file(package_dir, asset_path, f"{role}.asset_path")
            payload = png_path.read_bytes()
            if _sha(payload) != png_sha:
                raise FrameRenderError("ASSET_SHA256", "source PNG checksum does not match renderer job", asset_path)
            expected_width, expected_height = _package_item_dimensions(package_dir, asset_path, png_sha)
            image = decode_rgba_png(payload, expected_width=expected_width, expected_height=expected_height)
            decoded_total += len(image.pixels)
            if decoded_total > MAX_TOTAL_DECODED_BYTES:
                raise FrameRenderError("DECODE_LIMIT", "source PNG decoded-byte total exceeds limit", asset_path)
            assets[key] = image
    if not assets:
        raise FrameRenderError("ASSET_BINDING", "renderer job contains no assets", "placements")

    frame_files: dict[str, bytes] = {}
    inventory_entries: list[dict[str, Any]] = []
    cursor = 0
    for span_index, span in enumerate(spans):
        start = _bounded_integer(span.get("start_frame"), f"spans[{span_index}].start_frame", 0, frame_count - 1)
        end = _bounded_integer(span.get("end_frame"), f"spans[{span_index}].end_frame", 1, frame_count)
        if start != cursor or end <= start:
            raise FrameRenderError("SPAN_COVERAGE", "spans must be contiguous and non-empty", f"spans[{span_index}]")
        png = encode_rgba_png(_render_frame(canvas, span["placements"], assets))
        for frame_index in range(start, end):
            relative = f"{FRAMES_DIR}/{frame_index:08d}.png"
            frame_files[relative] = png
            inventory_entries.append({"index": frame_index, "path": relative, "sha256": _sha(png), "size": len(png), "start_time_num": frame_index * renderer_job["fps_den"], "end_time_num": (frame_index + 1) * renderer_job["fps_den"], "time_den": renderer_job["fps_num"], "span_index": span_index})
        cursor = end
    if cursor != frame_count:
        raise FrameRenderError("SPAN_COVERAGE", "spans do not cover the full frame range", "spans")
    inventory_core = {"kind": "paper-theater-frame-inventory", "schema_version": "1.0", "renderer_job_ref": renderer_job["id"], "frame_count": frame_count, "fps_num": renderer_job["fps_num"], "fps_den": renderer_job["fps_den"], "frames": inventory_entries}
    inventory_id = content_identifier("paper-theater-frame-inventory", inventory_core, 20)
    inventory = {"id": inventory_id, **inventory_core}
    inventory_bytes = _json_bytes(inventory)
    core = {
        "kind": "paper-theater-frame-render-package",
        "schema_version": "1.0",
        "source_renderer_job": {"id": renderer_job["id"], "path": renderer_relative, "sha256": _sha(renderer_path.read_bytes())},
        "intent": renderer_job["intent"],
        "audio_license_status": renderer_job["audio_license_status"],
        "canvas": canvas,
        "fps_num": renderer_job["fps_num"],
        "fps_den": renderer_job["fps_den"],
        "frame_count": frame_count,
        "span_count": renderer_job["span_count"],
        "audio_placement": renderer_job["audio_placement"],
        "render_policy": {"coordinate_rule": "inverse-pixel-center-floor", "scale_arithmetic": "reduced-rational-integer-only", "compositing": "straight-alpha-source-over-integer-round-half-up", "png_encoding": "rgba8-noninterlaced-srgb-filter0-zlib9"},
        "frame_inventory": {"id": inventory_id, "path": FRAME_INVENTORY, "sha256": _sha(inventory_bytes)},
        "media_created": True,
    }
    package_id = content_identifier("paper-theater-frame-render-package", core, 20)
    generated: dict[str, bytes] = {**frame_files, FRAME_INVENTORY: inventory_bytes}
    files = [{"path": path, "sha256": _sha(payload), "size": len(payload)} for path, payload in sorted(generated.items())]
    manifest = {"id": package_id, **core, "files": files}
    generated[FRAME_RENDER_MANIFEST] = _json_bytes(manifest)
    return manifest, generated, source_dirs


def _output_candidate(output_root: Path) -> Path:
    expanded = _reject_lexical(output_root, "output_root")
    _reject_symlink_components(expanded, "output_root")
    if expanded.exists() and not expanded.is_dir():
        raise FrameRenderError("ROOT_TYPE", "output_root must be a directory", "output_root")
    return expanded.resolve(strict=False)


def _reject_output_overlap(output_root: Path, sources: set[Path]) -> None:
    candidate = _output_candidate(output_root)
    for source in sorted(sources, key=str):
        resolved = source.resolve(strict=False)
        if candidate == resolved or _within(resolved, candidate):
            raise FrameRenderError("OUTPUT_OVERLAPS_SOURCE", f"output_root must not equal or be nested beneath source package {resolved}", "output_root")


def _write_package(output_root: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> bool:
    root_path = _reject_lexical(output_root, "output_root")
    _reject_symlink_components(root_path, "output_root")
    if root_path.exists() and not root_path.is_dir():
        raise FrameRenderError("ROOT_TYPE", "output_root must be a directory", "output_root")
    root_path.mkdir(parents=True, exist_ok=True)
    root = root_path.resolve()
    destination = root / manifest["id"]
    expected = set(files)
    if destination.is_symlink():
        raise FrameRenderError("OUTPUT_SYMLINK", "frame package destination is a symlink", "output_root")
    if destination.exists():
        if not destination.is_dir():
            raise FrameRenderError("OUTPUT_CONFLICT", "frame package destination is not a directory", "output_root")
        actual: set[str] = set()
        for candidate in destination.rglob("*"):
            if candidate.is_symlink():
                raise FrameRenderError("OUTPUT_SYMLINK", "existing frame package contains a symlink", str(candidate))
            if candidate.is_file():
                actual.add(candidate.relative_to(destination).as_posix())
        if actual != expected:
            raise FrameRenderError("OUTPUT_CONFLICT", "existing frame package file set differs", "output_root")
        for relative, payload in files.items():
            if destination.joinpath(*safe_relative_path(relative).parts).read_bytes() != payload:
                raise FrameRenderError("OUTPUT_CONFLICT", f"existing file differs: {relative}", relative)
        return False
    staging = root / f".{manifest['id']}.tmp"
    if staging.exists():
        if staging.is_symlink():
            raise FrameRenderError("STAGING_CONFLICT", "staging path is a symlink", "output_root")
        shutil.rmtree(staging)
    try:
        staging.mkdir()
        for relative, payload in files.items():
            target = staging.joinpath(*safe_relative_path(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        staging.replace(destination)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    return True


def build_frame_render_package(renderer_job_manifest: Path, renderer_job_root: Path, render_plan_root: Path, audio_preview_root: Path, preview_root: Path, package_root: Path, audio_root: Path, output_root: Path, *, write: bool = False) -> dict[str, Any]:
    manifest, files, sources = _build_expected(renderer_job_manifest, renderer_job_root, render_plan_root, audio_preview_root, preview_root, package_root, audio_root)
    written = False
    if write:
        _reject_output_overlap(output_root, sources)
        written = _write_package(output_root, manifest, files)
    return {"ok": True, "frame_render": manifest, "file_count": len(files), "written": written, "package_path": manifest["id"]}


def check_frame_render_package(manifest_path: Path, output_root: Path, renderer_job_root: Path, render_plan_root: Path, audio_preview_root: Path, preview_root: Path, package_root: Path, audio_root: Path) -> dict[str, Any]:
    root = _root(output_root, must_exist=True, field="output_root")
    _relative, resolved = _relative_file(manifest_path, root, "frame_render_manifest")
    manifest = _load_object(resolved)
    if resolved.read_bytes() != _json_bytes(manifest):
        raise FrameRenderError("MANIFEST_CANONICAL", "frame-render manifest JSON is not canonical", str(manifest_path))
    package_id = manifest.get("id")
    if not isinstance(package_id, str):
        raise FrameRenderError("MANIFEST_SCHEMA", "frame-render package ID is missing", "id")
    canonical = root / package_id / FRAME_RENDER_MANIFEST
    if resolved != canonical.resolve():
        raise FrameRenderError("MANIFEST_LOCATION", "frame-render manifest path is not canonical", str(manifest_path))
    binding = manifest.get("source_renderer_job")
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str):
        raise FrameRenderError("MANIFEST_SCHEMA", "source renderer-job binding is missing", "source_renderer_job")
    renderer_base = _root(renderer_job_root, must_exist=True, field="renderer_job_root")
    _renderer_relative, renderer_manifest = _safe_file(renderer_base, binding["path"], "source_renderer_job.path")
    expected_manifest, expected_files, sources = _build_expected(renderer_manifest, renderer_base, render_plan_root, audio_preview_root, preview_root, package_root, audio_root)
    _reject_output_overlap(root, sources)
    if manifest != expected_manifest:
        raise FrameRenderError("MANIFEST_BINDING_MISMATCH", "frame-render manifest is stale or not canonical", str(manifest_path))
    destination = root / package_id
    expected_names = set(expected_files)
    actual_names: set[str] = set()
    for candidate in destination.rglob("*"):
        if candidate.is_symlink():
            raise FrameRenderError("PACKAGE_SYMLINK", "frame package contains a symlink", str(candidate))
        if candidate.is_file():
            actual_names.add(candidate.relative_to(destination).as_posix())
    if actual_names != expected_names:
        raise FrameRenderError("FILE_SET_MISMATCH", f"missing={sorted(expected_names - actual_names)}; extra={sorted(actual_names - expected_names)}", str(destination))
    for relative, expected in expected_files.items():
        candidate = destination.joinpath(*safe_relative_path(relative).parts)
        if candidate.read_bytes() != expected:
            raise FrameRenderError("FILE_MISMATCH", f"frame package file was modified: {relative}", relative)
    return {"ok": True, "frame_render": manifest, "file_count": len(expected_files), "frame_count": manifest["frame_count"], "span_count": manifest["span_count"]}
