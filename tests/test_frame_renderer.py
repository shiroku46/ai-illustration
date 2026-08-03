from __future__ import annotations

import binascii
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ai_illustration.frame_renderer import (
    FRAME_INVENTORY,
    FRAME_RENDER_MANIFEST,
    PNG_SIGNATURE,
    FrameRenderError,
    RGBAImage,
    _render_frame,
    build_frame_render_package,
    check_frame_render_package,
    decode_rgba_png,
    encode_rgba_png,
)
from ai_illustration.naming import canonical_json


def canonical_bytes(value: object) -> bytes:
    return canonical_json(value) + b"\n"


def chunk(kind: bytes, data: bytes) -> bytes:
    crc = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def filtered_png(width: int, height: int, pixels: bytes, filter_type: int) -> bytes:
    stride = width * 4
    previous = bytes(stride)
    rows = bytearray()
    for y in range(height):
        raw = pixels[y * stride : (y + 1) * stride]
        rows.append(filter_type)
        encoded = bytearray(stride)
        for x, value in enumerate(raw):
            left = raw[x - 4] if x >= 4 else 0
            up = previous[x]
            upper_left = previous[x - 4] if x >= 4 else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                estimate = left + up - upper_left
                dl, du, dul = abs(estimate - left), abs(estimate - up), abs(estimate - upper_left)
                predictor = left if dl <= du and dl <= dul else (up if du <= dul else upper_left)
            else:
                predictor = 0
            encoded[x] = (value - predictor) & 0xFF
        rows.extend(encoded)
        previous = raw
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"sRGB", b"\x00") + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


def rgba_png_with_chunk(kind: bytes, data: bytes = b"") -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    raw = bytes((0, 1, 2, 3, 4))
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", ihdr)
        + chunk(b"sRGB", b"\x00")
        + chunk(kind, data)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


class FrameRendererTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.renderer_root = self.root / "renderer-jobs"
        self.render_plan_root = self.root / "render-plans"
        self.audio_preview_root = self.root / "audio-previews"
        self.preview_root = self.root / "previews"
        self.package_root = self.root / "packages"
        self.audio_root = self.root / "audio"
        self.output_root = self.root / "frames"
        for directory in (
            self.renderer_root,
            self.render_plan_root,
            self.audio_preview_root,
            self.preview_root,
            self.package_root,
            self.audio_root,
        ):
            directory.mkdir()
        self.renderer_id = "paper-theater-renderer-job-" + "1" * 20
        self.renderer_dir = self.renderer_root / self.renderer_id
        self.renderer_dir.mkdir()
        self.renderer_manifest = self.renderer_dir / "renderer-job-manifest.json"
        self.renderer_manifest.write_bytes(canonical_bytes({"id": self.renderer_id}))
        self.red = encode_rgba_png(RGBAImage(1, 1, bytes((255, 0, 0, 255))))
        self.blue = encode_rgba_png(RGBAImage(1, 1, bytes((0, 0, 255, 128))))
        self.red_sha = hashlib.sha256(self.red).hexdigest()
        self.blue_sha = hashlib.sha256(self.blue).hexdigest()
        self._write_package("boke-package", "variants/boke.png", "variants/boke.json", self.red, self.red_sha)
        self._write_package("tsukkomi-package", "variants/tsukkomi.png", "variants/tsukkomi.json", self.blue, self.blue_sha)
        placements = [
            self._placement("boke", "variants/boke.png", self.red_sha, 0, 0),
            self._placement("tsukkomi", "variants/tsukkomi.png", self.blue_sha, 1, 1),
        ]
        self.transforms = {
            "kind": "paper-theater-span-transform-inventory",
            "schema_version": "1.0",
            "renderer_job_ref": self.renderer_id,
            "render_plan_ref": "plan-demo",
            "composition_profile_ref": "profile-demo",
            "frame_count": 2,
            "span_count": 1,
            "spans": [
                {
                    "index": 0,
                    "start_frame": 0,
                    "end_frame": 2,
                    "start_time_num": 0,
                    "end_time_num": 2000,
                    "time_den": 1,
                    "clear": {"op": "clear-canvas", "background_rgba": [0, 0, 0, 0]},
                    "placements": placements,
                }
            ],
        }
        self.source_bindings = {
            "kind": "paper-theater-renderer-source-bindings",
            "schema_version": "1.0",
            "renderer_job_ref": self.renderer_id,
            "render_plan": {"id": "plan-demo", "path": "plan-demo/render-plan-manifest.json", "sha256": "a" * 64},
            "composition_profile": {"id": "profile-demo", "path": "composition-profile.json", "sha256": "b" * 64},
            "upstream": {
                "roles": {
                    "boke": {"package_id": "boke-package"},
                    "tsukkomi": {"package_id": "tsukkomi-package"},
                }
            },
            "audio_placement": {"offset_ms": 0},
            "intent": "evaluation",
            "audio_license_status": "reviewing",
        }
        self._write_renderer_inventories()
        self.renderer_job = {
            "id": self.renderer_id,
            "kind": "paper-theater-renderer-job",
            "schema_version": "1.0",
            "intent": "evaluation",
            "audio_license_status": "reviewing",
            "canvas": {"width": 3, "height": 1, "background_rgba": [0, 0, 0, 0]},
            "fps_num": 1,
            "fps_den": 1,
            "frame_count": 2,
            "span_count": 1,
            "audio_placement": {"offset_ms": 0},
        }

    def _write_renderer_inventories(self) -> None:
        (self.renderer_dir / "span-transforms.json").write_bytes(canonical_bytes(self.transforms))
        (self.renderer_dir / "source-bindings.json").write_bytes(canonical_bytes(self.source_bindings))

    def _placement(self, role: str, path: str, sha: str, x: int, z: int) -> dict[str, object]:
        slot = "left" if x == 0 else "right"
        return {
            "op": "place-source-asset",
            "role": role,
            "slot": slot,
            "asset": {
                "key": f"{role}.neutral",
                "variant_id": f"variant-{role}",
                "asset_path": path,
                "png_sha256": sha,
                "stage_slot": slot,
            },
            "source_anchor": {"x": 0, "y": 0},
            "target_anchor": {"x": x, "y": 0},
            "scale": {"numerator": 1, "denominator": 1},
            "translation": {"x": 0, "y": 0},
            "z_order": z,
            "alpha_mode": "straight-preserve",
        }

    def _write_package(self, package_id: str, png_path: str, sidecar_path: str, png: bytes, sha: str) -> None:
        directory = self.package_root / package_id
        directory.mkdir()
        (directory / Path(png_path).parent).mkdir(parents=True, exist_ok=True)
        (directory / png_path).write_bytes(png)
        sidecar = canonical_bytes({"width": 1, "height": 1})
        (directory / sidecar_path).write_bytes(sidecar)
        (directory / "package-manifest.json").write_bytes(
            canonical_bytes(
                {
                    "items": [
                        {
                            "png_path": png_path,
                            "png_sha256": sha,
                            "sidecar_path": sidecar_path,
                            "sidecar_sha256": hashlib.sha256(sidecar).hexdigest(),
                        }
                    ]
                }
            )
        )

    def _checked(self) -> dict[str, object]:
        return {"ok": True, "renderer_job": self.renderer_job, "frame_count": self.renderer_job["frame_count"], "span_count": 1}

    def _build(self, **kwargs):
        with patch("ai_illustration.frame_renderer.check_composition_job_package", return_value=self._checked()):
            return build_frame_render_package(
                self.renderer_manifest,
                self.renderer_root,
                self.render_plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
                self.output_root,
                **kwargs,
            )

    def test_png_filters_zero_through_four(self) -> None:
        pixels = bytes((1, 2, 3, 4, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130))
        for filter_type in range(5):
            with self.subTest(filter_type=filter_type):
                self.assertEqual(decode_rgba_png(filtered_png(2, 2, pixels, filter_type)), RGBAImage(2, 2, pixels))

    def test_png_structure_crc_filter_and_dimension_fail_closed(self) -> None:
        png = bytearray(self.red)
        png[-1] ^= 1
        with self.assertRaisesRegex(FrameRenderError, "PNG_CRC"):
            decode_rgba_png(bytes(png))
        with self.assertRaisesRegex(FrameRenderError, "PNG_FILTER"):
            decode_rgba_png(filtered_png(1, 1, bytes((1, 2, 3, 4)), 5))
        with self.assertRaisesRegex(FrameRenderError, "PNG_DIMENSION_MISMATCH"):
            decode_rgba_png(self.red, expected_width=2)

    def test_png_decompression_and_chunk_validation_are_bounded(self) -> None:
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        bomb = (
            PNG_SIGNATURE
            + chunk(b"IHDR", ihdr)
            + chunk(b"sRGB", b"\x00")
            + chunk(b"IDAT", zlib.compress(b"\x00" + b"\x01" * 1_000_000, 9))
            + chunk(b"IEND", b"")
        )
        with self.assertRaisesRegex(FrameRenderError, "PNG_DECODE_LIMIT"):
            decode_rgba_png(bomb)
        for payload, code in (
            (rgba_png_with_chunk(b"tRNS", b"\x00" * 6), "PNG_FORMAT"),
            (rgba_png_with_chunk(b"1BAD"), "PNG_CHUNK_TYPE"),
            (rgba_png_with_chunk(b"abca"), "PNG_CHUNK_RESERVED"),
        ):
            with self.subTest(code=code):
                with self.assertRaisesRegex(FrameRenderError, code):
                    decode_rgba_png(payload)

    def test_pixel_center_placement_alpha_and_z_order(self) -> None:
        assets = {
            ("boke", "red.png", self.red_sha): RGBAImage(1, 1, bytes((255, 0, 0, 255))),
            ("tsukkomi", "blue.png", self.blue_sha): RGBAImage(1, 1, bytes((0, 0, 255, 128))),
        }
        lower = self._placement("boke", "red.png", self.red_sha, 0, 0)
        upper = self._placement("tsukkomi", "blue.png", self.blue_sha, 0, 1)
        image = _render_frame({"width": 2, "height": 1, "background_rgba": [0, 0, 0, 0]}, [upper, lower], assets)
        self.assertEqual(image.pixels[:4], bytes((127, 0, 128, 255)))
        self.assertEqual(image.pixels[4:], bytes((0, 0, 0, 0)))

    def test_dry_run_write_idempotency_and_checker(self) -> None:
        first = self._build()
        self.assertEqual(first, self._build())
        self.assertFalse(self.output_root.exists())
        written = self._build(write=True)
        repeated = self._build(write=True)
        self.assertTrue(written["written"])
        self.assertFalse(repeated["written"])
        directory = self.output_root / written["frame_render"]["id"]
        self.assertEqual(decode_rgba_png((directory / "frames/00000000.png").read_bytes()).pixels[:4], bytes((255, 0, 0, 255)))
        self.assertTrue((directory / FRAME_INVENTORY).is_file())
        with patch("ai_illustration.frame_renderer.check_composition_job_package", return_value=self._checked()):
            result = check_frame_render_package(
                directory / FRAME_RENDER_MANIFEST,
                self.output_root,
                self.renderer_root,
                self.render_plan_root,
                self.audio_preview_root,
                self.preview_root,
                self.package_root,
                self.audio_root,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["frame_count"], 2)

    def test_fractional_final_frame_uses_millisecond_rational_time(self) -> None:
        self.renderer_job.update({"fps_num": 2, "fps_den": 1, "frame_count": 3})
        self.transforms["frame_count"] = 3
        self.transforms["spans"][0].update({"end_frame": 3, "end_time_num": 2500, "time_den": 2})
        self._write_renderer_inventories()
        result = self._build(write=True)
        directory = self.output_root / result["frame_render"]["id"]
        inventory = json.loads((directory / FRAME_INVENTORY).read_text(encoding="utf-8"))
        self.assertEqual(inventory["time_unit"], "milliseconds")
        self.assertEqual(
            [(item["start_time_num"], item["end_time_num"], item["time_den"]) for item in inventory["frames"]],
            [(0, 1000, 2), (1000, 2000, 2), (2000, 2500, 2)],
        )

    def test_output_overlap_is_rejected_in_both_directions(self) -> None:
        original = self.output_root
        for output in (self.package_root / "boke-package" / "nested", self.root):
            with self.subTest(output=output):
                self.output_root = output
                with self.assertRaisesRegex(FrameRenderError, "OUTPUT_OVERLAPS_SOURCE"):
                    self._build(write=True)
        self.output_root = original

    def test_tampered_frame_extra_file_and_source_fail(self) -> None:
        written = self._build(write=True)
        directory = self.output_root / written["frame_render"]["id"]
        frame = directory / "frames/00000000.png"
        original = frame.read_bytes()
        frame.write_bytes(original + b"x")
        with patch("ai_illustration.frame_renderer.check_composition_job_package", return_value=self._checked()):
            with self.assertRaisesRegex(FrameRenderError, "FILE_MISMATCH"):
                check_frame_render_package(
                    directory / FRAME_RENDER_MANIFEST,
                    self.output_root,
                    self.renderer_root,
                    self.render_plan_root,
                    self.audio_preview_root,
                    self.preview_root,
                    self.package_root,
                    self.audio_root,
                )
        frame.write_bytes(original)
        (directory / "extra.txt").write_text("extra", encoding="utf-8")
        with patch("ai_illustration.frame_renderer.check_composition_job_package", return_value=self._checked()):
            with self.assertRaisesRegex(FrameRenderError, "FILE_SET_MISMATCH"):
                check_frame_render_package(
                    directory / FRAME_RENDER_MANIFEST,
                    self.output_root,
                    self.renderer_root,
                    self.render_plan_root,
                    self.audio_preview_root,
                    self.preview_root,
                    self.package_root,
                    self.audio_root,
                )
        (self.package_root / "boke-package/variants/boke.png").write_bytes(self.red + b"x")
        with self.assertRaisesRegex(FrameRenderError, "ASSET_SHA256"):
            self._build()

    def test_invalid_timing_fails_closed(self) -> None:
        self.transforms["spans"][0]["time_den"] = 2
        self._write_renderer_inventories()
        with self.assertRaisesRegex(FrameRenderError, "SPAN_TIME_BINDING"):
            self._build()

    def test_no_external_execution_metadata(self) -> None:
        payload = canonical_json(self._build()["frame_render"]).decode("ascii").lower()
        for forbidden in ("ffmpeg", "subprocess", "http://", "https://", "credential", "secret", "video"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
