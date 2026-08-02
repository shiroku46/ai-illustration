from __future__ import annotations
import json
from pathlib import Path
import sys, tempfile, unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from ai_illustration.validation import validate_path
FIXTURES=Path(__file__).parent/"fixtures"/"valid"
class ValidationTests(unittest.TestCase):
 def _fixture_set(self):return {p.name:json.loads(p.read_text()) for p in FIXTURES.glob("*.json")}
 def _validate_modified(self,mutate):
  docs=self._fixture_set(); mutate(docs)
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   for name,data in docs.items():(root/name).write_text(json.dumps(data))
   return {item.code for item in validate_path(root)}
 def test_valid_fixture_set_passes(self):self.assertEqual(validate_path(FIXTURES),[])
 def test_missing_required_field_fails_closed(self):
  codes=self._validate_modified(lambda d:d["character-spec.json"].pop("role")); self.assertIn("MISSING_FIELD",codes)
 def test_unsafe_path_and_bad_checksum_fail(self):
  def mutate(d):d["candidate-asset.json"]["path"]="../outside.png"; d["candidate-asset.json"]["sha256"]="bad"
  codes=self._validate_modified(mutate); self.assertIn("UNSAFE_PATH",codes); self.assertIn("CHECKSUM",codes)
 def test_unresolved_reference_fails(self):
  codes=self._validate_modified(lambda d:d["candidate-asset.json"].__setitem__("request_ref","missing-request")); self.assertIn("UNRESOLVED_REFERENCE",codes)
 def test_unready_candidate_cannot_be_accepted(self):
  def mutate(d):d["candidate-asset.json"]["status"]="received"; d["review-decision.json"]["decision"]="accept"
  self.assertIn("NOT_REVIEW_READY",self._validate_modified(mutate))
 def test_export_requires_accept_and_matching_metadata(self):
  def mutate(d):d["review-decision.json"]["decision"]="shortlist"; d["export-manifest.json"]["status"]="validated"; d["export-manifest.json"]["width"]=1024
  codes=self._validate_modified(mutate); self.assertIn("NOT_APPROVED",codes); self.assertIn("EXPORT_MISMATCH",codes)
 def test_unknown_provenance_fails(self):
  self.assertIn("UNKNOWN_PROVENANCE",self._validate_modified(lambda d:d["generation-request.json"].__setitem__("provenance",{})))
 def test_export_requires_approved_source_chain(self):
  def mutate(d):d["generation-request.json"]["license_status"]="rejected"; d["character-spec.json"]["review_status"]="draft"; d["style-profile.json"]["license_status"]="unreviewed"
  self.assertIn("SOURCE_NOT_APPROVED",self._validate_modified(mutate))
 def test_export_metadata_must_match_source_request(self):
  self.assertIn("SOURCE_METADATA_MISMATCH",self._validate_modified(lambda d:d["export-manifest.json"].__setitem__("pose","pointing")))
 def test_review_is_bound_to_candidate_checksum(self):
  self.assertIn("REVIEW_CHECKSUM_MISMATCH",self._validate_modified(lambda d:d["review-decision.json"].__setitem__("candidate_sha256","b"*64)))
 def test_review_is_bound_to_candidate_source_request(self):
  def mutate(d):other=dict(d["generation-request.json"]); other["id"]="request-other"; d["generation-request-other.json"]=other; d["candidate-asset.json"]["request_ref"]="request-other"
  self.assertIn("REVIEW_SOURCE_MISMATCH",self._validate_modified(mutate))
 def test_technically_valid_candidate_requires_matching_png_bytes(self):
  docs=self._fixture_set(); docs["candidate-asset.json"]["status"]="technically_valid"
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   for name,data in docs.items():(root/name).write_text(json.dumps(data))
   codes={item.code for item in validate_path(root)}
  self.assertIn("ASSET_MISSING",codes)
