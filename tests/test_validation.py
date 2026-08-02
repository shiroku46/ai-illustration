from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_illustration.naming import export_paths
from ai_illustration.validation import validate_path

FIXTURES = Path(__file__).parent / "fixtures" / "valid"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + kind + payload + (zlib.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big")


def ihdr(width: int = 1, height: int = 1, color_type: int = 6) -> bytes:
    return width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, color_type, 0, 0, 0])


def valid_rgba_png(width: int = 1, height: int = 1) -> bytes:
    rows = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))
    return PNG_SIGNATURE + png_chunk(b"IHDR", ihdr(width, height)) + png_chunk(b"sRGB", b"\x00") + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"IEND", b"")


class ValidationTests(unittest.TestCase):
    def _fixture_set(self) -> dict[str, dict]:
        return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in FIXTURES.glob("*.json")}

    def _write_set(self, root: Path, documents: dict[str, dict]) -> None:
        for name, data in documents.items():
            (root / name).write_text(json.dumps(data), encoding="utf-8")

    def _validate_modified(self, mutate) -> set[str]:
        documents = self._fixture_set()
        mutate(documents)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_set(root, documents)
            return {item.code for item in validate_path(root)}

    def _bind_asset(self, documents: dict[str, dict], payload: bytes, path: str = "asset.png") -> str:
        checksum = hashlib.sha256(payload).hexdigest()
        documents["candidate-asset.json"].update({"path": path, "sha256": checksum, "width": 1, "height": 1, "status": "technically_valid"})
        documents["review-decision.json"].update({"candidate_sha256": checksum, "decision": "accept"})
        export = documents["export-manifest.json"]
        export.update({"sha256": checksum, "width": 1, "height": 1})
        export["path"], export["sidecar_path"] = export_paths(
            character_id="boke", crop=export["crop"], facing=export["facing"],
            pose=export["pose"], expression=export["expression"],
            version=export["version"], sha256=checksum,
        )
        return checksum

    def _validate_asset(self, payload: bytes) -> set[str]:
        documents = self._fixture_set()
        self._bind_asset(documents, payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_set(root, documents)
            (root / "asset.png").write_bytes(payload)
            return {item.code for item in validate_path(root)}

    def test_valid_fixture_set_passes(self):
        self.assertEqual(validate_path(FIXTURES), [])

    def test_missing_required_field_fails_closed(self):
        self.assertIn("MISSING_FIELD", self._validate_modified(lambda d: d["character-spec.json"].pop("role")))

    def test_unsafe_path_and_bad_checksum_fail(self):
        def mutate(d):
            d["candidate-asset.json"]["path"] = "../outside.png"
            d["candidate-asset.json"]["sha256"] = "bad"
        codes = self._validate_modified(mutate)
        self.assertIn("UNSAFE_PATH", codes)
        self.assertIn("CHECKSUM", codes)

    def test_unresolved_reference_fails(self):
        self.assertIn("UNRESOLVED_REFERENCE", self._validate_modified(lambda d: d["candidate-asset.json"].__setitem__("request_ref", "missing-request")))

    def test_unready_candidate_cannot_be_accepted(self):
        def mutate(d):
            d["candidate-asset.json"]["status"] = "received"
            d["review-decision.json"]["decision"] = "accept"
        self.assertIn("NOT_REVIEW_READY", self._validate_modified(mutate))

    def test_export_requires_accept_and_matching_metadata(self):
        def mutate(d):
            d["review-decision.json"]["decision"] = "shortlist"
            d["export-manifest.json"]["status"] = "validated"
            d["export-manifest.json"]["width"] = 1024
        codes = self._validate_modified(mutate)
        self.assertIn("NOT_APPROVED", codes)
        self.assertIn("EXPORT_MISMATCH", codes)

    def test_unknown_provenance_fails(self):
        self.assertIn("UNKNOWN_PROVENANCE", self._validate_modified(lambda d: d["generation-request.json"].__setitem__("provenance", {})))

    def test_export_requires_approved_source_chain(self):
        def mutate(d):
            d["generation-request.json"]["license_status"] = "rejected"
            d["character-spec.json"]["review_status"] = "draft"
            d["style-profile.json"]["license_status"] = "unreviewed"
        self.assertIn("SOURCE_NOT_APPROVED", self._validate_modified(mutate))

    def test_export_metadata_must_match_source_request(self):
        self.assertIn("SOURCE_METADATA_MISMATCH", self._validate_modified(lambda d: d["export-manifest.json"].__setitem__("pose", "pointing")))

    def test_review_is_bound_to_candidate_checksum(self):
        self.assertIn("REVIEW_CHECKSUM_MISMATCH", self._validate_modified(lambda d: d["review-decision.json"].__setitem__("candidate_sha256", "b" * 64)))

    def test_review_is_bound_to_candidate_source_request(self):
        def mutate(d):
            other = dict(d["generation-request.json"])
            other["id"] = "request-other"
            d["generation-request-other.json"] = other
            d["candidate-asset.json"]["request_ref"] = "request-other"
        self.assertIn("REVIEW_SOURCE_MISMATCH", self._validate_modified(mutate))

    def test_valid_production_png_bytes_pass(self):
        self.assertEqual(self._validate_asset(valid_rgba_png()), set())

    def test_corrupt_png_with_matching_checksum_is_rejected(self):
        self.assertIn("PNG_STRUCTURE", self._validate_asset(PNG_SIGNATURE + b"not-a-chunk-stream"))

    def test_png_with_bad_crc_is_rejected(self):
        payload = bytearray(valid_rgba_png())
        payload[-1] ^= 0xFF
        self.assertIn("PNG_STRUCTURE", self._validate_asset(bytes(payload)))

    def test_duplicate_srgb_is_rejected(self):
        rows = b"\x00\x00\x00\x00\x00"
        payload = PNG_SIGNATURE + png_chunk(b"IHDR", ihdr()) + png_chunk(b"sRGB", b"\x00") + png_chunk(b"sRGB", b"\x00") + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"IEND", b"")
        self.assertIn("PNG_STRUCTURE", self._validate_asset(payload))

    def test_late_srgb_is_rejected(self):
        rows = b"\x00\x00\x00\x00\x00"
        payload = PNG_SIGNATURE + png_chunk(b"IHDR", ihdr()) + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"sRGB", b"\x00") + png_chunk(b"IEND", b"")
        self.assertIn("PNG_STRUCTURE", self._validate_asset(payload))

    def test_nonconsecutive_idat_is_rejected(self):
        compressed = zlib.compress(b"\x00\x00\x00\x00\x00")
        midpoint = len(compressed) // 2
        payload = PNG_SIGNATURE + png_chunk(b"IHDR", ihdr()) + png_chunk(b"sRGB", b"\x00") + png_chunk(b"IDAT", compressed[:midpoint]) + png_chunk(b"tEXt", b"x\x00y") + png_chunk(b"IDAT", compressed[midpoint:]) + png_chunk(b"IEND", b"")
        self.assertIn("PNG_STRUCTURE", self._validate_asset(payload))

    def test_decompression_output_is_bounded_to_ihdr(self):
        payload = PNG_SIGNATURE + png_chunk(b"IHDR", ihdr()) + png_chunk(b"sRGB", b"\x00") + png_chunk(b"IDAT", zlib.compress(b"\x00" * 10000)) + png_chunk(b"IEND", b"")
        self.assertIn("PNG_STRUCTURE", self._validate_asset(payload))

    def test_grayscale_alpha_png_rejects_plte(self):
        rows = b"\x00\x00\xff"
        payload = (
            PNG_SIGNATURE
            + png_chunk(b"IHDR", ihdr(color_type=4))
            + png_chunk(b"sRGB", b"\x00")
            + png_chunk(b"PLTE", b"\x00\x00\x00")
            + png_chunk(b"IDAT", zlib.compress(rows))
            + png_chunk(b"IEND", b"")
        )
        self.assertIn("PNG_STRUCTURE", self._validate_asset(payload))


if __name__ == "__main__":
    unittest.main()
