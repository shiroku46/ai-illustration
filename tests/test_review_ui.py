from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
import zlib

from ai_illustration.models import Manifest
from ai_illustration.review_ui import (
    REVIEW_CATEGORIES,
    ReviewUIError,
    create_server,
    load_review_data,
    make_review_decision,
    resolve_beneath,
)
from ai_illustration.validation import validate_document


def _chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + (zlib.crc32(kind + data) & 0xFFFFFFFF).to_bytes(4, "big")


def _png(red: int = 20) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 6, 0, 0, 0])
    raw = bytes([0, red, 40, 60, 255])
    return signature + _chunk(b"IHDR", ihdr) + _chunk(b"sRGB", bytes([0])) + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"")


class ReviewUITest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifests = self.root / "manifests"
        self.assets = self.root / "assets"
        self.manifests.mkdir()
        self.assets.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_json(self, name: str, value: dict[str, object]) -> None:
        (self.manifests / name).write_text(json.dumps(value), encoding="utf-8")

    def seed(self, *, with_image: bool = False) -> bytes | None:
        self.write_json("character.json", {
            "kind": "character-spec", "schema_version": "1.0", "id": "boke",
            "version": "v001", "role": "boke", "review_status": "approved",
            "identity_anchors": ["asymmetric silhouette"], "license_status": "approved",
        })
        self.write_json("request.json", {
            "kind": "generation-request", "schema_version": "1.0", "id": "request-demo",
            "character_ref": "boke@v001", "style_ref": "rough-flat@v001",
            "pose": "standing-neutral", "expression": "neutral", "crop": "full",
            "facing": "front", "tool_id": "fixture-tool", "model_id": "fixture-model",
            "license_status": "approved", "config": {"steps": 1},
            "output_intent": "review", "provenance": {"source": "synthetic-fixture"},
        })
        payload = _png() if with_image else None
        checksum = hashlib.sha256(payload).hexdigest() if payload else "a" * 64
        if payload:
            (self.assets / "candidate-demo.png").write_bytes(payload)
        self.write_json("candidate.json", {
            "kind": "candidate-asset", "schema_version": "1.0", "id": "candidate-demo",
            "request_ref": "request-demo", "path": "candidate-demo.png", "sha256": checksum,
            "width": 1 if payload else 2048, "height": 1 if payload else 2048,
            "color_space": "sRGB", "has_alpha": True, "media_type": "image/png",
            "status": "technically_valid" if payload else "received",
            "provenance": {"source": "synthetic-fixture"},
        })
        return payload

    def test_missing_image_becomes_metadata_placeholder(self) -> None:
        self.seed()
        data = load_review_data(self.manifests, self.assets)
        self.assertEqual([item.payload["id"] for item in data.candidates], ["candidate-demo"])
        self.assertFalse(data.candidates[0].payload["image_available"])
        self.assertIsNone(data.candidates[0].read_verified_asset())

    def test_verified_png_is_available(self) -> None:
        payload = self.seed(with_image=True)
        candidate = load_review_data(self.manifests, self.assets).candidates[0]
        self.assertTrue(candidate.payload["image_available"])
        self.assertEqual(candidate.read_verified_asset(), payload)

    def test_manifest_file_symlink_is_rejected_even_when_target_is_valid(self) -> None:
        self.seed()
        outside = self.root / "outside.json"
        outside.write_text((self.manifests / "character.json").read_text(), encoding="utf-8")
        link = self.manifests / "escape.json"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        with self.assertRaisesRegex(ReviewUIError, "symlinked manifest"):
            load_review_data(self.manifests, self.assets)

    def test_review_export_is_existing_schema_valid(self) -> None:
        self.seed()
        candidate = load_review_data(self.manifests, self.assets).candidates[0].payload
        review = make_review_decision(
            candidate, decision="needs_revision", reviewer="owner",
            categories=["generic_eyes", "line_uniformity", "generic_eyes"],
            notes="retain silhouette", timestamp="2026-08-02T11:00:00Z",
        )
        self.assertEqual(review["categories"], ["generic_eyes", "line_uniformity"])
        self.assertEqual(validate_document(Manifest(Path("review.json"), review)), [])

    def test_review_rejects_unknown_category(self) -> None:
        self.seed()
        candidate = load_review_data(self.manifests, self.assets).candidates[0].payload
        with self.assertRaises(ReviewUIError):
            make_review_decision(candidate, decision="accept", reviewer="owner", categories=["invented"])
        self.assertIn("identity_drift", REVIEW_CATEGORIES)

    def test_path_traversal_and_symlink_escape_are_rejected(self) -> None:
        (self.assets / "ok.png").write_bytes(b"x")
        self.assertEqual(resolve_beneath(self.assets, "ok.png", require_file=True), (self.assets / "ok.png").resolve())
        with self.assertRaises(ReviewUIError):
            resolve_beneath(self.assets, "../outside.png")
        outside = self.root / "outside.png"
        outside.write_bytes(b"x")
        link = self.assets / "link.png"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(ReviewUIError):
            resolve_beneath(self.assets, "link.png", require_file=True)

    def _start_server(self, *, with_image: bool = False):
        self.seed(with_image=with_image)
        server = create_server(self.manifests, self.assets, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_http_server_is_loopback_read_only_and_hardened(self) -> None:
        server, thread = self._start_server()
        self.assertEqual(server.server_address[0], "127.0.0.1")
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/candidates")
            response = connection.getresponse()
            parsed = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            self.assertIn("default-src 'self'", response.getheader("Content-Security-Policy"))
            self.assertEqual(parsed["candidates"][0]["id"], "candidate-demo")
            connection.request("HEAD", "/")
            head = connection.getresponse()
            self.assertEqual(head.status, 200)
            self.assertEqual(head.read(), b"")
            connection.request("POST", "/api/candidates", body=b"{}")
            rejected = connection.getresponse()
            self.assertEqual(rejected.status, 405)
            self.assertEqual(rejected.getheader("Allow"), "GET, HEAD")
            rejected.read()
            connection.request("GET", "/../README.md")
            hidden = connection.getresponse()
            self.assertEqual(hidden.status, 404)
            hidden.read()
            connection.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_asset_is_revalidated_after_server_start(self) -> None:
        original = self.seed(with_image=True)
        server = create_server(self.manifests, self.assets, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        asset = self.assets / "candidate-demo.png"
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/assets/candidate-demo")
            first = connection.getresponse()
            self.assertEqual(first.status, 200)
            self.assertEqual(first.read(), original)
            asset.write_bytes(_png(99))
            connection.request("GET", "/assets/candidate-demo")
            changed = connection.getresponse()
            self.assertEqual(changed.status, 404)
            changed.read()
            outside = self.root / "outside.bin"
            outside.write_bytes(b"private replacement")
            asset.unlink()
            try:
                asset.symlink_to(outside)
            except (OSError, NotImplementedError):
                connection.close()
                return
            connection.request("GET", "/assets/candidate-demo")
            escaped = connection.getresponse()
            self.assertEqual(escaped.status, 404)
            self.assertNotIn(b"private replacement", escaped.read())
            connection.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_payload_order_is_deterministic(self) -> None:
        self.seed()
        first = load_review_data(self.manifests, self.assets).public_payload()
        second = load_review_data(self.manifests, self.assets).public_payload()
        self.assertEqual(json.dumps(first, sort_keys=True, separators=(",", ":")), json.dumps(second, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    unittest.main()
