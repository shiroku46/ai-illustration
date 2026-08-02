from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ai_illustration.validation import validate_path

FIXTURES = Path(__file__).parent / "fixtures" / "valid"

class ValidationTests(unittest.TestCase):
    def _fixture_set(self) -> dict[str, dict]:
        return {path.name: json.loads(path.read_text(encoding="utf-8")) for path in FIXTURES.glob("*.json")}
    def _validate_modified(self, mutate) -> set[str]:
        docs=self._fixture_set(); mutate(docs)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,data in docs.items():(root/name).write_text(json.dumps(data),encoding="utf-8")
            return {item.code for item in validate_path(root)}
    def test_valid_fixture_set_passes(self):self.assertEqual(validate_path(FIXTURES),[])
    def test_missing_required_field_fails_closed(self):
        codes=self._validate_modified(lambda docs:docs["character-spec.json"].pop("role")); self.assertIn("MISSING_FIELD",codes)
    def test_unsafe_path_and_bad_checksum_fail(self):
        def mutate(docs):docs["candidate-asset.json"]["path"]="../outside.png"; docs["candidate-asset.json"]["sha256"]="bad"
        codes=self._validate_modified(mutate); self.assertIn("UNSAFE_PATH",codes); self.assertIn("CHECKSUM",codes)
    def test_unresolved_reference_fails(self):
        codes=self._validate_modified(lambda docs:docs["candidate-asset.json"].__setitem__("request_ref","missing-request")); self.assertIn("UNRESOLVED_REFERENCE",codes)
    def test_unready_candidate_cannot_be_accepted(self):
        codes=self._validate_modified(lambda docs:docs["candidate-asset.json"].__setitem__("status","received")); self.assertIn("NOT_REVIEW_READY",codes)
    def test_export_requires_accept_and_matching_metadata(self):
        def mutate(docs):docs["review-decision.json"]["decision"]="shortlist"; docs["export-manifest.json"]["width"]=1024
        codes=self._validate_modified(mutate); self.assertIn("NOT_APPROVED",codes); self.assertIn("EXPORT_MISMATCH",codes)
    def test_unknown_provenance_fails(self):
        codes=self._validate_modified(lambda docs:docs["generation-request.json"].__setitem__("provenance",{})); self.assertIn("UNKNOWN_PROVENANCE",codes)
    def test_export_requires_approved_source_chain(self):
        def mutate(docs):docs["generation-request.json"]["license_status"]="rejected"; docs["character-spec.json"]["review_status"]="draft"; docs["style-profile.json"]["license_status"]="unreviewed"
        self.assertIn("SOURCE_NOT_APPROVED",self._validate_modified(mutate))
    def test_export_metadata_must_match_source_request(self):
        codes=self._validate_modified(lambda docs:docs["export-manifest.json"].__setitem__("pose","pointing")); self.assertIn("SOURCE_METADATA_MISMATCH",codes)
    def test_review_is_bound_to_candidate_checksum(self):
        codes=self._validate_modified(lambda docs:docs["review-decision.json"].__setitem__("candidate_sha256","b"*64)); self.assertIn("REVIEW_CHECKSUM_MISMATCH",codes)
    def test_review_is_bound_to_candidate_source_request(self):
        def mutate(docs):
            other=dict(docs["generation-request.json"]); other["id"]="request-other"; docs["generation-request-other.json"]=other; docs["candidate-asset.json"]["request_ref"]="request-other"
        self.assertIn("REVIEW_SOURCE_MISMATCH",self._validate_modified(mutate))
