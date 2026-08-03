from __future__ import annotations

import binascii
import hashlib
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
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)


def filtered_png(width: int, height: int, pixels: bytes, filter_type: int) -> bytes:
    stride = width * 4
    previous = bytes(stride)
    rows = bytearray()
    for y in range(height):
        raw = pixels[y * stride : (y + 1) * stride]
        rows.append(filter_type)
        filtered = bytearray(stride)
        for x, value in enumerate(raw):
            left = raw[x - 4] if x >= 4 else 0
            up = previous[x]
            up_left = previous[x - 4] if x >= 4 else 0
            if filter_type == 0:
                predictor = 0
            elif filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                estimate = left + up - up_left
                distances = (abs(estimate - left), abs(estimate - up), abs(estimate - up_left))
                predictor = left if distances[0] <= distances[1] and distances[0] <= distances[2] else (up if distances[1] <= distances[2] else up_left)
            else:
                predictor = 0
            filtered[x] = (value - predictor) & 0xFF
        rows.extend(filtered)
        previous = raw
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"sRGB", b"\x00") + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b"")


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
        for directory in (self.renderer_root, self.render_plan_root, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root):
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
        transforms = {
            "kind": "paper-theater-span-transform-inventory",
            "schema_version": "1.0",
            "renderer_job_ref": self.renderer_id,
            "render_plan_ref": "plan-demo",
            "composition_profile_ref": "profile-demo",
            "frame_count": 2,
            "span_count": 1,
            "spans": [{"index": 0, "start_frame": 0, "end_frame": 2, "start_time_num": 0, "end_time_num": 2, "time_den": 1, "clear": {"op": "clear-canvas", "background_rgba": [0, 0, 0, 0]}, "placements": placements}],
        }
        source_bindings = {
            "kind": "paper-theater-renderer-source-bindings",
            "schema_version": "1.0",
            "renderer_job_ref": self.renderer_id,
            "render_plan": {"id": "plan-demo", "path": "plan-demo/render-plan-manifest.json", "sha256": "a" * 64},
            "composition_profile": {"id": "profile-demo", "path": "composition-profile.json", "sha256": "b" * 64},
            "upstream": {"roles": {"boke": {"package_id": "boke-package"}, "tsukkomi": {"package_id": "tsukkomi-package"}}},
            "audio_placement": {"offset_ms": 0},
            "intent": "evaluation",
            "audio_license_status": "reviewing",
        }
        (self.renderer_dir / "span-transforms.json").write_bytes(canonical_bytes(transforms))
        (self.renderer_dir / "source-bindings.json").write_bytes(canonical_bytes(source_bindings))
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

    def _placement(self, role: str, path: str, sha: str, x: int, z: int) -> dict[str, object]:
        slot = "left" if x == 0 else "right"
        return {"op": "place-source-asset", "role": role, "slot": slot, "asset": {"key": f"{role}.neutral", "variant_id": f"variant-{role}", "asset_path": path, "png_sha256": sha, "stage_slot": slot}, "source_anchor": {"x": 0, "y": 0}, "target_anchor": {"x": x, "y": 0}, "scale": {"numerator": 1, "denominator": 1}, "translation": {"x": 0, "y": 0}, "z_order": z, "alpha_mode": "straight-preserve"}

    def _write_package(self, package_id: str, png_path: str, sidecar_path: str, png: bytes, sha: str) -> None:
        directory = self.package_root / package_id
        directory.mkdir()
        (directory / Path(png_path).parent).mkdir(parents=True, exist_ok=True)
        (directory / png_path).write_bytes(png)
        (directory / sidecar_path).write_bytes(canonical_bytes({"width": 1, "height": 1}))
        (directory / "package-manifest.json").write_bytes(canonical_bytes({"items": [{"png_path": png_path, "png_sha256": sha, "sidecar_path": sidecar_path}]}))

    def _build(self, **kwargs):
        checked = {"ok": True, "renderer_job": self.renderer_job, "frame_count": 2, "span_count": 1}
        with patch("ai_illustration.frame_renderer.check_composition_job_package", return_value=checked):
            return build_frame_render_package(self.renderer_manifest, self.renderer_root, self.render_plan_root, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root, self.output_root, **kwargs)

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

    def test_pixel_center_placement_alpha_and_z_order(self) -> None:
        assets = {("boke", "red.png", self.red_sha): RGBAImage(1, 1, bytes((255, 0, 0, 255))), ("tsukkomi", "blue.png", self.blue_sha): RGBAImage(1, 1, bytes((0, 0, 255, 128)))}
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
        first_frame = decode_rgba_png((directory / "frames/00000000.png").read_bytes())
        self.assertEqual(first_frame.pixels[:4], bytes((255, 0, 0, 255)))
        self.assertEqual(first_frame.pixels[4:8], bytes((0, 0, 255, 128)))
        self.assertTrue((directory / FRAME_INVENTORY).is_file())
        checked = {"ok": True, "renderer_job": self.renderer_job, "frame_count": 2, "span_count": 1}
        with patch("ai_illustration.frame_renderer.check_composition_job_package", return_value=checked):
            result = check_frame_render_package(directory / FRAME_RENDER_MANIFEST, self.output_root, self.renderer_root, self.render_plan_root, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["frame_count"], 2)

    def test_tampered_frame_extra_file_and_source_fail(self) -> None:
        written = self._build(write=True)
        directory = self.output_root / written["frame_render"]["id"]
        frame = directory / "frames/00000000.png"
        original = frame.read_bytes()
        frame.write_bytes(original + b"x")
        checked = {"ok": True, "renderer_job": self.renderer_job, "frame_count": 2, "span_count": 1}
        with patch("ai_illustration.frame_renderer.check_composition_job_package", return_value=checked):
            with self.assertRaisesRegex(FrameRenderError, "FILE_MISMATCH"):
                check_frame_render_package(directory / FRAME_RENDER_MANIFEST, self.output_root, self.renderer_root, self.render_plan_root, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root)
        frame.write_bytes(original)
        (directory / "extra.txt").write_text("extra", encoding="utf-8")
        with patch("ai_illustration.frame_renderer.check_composition_job_package", return_value=checked):
            with self.assertRaisesRegex(FrameRenderError, "FILE_SET_MISMATCH"):
                check_frame_render_package(directory / FRAME_RENDER_MANIFEST, self.output_root, self.renderer_root, self.render_plan_root, self.audio_preview_root, self.preview_root, self.package_root, self.audio_root)
        (self.package_root / "boke-package/variants/boke.png").write_bytes(self.red + b"x")
        with self.assertRaisesRegex(FrameRenderError, "ASSET_SHA256"):
            self._build()

    def test_no_external_execution_metadata(self) -> None:
        payload = canonical_json(self._build()["frame_render"]).decode("ascii").lower()
        for forbidden in ("ffmpeg", "subprocess", "http://", "https://", "credential", "secret", "video"):
            self.assertNotIn(forbidden, payload)


if __name__ == "__main__":
    unittest.main()
