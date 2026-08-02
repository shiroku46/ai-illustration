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


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        len(payload).to_bytes(4, "big")
        + kind
        + payload
        + (zlib.crc32(kind + payload) & 0xFFFFFFFF).to_bytes(4, "big")
    )


def valid_rgba_png(width: int = 1, height: int = 1) -> bytes:
    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes([8, 6, 0, 0, 0])
    )
    rows = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", ihdr)
        + png_chunk(b"sRGB", b"\x00")
        + png_chunk(b"IDAT", zlib.compress(rows))
        + png_chunk(b"IEND", b"")
    )


class ValidationTests(unittest.TestCase):
    def _fixture_set(self) -> dict[str, dict]:
        return {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in FIXTURES.glob("*.json")
        }

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
        candidate = documents["candidate-asset.json"]
        candidate.update({"path": path, "sha256": checksum, "width": 1, "height": 1, "status": "technically_valid"})
        review = documents["review-decision.json"]
        review.update({"candidate_sha256": checksum, "decision": "accept"})
        export = documents["export-manifest.json"]
        export.update({"sha256": checksum, "width": 1, "height": 1})
        export["path"], export["sidecar_path"] = export_paths(
            character_id="boke",
            crop=export["crop"],
            facing=export["facing"],
            pose=export["pose"],
            expression=export["expression"],
            version=export["version"],
            sha256=checksum,
        )
        return checksum

    def test_valid_fixture_set_passes(self) -> None:
        self.assertEqual(validate_path(FIXTURES), [])

    def test_missing_required_field_fails_closed(self) -> None:
        codes = self._validate_modified(lambda docs: docs["character-spec.json"].pop("role"))
        self.assertIn("MISSING_FIELD", codes)

    def test_unsafe_path_and_bad_checksum_fail(self) -> None:
        def mutate(docs):
            docs["candidate-asset.json"]["path"] = "../outside.png"
            docs["candidate-asset.json"]["sha256"] = "bad"
        codes = self._validate_modified(mutate)
        self.assertIn("UNSAFE_PATH", codes)
        self.assertIn("CHECKSUM", codes)

    def test_unresolved_reference_fails(self) -> None:
        codes = self._validate_modified(lambda docs: docs["candidate-asset.json"].__setitem__("request_ref", "missing-request"))
        self.assertIn("UNRESOLVED_REFERENCE", codes)

    def test_unready_candidate_cannot_be_accepted(self) -> None:
        def mutate(docs):
            docs["candidate-asset.json"]["status"] = "received"
            docs["review-decision.json"]["decision"] = "accept"
        self.assertIn("NOT_REVIEW_READY", self._validate_modified(mutate))

    def test_export_requires_accept_and_matching_metadata(self) -> None:
        def mutate(docs):
            docs["review-decision.json"]["decision"] = "shortlist"
            docs["export-manifest.json"]["status"] = "validated"
            docs["export-manifest.json"]["width"] = 1024
        codes = self._validate_modified(mutate)
        self.assertIn("NOT_APPROVED", codes)
        self.assertIn("EXPORT_MISMATCH", codes)

    def test_unknown_provenance_fails(self) -> None:
        codes = self._validate_modified(lambda docs: docs["generation-request.json"].__setitem__("provenance", {}))
        self.assertIn("UNKNOWN_PROVENANCE", codes)

    def test_export_requires_approved_source_chain(self) -> None:
        def mutate(docs):
            docs["generation-request.json"]["license_status"] = "rejected"
            docs["character-spec.json"]["review_status"] = "draft"
            docs["style-profile.json"]["license_status"] = "unreviewed"
        self.assertIn("SOURCE_NOT_APPROVED", self._validate_modified(mutate))

    def test_export_metadata_must_match_source_request(self) -> None:
        codes = self._validate_modified(lambda docs: docs["export-manifest.json"].__setitem__("pose", "pointing"))
        self.assertIn("SOURCE_METADATA_MISMATCH", codes)

    def test_review_is_bound_to_candidate_checksum(self) -> None:
        codes = self._validate_modified(lambda docs: docs["review-decision.json"].__setitem__("candidate_sha256", "b" * 64))
        self.assertIn("REVIEW_CHECKSUM_MISMATCH", codes)

    def test_review_is_bound_to_candidate_source_request(self) -> None:
        def mutate(docs):
            other = dict(docs["generation-request.json"])
            other["id"] = "request-other"
            docs["generation-request-other.json"] = other
            docs["candidate-asset.json"]["request_ref"] = "request-other"
        self.assertIn("REVIEW_SOURCE_MISMATCH", self._validate_modified(mutate))

    def test_valid_production_png_bytes_pass(self) -> None:
        payload = valid_rgba_png()
        documents = self._fixture_set()
        self._bind_asset(documents, payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_set(root, documents)
            (root / "asset.png").write_bytes(payload)
            self.assertEqual(validate_path(root), [])

    def test_corrupt_png_with_matching_checksum_is_rejected(self) -> None:
        payload = b"\x89PNG\r\n\x1a\n" + b"not-a-chunk-stream"
        documents = self._fixture_set()
        self._bind_asset(documents, payload)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_set(root, documents)
            (root / "asset.png").write_bytes(payload)
            codes = {item.code for item in validate_path(root)}
        self.assertIn("PNG_STRUCTURE", codes)

    def test_png_with_bad_crc_is_rejected(self) -> None:
        payload = bytearray(valid_rgba_png())
        payload[-1] ^= 0xFF
        documents = self._fixture_set()
        self._bind_asset(documents, bytes(payload))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_set(root, documents)
            (root / "asset.png").write_bytes(payload)
            codes = {item.code for item in validate_path(root)}
        self.assertIn("PNG_STRUCTURE", codes)
