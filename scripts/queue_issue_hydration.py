#!/usr/bin/env python3
"""Pure stable-identity checks for connected Queue source Issues."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_DEFAULT_PREDICATES = {
    "exact_number": False,
    "open_state": False,
    "issue_not_pr": False,
    "trusted_author": False,
}


def issue_trust_predicates(
    issue: Mapping[str, Any],
    number: int,
    allowed_owners: set[str],
) -> dict[str, bool]:
    user = issue.get("user")
    author = str(user.get("login") or "").strip().casefold() if isinstance(user, Mapping) else ""
    connected_number = issue.get("number")
    return {
        "exact_number": (
            not isinstance(number, bool)
            and isinstance(number, int)
            and number > 0
            and not isinstance(connected_number, bool)
            and isinstance(connected_number, int)
            and connected_number == number
        ),
        "open_state": issue.get("state") == "open",
        "issue_not_pr": issue.get("pull_request") is None,
        "trusted_author": bool(author) and author in allowed_owners,
    }


def issue_stability_signature(issue: Mapping[str, Any]) -> tuple[Any, ...] | None:
    user = issue.get("user")
    author = str(user.get("login") or "").strip().casefold() if isinstance(user, Mapping) else ""
    number = issue.get("number")
    node_id = str(issue.get("node_id") or "")
    updated_at = str(issue.get("updated_at") or "")
    state = issue.get("state")
    if (
        isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
        or not node_id
        or not updated_at
        or state not in {"open", "closed"}
        or not author
    ):
        return None
    return (
        node_id,
        number,
        state,
        issue.get("pull_request") is None,
        author,
        updated_at,
    )


def resolve_stable_issue(
    samples: Iterable[Mapping[str, Any] | Any],
    *,
    number: int,
    allowed_owners: set[str],
    required_matches: int = 2,
) -> tuple[str, dict[str, Any] | None, dict[str, bool]]:
    """Prefer a later trusted stable pair; otherwise return the latest rejection."""
    if (
        isinstance(required_matches, bool)
        or not isinstance(required_matches, int)
        or required_matches < 2
        or required_matches > 4
    ):
        raise ValueError("required_matches must be between 2 and 4")

    previous: tuple[Any, ...] | None = None
    consecutive = 0
    last = dict(_DEFAULT_PREDICATES)
    rejected: dict[str, bool] | None = None
    for sample in samples:
        if not isinstance(sample, Mapping):
            continue
        signature = issue_stability_signature(sample)
        if signature is None:
            previous = None
            consecutive = 0
            continue
        last = issue_trust_predicates(sample, number, allowed_owners)
        if signature == previous:
            consecutive += 1
        else:
            previous = signature
            consecutive = 1
        if consecutive >= required_matches:
            if all(last.values()):
                return "trusted", dict(sample), last
            rejected = dict(last)
    if rejected is not None:
        return "rejected", None, rejected
    return "unstable", None, last
