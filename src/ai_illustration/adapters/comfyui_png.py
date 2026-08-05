"""Strict PNG decoding for bounded ComfyUI RGB and RGBA outputs."""

from __future__ import annotations

from dataclasses import dataclass
import binascii
import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_CANVAS = 8192
MAX_PNG_BYTES = 128 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024 * 1024
MAX_TOTAL_DECODED_BYTES = 256 * 1024 * 1024


@dataclass
class ComfyUIPngError(ValueError):
    code: str
    message: str
    field: str = "png"

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass(frozen=True)
class DecodedComfyUIPng:
    width: int
    height: int
    pixels: bytes
    has_alpha: bool


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    distance_left = abs(estimate - left)
    distance_up = abs(estimate - up)
    distance_upper_left = abs(estimate - upper_left)
    if distance_left <= distance_up and distance_left <= distance_upper_left:
        return left
    return up if distance_up <= distance_upper_left else upper_left


def decode_comfyui_png(
    payload: bytes,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
) -> DecodedComfyUIPng:
    """Decode a bounded non-interlaced 8-bit RGB/RGBA PNG.

    ComfyUI commonly emits true-colour RGB PNG files even when downstream
    consumers internally operate on RGBA pixels. RGB inputs are expanded with
    an opaque alpha channel while preserving whether the original file carried
    alpha in ``has_alpha``.
    """

    if len(payload) > MAX_PNG_BYTES:
        raise ComfyUIPngError("PNG_TOO_LARGE", "PNG exceeds the byte limit")
    if not payload.startswith(PNG_SIGNATURE):
        raise ComfyUIPngError("PNG_SIGNATURE", "invalid PNG signature")

    offset = len(PNG_SIGNATURE)
    width = height = channels = None
    has_alpha = False
    seen_ihdr = seen_idat = seen_iend = seen_srgb = False
    idat_closed = False
    compressed = bytearray()

    while offset < len(payload):
        if len(payload) - offset < 12:
            raise ComfyUIPngError("PNG_STRUCTURE", "truncated PNG chunk")
        length = struct.unpack(">I", payload[offset : offset + 4])[0]
        kind = payload[offset + 4 : offset + 8]
        if len(kind) != 4 or any(
            not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind
        ):
            raise ComfyUIPngError(
                "PNG_CHUNK_TYPE",
                "PNG chunk type must contain four ASCII letters",
            )
        if not 65 <= kind[2] <= 90:
            raise ComfyUIPngError(
                "PNG_CHUNK_RESERVED",
                "PNG chunk reserved bit must be zero",
            )
        if length > MAX_CHUNK_BYTES:
            raise ComfyUIPngError(
                "PNG_CHUNK_LIMIT",
                "PNG chunk exceeds the configured limit",
            )

        end = offset + 12 + length
        if end > len(payload):
            raise ComfyUIPngError(
                "PNG_STRUCTURE",
                "PNG chunk exceeds file length",
            )
        data = payload[offset + 8 : offset + 8 + length]
        stored_crc = struct.unpack(">I", payload[offset + 8 + length : end])[0]
        if (binascii.crc32(kind + data) & 0xFFFFFFFF) != stored_crc:
            raise ComfyUIPngError(
                "PNG_CRC",
                f"invalid CRC for {kind.decode('latin1')}",
            )
        if not seen_ihdr and kind != b"IHDR":
            raise ComfyUIPngError("PNG_STRUCTURE", "IHDR must be first")

        if kind == b"IHDR":
            if seen_ihdr or length != 13:
                raise ComfyUIPngError("PNG_IHDR", "invalid or duplicate IHDR")
            (
                width,
                height,
                depth,
                color_type,
                compression,
                filtering,
                interlace,
            ) = struct.unpack(">IIBBBBB", data)
            if not 1 <= width <= MAX_CANVAS or not 1 <= height <= MAX_CANVAS:
                raise ComfyUIPngError(
                    "PNG_DIMENSIONS",
                    "PNG dimensions are out of range",
                )
            if (
                depth != 8
                or color_type not in {2, 6}
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise ComfyUIPngError(
                    "PNG_FORMAT",
                    "only non-interlaced 8-bit RGB or RGBA PNG is supported",
                )
            channels = 4 if color_type == 6 else 3
            has_alpha = color_type == 6
            seen_ihdr = True
        elif kind == b"sRGB":
            if seen_srgb or seen_idat or length != 1 or data[0] > 3:
                raise ComfyUIPngError(
                    "PNG_SRGB",
                    "invalid, duplicate, or late sRGB chunk",
                )
            seen_srgb = True
        elif kind == b"IDAT":
            if idat_closed:
                raise ComfyUIPngError(
                    "PNG_STRUCTURE",
                    "IDAT chunks must be consecutive",
                )
            seen_idat = True
            compressed.extend(data)
            if len(compressed) > MAX_PNG_BYTES:
                raise ComfyUIPngError(
                    "PNG_TOO_LARGE",
                    "compressed PNG stream exceeds limit",
                )
        elif kind == b"IEND":
            if length != 0 or seen_iend or not seen_idat:
                raise ComfyUIPngError("PNG_IEND", "invalid IEND")
            seen_iend = True
            offset = end
            break
        else:
            if seen_idat:
                idat_closed = True
            if kind == b"tRNS":
                raise ComfyUIPngError(
                    "PNG_FORMAT",
                    "tRNS transparency is unsupported; use RGBA color type",
                )
            if 65 <= kind[0] <= 90:
                raise ComfyUIPngError(
                    "PNG_CRITICAL_CHUNK",
                    f"unsupported critical chunk {kind!r}",
                )
        offset = end

    if not (seen_ihdr and seen_idat and seen_iend):
        raise ComfyUIPngError(
            "PNG_STRUCTURE",
            "PNG is missing a required chunk",
        )
    if offset != len(payload):
        raise ComfyUIPngError("PNG_TRAILING_DATA", "bytes follow IEND")

    assert width is not None and height is not None and channels is not None
    if expected_width is not None and width != expected_width:
        raise ComfyUIPngError(
            "PNG_DIMENSION_MISMATCH",
            f"PNG width {width} != {expected_width}",
        )
    if expected_height is not None and height != expected_height:
        raise ComfyUIPngError(
            "PNG_DIMENSION_MISMATCH",
            f"PNG height {height} != {expected_height}",
        )

    expected_raw = height * (1 + width * channels)
    expected_rgba = width * height * 4
    if (
        expected_raw > MAX_TOTAL_DECODED_BYTES
        or expected_rgba > MAX_TOTAL_DECODED_BYTES
    ):
        raise ComfyUIPngError(
            "PNG_DECODE_LIMIT",
            "decoded PNG exceeds configured limit",
        )

    decoder = zlib.decompressobj()
    try:
        raw = decoder.decompress(bytes(compressed), expected_raw + 1)
        if len(raw) > expected_raw or decoder.unconsumed_tail:
            raise ComfyUIPngError(
                "PNG_DECODE_LIMIT",
                "decoded PNG exceeds IHDR-derived length",
            )
        remaining = expected_raw - len(raw)
        flushed = decoder.flush(max(1, remaining + 1))
    except ComfyUIPngError:
        raise
    except zlib.error as exc:
        raise ComfyUIPngError("PNG_ZLIB", str(exc)) from exc
    if len(flushed) > remaining:
        raise ComfyUIPngError(
            "PNG_DECODE_LIMIT",
            "decoded PNG exceeds IHDR-derived length",
        )
    raw += flushed
    if (
        len(raw) != expected_raw
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ComfyUIPngError(
            "PNG_DECODE_LENGTH",
            "decoded PNG byte length is invalid",
        )

    stride = width * channels
    decoded = bytearray(height * stride)
    previous = bytearray(stride)
    source_offset = 0
    for row_index in range(height):
        filter_type = raw[source_offset]
        source_offset += 1
        filtered = raw[source_offset : source_offset + stride]
        source_offset += stride
        row = bytearray(stride)
        for index, value in enumerate(filtered):
            left = row[index - channels] if index >= channels else 0
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
            elif filter_type == 4:
                predictor = _paeth(left, up, upper_left)
            else:
                raise ComfyUIPngError(
                    "PNG_FILTER",
                    f"unsupported PNG row filter {filter_type}",
                )
            row[index] = (value + predictor) & 0xFF
        start = row_index * stride
        decoded[start : start + stride] = row
        previous = row

    if channels == 4:
        rgba = bytes(decoded)
    else:
        expanded = bytearray(expected_rgba)
        target = 0
        for source in range(0, len(decoded), 3):
            expanded[target : target + 3] = decoded[source : source + 3]
            expanded[target + 3] = 255
            target += 4
        rgba = bytes(expanded)

    return DecodedComfyUIPng(width, height, rgba, has_alpha)
