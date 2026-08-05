#!/usr/bin/env python3
"""Pure validation for exact Queue retry records and workflow-run identities."""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{20}$")
TITLE_RE = re.compile(
    r"^Optional Claude issue-([1-9][0-9]*) "
    r"(?:(trigger|manual)|retry-([123]) request-([0-9a-f]{20}))$"
)
IGNORED_TITLE_RE = re.compile(r"^Optional Claude ignored issue-([1-9][0-9]*)$")
RETRYABLE_FAILURES = frozenset({"max_turns", "git_transport", "platform_outage", "unknown"})
ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})


def _positive_int(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def retry_run_title(issue_number: int, attempt: int, fingerprint: str) -> str:
    if not _positive_int(issue_number):
        raise ValueError("retry Issue number is invalid")
    if isinstance(attempt, bool) or attempt not in {1, 2, 3}:
        raise ValueError("retry attempt is invalid")
    if not isinstance(fingerprint, str) or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("retry fingerprint is invalid")
    return f"Optional Claude issue-{issue_number} retry-{attempt} request-{fingerprint}"


def parse_queue_run_title(title: Any) -> dict[str, Any] | None:
    if not isinstance(title, str):
        return None
    ignored = IGNORED_TITLE_RE.fullmatch(title)
    if ignored is not None:
        return {
            "kind": "ignored",
            "issue_number": int(ignored.group(1)),
            "attempt": None,
            "fingerprint": None,
        }
    match = TITLE_RE.fullmatch(title)
    if match is None:
        return None
    issue_number = int(match.group(1))
    kind = match.group(2) or "retry"
    return {
        "kind": kind,
        "issue_number": issue_number,
        "attempt": int(match.group(3)) if match.group(3) else None,
        "fingerprint": match.group(4),
    }


def retry_run_matches(
    run: Mapping[str, Any],
    *,
    repository: str,
    default_branch: str,
    base_sha: str,
    issue_number: int,
    attempt: int,
    fingerprint: str,
) -> bool:
    if not isinstance(repository, str) or not repository or "/" not in repository:
        raise ValueError("repository identity is invalid")
    if not isinstance(default_branch, str) or not default_branch:
        raise ValueError("default branch is invalid")
    if not isinstance(base_sha, str) or SHA_RE.fullmatch(base_sha) is None:
        raise ValueError("base SHA is invalid")
    expected_title = retry_run_title(issue_number, attempt, fingerprint)
    actor = run.get("actor") or {}
    run_repository = run.get("repository") or {}
    return (
        run.get("display_title") == expected_title
        and run.get("event") == "workflow_dispatch"
        and str(run.get("path") or "").split("@", 1)[0] == ".github/workflows/claude-queue.yml"
        and run.get("head_branch") == default_branch
        and run.get("head_sha") == base_sha
        and run.get("run_attempt") == 1
        and str(actor.get("login") or "").casefold() == "github-actions[bot]"
        and run_repository.get("full_name") == repository
    )


def exact_retry_runs(
    runs: Iterable[Mapping[str, Any]],
    **identity: Any,
) -> tuple[Mapping[str, Any], ...]:
    matches = tuple(run for run in runs if retry_run_matches(run, **identity))
    if len(matches) > 1:
        raise ValueError("duplicate exact Queue retry runs")
    return matches


def validate_retry_records(
    records: Sequence[Mapping[str, Any]],
    *,
    issue_number: int,
    base_sha: str,
    fingerprint: str,
    max_retries: int = 3,
) -> tuple[dict[str, Any], ...]:
    if not _positive_int(issue_number):
        raise ValueError("retry Issue number is invalid")
    if not isinstance(base_sha, str) or SHA_RE.fullmatch(base_sha) is None:
        raise ValueError("retry base SHA is invalid")
    if not isinstance(fingerprint, str) or FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("retry fingerprint is invalid")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 1 <= max_retries <= 3:
        raise ValueError("retry budget is invalid")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)) or len(records) > max_retries:
        raise ValueError("retry record collection is invalid")

    normalized: list[dict[str, Any]] = []
    for expected_attempt, source in enumerate(records, start=1):
        if not isinstance(source, Mapping):
            raise ValueError("retry record is not an object")
        failure = source.get("failure_class")
        source_run_id = source.get("source_run_id")
        valid_source_run = source_run_id is None or _positive_int(source_run_id)
        if (
            source.get("attempt") != expected_attempt
            or source.get("base_sha") != base_sha
            or source.get("issue_number") != issue_number
            or source.get("request_fingerprint") != fingerprint
            or source.get("notification") is not False
            or source.get("human_action_required") is not False
            or source.get("next_automatic_action") != "dispatch one optional Queue retry"
            or failure not in RETRYABLE_FAILURES
            or not valid_source_run
        ):
            raise ValueError("retry record identity is invalid")
        normalized.append(dict(source))
    return tuple(normalized)


def stranded_retry_attempt(
    records: Sequence[Mapping[str, Any]],
    dispatched_attempts: Iterable[int],
) -> int | None:
    attempts = [record.get("attempt") for record in records]
    if attempts != list(range(1, len(records) + 1)):
        raise ValueError("retry attempts are not contiguous")
    dispatched_list = list(dispatched_attempts)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in dispatched_list):
        raise ValueError("dispatched attempts are invalid")
    if len(dispatched_list) != len(set(dispatched_list)):
        raise ValueError("duplicate dispatched retry identity")
    dispatched = set(dispatched_list)
    if not dispatched.issubset(set(attempts)):
        raise ValueError("dispatch evidence has no retry record")
    missing = [attempt for attempt in attempts if attempt not in dispatched]
    if not missing:
        return None
    if len(missing) != 1 or missing[0] != attempts[-1]:
        raise ValueError("stranded retry record is not uniquely latest")
    return missing[0]


def is_active_queue_run(run: Mapping[str, Any]) -> bool:
    identity = parse_queue_run_title(run.get("display_title"))
    return (
        identity is not None
        and identity["kind"] != "ignored"
        and str(run.get("status") or "") in ACTIVE_STATUSES
        and str(run.get("path") or "").split("@", 1)[0] == ".github/workflows/claude-queue.yml"
    )


def is_active_ignored_run(run: Mapping[str, Any], issue_number: int) -> bool:
    identity = parse_queue_run_title(run.get("display_title"))
    return (
        identity is not None
        and identity["kind"] == "ignored"
        and identity["issue_number"] == issue_number
        and run.get("event") == "issue_comment"
        and str(run.get("status") or "") in ACTIVE_STATUSES
        and str(run.get("path") or "").split("@", 1)[0] == ".github/workflows/claude-queue.yml"
    )
