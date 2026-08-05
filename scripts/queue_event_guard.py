#!/usr/bin/env python3
"""Pure bounded admission for the explicitly optional Queue entry points."""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SCALAR_LENGTH = 256
MAX_COMMENT_LENGTH = 64
MAX_ISSUE_NUMBER = 2_147_483_647
_AUTOMATION_ACTOR = "github-actions[bot]"
_FINGERPRINT = re.compile(r"^[0-9a-f]{20}$")


@dataclass(frozen=True)
class QueueEventDecision:
    route: str
    allowed: bool
    issue_number: int | None
    automated_retry: bool
    fingerprint: str
    retry_attempt: int | None
    reason: str


def _text(value: object, *, limit: int = MAX_SCALAR_LENGTH) -> str | None:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        return None
    return value.strip()


def _issue_number(value: object) -> int | None:
    raw = _text(value, limit=16)
    if raw is None or not raw.isdigit():
        return None
    number = int(raw)
    return number if 0 < number <= MAX_ISSUE_NUMBER else None


def _flag(value: object) -> bool | None:
    raw = _text(value, limit=5)
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


def deny(reason: str, *, route: str = "denied") -> QueueEventDecision:
    return QueueEventDecision(route, False, None, False, "", None, reason)


def resolve_queue_event(
    *,
    event_name: object,
    actor: object,
    owner: object,
    ref_name: object,
    default_branch: object,
    run_attempt: object,
    dispatch_issue: object = "",
    dispatch_fingerprint: object = "",
    dispatch_attempt: object = "",
    comment_issue: object = "",
    comment_body: object = "",
    comment_is_pr: object = "false",
) -> QueueEventDecision:
    """Resolve one trusted route without reading a GitHub event payload file."""
    event = _text(event_name, limit=32)
    actor_value = _text(actor, limit=100)
    owner_value = _text(owner, limit=100)
    ref = _text(ref_name, limit=255)
    default = _text(default_branch, limit=255)
    attempt_raw = _text(run_attempt, limit=4)
    if None in (event, actor_value, owner_value, ref, default, attempt_raw):
        return deny("invalid-scalar")
    actor_folded = actor_value.casefold()
    owner_folded = owner_value.casefold()
    if not owner_folded or not default or not attempt_raw.isdigit():
        return deny("invalid-identity")

    dispatch_issue_raw = _text(dispatch_issue, limit=16)
    fingerprint = _text(dispatch_fingerprint, limit=20)
    retry_raw = _text(dispatch_attempt, limit=2)
    if None in (dispatch_issue_raw, fingerprint, retry_raw):
        return deny("invalid-dispatch-scalar")

    if event == "issue_comment":
        body = _text(comment_body, limit=MAX_COMMENT_LENGTH)
        is_pr = _flag(comment_is_pr)
        number = _issue_number(comment_issue)
        if body is None or is_pr is None:
            return deny("invalid-comment-scalar")
        if dispatch_issue_raw or fingerprint or retry_raw:
            return deny("mixed-comment-dispatch-input")
        if (
            actor_folded == owner_folded
            and number is not None
            and is_pr is False
            and body == "/claude-run"
        ):
            return QueueEventDecision(
                "owner-trigger", True, number, False, "", None, "trusted-owner-comment"
            )
        return deny("comment-not-admitted", route="ignored")

    if event != "workflow_dispatch":
        return deny("unsupported-event", route="ignored")

    if _text(comment_issue, limit=16) not in {"", None}:
        return deny("mixed-dispatch-comment-input")
    if _text(comment_body, limit=MAX_COMMENT_LENGTH) not in {"", None}:
        return deny("mixed-dispatch-comment-input")
    comment_pr = _flag(comment_is_pr)
    if comment_pr not in {False, None}:
        return deny("mixed-dispatch-comment-input")

    number = _issue_number(dispatch_issue_raw)
    if number is None or ref != default or attempt_raw != "1":
        return deny("dispatch-base-identity-failed")

    if actor_folded == owner_folded and not fingerprint and not retry_raw:
        return QueueEventDecision(
            "owner-manual", True, number, False, "", None, "trusted-owner-dispatch"
        )

    if actor_folded == _AUTOMATION_ACTOR:
        if not fingerprint or not retry_raw:
            return deny("partial-automated-retry-input")
        if _FINGERPRINT.fullmatch(fingerprint) is None or retry_raw not in {"1", "2", "3"}:
            return deny("invalid-automated-retry-input")
        return QueueEventDecision(
            "automated-retry",
            True,
            number,
            True,
            fingerprint,
            int(retry_raw),
            "trusted-automation-dispatch",
        )

    return deny("dispatch-actor-not-admitted")
