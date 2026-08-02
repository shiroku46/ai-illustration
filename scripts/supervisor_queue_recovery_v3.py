#!/usr/bin/env python3
"""Bind Queue recovery to exact request/default identity and complete scope audit."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from scripts import supervisor_policy as policy
from scripts import supervisor_queue_recovery as recovery
from scripts import supervisor_queue_recovery_v2 as hardened
from scripts import supervisor_runtime as runtime

_original_connected_exhaustion_snapshot = hardened._connected_exhaustion_snapshot
_original_intent_identity = hardened._intent_identity
_original_dispatch_fixed_retry = hardened._dispatch_fixed_retry


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _exact_default_sha() -> str:
    sha = str(runtime.current_default_sha())
    if not runtime.EXACT_SHA.fullmatch(sha):
        raise RuntimeError("Queue request default branch did not resolve exactly")
    return sha


def content_bound_request_fingerprint(issue_number: int, timestamp: str) -> str:
    issue = runtime.api(f"repos/{runtime.REPO}/issues/{issue_number}")
    if not recovery._trusted_issue(issue):
        raise RuntimeError("Queue request is no longer a trusted open Issue")
    body = str(issue.get("body") or "")
    trigger: dict[str, Any]
    if recovery._first_effective_line(body) == recovery.QUEUE_TRIGGER:
        created_at = str(issue.get("created_at") or "")
        if not created_at or timestamp != created_at:
            raise RuntimeError("Queue body trigger timestamp no longer matches")
        trigger = {
            "author": (issue.get("user") or {}).get("login") or "",
            "created_at": created_at,
            "kind": "issue-body",
            "text": recovery.QUEUE_TRIGGER,
        }
    else:
        comments = runtime.api_list(
            f"repos/{runtime.REPO}/issues/{issue_number}/comments?per_page=100"
        )
        matches = [
            comment
            for comment in comments
            if (comment.get("user") or {}).get("login")
            in runtime.TRUSTED_ISSUE_AUTHORS
            and str(comment.get("body") or "").strip() == recovery.QUEUE_TRIGGER
            and str(comment.get("created_at") or "") == timestamp
        ]
        if len(matches) != 1:
            raise RuntimeError("Queue comment trigger identity is missing or ambiguous")
        comment = matches[0]
        trigger = {
            "author": (comment.get("user") or {}).get("login") or "",
            "comment_id": int(comment.get("id") or 0),
            "created_at": timestamp,
            "kind": "issue-comment",
            "text": str(comment.get("body") or "").strip(),
            "updated_at": str(comment.get("updated_at") or ""),
        }
        if trigger["comment_id"] <= 0:
            raise RuntimeError("Queue comment trigger omitted its immutable ID")
    labels = sorted(
        str(label.get("name") or "")
        for label in issue.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    )
    payload = {
        "default_sha": _exact_default_sha(),
        "issue_author": (issue.get("user") or {}).get("login") or "",
        "issue_body": body,
        "issue_number": issue_number,
        "issue_title": str(issue.get("title") or ""),
        "labels": labels,
        "request_timestamp": timestamp,
        "trigger": trigger,
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()[:20]


def _validated_issue_scope(issue_number: int) -> dict[str, Any]:
    issue = runtime.api(f"repos/{runtime.REPO}/issues/{issue_number}")
    if not recovery._trusted_issue(issue):
        raise RuntimeError("Queue source Issue is no longer trusted")
    body = str(issue.get("body") or "")
    declared = sorted(policy.declared_paths(body))
    protected = sorted(policy.protected_authorized_paths(body))
    if not declared or not policy.scope_is_authorized(declared, body):
        raise RuntimeError("Queue source Issue ordinary scope is missing or invalid")
    if any(policy.is_protected(path) for path in declared) and not policy.protected_scope_is_authorized(
        declared, body
    ):
        raise RuntimeError("Queue source Issue protected scope is missing or invalid")
    return {
        "declared_paths": declared,
        "protected_authorized_paths": protected,
    }


def _all_trusted_open_candidates() -> list[dict[str, Any]]:
    """Return complete current Pull Request records without supervision caps."""
    summaries = runtime.api_list(
        f"repos/{runtime.REPO}/pulls?state=open&per_page=100"
    )
    candidates: list[dict[str, Any]] = []
    for summary in sorted(
        summaries, key=lambda item: int(item.get("number") or 0)
    ):
        if not runtime.trusted_candidate(summary):
            continue
        number = int(summary.get("number") or 0)
        if number <= 0:
            raise RuntimeError("Trusted candidate summary omitted its Pull Request number")
        live = runtime.api(f"repos/{runtime.REPO}/pulls/{number}")
        if int(live.get("number") or 0) != number:
            raise RuntimeError("Live Pull Request record does not match its summary")
        changed_files = live.get("changed_files")
        if (
            live.get("state") != "open"
            or not isinstance(live.get("labels"), list)
            or isinstance(changed_files, bool)
            or not isinstance(changed_files, int)
            or changed_files < 0
        ):
            raise RuntimeError("Live Pull Request record is incomplete or no longer open")
        if runtime.trusted_candidate(live):
            candidates.append(live)
    return candidates


def _trusted_alternative_candidates(issue_number: int) -> list[int]:
    candidates: list[int] = []
    for pull in _all_trusted_open_candidates():
        source_issue, _, _, error = runtime.source_and_scope(pull)
        if source_issue == issue_number and error is None:
            candidates.append(int(pull.get("number") or 0))
    return sorted(number for number in candidates if number > 0)


def require_no_trusted_alternative(issue_number: int) -> None:
    alternatives = _trusted_alternative_candidates(issue_number)
    if alternatives:
        raise RuntimeError(
            "Trusted alternative candidate already exists: "
            + ",".join(str(number) for number in alternatives)
        )


def intent_identity_without_alternative(
    issue_number: int, fingerprint: str, attempt: int
):
    """Validate current scope before retry persistence and suppress alternatives."""
    _validated_issue_scope(issue_number)
    require_no_trusted_alternative(issue_number)
    return _original_intent_identity(issue_number, fingerprint, attempt)


def dispatch_without_alternative(
    issue_number: int,
    fingerprint: str,
    attempt: int,
    expected_default_sha: str,
) -> None:
    """Revalidate current scope and alternative work immediately before dispatch."""
    _validated_issue_scope(issue_number)
    require_no_trusted_alternative(issue_number)
    _original_dispatch_fixed_retry(
        issue_number, fingerprint, attempt, expected_default_sha
    )


def wait_for_admitted_implementation(
    issue_number: int,
    fingerprint: str,
    attempt: int,
    expected_default_sha: str,
) -> int:
    """Return after implementation starts, including a fast terminal failure."""
    deadline = time.monotonic() + hardened.QUEUE_START_TIMEOUT_SECONDS
    selected_run_id: int | None = None
    while time.monotonic() < deadline:
        matches = hardened._matching_dispatch_runs(
            issue_number, fingerprint, attempt, expected_default_sha
        )
        if len(matches) > 1:
            raise RuntimeError("Queue retry dispatch produced ambiguous workflow runs")
        if matches:
            selected_run_id = int(matches[0]["id"])
            jobs = runtime.api_key_pages(
                f"repos/{runtime.REPO}/actions/runs/{selected_run_id}/jobs?filter=all&per_page=100",
                "jobs",
            )
            prepare = [job for job in jobs if job.get("name") == "prepare"]
            implement = [job for job in jobs if job.get("name") == "implement"]
            if len(prepare) > 1 or len(implement) > 1:
                raise RuntimeError("Queue retry jobs are ambiguous")
            if prepare and prepare[0].get("status") == "completed":
                if prepare[0].get("conclusion") != "success":
                    raise RuntimeError("Queue retry prepare admission did not succeed")
                if implement:
                    status = str(implement[0].get("status") or "")
                    conclusion = str(implement[0].get("conclusion") or "")
                    started_at = str(implement[0].get("started_at") or "")
                    if status == "in_progress":
                        return selected_run_id
                    if status == "completed":
                        if conclusion == "success":
                            return selected_run_id
                        if conclusion == "failure" and started_at:
                            return selected_run_id
                        if conclusion == "failure":
                            raise RuntimeError(
                                "Queue retry failure omitted implementation start evidence"
                            )
                        if conclusion in {"cancelled", "skipped"}:
                            raise RuntimeError(
                                "Queue retry implementation was not admitted"
                            )
            if matches[0].get("status") == "completed":
                raise RuntimeError(
                    "Queue retry completed without an admitted implementation job"
                )
        time.sleep(hardened.QUEUE_START_POLL_SECONDS)
    raise RuntimeError(
        "Queue retry did not start while supervisor remained active: "
        f"{selected_run_id or 'unresolved'}"
    )


def complete_connected_exhaustion_snapshot(
    issue_number: int,
    fingerprint: str,
    expected_default_sha: str,
    expected_retry_records: list[str],
) -> dict[str, Any]:
    if _exact_default_sha() != expected_default_sha:
        raise RuntimeError("Default branch moved before complete Queue audit")
    scope = _validated_issue_scope(issue_number)
    alternatives = _trusted_alternative_candidates(issue_number)
    if alternatives:
        raise RuntimeError("Trusted alternative candidate path remains available")
    snapshot = _original_connected_exhaustion_snapshot(
        issue_number,
        fingerprint,
        expected_default_sha,
        expected_retry_records,
    )
    if _exact_default_sha() != expected_default_sha:
        raise RuntimeError("Default branch moved during complete Queue audit")
    final_alternatives = _trusted_alternative_candidates(issue_number)
    if final_alternatives:
        raise RuntimeError(
            "Trusted alternative candidate appeared during complete Queue audit"
        )
    snapshot.update(
        {
            "alternative_candidate_prs": final_alternatives,
            "alternative_paths_exhausted": not final_alternatives,
            "source_declared_paths": scope["declared_paths"],
            "source_protected_authorized_paths": scope[
                "protected_authorized_paths"
            ],
            "source_issue_authorization_verified": True,
        }
    )
    return snapshot


def main() -> int:
    recovery._request_fingerprint = content_bound_request_fingerprint
    hardened._intent_identity = intent_identity_without_alternative
    hardened._dispatch_fixed_retry = dispatch_without_alternative
    hardened._wait_for_queue_implementation_start = wait_for_admitted_implementation
    hardened._connected_exhaustion_snapshot = complete_connected_exhaustion_snapshot
    return hardened.main()


if __name__ == "__main__":
    raise SystemExit(main())
