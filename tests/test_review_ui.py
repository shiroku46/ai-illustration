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
from ai_illustration.quality import (
    CREATIVE_CANDIDATE,
    HARD_FAIL_CATEGORIES,
    TECHNICAL_CANDIDATE,
    TRANSPORT_SMOKE_OUTPUT,
)
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

    def seed(
        self,
        *,
        with_image: bool = False,
        quality_stage: str | None = TECHNICAL_CANDIDATE,
    ) -> bytes | None:
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
            "output_intent": "candidate", "provenance": {"source": "synthetic-fixture"},
        })
        payload = _png() if with_image else None
        checksum = hashlib.sha256(payload).hexdigest() if payload else "a" * 64
        if payload:
            (self.assets / "candidate-demo.png").write_bytes(payload)
        candidate: dict[str, object] = {
            "kind": "candidate-asset", "schema_version": "1.0", "id": "candidate-demo",
            "request_ref": "request-demo", "path": "candidate-demo.png", "sha256": checksum,
            "width": 1 if payload else 2048, "height": 1 if payload else 2048,
            "color_space": "sRGB", "has_alpha": True, "media_type": "image/png",
            "status": "technically_valid" if payload else "received",
            "provenance": {"source": "synthetic-fixture"},
        }
        if quality_stage is not None:
            candidate["quality_stage"] = quality_stage
        self.write_json("candidate.json", candidate)
        return payload

    def test_missing_image_becomes_metadata_placeholder(self) -> None:
        self.seed()
        data = load_review_data(self.manifests, self.assets)
        self.assertEqual([item.payload["id"] for item in data.candidates], ["candidate-demo"])
        self.assertFalse(data.candidates[0].payload["image_available"])
        self.assertEqual(data.candidates[0].payload["quality_stage"], TECHNICAL_CANDIDATE)
        self.assertIsNone(data.candidates[0].read_verified_asset())

    def test_verified_png_is_available(self) -> None:
        payload = self.seed(with_image=True)
        candidate = load_review_data(self.manifests, self.assets).candidates[0]
        self.assertTrue(candidate.payload["image_available"])
        self.assertEqual(candidate.read_verified_asset(), payload)

    def test_character_reference_version_must_match(self) -> None:
        self.seed()
        request_path = self.manifests / "request.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["character_ref"] = "boke@v002"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        with self.assertRaisesRegex(ReviewUIError, "mismatched character version"):
            load_review_data(self.manifests, self.assets)

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

    def test_review_export_is_quality_schema_valid(self) -> None:
        self.seed()
        candidate = load_review_data(self.manifests, self.assets).candidates[0].payload
        review = make_review_decision(
            candidate,
            decision="needs_revision",
            reviewer="owner",
            categories=["generic_eyes", "line_uniformity", "generic_eyes"],
            notes="retain silhouette",
            timestamp="2026-08-02T11:00:00Z",
        )
        self.assertEqual(review["categories"], ["generic_eyes", "line_uniformity"])
        self.assertEqual(review["review_scope"], "technical")
        self.assertEqual(review["resulting_quality_stage"], TECHNICAL_CANDIDATE)
        self.assertEqual(review["hard_fail_categories"], [])
        self.assertEqual(validate_document(Manifest(Path("review.json"), review)), [])

    def test_approval_requires_live_candidate_view_and_current_bytes(self) -> None:
        self.seed()
        received = load_review_data(self.manifests, self.assets).candidates[0]
        for decision in ("accept", "shortlist"):
            with self.subTest(status="received", decision=decision):
                with self.assertRaisesRegex(ReviewUIError, "live CandidateView"):
                    make_review_decision(
                        received.payload, decision=decision, reviewer="owner", categories=[],
                        timestamp="2026-08-02T11:00:00Z",
                    )
        self.assertEqual(make_review_decision(
            received.payload, decision="reject", reviewer="owner", categories=["other"],
            timestamp="2026-08-02T11:00:00Z",
        )["decision"], "reject")

        for path in self.manifests.glob("*.json"):
            path.unlink()
        self.seed(with_image=True)
        verified_view = load_review_data(self.manifests, self.assets).candidates[0]
        with self.assertRaisesRegex(ReviewUIError, "live CandidateView"):
            make_review_decision(
                verified_view.payload, decision="accept", reviewer="owner", categories=[],
                timestamp="2026-08-02T11:00:01Z",
            )
        accepted = make_review_decision(
            verified_view, decision="accept", reviewer="owner", categories=[],
            timestamp="2026-08-02T11:00:02Z",
        )
        self.assertEqual(accepted["decision"], "accept")
        self.assertEqual(accepted["resulting_quality_stage"], TECHNICAL_CANDIDATE)

        (self.assets / "candidate-demo.png").write_bytes(_png(99))
        with self.assertRaisesRegex(ReviewUIError, "live verified image"):
            make_review_decision(
                verified_view, decision="shortlist", reviewer="owner", categories=[],
                timestamp="2026-08-02T11:00:03Z",
            )
        (self.assets / "candidate-demo.png").unlink()
        with self.assertRaisesRegex(ReviewUIError, "live verified image"):
            make_review_decision(
                verified_view, decision="accept", reviewer="owner", categories=[],
                timestamp="2026-08-02T11:00:04Z",
            )

    def test_creative_accept_requires_live_technical_candidate_and_no_hard_fail(self) -> None:
        self.seed(with_image=True)
        view = load_review_data(self.manifests, self.assets).candidates[0]
        accepted = make_review_decision(
            view,
            decision="accept",
            reviewer="owner",
            categories=[],
            review_scope="creative",
            hard_fail_categories=[],
            timestamp="2026-08-02T12:00:00Z",
        )
        self.assertEqual(accepted["resulting_quality_stage"], CREATIVE_CANDIDATE)
        self.assertEqual(validate_document(Manifest(Path("review.json"), accepted)), [])
        with self.assertRaisesRegex(ReviewUIError, "hard-fail"):
            make_review_decision(
                view,
                decision="accept",
                reviewer="owner",
                categories=[],
                review_scope="creative",
                hard_fail_categories=["identity_drift"],
                timestamp="2026-08-02T12:00:01Z",
            )

        candidate_path = self.manifests / "candidate.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["quality_stage"] = TRANSPORT_SMOKE_OUTPUT
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        smoke_view = load_review_data(self.manifests, self.assets).candidates[0]
        with self.assertRaisesRegex(ReviewUIError, "technical_candidate"):
            make_review_decision(
                smoke_view,
                decision="reject",
                reviewer="owner",
                categories=[],
                review_scope="creative",
                timestamp="2026-08-02T12:00:02Z",
            )

        candidate.pop("quality_stage")
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        legacy_view = load_review_data(self.manifests, self.assets).candidates[0]
        with self.assertRaisesRegex(ReviewUIError, "packaged quality_stage"):
            make_review_decision(
                legacy_view,
                decision="accept",
                reviewer="owner",
                categories=[],
                review_scope="creative",
                timestamp="2026-08-02T12:00:03Z",
            )

    def test_technical_review_never_promotes_and_id_binds_quality_semantics(self) -> None:
        self.seed(with_image=True)
        view = load_review_data(self.manifests, self.assets).candidates[0]
        technical = make_review_decision(
            view,
            decision="accept",
            reviewer="owner",
            categories=[],
            review_scope="technical",
            timestamp="2026-08-02T13:00:00Z",
        )
        creative = make_review_decision(
            view,
            decision="accept",
            reviewer="owner",
            categories=[],
            review_scope="creative",
            timestamp="2026-08-02T13:00:00Z",
        )
        self.assertEqual(technical["resulting_quality_stage"], TECHNICAL_CANDIDATE)
        self.assertEqual(creative["resulting_quality_stage"], CREATIVE_CANDIDATE)
        self.assertNotEqual(technical["id"], creative["id"])

    def test_imported_review_must_bind_current_request_and_checksum(self) -> None:
        self.seed()
        base = {
            "kind": "review-decision", "schema_version": "1.0", "id": "review-demo",
            "candidate_ref": "candidate-demo", "candidate_request_ref": "request-wrong",
            "candidate_sha256": "a" * 64, "decision": "reject", "reviewer": "owner",
            "timestamp": "2026-08-02T11:00:00Z", "categories": ["other"],
        }
        self.write_json("review.json", base)
        with self.assertRaisesRegex(ReviewUIError, "source request"):
            load_review_data(self.manifests, self.assets)
        base["candidate_request_ref"] = "request-demo"
        base["candidate_sha256"] = "b" * 64
        self.write_json("review.json", base)
        with self.assertRaisesRegex(ReviewUIError, "candidate checksum"):
            load_review_data(self.manifests, self.assets)

    def test_imported_creative_claim_is_revalidated(self) -> None:
        payload = self.seed(with_image=True)
        checksum = hashlib.sha256(payload).hexdigest()
        review = {
            "kind": "review-decision", "schema_version": "1.0", "id": "review-creative",
            "candidate_ref": "candidate-demo", "candidate_request_ref": "request-demo",
            "candidate_sha256": checksum, "decision": "accept", "reviewer": "owner",
            "timestamp": "2026-08-02T14:00:00Z", "categories": [],
            "review_scope": "creative", "resulting_quality_stage": CREATIVE_CANDIDATE,
            "hard_fail_categories": [],
        }
        self.write_json("review.json", review)
        data = load_review_data(self.manifests, self.assets)
        self.assertEqual(data.candidates[0].payload["review_resulting_quality_stage"], CREATIVE_CANDIDATE)

        candidate_path = self.manifests / "candidate.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["quality_stage"] = TRANSPORT_SMOKE_OUTPUT
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
        with self.assertRaisesRegex(ReviewUIError, "non-technical candidate"):
            load_review_data(self.manifests, self.assets)

    def test_review_rejects_unknown_category_and_hard_fail(self) -> None:
        self.seed(with_image=True)
        candidate = load_review_data(self.manifests, self.assets).candidates[0]
        with self.assertRaises(ReviewUIError):
            make_review_decision(candidate, decision="accept", reviewer="owner", categories=["invented"])
        with self.assertRaisesRegex(ReviewUIError, "HARD_FAIL_CATEGORIES"):
            make_review_decision(
                candidate,
                decision="reject",
                reviewer="owner",
                categories=[],
                hard_fail_categories=["invented"],
            )
        self.assertIn("identity_drift", REVIEW_CATEGORIES)
        self.assertIn("identity_drift", HARD_FAIL_CATEGORIES)

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
            self.assertEqual(parsed["candidates"][0]["technical_status"], "received")
            self.assertEqual(parsed["candidates"][0]["quality_stage"], TECHNICAL_CANDIDATE)
            self.assertEqual(parsed["review_scopes"], ["creative", "technical"])
            self.assertEqual(set(parsed["hard_fail_categories"]), set(HARD_FAIL_CATEGORIES))
            connection.request("GET", "/app.js")
            script = connection.getresponse()
            script_body = script.read()
            self.assertEqual(script.status, 200)
            self.assertIn(b"liveVerifiedChecksum", script_body)
            self.assertIn(b"crypto.subtle.digest", script_body)
            self.assertIn(b"image_available", script_body)
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
