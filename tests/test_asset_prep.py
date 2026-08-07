from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ai_illustration import asset_prep as ap
from ai_illustration.naming import canonical_json


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _rgba_png(width: int, height: int, pixels: list[tuple[int, int, int, int]], *, srgb: bool = True) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixels[y * width + x])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    payload = ap.PNG_SIGNATURE + _chunk(b"IHDR", ihdr)
    if srgb:
        payload += _chunk(b"sRGB", b"\x00")
    payload += _chunk(b"IDAT", zlib.compress(bytes(rows))) + _chunk(b"IEND", b"")
    return payload


def _gray_alpha_png(width: int, height: int, pixels: list[tuple[int, int]]) -> bytes:
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(pixels[y * width + x])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 4, 0, 0, 0)
    return (
        ap.PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"sRGB", b"\x00")
        + _chunk(b"IDAT", zlib.compress(bytes(rows)))
        + _chunk(b"IEND", b"")
    )


def _canonical(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(canonical_json(value) + b"\n")


class AssetPrepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.package_root = self.root / "packages"
        self.package_root.mkdir()
        self.package_id = "variant-export-package-" + "1" * 20
        self.package_dir = self.package_root / self.package_id
        self.package_dir.mkdir()
        self.output_parent = self.root / "outputs"
        self.output_parent.mkdir()

        pixels = [(0, 0, 0, 0)] * 36
        for y in range(2, 5):
            for x in range(2, 4):
                pixels[y * 6 + x] = (20 + x, 40 + y, 90, 255)
        self.source_png = _rgba_png(6, 6, pixels)
        self.variant_id = "variant-" + "2" * 20
        self.png_relative = f"variants/v1/boke/full/front/standing-neutral/{self.variant_id}.png"
        self.sidecar_relative = f"variants/v1/boke/full/front/standing-neutral/{self.variant_id}.json"
        png_path = self.package_dir / self.png_relative
        sidecar_path = self.package_dir / self.sidecar_relative
        png_path.parent.mkdir(parents=True)
        png_path.write_bytes(self.source_png)
        sidecar = {"width": 6, "height": 6}
        _canonical(sidecar_path, sidecar)
        review_relative = f"reviews/{self.variant_id}.json"
        review_path = self.package_dir / review_relative
        review_path.parent.mkdir(parents=True)
        _canonical(review_path, {"fixture": True})

        self.package = {
            "id": self.package_id,
            "kind": "variant-export-package",
            "schema_version": "1.0",
            "variant_set_ref": "variant-set-" + "3" * 20,
            "variant_set_sha256": "4" * 64,
            "intent": "production",
            "source_candidate_ref": "candidate-demo",
            "source_candidate_sha256": "5" * 64,
            "source_request_ref": "request-demo",
            "review_ref": "review-demo",
            "character_ref": "boke@v001",
            "style_ref": "rough-flat@v001",
            "license_status": "approved",
            "identity_gate": "owner-approved",
            "identity_review_ref": "identity-review-" + "6" * 16,
            "identity_review_sha256": "7" * 64,
            "identity_strategy_id": "reference-baseline",
            "identity_evidence_run_ids": ["identity-run-a", "identity-run-b"],
            "identity_model": {
                "family": "fixture-family",
                "profile_ref": "fixture-model@v001",
                "profile_sha256": "8" * 64,
                "workflow_sha256": "9" * 64,
            },
            "paper_theater_index_path": "paper-theater-index.json",
            "paper_theater_index_sha256": "a" * 64,
            "items": [{
                "variant_id": self.variant_id,
                "paper_theater_key": "boke.boke.neutral.standing-neutral.front.full",
                "png_path": self.png_relative,
                "png_sha256": hashlib.sha256(self.source_png).hexdigest(),
                "sidecar_path": self.sidecar_relative,
                "sidecar_sha256": hashlib.sha256(sidecar_path.read_bytes()).hexdigest(),
                "variant_review_ref": "variant-review-" + "b" * 20,
                "variant_review_path": review_relative,
                "variant_review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            }],
        }
        self.package_manifest = self.package_dir / "package-manifest.json"
        _canonical(self.package_manifest, self.package)
        self.profile = self._profile()
        self.profile_path = self.root / "asset-prep-profile.json"
        _canonical(self.profile_path, self.profile)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _config(self, role: str) -> dict[str, object]:
        return {
            "role": role,
            "target_canvas_width": 10,
            "target_canvas_height": 10,
            "target_anchor_x": 5,
            "target_anchor_y": 8,
            "source_anchor_policy": "bottom-center-visible-bounds",
            "transparent_border_inset": 1,
            "max_border_alpha_pixels": 0,
            "min_visible_pixels": 1,
            "max_semitransparent_fraction": {"numerator": 1, "denominator": 2},
        }

    def _profile(self) -> dict[str, object]:
        profile: dict[str, object] = {
            "kind": "asset-prep-profile",
            "schema_version": "1.0",
            "id": "asset-prep-profile-" + "0" * 16,
            "version": "v001",
            "roles": [self._config("boke"), self._config("tsukkomi")],
        }
        profile["id"] = ap.expected_profile_id(profile)
        return profile

    def _checked(self, package: dict[str, object] | None = None):
        return patch("ai_illustration.asset_prep.check_export_package", return_value={"ok": True, "package": package or self.package, "file_count": 4})

    def _rewrite_source(self, payload: bytes, *, width: int = 6, height: int = 6) -> None:
        png_path = self.package_dir / self.png_relative
        png_path.write_bytes(payload)
        self.package["items"][0]["png_sha256"] = hashlib.sha256(payload).hexdigest()
        sidecar_path = self.package_dir / self.sidecar_relative
        _canonical(sidecar_path, {"width": width, "height": height})
        self.package["items"][0]["sidecar_sha256"] = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
        _canonical(self.package_manifest, self.package)

    def _rewrite_profile(self) -> None:
        self.profile["id"] = ap.expected_profile_id(self.profile)
        _canonical(self.profile_path, self.profile)

    def test_profile_is_content_addressed_and_requires_exact_two_roles(self) -> None:
        roles = ap.validate_profile(self.profile)
        self.assertEqual(set(roles), {"boke", "tsukkomi"})
        changed = copy.deepcopy(self.profile)
        changed["roles"][0]["target_anchor_x"] = 4
        with self.assertRaisesRegex(ap.AssetPrepError, "PROFILE_ID"):
            ap.validate_profile(changed)
        changed["id"] = ap.expected_profile_id(changed)
        self.assertEqual(ap.validate_profile(changed)["boke"]["target_anchor_x"], 4)
        duplicate = copy.deepcopy(self.profile)
        duplicate["roles"][1]["role"] = "boke"
        duplicate["id"] = ap.expected_profile_id(duplicate)
        with self.assertRaisesRegex(ap.AssetPrepError, "PROFILE_ROLE"):
            ap.validate_profile(duplicate)

    def test_check_is_read_only_and_records_exact_metrics_alignment_and_bindings(self) -> None:
        before = {path.relative_to(self.package_root): path.read_bytes() for path in self.package_root.rglob("*") if path.is_file()}
        with self._checked() as checker:
            result = ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)
        checker.assert_called_once()
        self.assertTrue(result["ok"])
        manifest = result["manifest"]
        self.assertEqual(manifest["role"], "boke")
        self.assertEqual(manifest["source_package_ref"], self.package_id)
        self.assertEqual(manifest["source_variant_set_sha256"], self.package["variant_set_sha256"])
        self.assertEqual(manifest["identity_model"], self.package["identity_model"])
        item = manifest["items"][0]
        self.assertEqual(item["variant_review_ref"], self.package["items"][0]["variant_review_ref"])
        self.assertEqual(item["metrics"]["visible_bbox"], {"x": 2, "y": 2, "width": 2, "height": 3})
        self.assertEqual(item["metrics"]["visible_pixels"], 6)
        self.assertEqual(item["metrics"]["semitransparent_fraction"], {"numerator": 0, "denominator": 1})
        self.assertEqual(item["crop_box"], {"x": 2, "y": 2, "width": 2, "height": 3})
        self.assertEqual(item["translation"], {"x": 5, "y": 6})
        after = {path.relative_to(self.package_root): path.read_bytes() for path in self.package_root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(list(self.output_parent.iterdir()), [])

    def test_build_is_deterministic_pixel_exact_atomic_and_source_immutable(self) -> None:
        source_before = (self.package_dir / self.png_relative).read_bytes()
        first = self.output_parent / "first"
        second = self.output_parent / "second"
        with self._checked():
            result1 = ap.build_asset_prep(self.profile_path, self.package_manifest, self.package_root, first)
        with self._checked():
            result2 = ap.build_asset_prep(self.profile_path, self.package_manifest, self.package_root, second)
        self.assertTrue(result1["published"])
        self.assertEqual(result1["manifest"], result2["manifest"])
        inventory1 = {p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()}
        inventory2 = {p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()}
        self.assertEqual(inventory1, inventory2)
        item = result1["manifest"]["items"][0]
        prepared = ap.decode_png((first / item["output_path"]).read_bytes())
        source = ap.decode_png(source_before)
        for y in range(2, 5):
            for x in range(2, 4):
                source_offset = (y * source.width + x) * 4
                target_x = item["translation"]["x"] + x - 2
                target_y = item["translation"]["y"] + y - 2
                target_offset = (target_y * prepared.width + target_x) * 4
                self.assertEqual(source.rgba[source_offset:source_offset + 4], prepared.rgba[target_offset:target_offset + 4])
        self.assertEqual((self.package_dir / self.png_relative).read_bytes(), source_before)
        with self._checked():
            with self.assertRaisesRegex(ap.AssetPrepError, "OUTPUT_NOT_FRESH"):
                ap.build_asset_prep(self.profile_path, self.package_manifest, self.package_root, first)

    def test_only_verified_production_owner_approved_packages_are_accepted(self) -> None:
        cases = (
            ("intent", "evaluation", "PRODUCTION_PACKAGE_REQUIRED"),
            ("license_status", "reviewing", "PRODUCTION_PACKAGE_REQUIRED"),
            ("identity_gate", "evaluation-unlocked", "IDENTITY_GATE_REQUIRED"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                package = copy.deepcopy(self.package)
                package[field] = value
                with self._checked(package):
                    with self.assertRaisesRegex(ap.AssetPrepError, code):
                        ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)

    def test_grayscale_alpha_is_decoded_to_equivalent_rgba(self) -> None:
        decoded = ap.decode_png(_gray_alpha_png(2, 1, [(12, 255), (34, 128)]))
        self.assertEqual(decoded.width, 2)
        self.assertEqual(decoded.height, 1)
        self.assertEqual(decoded.rgba, bytes((12, 12, 12, 255, 34, 34, 34, 128)))

    def test_png_structure_crc_animation_srgb_and_trailing_data_fail_closed(self) -> None:
        valid = self.source_png
        corrupted = bytearray(valid)
        corrupted[-1] ^= 1
        with self.assertRaisesRegex(ap.AssetPrepError, "PNG_CRC"):
            ap.decode_png(bytes(corrupted))
        with self.assertRaisesRegex(ap.AssetPrepError, "PNG_TRAILING_DATA"):
            ap.decode_png(valid + b"trailing")
        no_srgb_pixels = [(0, 0, 0, 0)] * 4
        no_srgb_pixels[3] = (1, 2, 3, 255)
        with self.assertRaisesRegex(ap.AssetPrepError, "PNG_STRUCTURE"):
            ap.decode_png(_rgba_png(2, 2, no_srgb_pixels, srgb=False))
        ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        animated = (
            ap.PNG_SIGNATURE
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"sRGB", b"\x00")
            + _chunk(b"acTL", struct.pack(">II", 1, 0))
            + _chunk(b"IDAT", zlib.compress(b"\x00\x01\x02\x03\xff"))
            + _chunk(b"IEND", b"")
        )
        with self.assertRaisesRegex(ap.AssetPrepError, "PNG_ANIMATION"):
            ap.decode_png(animated)

    def test_isolation_thresholds_and_crop_fit_fail_closed(self) -> None:
        border_pixels = [(0, 0, 0, 0)] * 36
        border_pixels[0] = (1, 2, 3, 255)
        self._rewrite_source(_rgba_png(6, 6, border_pixels))
        with self._checked():
            with self.assertRaisesRegex(ap.AssetPrepError, "BORDER_ALPHA"):
                ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)

        semi_pixels = [(0, 0, 0, 0)] * 36
        semi_pixels[2 * 6 + 2] = (1, 2, 3, 128)
        self._rewrite_source(_rgba_png(6, 6, semi_pixels))
        self.profile["roles"][0]["max_semitransparent_fraction"] = {"numerator": 0, "denominator": 1}
        self._rewrite_profile()
        with self._checked():
            with self.assertRaisesRegex(ap.AssetPrepError, "SEMITRANSPARENT_FRACTION"):
                ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)

        self.profile = self._profile()
        self.profile["roles"][0]["min_visible_pixels"] = 2
        self._rewrite_profile()
        with self._checked():
            with self.assertRaisesRegex(ap.AssetPrepError, "VISIBLE_PIXEL_MINIMUM"):
                ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)

        fit_pixels = [(0, 0, 0, 0)] * 36
        for y in range(2, 5):
            for x in range(2, 4):
                fit_pixels[y * 6 + x] = (1, 2, 3, 255)
        self._rewrite_source(_rgba_png(6, 6, fit_pixels))
        self.profile = self._profile()
        self.profile["roles"][0]["target_anchor_x"] = 0
        self._rewrite_profile()
        with self._checked():
            with self.assertRaisesRegex(ap.AssetPrepError, "CROP_FIT"):
                ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)

    def test_symlink_checksum_dimension_and_output_overlap_fail_closed(self) -> None:
        source = self.package_dir / self.png_relative
        outside = self.root / "outside.png"
        outside.write_bytes(source.read_bytes())
        try:
            source.unlink()
            source.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self._checked():
            with self.assertRaisesRegex(ap.AssetPrepError, "SYMLINK_PATH"):
                ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)
        source.unlink()
        source.write_bytes(outside.read_bytes())

        package = copy.deepcopy(self.package)
        package["items"][0]["png_sha256"] = "0" * 64
        with self._checked(package):
            with self.assertRaisesRegex(ap.AssetPrepError, "SOURCE_CHECKSUM"):
                ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)

        sidecar = self.package_dir / self.sidecar_relative
        _canonical(sidecar, {"width": 5, "height": 6})
        package = copy.deepcopy(self.package)
        package["items"][0]["sidecar_sha256"] = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        with self._checked(package):
            with self.assertRaisesRegex(ap.AssetPrepError, "SOURCE_DIMENSIONS"):
                ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)

        _canonical(sidecar, {"width": 6, "height": 6})
        with self._checked():
            with self.assertRaisesRegex(ap.AssetPrepError, "ROOT_OVERLAP"):
                ap.build_asset_prep(self.profile_path, self.package_manifest, self.package_root, self.package_dir / "prepared")

    def test_atomic_publish_failure_cleans_staging(self) -> None:
        output = self.output_parent / "atomic"
        with self._checked(), patch("ai_illustration.asset_prep.os.replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                ap.build_asset_prep(self.profile_path, self.package_manifest, self.package_root, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.output_parent.iterdir()), [])

    def test_cli_and_schema_contract(self) -> None:
        schema = json.loads((Path(__file__).parents[1] / "schemas" / "asset-prep-profile.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(schema["required"]), ap.PROFILE_FIELDS)
        self.assertEqual(set(schema["$defs"]["role"]["required"]), ap.ROLE_FIELDS)
        buffer = io.StringIO()
        with self._checked(), redirect_stdout(buffer):
            code = ap.main(["check", str(self.profile_path), str(self.package_manifest), "--package-root", str(self.package_root)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(buffer.getvalue())["ok"])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = ap.main(["check", str(self.root / "missing.json"), str(self.package_manifest), "--package-root", str(self.package_root)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buffer.getvalue())["ok"])

    def test_no_remote_execution_or_aesthetic_decision_fields(self) -> None:
        with self._checked():
            result = ap.check_asset_prep(self.profile_path, self.package_manifest, self.package_root)
        text = json.dumps(result, sort_keys=True)
        for forbidden in ("http://", "https://", "credential", "secret", "subprocess", "execute", "score", "rank", "winner", "recommendation"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
