from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "review" / "index.html").read_text(encoding="utf-8")
SCRIPT = (ROOT / "web" / "review" / "app.js").read_text(encoding="utf-8")
DOC = (ROOT / "docs" / "QUALITY_STAGES.md").read_text(encoding="utf-8")


class ReviewUIBrowserContractTest(unittest.TestCase):
    def test_html_exposes_separate_quality_controls_and_status(self) -> None:
        for marker in (
            'id="review-scope"',
            'id="hard-fail-list"',
            'id="resulting-quality-stage"',
            'id="review-gate-status"',
            "transport_smoke_output",
            "creative_candidate",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, HTML)
        self.assertIn('id="download-review" type="button" disabled', HTML)

    def test_browser_consumes_api_vocab_and_defaults_to_technical(self) -> None:
        self.assertIn("payload.hard_fail_categories", SCRIPT)
        self.assertIn("payload.review_scopes", SCRIPT)
        self.assertIn('$("review-scope").value = "technical"', SCRIPT)
        self.assertIn('state.reviewScopes.includes("technical")', SCRIPT)
        self.assertIn('state.reviewScopes.includes("creative")', SCRIPT)

    def test_candidate_and_comparison_cards_show_quality_facts_separately(self) -> None:
        for marker in (
            "technical_status",
            "quality_stage",
            "latest review_scope",
            "latest resulting stage",
            "latest hard fails",
            "qualitySummary(candidate)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, SCRIPT)

    def test_review_id_matches_backend_semantic_identity(self) -> None:
        for marker in (
            "candidate.id,",
            "candidate.request_id,",
            "candidate.sha256,",
            "reviewer,",
            "decision,",
            "reviewScope,",
            "resultingQualityStage,",
            "timestamp,",
            'categories.join(","),',
            'hardFailCategories.join(","),',
            '].join("\\n")',
            "crypto.subtle.digest(\"SHA-256\"",
            "digestHex(digest).slice(0, 12)",
            "`review-${candidateId}-${digestHex(digest).slice(0, 12)}`",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, SCRIPT)

    def test_creative_gate_is_fail_closed(self) -> None:
        for marker in (
            'candidate?.quality_stage === "technical_candidate"',
            'candidate?.technical_status === "technically_valid"',
            "candidate?.image_available === true",
            "globalThis.crypto?.subtle",
            "digest === candidate.sha256 ? digest : null",
            'reviewScope === "creative" && decision === "accept" && hardFailCategories.length',
            "needsLiveImage && !await refreshLiveVerification(candidate)",
            "PACKAGED_QUALITY_STAGES.has(candidate?.quality_stage)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, SCRIPT)

    def test_download_contains_complete_quality_fields_and_exact_preview_bytes(self) -> None:
        for marker in (
            "review_scope: reviewScope",
            "resulting_quality_stage: resultingQualityStage",
            "hard_fail_categories: hardFailCategories",
            'const text = `${JSON.stringify(documentValue, null, 2)}\\n`',
            '$("review-preview").textContent = text',
            "new Blob([text]",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, SCRIPT)

    def test_workspace_remains_local_only_and_read_only(self) -> None:
        self.assertIn('fetch("/api/candidates"', SCRIPT)
        for prohibited in (
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "XMLHttpRequest",
            "eval(",
            "new Function(",
            "http://",
            "https://",
        ):
            with self.subTest(prohibited=prohibited):
                self.assertNotIn(prohibited, SCRIPT)
        self.assertIn("No browser storage, telemetry, upload, server mutation", DOC)


if __name__ == "__main__":
    unittest.main()
