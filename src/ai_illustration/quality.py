"""Fail-closed quality-stage separation for generated illustration assets."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

TRANSPORT_SMOKE_INTENT = "transport-smoke"
CANDIDATE_INTENT = "candidate"
TRANSPORT_SMOKE_OUTPUT = "transport_smoke_output"
TECHNICAL_CANDIDATE = "technical_candidate"
CREATIVE_CANDIDATE = "creative_candidate"

PACKAGED_QUALITY_STAGES = {TRANSPORT_SMOKE_OUTPUT, TECHNICAL_CANDIDATE}
QUALITY_STAGES = {*PACKAGED_QUALITY_STAGES, CREATIVE_CANDIDATE}
REVIEW_SCOPES = {"technical", "creative"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HARD_FAIL_CATEGORIES = (
    "malformed_or_missing_limb",
    "broken_joint_or_torso",
    "incoherent_clothing",
    "face_asymmetry",
    "unintended_background",
    "generic_ai_style",
    "identity_drift",
    "isolation_failure",
)


@dataclass(frozen=True)
class QualityGateError(ValueError):
    """A deterministic failure at the creative-candidate boundary."""

    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def packaged_quality_stage(request: dict[str, Any]) -> str:
    """Classify package output without ever granting creative approval."""

    intent = request.get("output_intent")
    if intent == TRANSPORT_SMOKE_INTENT:
        return TRANSPORT_SMOKE_OUTPUT
    if intent in {None, CANDIDATE_INTENT}:
        return TECHNICAL_CANDIDATE
    raise QualityGateError(
        "OUTPUT_INTENT",
        "output_intent must be candidate or transport-smoke",
        "output_intent",
    )


def normalized_hard_fail_categories(value: Any) -> tuple[str, ...]:
    """Return a sorted, unique hard-fail list or fail closed."""

    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise QualityGateError(
            "HARD_FAIL_CATEGORIES",
            "hard_fail_categories must be a list of strings",
            "hard_fail_categories",
        )
    if len(value) != len(set(value)):
        raise QualityGateError(
            "HARD_FAIL_CATEGORIES",
            "hard_fail_categories must not contain duplicates",
            "hard_fail_categories",
        )
    unknown = sorted(set(value) - set(HARD_FAIL_CATEGORIES))
    if unknown:
        raise QualityGateError(
            "HARD_FAIL_CATEGORIES",
            "unknown hard-fail categories: " + ", ".join(unknown),
            "hard_fail_categories",
        )
    return tuple(sorted(value))


def require_creative_candidate(
    candidate: dict[str, Any],
    review: dict[str, Any],
    *,
    request_id: str,
    candidate_id: str | None = None,
) -> None:
    """Require an explicit, current, clean owner creative approval.

    Technical validity is necessary but never sufficient. Transport-smoke
    outputs, legacy records without a stage, technical reviews, stale reviews,
    and creative reviews carrying a hard fail are all rejected.
    """

    if candidate.get("status") != "technically_valid":
        raise QualityGateError(
            "SOURCE_NOT_VALID",
            "source candidate must be technically_valid",
            "source_candidate",
        )

    stage = candidate.get("quality_stage")
    if stage == TRANSPORT_SMOKE_OUTPUT:
        raise QualityGateError(
            "SMOKE_OUTPUT_FORBIDDEN",
            "transport smoke output cannot enter creative or downstream planning",
            "quality_stage",
        )
    if stage != TECHNICAL_CANDIDATE:
        raise QualityGateError(
            "CREATIVE_GATE_REQUIRED",
            "an explicit technical_candidate quality stage is required before creative review",
            "quality_stage",
        )

    candidate_sha256 = candidate.get("sha256")
    if not isinstance(candidate_sha256, str) or not SHA256_RE.fullmatch(candidate_sha256):
        raise QualityGateError(
            "CHECKSUM",
            "source candidate must carry a valid lowercase SHA-256",
            "sha256",
        )
    if candidate.get("request_ref") != request_id:
        raise QualityGateError(
            "STALE_REVIEW",
            "candidate source request does not match the required request",
            "request_ref",
        )
    if candidate_id is not None and candidate.get("id") != candidate_id:
        raise QualityGateError(
            "STALE_REVIEW",
            "candidate id does not match the required candidate",
            "id",
        )
    if candidate_id is not None and review.get("candidate_ref") != candidate_id:
        raise QualityGateError(
            "STALE_REVIEW",
            "review candidate reference does not match the source candidate",
            "candidate_ref",
        )
    if review.get("candidate_request_ref") != request_id:
        raise QualityGateError(
            "STALE_REVIEW",
            "review source request does not match the candidate",
            "candidate_request_ref",
        )
    if review.get("candidate_sha256") != candidate_sha256:
        raise QualityGateError(
            "STALE_REVIEW",
            "review checksum does not match the candidate",
            "candidate_sha256",
        )
    if review.get("decision") != "accept":
        raise QualityGateError(
            "ACCEPT_REVIEW_REQUIRED",
            "latest review decision must be accept",
            "decision",
        )
    reviewer = review.get("reviewer")
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise QualityGateError(
            "REVIEWER_REQUIRED",
            "an explicit reviewer identity is required",
            "reviewer",
        )
    if review.get("review_scope") != "creative":
        raise QualityGateError(
            "CREATIVE_REVIEW_REQUIRED",
            "an explicit creative-scope review is required",
            "review_scope",
        )
    if review.get("resulting_quality_stage") != CREATIVE_CANDIDATE:
        raise QualityGateError(
            "CREATIVE_REVIEW_REQUIRED",
            "creative accept must explicitly yield creative_candidate",
            "resulting_quality_stage",
        )
    hard_fails = normalized_hard_fail_categories(review.get("hard_fail_categories"))
    if hard_fails:
        raise QualityGateError(
            "CREATIVE_HARD_FAIL",
            "creative approval contains hard-fail categories: " + ", ".join(hard_fails),
            "hard_fail_categories",
        )
