from __future__ import annotations

import binascii
import struct
import unittest
import zlib

from ai_illustration.adapters.comfyui_execution_package import candidate_files
from ai_illustration.adapters.comfyui_png import (
    ComfyUIPngError,
    PNG_SIGNATURE,
    decode_comfyui_png,
)


def chunk(kind: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def png_bytes(
    width: int,
    height: int,
    color_type: int,
    pixels: bytes,
    *,
    metadata: bool = False,
    interlace: int = 0,
) -> bytes:
    channels = 4 if color_type == 6 else 3
    assert len(pixels) == width * height * channels
    stride = width * channels
    rows = bytearray()
    for row in range(height):
        rows.append(0)
        rows.extend(pixels[row * stride : (row + 1) * stride])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, interlace)
    parts = [PNG_SIGNATURE, chunk(b"IHDR", ihdr)]
    if metadata:
        parts.append(chunk(b"tEXt", b"prompt\x00fixture"))
    parts.extend([chunk(b"IDAT", zlib.compress(bytes(rows), 9)), chunk(b"IEND", b"")])
    return b"".join(parts)


class ComfyUIPngTests(unittest.TestCase):
    def test_rgb_is_accepted_and_expanded_to_opaque_rgba(self) -> None:
        payload = png_bytes(
            2,
            1,
            2,
            bytes([255, 0, 0, 0, 128, 255]),
            metadata=True,
        )
        image = decode_comfyui_png(payload, expected_width=2, expected_height=1)
        self.assertFalse(image.has_alpha)
        self.assertEqual(image.width, 2)
        self.assertEqual(image.height, 1)
        self.assertEqual(
            image.pixels,
            bytes([255, 0, 0, 255, 0, 128, 255, 255]),
        )

    def test_rgba_is_accepted_without_pixel_changes(self) -> None:
        pixels = bytes([1, 2, 3, 4, 5, 6, 7, 8])
        image = decode_comfyui_png(png_bytes(2, 1, 6, pixels))
        self.assertTrue(image.has_alpha)
        self.assertEqual(image.pixels, pixels)

    def test_candidate_sidecar_reports_original_alpha_truthfully(self) -> None:
        payload = png_bytes(1, 1, 2, bytes([10, 20, 30]))
        plan = {
            "id": "execution-plan-test",
            "expected_width": 1,
            "expected_height": 1,
            "request": {"id": "request-test"},
            "workflow": {"sha256": "a" * 64, "bound_sha256": "b" * 64},
            "tool_profile": {"id": "tool-test"},
            "model_profile": {"id": "model-test"},
        }
        descriptor = {
            "node_id": "9",
            "filename": "comfy-output.png",
            "subfolder": "",
            "type": "output",
        }
        sidecar, sidecar_bytes, png_path, returned = candidate_files(
            plan,
            "prompt-test",
            descriptor,
            payload,
            0,
        )
        self.assertFalse(sidecar["has_alpha"])
        self.assertEqual(sidecar["width"], 1)
        self.assertEqual(sidecar["height"], 1)
        self.assertTrue(png_path.endswith(".png"))
        self.assertEqual(returned, payload)
        self.assertIn(b'"has_alpha":false', sidecar_bytes)

    def test_invalid_format_crc_dimensions_and_trailing_bytes_fail_closed(self) -> None:
        with self.subTest(case="indexed"):
            with self.assertRaises(ComfyUIPngError) as caught:
                decode_comfyui_png(png_bytes(1, 1, 3, bytes([1, 2, 3])))
            self.assertEqual(caught.exception.code, "PNG_FORMAT")

        with self.subTest(case="interlaced"):
            with self.assertRaises(ComfyUIPngError) as caught:
                decode_comfyui_png(
                    png_bytes(1, 1, 2, bytes([1, 2, 3]), interlace=1)
                )
            self.assertEqual(caught.exception.code, "PNG_FORMAT")

        valid = png_bytes(1, 1, 2, bytes([1, 2, 3]))
        with self.subTest(case="crc"):
            corrupted = bytearray(valid)
            corrupted[-5] ^= 1
            with self.assertRaises(ComfyUIPngError) as caught:
                decode_comfyui_png(bytes(corrupted))
            self.assertEqual(caught.exception.code, "PNG_CRC")

        with self.subTest(case="dimensions"):
            with self.assertRaises(ComfyUIPngError) as caught:
                decode_comfyui_png(valid, expected_width=2, expected_height=1)
            self.assertEqual(caught.exception.code, "PNG_DIMENSION_MISMATCH")

        with self.subTest(case="trailing"):
            with self.assertRaises(ComfyUIPngError) as caught:
                decode_comfyui_png(valid + b"x")
            self.assertEqual(caught.exception.code, "PNG_TRAILING_DATA")


if __name__ == "__main__":
    unittest.main()
