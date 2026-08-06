from __future__ import annotations

from contextlib import redirect_stdout
import copy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ai_illustration import art_direction as ad


PNG = b"\x89PNG\r\n\x1a\nreference-board"
ANTI_GOALS = sorted(ad.REQUIRED_GLOBAL_ANTI_GOALS)


def _role(role: str) -> dict[str, object]:
    return {
        "role": role,
        "silhouette": f"distinct {role} silhouette",
        "body_ratio": "compact stylized proportions",
        "head_exaggeration": "slightly oversized head",
        "hand_exaggeration": "readable enlarged hands",
        "foot_exaggeration": "stable simplified feet",
        "costume_construction": "layered clothing with explicit seams and closures",
        "palette": ["ink", f"{role}-accent", "paper"],
        "line_behavior": "uneven hand-drawn contour with controlled breaks",
        "eye_design": "simple asymmetric eyes without generic mobile-game rendering",
        "shading_ceiling": "one flat shadow shape maximum",
        "front_full_body_neutral_target": "front full-body neutral standing reference",
        "background_isolation_target": "transparent or plain single-color isolation",
        "identity_anchors": [f"{role} hair shape", f"{role} costume topology"],
        "prohibited_ai_traits": ["uniform line polish", "fused anatomy"],
    }


def _profile(checksums: dict[str, str]) -> dict[str, object]:
    return {
        "kind": ad.PROFILE_KIND,
        "schema_version": ad.SCHEMA_VERSION,
        "id": "manzai-duo-direction",
        "version": "v001",
        "status": "reviewing",
        "roles": [_role("boke"), _role("tsukkomi")],
        "global_anti_goals": ANTI_GOALS,
        "visual_references": [
            {
                "id": "boke-board",
                "role": "boke",
                "path": "boke/board.png",
                "media_type": "image/png",
                "sha256": checksums["boke"],
                "purpose": "owner rough design and silhouette board",
            },
            {
                "id": "tsukkomi-board",
                "role": "tsukkomi",
                "path": "tsukkomi/board.png",
                "media_type": "image/png",
                "sha256": checksums["tsukkomi"],
                "purpose": "owner rough design and silhouette board",
            },
        ],
    }


def _review(profile: dict[str, object], decision: str = "approve") -> dict[str, object]:
    value: dict[str, object] = {
        "kind": ad.REVIEW_KIND,
        "schema_version": ad.SCHEMA_VERSION,
        "id": "placeholder",
        "profile_ref": profile["id"],
        "profile_version": profile["version"],
        "profile_sha256": ad.profile_sha256(profile),
        "decision": decision,
        "reviewer": "owner",
        "timestamp": "2026-08-06T01:00:00Z",
        "observations": ["role silhouettes are distinct", "rough line target is explicit"],
    }
    value["id"] = ad.expected_review_id(value)
    return value


class ArtDirectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for role in ("boke", "tsukkomi"):
            directory = self.root / role
            directory.mkdir()
            (directory / "board.png").write_bytes(PNG + role.encode("ascii"))
        self.checksums = {
            role: hashlib.sha256((self.root / role / "board.png").read_bytes()).hexdigest()
            for role in ("boke", "tsukkomi")
        }
        self.profile = _profile(self.checksums)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_complete_profile_and_live_references_pass(self) -> None:
        self.assertEqual(ad.validate_profile(self.profile), [])
        self.assertEqual(ad.validate_live_references(self.profile, self.root), [])

    def test_profile_bytes_and_sha_are_deterministic(self) -> None:
        reordered = json.loads(json.dumps(self.profile, sort_keys=True))
        self.assertEqual(ad.document_bytes(self.profile), ad.document_bytes(reordered))
        self.assertEqual(ad.profile_sha256(self.profile), ad.profile_sha256(reordered))
        self.assertTrue(ad.document_bytes(self.profile).endswith(b"\n"))

    def test_roles_are_exact_and_complete(self) -> None:
        missing = copy.deepcopy(self.profile)
        missing["roles"] = [missing["roles"][0]]
        codes = {item["code"] for item in ad.validate_profile(missing)}
        self.assertIn("ROLE_COVERAGE", codes)

        duplicate = copy.deepcopy(self.profile)
        duplicate["roles"][1]["role"] = "boke"
        codes = {item["code"] for item in ad.validate_profile(duplicate)}
        self.assertIn("ROLE_COVERAGE", codes)

        incomplete = copy.deepcopy(self.profile)
        del incomplete["roles"][0]["line_behavior"]
        diagnostics = ad.validate_profile(incomplete)
        self.assertTrue(any(item["code"] == "MISSING_FIELD" and item["field"] == "roles[0]" for item in diagnostics))

    def test_global_anti_goal_coverage_is_required(self) -> None:
        incomplete = copy.deepcopy(self.profile)
        incomplete["global_anti_goals"].remove("anatomical_collapse")
        diagnostics = ad.validate_profile(incomplete)
        self.assertTrue(any(item["code"] == "ANTI_GOAL_COVERAGE" for item in diagnostics))

    def test_reference_contract_rejects_unsafe_and_unsupported_values(self) -> None:
        unsafe = copy.deepcopy(self.profile)
        unsafe["visual_references"][0]["path"] = "../escape.png"
        self.assertTrue(any(item["code"] == "UNSAFE_PATH" for item in ad.validate_profile(unsafe)))

        unsupported = copy.deepcopy(self.profile)
        unsupported["visual_references"][0]["media_type"] = "image/svg+xml"
        self.assertTrue(any(item["code"] == "MEDIA_TYPE" for item in ad.validate_profile(unsupported)))

        non_string = copy.deepcopy(self.profile)
        non_string["visual_references"][0]["path"] = ["board.png"]
        self.assertTrue(any(item["code"] == "UNSAFE_PATH" for item in ad.validate_profile(non_string)))

    def test_live_reference_failures_are_closed(self) -> None:
        missing = copy.deepcopy(self.profile)
        missing["visual_references"][0]["path"] = "boke/missing.png"
        self.assertTrue(any(item["code"] == "REFERENCE_MISSING" for item in ad.validate_live_references(missing, self.root)))

        changed_path = self.root / "boke" / "board.png"
        changed_path.write_bytes(PNG + b"changed")
        self.assertTrue(any(item["code"] == "REFERENCE_CHECKSUM" for item in ad.validate_live_references(self.profile, self.root)))
        changed_path.write_bytes(PNG + b"boke")

        wrong_media = copy.deepcopy(self.profile)
        wrong_media["visual_references"][0]["media_type"] = "image/jpeg"
        self.assertTrue(any(item["code"] == "REFERENCE_MEDIA" for item in ad.validate_live_references(wrong_media, self.root)))

        with mock.patch.object(ad, "MAX_REFERENCE_BYTES", 4):
            self.assertTrue(any(item["code"] == "REFERENCE_SIZE" for item in ad.validate_live_references(self.profile, self.root)))

    def test_symlinked_reference_and_root_are_rejected(self) -> None:
        target = self.root / "outside.png"
        target.write_bytes(PNG)
        link = self.root / "boke" / "link.png"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are unavailable")
        profile = copy.deepcopy(self.profile)
        profile["visual_references"][0]["path"] = "boke/link.png"
        profile["visual_references"][0]["sha256"] = hashlib.sha256(PNG).hexdigest()
        self.assertTrue(any(item["code"] == "REFERENCE_SYMLINK" for item in ad.validate_live_references(profile, self.root)))

        root_link = self.root.parent / f"{self.root.name}-link"
        try:
            root_link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        try:
            self.assertTrue(any(item["code"] == "REFERENCE_ROOT" for item in ad.validate_live_references(self.profile, root_link)))
        finally:
            root_link.unlink(missing_ok=True)

    def test_exact_owner_approval_passes_and_is_deterministic(self) -> None:
        review = _review(self.profile)
        self.assertEqual(ad.validate_review(review), [])
        self.assertEqual(ad.expected_review_id(review), review["id"])
        self.assertEqual(ad.validate_approval(self.profile, review, self.root), [])

        reordered = copy.deepcopy(review)
        reordered["observations"] = list(reversed(reordered["observations"]))
        self.assertEqual(ad.expected_review_id(reordered), review["id"])

    def test_stale_and_non_approval_reviews_never_authorize_benchmarking(self) -> None:
        stale = _review(self.profile)
        stale["profile_sha256"] = "0" * 64
        stale["id"] = ad.expected_review_id(stale)
        self.assertTrue(any(item["code"] == "PROFILE_BINDING" for item in ad.validate_approval(self.profile, stale, self.root)))

        for decision in ("reject", "needs_revision"):
            with self.subTest(decision=decision):
                review = _review(self.profile, decision)
                diagnostics = ad.validate_approval(self.profile, review, self.root)
                self.assertTrue(any(item["code"] == "NOT_APPROVED" for item in diagnostics))

    def test_profile_edit_invalidates_prior_approval(self) -> None:
        review = _review(self.profile)
        edited = copy.deepcopy(self.profile)
        edited["roles"][0]["silhouette"] = "edited silhouette"
        diagnostics = ad.validate_approval(edited, review, self.root)
        self.assertTrue(any(item["code"] == "PROFILE_BINDING" for item in diagnostics))

    def test_cli_is_deterministic_and_read_only(self) -> None:
        profile_path = self.root / "profile.json"
        review_path = self.root / "review.json"
        profile_path.write_text(json.dumps(self.profile), encoding="utf-8")
        review_path.write_text(json.dumps(_review(self.profile)), encoding="utf-8")
        before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}

        first = StringIO()
        with redirect_stdout(first):
            code = ad.main(["approval-check", str(profile_path), str(review_path), "--reference-root", str(self.root)])
        self.assertEqual(code, 0)
        parsed = json.loads(first.getvalue())
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["diagnostics"], [])

        second = StringIO()
        with redirect_stdout(second):
            self.assertEqual(ad.main(["approval-check", str(profile_path), str(review_path), "--reference-root", str(self.root)]), 0)
        self.assertEqual(first.getvalue(), second.getvalue())
        after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

        invalid = copy.deepcopy(self.profile)
        invalid["roles"] = [invalid["roles"][0]]
        profile_path.write_text(json.dumps(invalid), encoding="utf-8")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(ad.main(["profile-check", str(profile_path), "--reference-root", str(self.root)]), 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_schema_files_are_strict_and_mirror_contract(self) -> None:
        schema_root = Path(__file__).resolve().parents[1] / "schemas"
        profile_schema = json.loads((schema_root / "art-direction-profile.schema.json").read_text(encoding="utf-8"))
        review_schema = json.loads((schema_root / "art-direction-review.schema.json").read_text(encoding="utf-8"))
        self.assertFalse(profile_schema["additionalProperties"])
        self.assertFalse(review_schema["additionalProperties"])
        self.assertEqual(profile_schema["properties"]["kind"]["const"], ad.PROFILE_KIND)
        self.assertEqual(review_schema["properties"]["kind"]["const"], ad.REVIEW_KIND)
        self.assertEqual(set(review_schema["properties"]["decision"]["enum"]), set(ad.REVIEW_DECISIONS))
        anti_schema = profile_schema["properties"]["global_anti_goals"]
        required_constants = {entry["contains"]["const"] for entry in anti_schema["allOf"]}
        self.assertEqual(required_constants, set(ad.REQUIRED_GLOBAL_ANTI_GOALS))

    def test_source_has_no_effectful_modules_or_remote_urls(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "ai_illustration" / "art_direction.py").read_text(encoding="utf-8")
        for prohibited in (
            "subprocess",
            "socket",
            "urllib",
            "requests",
            "http.client",
            "shutil.copy",
            "write_text(",
            "write_bytes(",
            "open(\"w",
            "https://",
            "http://",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, source)


if __name__ == "__main__":
    unittest.main()
