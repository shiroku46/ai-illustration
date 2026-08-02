#!/usr/bin/env python3
"""Bounded non-notifying recovery for Queue failures before PR creation."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from scripts import supervisor_runtime as runtime

QUEUE_WORKFLOW_FILE = "claude-queue.yml"
QUEUE_WORKFLOW_NAME = "Claude Issue Queue"
QUEUE_WORKFLOW_PATH = ".github/workflows/claude-queue.yml"
QUEUE_TRIGGER = "/claude-run"
MAX_QUEUE_RECOVERY_ATTEMPTS = 3
MAX_ISSUES = 100
RETRY_REASON = "QUEUE_PIPELINE_RETRY_EXHAUSTED"
RETRY_ROOT = f"{runtime.INTERNAL_STOP_ROOT}/queue"


def _event_payload() -> dict[str, Any]:
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path:
        return {}
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _event_allows_recovery() -> bool:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name in {"schedule", "workflow_dispatch"}:
        return True
    if event_name != "workflow_run":
        return False
    run = _event_payload().get("workflow_run") or {}
    repository = run.get("repository") or {}
    actor = (run.get("actor") or {}).get("login") or ""
    path = str(run.get("path") or "").split("@", 1)[0]
    return bool(
        repository.get("full_name") == runtime.REPO
        and run.get("name") == QUEUE_WORKFLOW_NAME
        and path == QUEUE_WORKFLOW_PATH
        and run.get("head_branch") == runtime.DEFAULT_BRANCH
        and run.get("status") == "completed"
        and run.get("conclusion") not in {"success", "skipped", "neutral"}
        and run.get("event") in {"issues", "issue_comment", "workflow_dispatch"}
        and actor in runtime.ALLOWED_AUTHORS
    )


def _first_effective_line(body: str) -> str:
    stripped = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    return next((line.strip() for line in stripped.splitlines() if line.strip()), "")


def _trusted_issue(issue: dict[str, Any]) -> bool:
    return bool(
        issue.get("state") == "open"
        and not issue.get("pull_request")
        and runtime.trusted_source_issue(issue)
    )


def _request_timestamp(issue: dict[str, Any]) -> str | None:
    if _first_effective_line(str(issue.get("body") or "")) == QUEUE_TRIGGER:
        return str(issue.get("created_at") or "") or None
    issue_number = int(issue.get("number") or 0)
    if issue_number <= 0:
        return None
    comments = runtime.api_list(
        f"repos/{runtime.REPO}/issues/{issue_number}/comments?per_page=100"
    )
    trusted = [
        comment
        for comment in comments
        if (comment.get("user") or {}).get("login") in runtime.TRUSTED_ISSUE_AUTHORS
        and str(comment.get("body") or "").strip() == QUEUE_TRIGGER
        and comment.get("created_at")
    ]
    if not trusted:
        return None
    return str(max(trusted, key=lambda item: str(item["created_at"]))["created_at"])


def _request_fingerprint(issue_number: int, timestamp: str) -> str:
    raw = f"{issue_number}:{timestamp}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def _open_queue_pr_exists(issue_number: int, pulls: list[dict[str, Any]]) -> bool:
    prefix = f"claude-issue-{issue_number}-"
    return any(
        ((pull.get("head") or {}).get("ref") or "").startswith(prefix)
        and (((pull.get("head") or {}).get("repo") or {}).get("full_name") == runtime.REPO)
        for pull in pulls
    )


def _queue_runs() -> list[dict[str, Any]]:
    return runtime.api_key_pages(
        f"repos/{runtime.REPO}/actions/workflows/{QUEUE_WORKFLOW_FILE}/runs?per_page=100",
        "workflow_runs",
    )


def _active_queue_run_exists() -> bool:
    for run in _queue_runs():
        repository = run.get("repository") or {}
        path = str(run.get("path") or "").split("@", 1)[0]
        if (
            repository.get("full_name") == runtime.REPO
            and path == QUEUE_WORKFLOW_PATH
            and run.get("head_branch") == runtime.DEFAULT_BRANCH
            and str(run.get("status") or "") in runtime.ACTIVE_RUN_STATES
        ):
            return True
    return False


def _not_found(result: Any) -> bool:
    text = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}".lower()
    return getattr(result, "returncode", 1) != 0 and (
        "404" in text or "not found" in text
    )


def _read_record(path: str) -> str | None:
    result = runtime.gh_result(
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{runtime.REPO}/contents/{path}?ref={runtime.INTERNAL_STOP_BRANCH}",
    )
    if result.returncode == 0:
        payload = json.loads(result.stdout)
        encoded = str(payload.get("content") or "").replace("\n", "")
        return base64.b64decode(encoded).decode("utf-8")
    if _not_found(result):
        return None
    raise RuntimeError(f"Could not inspect Queue recovery record: {result.stderr.strip()}")


def _list_records(root: str) -> list[str]:
    runtime.ensure_internal_stop_branch()
    result = runtime.gh_result(
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{runtime.REPO}/contents/{root}?ref={runtime.INTERNAL_STOP_BRANCH}",
    )
    if result.returncode == 0:
        payload = json.loads(result.stdout)
        if not isinstance(payload, list):
            raise RuntimeError("Queue recovery record directory is not a list")
        return sorted(str(item.get("name") or "") for item in payload)
    if _not_found(result):
        return []
    raise RuntimeError(f"Could not list Queue recovery records: {result.stderr.strip()}")


def _put_exact_record(path: str, content: str, message: str) -> bool:
    runtime.ensure_internal_stop_branch()
    existing = _read_record(path)
    if existing is not None:
        if existing != content:
            raise RuntimeError("Existing Queue recovery record content does not match")
        return False
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    result = runtime.gh_result(
        "api",
        "--method",
        "PUT",
        f"repos/{runtime.REPO}/contents/{path}",
        "-f",
        f"message={message}",
        "-f",
        f"content={encoded}",
        "-f",
        f"branch={runtime.INTERNAL_STOP_BRANCH}",
    )
    if result.returncode == 0:
        return True
    if _read_record(path) == content:
        return False
    raise RuntimeError(f"Could not persist Queue recovery record: {result.stderr.strip()}")


def _canonical_record(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _revalidate_request(issue_number: int, expected_fingerprint: str) -> dict[str, Any]:
    issue = runtime.api(f"repos/{runtime.REPO}/issues/{issue_number}")
    if not _trusted_issue(issue):
        raise RuntimeError("Queue recovery Issue is no longer trusted and open")
    timestamp = _request_timestamp(issue)
    if not timestamp or _request_fingerprint(issue_number, timestamp) != expected_fingerprint:
        raise RuntimeError("Queue recovery request changed before dispatch")
    pulls = runtime.api_list(f"repos/{runtime.REPO}/pulls?state=open&per_page=100")
    if _open_queue_pr_exists(issue_number, pulls):
        raise RuntimeError("Queue Pull Request appeared before recovery dispatch")
    return issue


def _dispatch_retry(issue_number: int, fingerprint: str, attempt: int) -> bool:
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not run_id.isdigit():
        raise RuntimeError("Supervisor run ID is unavailable for trusted Queue dispatch")
    default_sha = runtime.current_default_sha()
    if not runtime.EXACT_SHA.fullmatch(default_sha):
        raise RuntimeError("Default branch did not resolve to one exact SHA")
    root = f"{RETRY_ROOT}/issue-{issue_number}/request-{fingerprint}"
    path = f"{root}/retry-{attempt}.json"
    content = _canonical_record(
        {
            "attempt": attempt,
            "default_sha": default_sha,
            "fixed_workflow": QUEUE_WORKFLOW_FILE,
            "issue_number": issue_number,
            "notification": False,
            "reason": "QUEUE_PIPELINE_RETRY",
            "request_fingerprint": fingerprint,
        }
    )
    created = _put_exact_record(
        path,
        content,
        f"Record Queue recovery retry {attempt} for Issue #{issue_number}",
    )
    if not created:
        return False
    _revalidate_request(issue_number, fingerprint)
    if runtime.current_default_sha() != default_sha:
        raise RuntimeError("Default branch moved before Queue recovery dispatch")
    runtime.gh(
        "workflow",
        "run",
        QUEUE_WORKFLOW_FILE,
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
    )
    return True


def _record_exhaustion(
    issue_number: int, fingerprint: str, retry_records: list[str]
) -> bool:
    root = f"{RETRY_ROOT}/issue-{issue_number}/request-{fingerprint}"
    path = f"{root}/exhausted.json"
    content = _canonical_record(
        {
            "issue_number": issue_number,
            "max_attempts": MAX_QUEUE_RECOVERY_ATTEMPTS,
            "notification": False,
            "reason": RETRY_REASON,
            "request_fingerprint": fingerprint,
            "retry_records": sorted(retry_records),
        }
    )
    return _put_exact_record(
        path,
        content,
        f"Record Queue retry exhaustion for Issue #{issue_number}",
    )


def reconcile() -> int:
    if not _event_allows_recovery() or _active_queue_run_exists():
        return 0
    issues = runtime.api_list(
        f"repos/{runtime.REPO}/issues?state=open&sort=updated&direction=desc&per_page={MAX_ISSUES}"
    )
    pulls = runtime.api_list(f"repos/{runtime.REPO}/pulls?state=open&per_page=100")
    for issue in issues:
        if not _trusted_issue(issue):
            continue
        issue_number = int(issue.get("number") or 0)
        timestamp = _request_timestamp(issue)
        if issue_number <= 0 or not timestamp or _open_queue_pr_exists(issue_number, pulls):
            continue
        fingerprint = _request_fingerprint(issue_number, timestamp)
        root = f"{RETRY_ROOT}/issue-{issue_number}/request-{fingerprint}"
        records = _list_records(root)
        retry_records = [
            name for name in records if re.fullmatch(r"retry-[1-9][0-9]*\.json", name)
        ]
        if "exhausted.json" in records:
            continue
        if len(retry_records) >= MAX_QUEUE_RECOVERY_ATTEMPTS:
            _record_exhaustion(issue_number, fingerprint, retry_records)
            continue
        _dispatch_retry(issue_number, fingerprint, len(retry_records) + 1)
        return 0
    return 0


def main() -> int:
    return reconcile()


if __name__ == "__main__":
    raise SystemExit(main())
