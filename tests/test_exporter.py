from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from ai_illustration.exporter import ExportError, build_export_package, check_export_package
from ai_illustration.naming import canonical_json, content_identifier
from ai_illustration.variants import plan_variant_set


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_canonical(path: Path, data: dict[str, object]) -> None:
    path.write_bytes(canonical_json(data) + b"\n")


def _png(pixel: tuple[int, ...], *, width: int = 1, height: int = 1, srgb: bool = True, color_type: int = 6) -> bytes:
    channels = 4 if color_type == 6 else 2 if color_type == 4 else 3
    if len(pixel) != channels:
        raise ValueError("pixel channel count mismatch")

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    row = b"\x00" + bytes(pixel) * width
    payload = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    )
    if srgb:
        payload += chunk(b"sRGB", b"\x00")
    payload += chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b"")
    return payload


class ExporterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manifests = self.root / "manifests"
        self.sources = self.root / "sources"
        self.approvals = self.root / "approvals"
        self.output = self.root / "output"
        self.manifests.mkdir()
        self.sources.mkdir()
        self.approvals.mkdir()

        candidate_payload = _png((0, 0, 0, 0))
        candidate_sha = hashlib.sha256(candidate_payload).hexdigest()
        (self.manifests / "candidate.png").write_bytes(candidate_payload)
        _write_json(self.manifests / "character.json", {
            "kind": "character-spec", "schema_version": "1.0", "id": "boke",
            "version": "v001", "role": "boke", "review_status": "approved",
            "identity_anchors": ["fixture"], "license_status": "approved",
        })
        _write_json(self.manifests / "style.json", {
            "kind": "style-profile", "schema_version": "1.0", "id": "rough-flat",
            "version": "v001", "line": {}, "palette": {},
            "anti_ai_checks": ["fixture"], "license_status": "approved",
        })
        _write_json(self.manifests / "request.json", {
            "kind": "generation-request", "schema_version": "1.0", "id": "request-demo",
            "character_ref": "boke@v001", "style_ref": "rough-flat@v001",
            "pose": "neutral", "expression": "neutral", "crop": "full", "facing": "front",
            "tool_id": "fixture-tool", "model_id": "fixture-model",
            "license_status": "approved", "config": {}, "output_intent": "evaluation",
            "provenance": {"source": "fixture"},
        })
        _write_json(self.manifests / "candidate.json", {
            "kind": "candidate-asset", "schema_version": "1.0", "id": "candidate-demo",
            "request_ref": "request-demo", "path": "candidate.png", "sha256": candidate_sha,
            "width": 1, "height": 1, "color_space": "sRGB", "has_alpha": True,
            "media_type": "image/png", "status": "technically_valid",
            "provenance": {"source": "fixture"},
        })
        _write_json(self.manifests / "review.json", {
            "kind": "review-decision", "schema_version": "1.0", "id": "review-demo",
            "candidate_ref": "candidate-demo", "candidate_request_ref": "request-demo",
            "candidate_sha256": candidate_sha, "decision": "accept", "reviewer": "owner",
            "timestamp": "2026-08-02T00:00:00Z", "categories": [],
        })
        self.matrix = {
            "combinations": [
                {"expression": "smile", "pose": "talking", "facing": "front", "crop": "full"},
                {"expression": "neutral", "pose": "listening", "facing": "front", "crop": "full", "mouth_state": "closed"},
            ]
        }
        self.variant_set = plan_variant_set(self.manifests, "candidate-demo", self.matrix, "evaluation")
        self.variant_set_path = self.root / "variant-set.json"
        _write_json(self.variant_set_path, self.variant_set)
        self._restore_sources()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _clear(self, directory: Path) -> None:
        for child in list(directory.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()

    def _restore_sources(self) -> None:
        self._clear(self.sources)
        for variant in self.variant_set["variants"]:
            pixel = (255, 0, 0, 255) if variant["expression"] == "neutral" else (0, 0, 255, 255)
            (self.sources / f"{variant['id']}.png").write_bytes(_png(pixel))

    def _build(
        self,
        output: Path | None = None,
        *,
        write: bool = False,
        variant_set_path: Path | None = None,
        approval_root: Path | None = None,
    ) -> dict[str, object]:
        return build_export_package(
            variant_set_path or self.variant_set_path,
            self.manifests,
            self.sources,
            output or self.output,
            approval_root=approval_root,
            write=write,
        )

    def _production_variant_set(self) -> tuple[dict[str, object], Path]:
        production = plan_variant_set(self.manifests, "candidate-demo", self.matrix, "production")
        path = self.root / "variant-set-production.json"
        _write_json(path, production)
        return production, path

    def _write_approvals(self, variant_set: dict[str, object]) -> None:
        self._clear(self.approvals)
        for variant in variant_set["variants"]:
            png_path = self.sources / f"{variant['id']}.png"
            core = {
                "kind": "variant-review-decision",
                "schema_version": "1.0",
                "variant_set_ref": variant_set["id"],
                "variant_id": variant["id"],
                "png_sha256": hashlib.sha256(png_path.read_bytes()).hexdigest(),
                "decision": "accept",
                "reviewer": "owner",
            }
            review = {"id": content_identifier("variant-review", core, 20), **core}
            _write_json(self.approvals / f"{variant['id']}.json", review)

    def test_deterministic_dry_run_matches_fixtures_and_does_not_write(self) -> None:
        first = self._build()
        second = self._build()
        package_fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "export" / "package-manifest.json").read_text(encoding="utf-8")
        )
        index_fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "export" / "paper-theater-index.json").read_text(encoding="utf-8")
        )
        self.assertEqual(first, second)
        self.assertEqual(first["package"], package_fixture)
        self.assertEqual(first["paper_theater_index"], index_fixture)
        self.assertFalse(self.output.exists())

    def test_write_is_byte_identical_atomic_idempotent_and_checkable(self) -> None:
        result = self._build(write=True)
        self.assertTrue(result["published"])
        package_root = self.output / result["package_directory"]
        for item in result["package"]["items"]:
            source = self.sources / f"{item['variant_id']}.png"
            exported = package_root / item["png_path"]
            self.assertEqual(source.read_bytes(), exported.read_bytes())
        checked = check_export_package(package_root / "package-manifest.json", self.output)
        self.assertTrue(checked["ok"])
        repeated = self._build(write=True)
        self.assertTrue(repeated["idempotent"])
        self.assertFalse(repeated["published"])

    def test_production_requires_included_exact_byte_bound_accept_reviews(self) -> None:
        production, production_path = self._production_variant_set()
        with self.assertRaisesRegex(ExportError, "PRODUCTION_VARIANT_REVIEW_REQUIRED"):
            self._build(variant_set_path=production_path)
        self._write_approvals(production)
        production_output = self.root / "production-output"
        result = self._build(
            production_output,
            write=True,
            variant_set_path=production_path,
            approval_root=self.approvals,
        )
        self.assertEqual(result["package"]["intent"], "production")
        self.assertTrue(all("variant_review_path" in item for item in result["package"]["items"]))
        package_root = production_output / result["package_directory"]
        for item in result["package"]["items"]:
            self.assertTrue((package_root / item["variant_review_path"]).is_file())
        self.assertTrue(
            check_export_package(package_root / "package-manifest.json", production_output)["ok"]
        )

        first = production["variants"][0]
        review_path = self.approvals / f"{first['id']}.json"
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["png_sha256"] = "0" * 64
        review_core = {key: value for key, value in review.items() if key != "id"}
        review["id"] = content_identifier("variant-review", review_core, 20)
        _write_json(review_path, review)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_CHECKSUM"):
            self._build(variant_set_path=production_path, approval_root=self.approvals)

        self._write_approvals(production)
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["decision"] = "reject"
        review_core = {key: value for key, value in review.items() if key != "id"}
        review["id"] = content_identifier("variant-review", review_core, 20)
        _write_json(review_path, review)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_NOT_ACCEPTED"):
            self._build(variant_set_path=production_path, approval_root=self.approvals)

    def test_export_check_rejects_tampered_production_review_binding(self) -> None:
        production, production_path = self._production_variant_set()
        self._write_approvals(production)
        output = self.root / "tampered-production"
        result = self._build(output, write=True, variant_set_path=production_path, approval_root=self.approvals)
        package_root = output / result["package_directory"]
        first_item = result["package"]["items"][0]
        review_path = package_root / first_item["variant_review_path"]
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["png_sha256"] = "0" * 64
        review_core = {key: value for key, value in review.items() if key != "id"}
        review["id"] = content_identifier("variant-review", review_core, 20)
        _write_canonical(review_path, review)
        with self.assertRaisesRegex(ExportError, "VARIANT_REVIEW_FILE_MISMATCH"):
            check_export_package(package_root / "package-manifest.json", output)

    def test_evaluation_rejects_approval_root_and_embedded_approval_claims(self) -> None:
        with self.assertRaisesRegex(ExportError, "APPROVAL_ROOT_NOT_ALLOWED"):
            self._build(approval_root=self.approvals)
        output = self.root / "evaluation-claim"
        result = self._build(output, write=True)
        old_root = output / result["package_directory"]

        malicious_item_package = copy.deepcopy(result["package"])
        malicious_item = malicious_item_package["items"][0]
        malicious_item["variant_review_ref"] = "variant-review-" + "0" * 20
        malicious_item["variant_review_path"] = f"reviews/{malicious_item['variant_id']}.json"
        malicious_item["variant_review_sha256"] = "0" * 64
        item_core = {key: value for key, value in malicious_item_package.items() if key != "id"}
        malicious_item_package["id"] = content_identifier("variant-export-package", item_core, 20)
        item_root = output / malicious_item_package["id"]
        shutil.copytree(old_root, item_root)
        _write_canonical(item_root / "package-manifest.json", malicious_item_package)
        with self.assertRaisesRegex(ExportError, "EVALUATION_REVIEW_CLAIM"):
            check_export_package(item_root / "package-manifest.json", output)

        malicious_sidecar_package = copy.deepcopy(result["package"])
        sidecar_item = malicious_sidecar_package["items"][0]
        sidecar_path = old_root / sidecar_item["sidecar_path"]
        malicious_sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        malicious_sidecar["variant_review_ref"] = "variant-review-" + "0" * 20
        malicious_sidecar["variant_review_path"] = f"reviews/{sidecar_item['variant_id']}.json"
        malicious_sidecar["variant_review_sha256"] = "0" * 64
        malicious_sidecar["variant_reviewer"] = "attacker"
        malicious_sidecar_payload = canonical_json(malicious_sidecar) + b"\n"
        sidecar_item["sidecar_sha256"] = hashlib.sha256(malicious_sidecar_payload).hexdigest()
        sidecar_core = {key: value for key, value in malicious_sidecar_package.items() if key != "id"}
        malicious_sidecar_package["id"] = content_identifier("variant-export-package", sidecar_core, 20)
        sidecar_root = output / malicious_sidecar_package["id"]
        shutil.copytree(old_root, sidecar_root)
        (sidecar_root / sidecar_item["sidecar_path"]).write_bytes(malicious_sidecar_payload)
        _write_canonical(sidecar_root / "package-manifest.json", malicious_sidecar_package)
        with self.assertRaisesRegex(ExportError, "EVALUATION_REVIEW_CLAIM"):
            check_export_package(sidecar_root / "package-manifest.json", output)

    def test_missing_extra_malformed_dimension_srgb_and_alpha_fail_closed(self) -> None:
        first_id = self.variant_set["variants"][0]["id"]
        target = self.sources / f"{first_id}.png"
        target.unlink()
        with self.assertRaisesRegex(ExportError, "SOURCE_MISSING"):
            self._build()
        self._restore_sources()
        (self.sources / "extra.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(ExportError, "EXTRA_SOURCE_FILE"):
            self._build()
        self._restore_sources()
        target = self.sources / f"{first_id}.png"
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(ExportError, "PNG_STRUCTURE"):
            self._build()
        self._restore_sources()
        target.write_bytes(_png((255, 0, 0, 255), width=2))
        with self.assertRaisesRegex(ExportError, "PNG_DIMENSION_MISMATCH"):
            self._build()
        self._restore_sources()
        target.write_bytes(_png((255, 0, 0, 255), srgb=False))
        with self.assertRaisesRegex(ExportError, "PNG_SRGB_REQUIRED"):
            self._build()
        self._restore_sources()
        target.write_bytes(_png((255, 0, 0), color_type=2))
        with self.assertRaisesRegex(ExportError, "PNG_STRUCTURE"):
            self._build()

    def test_roots_symlinks_and_defensive_duplicate_or_unsafe_plans_fail(self) -> None:
        for unsafe_output in (self.sources / "out", self.manifests / "out"):
            with self.assertRaisesRegex(ExportError, "ROOT_OVERLAP"):
                build_export_package(
                    self.variant_set_path, self.manifests, self.sources, unsafe_output, write=False
                )
        if hasattr(os, "symlink"):
            outside = self.root / "outside.png"
            outside.write_bytes(_png((1, 2, 3, 255)))
            link = self.sources / "link.png"
            try:
                link.symlink_to(outside)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(ExportError, "SOURCE_SYMLINK"):
                    self._build()
                link.unlink()

        duplicate = copy.deepcopy(self.variant_set)
        duplicate["variants"][1]["paper_theater_key"] = duplicate["variants"][0]["paper_theater_key"]
        with patch("ai_illustration.exporter.check_variant_set", return_value=duplicate):
            with self.assertRaisesRegex(ExportError, "DUPLICATE_PAPER_THEATER_KEY"):
                self._build()
        unsafe = copy.deepcopy(self.variant_set)
        unsafe["variants"][0]["path"] = "../escape.png"
        with patch("ai_illustration.exporter.check_variant_set", return_value=unsafe):
            with self.assertRaises(ExportError):
                self._build()

    def test_atomic_failure_leaves_no_published_or_staging_package(self) -> None:
        with patch("ai_illustration.exporter.os.replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                self._build(write=True)
        if self.output.exists():
            self.assertEqual(list(self.output.iterdir()), [])

    def test_conflicts_and_modified_package_files_fail_closed(self) -> None:
        result = self._build(write=True)
        package_root = self.output / result["package_directory"]
        first_item = result["package"]["items"][0]
        (package_root / first_item["png_path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ExportError, "PACKAGE_FILE_MISMATCH"):
            check_export_package(package_root / "package-manifest.json", self.output)
        with self.assertRaisesRegex(ExportError, "OUTPUT_CONFLICT"):
            self._build(write=True)

        for kind in ("sidecar", "index", "manifest"):
            out = self.root / f"check-{kind}"
            fresh = self._build(out, write=True)
            root = out / fresh["package_directory"]
            if kind == "sidecar":
                path = root / fresh["package"]["items"][0]["sidecar_path"]
                path.write_bytes(path.read_bytes() + b" ")
                code = "PACKAGE_FILE_MISMATCH"
            elif kind == "index":
                path = root / "paper-theater-index.json"
                path.write_bytes(path.read_bytes() + b" ")
                code = "INDEX_CHECKSUM_MISMATCH"
            else:
                path = root / "package-manifest.json"
                data = json.loads(path.read_text(encoding="utf-8"))
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                code = "PACKAGE_CANONICAL"
            with self.assertRaisesRegex(ExportError, code):
                check_export_package(root / "package-manifest.json", out)

    def test_output_contains_no_remote_execution_or_secret_material(self) -> None:
        text = json.dumps(self._build(), sort_keys=True)
        for forbidden in ("http://", "https://", "credential", "secret", "subprocess", "execute"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
