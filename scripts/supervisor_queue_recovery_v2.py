#!/usr/bin/env python3
"""Harden Queue recovery dispatch admission and exhaustion auditing."""
from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any

from scripts import supervisor_queue_recovery as recovery
from scripts import supervisor_runtime as runtime

QUEUE_START_TIMEOUT_SECONDS = 720
QUEUE_START_POLL_SECONDS = 5
QUEUE_RECONCILE_GRACE_SECONDS = 30
TERMINAL_PREPARE_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "skipped", "timed_out"}
)
_original_list_records = recovery._list_records


def _retry_root(issue_number: int, fingerprint: str) -> str:
    return f"{recovery.RETRY_ROOT}/issue-{issue_number}/request-{fingerprint}"


def _attempt_run_title(issue_number: int, fingerprint: str, attempt: int) -> str:
    if issue_number <= 0 or attempt <= 0 or not re.fullmatch(r"[0-9a-f]{20}", fingerprint):
        raise ValueError("Queue attempt identity is invalid")
    return f"Claude Issue Queue issue-{issue_number} request-{fingerprint} attempt-{attempt}"


def _record_payload(path: str) -> dict[str, Any] | None:
    content = recovery._read_record(path)
    if content is None:
        return None
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise RuntimeError("Queue recovery record is not a JSON object")
    return payload


def started_attempt_records(root: str) -> list[str]:
    """Expose only admitted or terminal-before-start attempts to the counter."""
    records = _original_list_records(root)
    record_set = set(records)
    visible: list[str] = []
    for name in records:
        match = re.fullmatch(r"retry-([1-9][0-9]*)\.json", name)
        if match:
            attempt = match.group(1)
            if (
                f"retry-{attempt}-started.json" not in record_set
                and f"retry-{attempt}-terminal.json" not in record_set
            ):
                continue
        visible.append(name)
    return sorted(visible)


def _matching_dispatch_runs(
    issue_number: int,
    fingerprint: str,
    attempt: int,
    expected_default_sha: str,
) -> list[dict[str, Any]]:
    expected_title = _attempt_run_title(issue_number, fingerprint, attempt)
    matches: list[dict[str, Any]] = []
    for run in recovery._queue_runs():
        run_id = int(run.get("id") or 0)
        repository = run.get("repository") or {}
        actor = (run.get("actor") or {}).get("login") or ""
        path = str(run.get("path") or "").split("@", 1)[0]
        if (
            run_id > 0
            and repository.get("full_name") == runtime.REPO
            and path == recovery.QUEUE_WORKFLOW_PATH
            and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == runtime.DEFAULT_BRANCH
            and run.get("head_sha") == expected_default_sha
            and run.get("display_title") == expected_title
            and actor == runtime.ACTIONS_LOGIN
        ):
            matches.append(run)
    return sorted(matches, key=lambda item: int(item.get("id") or 0))


def _wait_for_existing_attempt_run(
    issue_number: int,
    fingerprint: str,
    attempt: int,
    expected_default_sha: str,
) -> int | None:
    deadline = time.monotonic() + QUEUE_RECONCILE_GRACE_SECONDS
    while time.monotonic() < deadline:
        matches = _matching_dispatch_runs(
            issue_number, fingerprint, attempt, expected_default_sha
        )
        if len(matches) > 1:
            raise RuntimeError("Queue retry identity resolved to duplicate workflow runs")
        if matches:
            return int(matches[0]["id"])
        time.sleep(QUEUE_START_POLL_SECONDS)
    return None


def _wait_for_queue_implementation_start(
    issue_number: int,
    fingerprint: str,
    attempt: int,
    expected_default_sha: str,
) -> int:
    deadline = time.monotonic() + QUEUE_START_TIMEOUT_SECONDS
    selected_run_id: int | None = None
    while time.monotonic() < deadline:
        matches = _matching_dispatch_runs(
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
                    conclusion = implement[0].get("conclusion")
                    if conclusion in {"failure", "cancelled", "skipped"}:
                        raise RuntimeError("Queue retry implementation did not start")
                    if status == "in_progress" or (
                        status == "completed" and conclusion == "success"
                    ):
                        return selected_run_id
            if matches[0].get("status") == "completed":
                raise RuntimeError("Queue retry completed without an admitted implementation job")
        time.sleep(QUEUE_START_POLL_SECONDS)
    raise RuntimeError(
        f"Queue retry did not start while supervisor remained active: {selected_run_id or 'unresolved'}"
    )


def _intent_content(
    issue_number: int, fingerprint: str, attempt: int, default_sha: str
) -> str:
    return recovery._canonical_record(
        {
            "attempt": attempt,
            "default_sha": default_sha,
            "expected_run_title": _attempt_run_title(issue_number, fingerprint, attempt),
            "fixed_workflow": recovery.QUEUE_WORKFLOW_FILE,
            "issue_number": issue_number,
            "notification": False,
            "reason": "QUEUE_PIPELINE_RETRY",
            "request_fingerprint": fingerprint,
        }
    )


def _intent_identity(
    issue_number: int, fingerprint: str, attempt: int
) -> tuple[str, str, str, bool]:
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not run_id.isdigit():
        raise RuntimeError("Supervisor run ID is unavailable for Queue recovery")
    default_sha = runtime.current_default_sha()
    if not runtime.EXACT_SHA.fullmatch(default_sha):
        raise RuntimeError("Default branch did not resolve to one exact SHA")
    root = _retry_root(issue_number, fingerprint)
    intent_path = f"{root}/retry-{attempt}.json"
    started_path = f"{root}/retry-{attempt}-started.json"
    recovery._revalidate_request(issue_number, fingerprint)
    if recovery._active_queue_run_exists():
        raise RuntimeError("Queue recovery became active before retry intent")
    if runtime.current_default_sha() != default_sha:
        raise RuntimeError("Default branch moved before Queue retry intent")
    created = recovery._put_exact_record(
        intent_path,
        _intent_content(issue_number, fingerprint, attempt, default_sha),
        f"Record Queue recovery retry {attempt} for Issue #{issue_number}",
    )
    payload = _record_payload(intent_path)
    if payload is None:
        raise RuntimeError("Queue retry record disappeared before dispatch")
    if (
        payload.get("issue_number") != issue_number
        or payload.get("attempt") != attempt
        or payload.get("request_fingerprint") != fingerprint
        or payload.get("fixed_workflow") != recovery.QUEUE_WORKFLOW_FILE
        or payload.get("expected_run_title")
        != _attempt_run_title(issue_number, fingerprint, attempt)
        or payload.get("default_sha") != default_sha
        or payload.get("notification") is not False
    ):
        raise RuntimeError("Queue retry record identity does not match the request")
    return default_sha, intent_path, started_path, created


def _dispatch_fixed_retry(
    issue_number: int, fingerprint: str, attempt: int, expected_default_sha: str
) -> None:
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    recovery._revalidate_request(issue_number, fingerprint)
    if recovery._active_queue_run_exists():
        raise RuntimeError("Queue recovery became active before retry dispatch")
    if runtime.current_default_sha() != expected_default_sha:
        raise RuntimeError("Default branch moved before Queue retry dispatch")
    runtime.gh(
        "workflow",
        "run",
        recovery.QUEUE_WORKFLOW_FILE,
        "--repo",
        runtime.REPO,
        "--ref",
        runtime.DEFAULT_BRANCH,
        "-f",
        f"issue_number={issue_number}",
        "-f",
        "trusted_supervisor=true",
        "-f",
        f"trusted_run_id={run_id}",
        "-f",
        f"request_fingerprint={fingerprint}",
        "-f",
        f"recovery_attempt={attempt}",
    )


def _record_started(
    issue_number: int,
    fingerprint: str,
    attempt: int,
    expected_default_sha: str,
    queue_run_id: int,
    started_path: str,
) -> bool:
    if queue_run_id <= 0:
        raise RuntimeError("Queue retry start omitted its workflow run ID")
    recovery._revalidate_request(issue_number, fingerprint)
    if runtime.current_default_sha() != expected_default_sha:
        raise RuntimeError("Default branch moved before Queue start persistence")
    content = recovery._canonical_record(
        {
            "attempt": attempt,
            "default_sha": expected_default_sha,
            "expected_run_title": _attempt_run_title(issue_number, fingerprint, attempt),
            "issue_number": issue_number,
            "notification": False,
            "queue_run_id": queue_run_id,
            "reason": "QUEUE_PIPELINE_RETRY_STARTED",
            "request_fingerprint": fingerprint,
            "supervisor_run_id": int(os.environ.get("GITHUB_RUN_ID", "0") or 0),
        }
    )
    return recovery._put_exact_record(
        started_path,
        content,
        f"Record admitted Queue retry start for Issue #{issue_number}",
    )


def _terminal_path(issue_number: int, fingerprint: str, attempt: int) -> str:
    return f"{_retry_root(issue_number, fingerprint)}/retry-{attempt}-terminal.json"


def _record_terminal_before_start(
    issue_number: int,
    fingerprint: str,
    attempt: int,
    expected_default_sha: str,
    queue_run_id: int,
    prepare_conclusion: str,
) -> bool:
    """Persist one exact failed attempt without claiming implementation started."""
    if queue_run_id <= 0 or prepare_conclusion not in TERMINAL_PREPARE_CONCLUSIONS:
        raise RuntimeError("Queue terminal-before-start evidence is invalid")
    recovery._revalidate_request(issue_number, fingerprint)
    if runtime.current_default_sha() != expected_default_sha:
        raise RuntimeError("Default branch moved before Queue terminal persistence")
    content = recovery._canonical_record(
        {
            "attempt": attempt,
            "default_sha": expected_default_sha,
            "expected_run_title": _attempt_run_title(issue_number, fingerprint, attempt),
            "fixed_workflow": recovery.QUEUE_WORKFLOW_FILE,
            "issue_number": issue_number,
            "notification": False,
            "prepare_conclusion": prepare_conclusion,
            "queue_run_id": queue_run_id,
            "reason": "QUEUE_PIPELINE_RETRY_TERMINAL_BEFORE_IMPLEMENTATION_START",
            "request_fingerprint": fingerprint,
            "trusted_workflow_path": recovery.QUEUE_WORKFLOW_PATH,
        }
    )
    return recovery._put_exact_record(
        _terminal_path(issue_number, fingerprint, attempt),
        content,
        f"Record terminal Queue retry {attempt} for Issue #{issue_number}",
    )


def _reconcile_attempt_run(
    issue_number: int,
    fingerprint: str,
    attempt: int,
    expected_default_sha: str,
) -> tuple[str, int, str | None]:
    """Wait for the exact run to start implementation or terminate in prepare."""
    deadline = time.monotonic() + QUEUE_START_TIMEOUT_SECONDS
    selected_run_id: int | None = None
    while time.monotonic() < deadline:
        matches = _matching_dispatch_runs(
            issue_number, fingerprint, attempt, expected_default_sha
        )
        if len(matches) > 1:
            raise RuntimeError("Queue retry dispatch produced ambiguous workflow runs")
        if matches:
            run = matches[0]
            selected_run_id = int(run["id"])
            jobs = runtime.api_key_pages(
                f"repos/{runtime.REPO}/actions/runs/{selected_run_id}/jobs?filter=all&per_page=100",
                "jobs",
            )
            prepare = [job for job in jobs if job.get("name") == "prepare"]
            implement = [job for job in jobs if job.get("name") == "implement"]
            if len(prepare) > 1 or len(implement) > 1:
                raise RuntimeError("Queue retry jobs are ambiguous")
            if prepare and prepare[0].get("status") == "completed":
                conclusion = str(prepare[0].get("conclusion") or "")
                if conclusion in TERMINAL_PREPARE_CONCLUSIONS:
                    if run.get("status") != "completed":
                        raise RuntimeError("Queue prepare was terminal before its run completed")
                    return "terminal", selected_run_id, conclusion
                if conclusion == "success" and implement:
                    status = str(implement[0].get("status") or "")
                    implement_conclusion = str(implement[0].get("conclusion") or "")
                    started_at = str(implement[0].get("started_at") or "")
                    if status == "in_progress" or (
                        status == "completed"
                        and (
                            implement_conclusion == "success"
                            or (implement_conclusion == "failure" and started_at)
                        )
                    ):
                        return "started", selected_run_id, None
            if run.get("status") == "completed":
                raise RuntimeError("Queue retry completed without recognized attempt evidence")
        time.sleep(QUEUE_START_POLL_SECONDS)
    raise RuntimeError(
        "Queue retry did not resolve while supervisor remained active: "
        f"{selected_run_id or 'unresolved'}"
    )


def guarded_dispatch_retry(issue_number: int, fingerprint: str, attempt: int) -> bool:
    default_sha, _, started_path, _ = _intent_identity(
        issue_number, fingerprint, attempt
    )
    started = _record_payload(started_path)
    terminal_path = _terminal_path(issue_number, fingerprint, attempt)
    terminal = _record_payload(terminal_path)
    if started is not None and terminal is not None:
        raise RuntimeError("Queue retry has conflicting terminal and started evidence")
    if started is not None:
        if (
            started.get("issue_number") != issue_number
            or started.get("attempt") != attempt
            or started.get("request_fingerprint") != fingerprint
            or started.get("default_sha") != default_sha
            or started.get("expected_run_title")
            != _attempt_run_title(issue_number, fingerprint, attempt)
            or int(started.get("queue_run_id") or 0) <= 0
            or started.get("notification") is not False
        ):
            raise RuntimeError("Queue retry started record identity does not match")
        return False

    if terminal is not None:
        if (
            terminal.get("issue_number") != issue_number
            or terminal.get("attempt") != attempt
            or terminal.get("request_fingerprint") != fingerprint
            or terminal.get("default_sha") != default_sha
            or terminal.get("expected_run_title")
            != _attempt_run_title(issue_number, fingerprint, attempt)
            or terminal.get("fixed_workflow") != recovery.QUEUE_WORKFLOW_FILE
            or terminal.get("trusted_workflow_path") != recovery.QUEUE_WORKFLOW_PATH
            or terminal.get("prepare_conclusion") not in TERMINAL_PREPARE_CONCLUSIONS
            or int(terminal.get("queue_run_id") or 0) <= 0
            or terminal.get("notification") is not False
        ):
            raise RuntimeError("Queue retry terminal record identity does not match")
        return False

    existing_run_id = _wait_for_existing_attempt_run(
        issue_number, fingerprint, attempt, default_sha
    )
    if existing_run_id is not None:
        outcome, queue_run_id, prepare_conclusion = _reconcile_attempt_run(
            issue_number, fingerprint, attempt, default_sha
        )
        if queue_run_id != existing_run_id:
            raise RuntimeError("Queue retry run identity changed during reconciliation")
        if outcome == "terminal":
            _record_terminal_before_start(
                issue_number, fingerprint, attempt, default_sha, queue_run_id,
                str(prepare_conclusion),
            )
        else:
            _record_started(
                issue_number, fingerprint, attempt, default_sha, queue_run_id,
                started_path,
            )
        return True

    _dispatch_fixed_retry(issue_number, fingerprint, attempt, default_sha)
    outcome, queue_run_id, prepare_conclusion = _reconcile_attempt_run(
        issue_number, fingerprint, attempt, default_sha
    )
    if outcome == "terminal":
        _record_terminal_before_start(
            issue_number, fingerprint, attempt, default_sha, queue_run_id,
            str(prepare_conclusion),
        )
    else:
        _record_started(
            issue_number, fingerprint, attempt, default_sha, queue_run_id, started_path,
        )
    return True


def _fetch_text(path: str, exact_sha: str) -> str:
    payload = runtime.api(f"repos/{runtime.REPO}/contents/{path}?ref={exact_sha}")
    if payload.get("type") != "file" or payload.get("encoding") != "base64":
        raise RuntimeError(f"Connected audit could not bind fixed file {path}")
    encoded = str(payload.get("content") or "").replace("\n", "")
    return base64.b64decode(encoded).decode("utf-8")


def _connected_exhaustion_snapshot(
    issue_number: int,
    fingerprint: str,
    expected_default_sha: str,
    expected_retry_records: list[str],
) -> dict[str, Any]:
    repository = runtime.api(f"repos/{runtime.REPO}")
    if (
        repository.get("full_name") != runtime.REPO
        or repository.get("default_branch") != runtime.DEFAULT_BRANCH
        or repository.get("archived") is True
    ):
        raise RuntimeError("Repository metadata failed Queue exhaustion audit")
    recovery._revalidate_request(issue_number, fingerprint)
    if recovery._active_queue_run_exists():
        raise RuntimeError("Queue recovery became active before exhaustion persistence")
    if runtime.current_default_sha() != expected_default_sha:
        raise RuntimeError("Default branch moved before Queue exhaustion persistence")
    queue_workflow = runtime.api(
        f"repos/{runtime.REPO}/actions/workflows/{recovery.QUEUE_WORKFLOW_FILE}"
    )
    reconcile_workflow = runtime.api(
        f"repos/{runtime.REPO}/actions/workflows/ci-reconcile.yml"
    )
    supervisor_workflow = runtime.api(
        f"repos/{runtime.REPO}/actions/workflows/supervisor.yml"
    )
    if (
        queue_workflow.get("path") != recovery.QUEUE_WORKFLOW_PATH
        or queue_workflow.get("state") != "active"
        or reconcile_workflow.get("path") != ".github/workflows/ci-reconcile.yml"
        or reconcile_workflow.get("state") != "active"
        or supervisor_workflow.get("path") != ".github/workflows/supervisor.yml"
        or supervisor_workflow.get("state") != "active"
    ):
        raise RuntimeError("Fixed workflow identity failed Queue exhaustion audit")
    queue_text = _fetch_text(recovery.QUEUE_WORKFLOW_PATH, expected_default_sha)
    reconcile_text = _fetch_text(
        ".github/workflows/ci-reconcile.yml", expected_default_sha
    )
    supervisor_text = _fetch_text(
        ".github/workflows/supervisor.yml", expected_default_sha
    )
    required_queue = (
        "workflow_dispatch:",
        "trusted_supervisor:",
        "trusted_run_id:",
        "request_fingerprint:",
        "recovery_attempt:",
        "run-name: Claude Issue Queue issue-",
        'expected_path = f".github/workflows/ci-reconcile.yml@{default_branch}"',
        "permissions:",
    )
    required_reconcile = (
        'workflows: ["CI", "Unit Tests", "Claude Issue Queue"]',
        "queue_recovery:",
        "actions: write",
        "contents: write",
        "issues: read",
        "pull-requests: read",
        "python -m scripts.supervisor_queue_recovery_v3",
    )
    required_supervisor = (
        "actions: read",
        "checks: read",
        "contents: write",
        "issues: write",
        "pull-requests: write",
        "python -m scripts.supervisor_final_guard",
    )
    if (
        not all(marker in queue_text for marker in required_queue)
        or not all(marker in reconcile_text for marker in required_reconcile)
        or not all(marker in supervisor_text for marker in required_supervisor)
        or "actions: write" in supervisor_text
        or "python -m scripts.supervisor_queue_recovery_v3" in supervisor_text
    ):
        raise RuntimeError("Fixed workflow permission or entry identity is incomplete")
    root = _retry_root(issue_number, fingerprint)
    records = _original_list_records(root)
    if "exhausted.json" in records:
        raise RuntimeError("Queue exhaustion was already persisted")
    live_retry_records = sorted(
        name
        for name in records
        if re.fullmatch(r"retry-[1-9][0-9]*\.json", name)
    )
    if (
        live_retry_records != sorted(expected_retry_records)
        or len(live_retry_records) < recovery.MAX_QUEUE_RECOVERY_ATTEMPTS
    ):
        raise RuntimeError("Queue retry evidence changed before exhaustion persistence")
    run_evidence: list[dict[str, Any]] = []
    for retry_name in live_retry_records:
        match = re.fullmatch(r"retry-([1-9][0-9]*)\.json", retry_name)
        if match is None:
            raise RuntimeError("Queue retry evidence name is invalid")
        attempt = int(match.group(1))
        started_name = f"retry-{attempt}-started.json"
        terminal_name = f"retry-{attempt}-terminal.json"
        if (started_name in records) == (terminal_name in records):
            raise RuntimeError("Queue attempt must have exactly one consumed outcome")
        outcome_name = started_name if started_name in records else terminal_name
        outcome = _record_payload(f"{root}/{outcome_name}")
        if outcome is None:
            raise RuntimeError("Queue attempt evidence disappeared during audit")
        queue_run_id = int(outcome.get("queue_run_id") or 0)
        expected_title = _attempt_run_title(issue_number, fingerprint, attempt)
        if (
            outcome.get("issue_number") != issue_number
            or outcome.get("attempt") != attempt
            or outcome.get("request_fingerprint") != fingerprint
            or outcome.get("default_sha") != expected_default_sha
            or outcome.get("expected_run_title") != expected_title
            or outcome.get("notification") is not False
            or queue_run_id <= 0
        ):
            raise RuntimeError("Queue attempt evidence identity failed audit")
        run = runtime.api(f"repos/{runtime.REPO}/actions/runs/{queue_run_id}")
        run_path = str(run.get("path") or "").split("@", 1)[0]
        if (
            (run.get("repository") or {}).get("full_name") != runtime.REPO
            or run_path != recovery.QUEUE_WORKFLOW_PATH
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != runtime.DEFAULT_BRANCH
            or run.get("head_sha") != expected_default_sha
            or run.get("display_title") != expected_title
            or run.get("status") != "completed"
        ):
            raise RuntimeError("Queue run evidence failed exact connected audit")
        jobs = runtime.api_key_pages(
            f"repos/{runtime.REPO}/actions/runs/{queue_run_id}/jobs?filter=all&per_page=100",
            "jobs",
        )
        prepare = [job for job in jobs if job.get("name") == "prepare"]
        implement = [job for job in jobs if job.get("name") == "implement"]
        if len(prepare) != 1:
            raise RuntimeError("Queue run prepare evidence is incomplete")
        if outcome_name == terminal_name:
            prepare_conclusion = str(prepare[0].get("conclusion") or "")
            if (
                outcome.get("fixed_workflow") != recovery.QUEUE_WORKFLOW_FILE
                or outcome.get("trusted_workflow_path") != recovery.QUEUE_WORKFLOW_PATH
                or outcome.get("prepare_conclusion") != prepare_conclusion
                or prepare[0].get("status") != "completed"
                or prepare_conclusion not in TERMINAL_PREPARE_CONCLUSIONS
            ):
                raise RuntimeError("Queue terminal prepare evidence failed audit")
            run_evidence.append(
                {
                    "attempt": attempt,
                    "prepare_conclusion": prepare_conclusion,
                    "queue_run_id": queue_run_id,
                    "run_title": expected_title,
                    "terminal_before_implementation_start": True,
                }
            )
            continue
        if (
            prepare[0].get("conclusion") != "success"
            or len(implement) != 1
            or implement[0].get("status") != "completed"
        ):
            raise RuntimeError("Queue run job evidence is incomplete")
        run_evidence.append(
            {
                "attempt": attempt,
                "implement_conclusion": implement[0].get("conclusion"),
                "queue_run_id": queue_run_id,
                "run_title": expected_title,
            }
        )
    return {
        "active_queue_run_absent": True,
        "alternative_paths_exhausted": True,
        "candidate_pull_request_absent": True,
        "codex_and_threads": "not-applicable-no-pull-request",
        "completed": True,
        "default_sha": expected_default_sha,
        "fixed_workflow_identity": True,
        "idempotency_records": sorted(records),
        "permission_markers_verified": True,
        "repository_metadata_verified": True,
        "request_fingerprint": fingerprint,
        "run_evidence": run_evidence,
        "source_issue": issue_number,
        "source_issue_authorization_verified": True,
    }


def guarded_record_exhaustion(
    issue_number: int, fingerprint: str, retry_records: list[str]
) -> bool:
    default_sha = runtime.current_default_sha()
    if not runtime.EXACT_SHA.fullmatch(default_sha):
        raise RuntimeError("Queue exhaustion default branch did not resolve exactly")
    expected_records = sorted(retry_records)
    first = _connected_exhaustion_snapshot(
        issue_number, fingerprint, default_sha, expected_records
    )
    second = _connected_exhaustion_snapshot(
        issue_number, fingerprint, default_sha, expected_records
    )
    if first != second:
        raise RuntimeError("Queue exhaustion audit changed between live passes")
    root = _retry_root(issue_number, fingerprint)
    path = f"{root}/exhausted.json"
    content = recovery._canonical_record(
        {
            "audit": second,
            "issue_number": issue_number,
            "max_attempts": recovery.MAX_QUEUE_RECOVERY_ATTEMPTS,
            "notification": False,
            "reason": recovery.RETRY_REASON,
            "request_fingerprint": fingerprint,
            "retry_records": expected_records,
        }
    )
    return recovery._put_exact_record(
        path,
        content,
        f"Record Queue retry exhaustion for Issue #{issue_number}",
    )


def main() -> int:
    recovery._list_records = started_attempt_records
    recovery._dispatch_retry = guarded_dispatch_retry
    recovery._record_exhaustion = guarded_record_exhaustion
    return recovery.main()


if __name__ == "__main__":
    raise SystemExit(main())
