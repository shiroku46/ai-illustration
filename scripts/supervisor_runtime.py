#!/usr/bin/env python3
"""Trusted default-branch supervisor and immutable evidence runtime.

Write-capable jobs execute this module only from the repository default branch.
Candidate code is executed only by read-only jobs in fixed public workflows.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Iterable

from scripts.supervisor_policy import (
    is_protected,
    parse_issue_number,
    protected_scope_is_authorized,
    scope_is_authorized,
)

REPO = os.environ["REPOSITORY"]
DEFAULT_BRANCH = os.environ["DEFAULT_BRANCH"]
AUTOMATION_OWNER = os.environ["AUTOMATION_OWNER"]
REPOSITORY_OWNER = REPO.split("/", 1)[0]
TRUSTED_ISSUE_AUTHORS = {AUTOMATION_OWNER, REPOSITORY_OWNER}
ATTESTATION_JOB_NAMES = ("CI / validate", "Unit Tests / test")
ATTESTATION_NAMES = ATTESTATION_JOB_NAMES
MAX_CANDIDATES = 10
MAX_CHANGED_FILES = 100
MAX_ATTESTATION_ATTEMPTS = 3
NO_PROGRESS_MINUTES = 60
ALLOWED_PREFIXES = ("claude-issue-", "automation/", "fix/")
ALLOWED_AUTHORS = {*TRUSTED_ISSUE_AUTHORS, "github-actions[bot]"}
TRUSTED_WORKFLOW_PATH = ".github/workflows/trusted-checks.yml"
NATIVE_WORKFLOW_SPECS = (
    ("ci.yml", "CI"),
    ("unit-tests.yml", "Unit Tests"),
)
OPTIONAL_NATIVE_WORKFLOW_SPECS = (("e2e.yml", "E2E Acceptance"),)
AUDIT_WORKFLOWS = (
    "trusted-checks.yml",
    "ci-reconcile.yml",
    "supervisor.yml",
    "claude-queue.yml",
)
INTERNAL_STOP_BRANCH = "automation-internal-stops"
INTERNAL_STOP_ROOT = "automation-stops"
CODEX_LOGIN = "chatgpt-codex-connector[bot]"
ACTIONS_LOGIN = "github-actions[bot]"
HUMAN_NOTICE_PREFIX = "<!-- foundation-human-only:"
E2E_AUTO_CLOSE_MARKER = "<!-- foundation-e2e-auto-close -->"
ACTIVE_RUN_STATES = {"queued", "in_progress", "waiting", "pending", "requested"}
EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")
SAFE_REASON = re.compile(r"^[A-Z0-9_]+$")

HUMAN_ONLY_ACTIONS = {
    "HUMAN_ONLY_ACCOUNT_LEVEL_REPOSITORY_CREATION_UI_UNAVAILABLE": (
        "Create and connect the exact missing public repositories in the GitHub account-level UI."
    ),
    "HUMAN_ONLY_CREDENTIAL_PROVIDER_UI_REQUIRED": (
        "Complete the required credential, MFA, CAPTCHA, or hardware-key step in the provider UI."
    ),
    "HUMAN_ONLY_DISCONNECTED_INTEGRATION_RECONNECTION_UI_REQUIRED": (
        "Reconnect the named integration in its provider or ChatGPT connection UI."
    ),
}
HUMAN_ONLY_REASONS = frozenset(HUMAN_ONLY_ACTIONS)
INTERNAL_STOP_REASONS_THAT_MUST_NOT_NOTIFY = frozenset(
    {
        "TRUSTED_ATTESTATION_RETRY_EXHAUSTED",
        "NO_MEANINGFUL_PROGRESS",
        "MISSING_TRUSTED_SOURCE_ISSUE",
        "UNTRUSTED_SOURCE_ISSUE",
        "INCOMPLETE_CHANGED_FILE_EVIDENCE",
        "UNAUTHORIZED_CHANGED_PATH",
        "UNAUTHORIZED_PROTECTED_PATH",
        "UNTRUSTED_EVIDENCE",
        "BLOCKING_CODEX_REVIEW",
        "MERGE_NOT_READY",
        "AMBIGUOUS_TECHNICAL_STATE",
    }
)


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def gh_result(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        text=True,
        capture_output=True,
        check=False,
    )


def api(path: str) -> Any:
    return json.loads(gh("api", "-H", "Accept: application/vnd.github+json", path))


def api_list(path: str) -> list[dict[str, Any]]:
    pages = json.loads(
        gh(
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "--paginate",
            "--slurp",
            path,
        )
    )
    items: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, list):
            raise RuntimeError(f"Expected list page from {path}")
        items.extend(page)
    return items


def api_key_pages(path: str, key: str) -> list[dict[str, Any]]:
    pages = json.loads(
        gh(
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "--paginate",
            "--slurp",
            path,
        )
    )
    items: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            raise RuntimeError(f"Expected object page from {path}")
        values = page.get(key) or []
        if not isinstance(values, list):
            raise RuntimeError(f"Expected list key {key} from {path}")
        items.extend(values)
    return items


def current_default_sha() -> str:
    return str(api(f"repos/{REPO}/commits/{DEFAULT_BRANCH}")["sha"])


@lru_cache(maxsize=1)
def trusted_workflow_id() -> int:
    return int(api(f"repos/{REPO}/actions/workflows/trusted-checks.yml")["id"])


@lru_cache(maxsize=128)
def workflow_run(run_id: int) -> dict[str, Any]:
    return api(f"repos/{REPO}/actions/runs/{run_id}")


def comment(number: int, body: str) -> None:
    gh("issue", "comment", str(number), "--repo", REPO, "--body", body)


def ensure_label(number: int, label: str, color: str, description: str) -> None:
    """Legacy compatibility helper; routine stops never invoke it."""
    gh(
        "label",
        "create",
        label,
        "--repo",
        REPO,
        "--color",
        color,
        "--description",
        description,
        "--force",
    )
    gh("issue", "edit", str(number), "--repo", REPO, "--add-label", label)


def remove_label(number: int, label: str) -> None:
    subprocess.run(
        ["gh", "issue", "edit", str(number), "--repo", REPO, "--remove-label", label],
        check=False,
        capture_output=True,
        text=True,
    )


def _require_positive_number(name: str, value: int | None) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _require_exact_sha(sha: str) -> str:
    if not EXACT_SHA.fullmatch(sha):
        raise ValueError("exact_head_sha must be a lowercase 40-character commit SHA")
    return sha


def _normalized_evidence(values: Iterable[str], name: str) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(item.strip() for item in values if item.strip()))
    if not normalized:
        raise ValueError(f"{name} must contain concrete nonempty evidence")
    return normalized


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_key(value: str | None) -> datetime:
    return _parse_timestamp(value) or datetime.min.replace(tzinfo=timezone.utc)


def minutes_since(value: str | None, now: datetime | None = None) -> int | None:
    observed = _parse_timestamp(value)
    if observed is None:
        return None
    current = now or datetime.now(timezone.utc)
    return max(0, int((current - observed).total_seconds() // 60))


def minutes_without_progress(updated_at: str, now: datetime | None = None) -> int:
    """Compatibility helper; decisions use immutable evidence timestamps."""
    value = minutes_since(updated_at, now)
    return value if value is not None else 0


def _live_pr(pr_number: int, expected_sha: str) -> dict[str, Any]:
    live = api(f"repos/{REPO}/pulls/{pr_number}")
    live_sha = _require_exact_sha(str((live.get("head") or {}).get("sha") or ""))
    if live_sha != expected_sha:
        raise RuntimeError("Pull Request head moved during exact-SHA validation")
    return live


def unresolved_review_threads(pr_number: int) -> bool:
    owner, name = REPO.split("/", 1)
    query = """
    query($owner:String!,$name:String!,$number:Int!,$cursor:String){
      repository(owner:$owner,name:$name){
        pullRequest(number:$number){
          reviewThreads(first:100,after:$cursor){
            nodes{isResolved}
            pageInfo{hasNextPage endCursor}
          }
        }
      }
    }
    """
    cursor: str | None = None
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        payload = json.loads(gh(*args))
        threads = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
        if any(not node.get("isResolved") for node in threads.get("nodes") or []):
            return True
        page_info = threads["pageInfo"]
        if not page_info.get("hasNextPage"):
            return False
        cursor = page_info.get("endCursor")
        if not cursor:
            raise RuntimeError("Review-thread pagination returned no end cursor")


def _codex_event_timestamp(item: dict[str, Any]) -> str | None:
    return str(item.get("submitted_at") or item.get("created_at") or "") or None


def _codex_items(pr_number: int) -> list[dict[str, Any]]:
    items = [
        *api_list(f"repos/{REPO}/issues/{pr_number}/comments?per_page=100"),
        *api_list(f"repos/{REPO}/pulls/{pr_number}/reviews?per_page=100"),
    ]
    return sorted(items, key=lambda item: _timestamp_key(_codex_event_timestamp(item)))


def exact_codex_evidence(pr_number: int, sha: str) -> dict[str, str | None]:
    short = sha[:10]
    marker = f"<!-- foundation-codex-request:{sha} -->"
    trusted_requests: list[dict[str, Any]] = []
    for item in reversed(_codex_items(pr_number)):
        body = item.get("body") or ""
        login = (item.get("user") or {}).get("login") or ""
        if login == CODEX_LOGIN and (sha in body or short in body):
            lower = body.lower()
            clean = "didn't find any major issues" in lower or "no major issues" in lower
            state = "clean" if clean and not unresolved_review_threads(pr_number) else "blocking"
            return {
                "state": state,
                "timestamp": _codex_event_timestamp(item),
                "request_timestamp": None,
            }
        if (
            login == ACTIONS_LOGIN
            and marker in body
            and sha in body
            and item.get("created_at") == item.get("updated_at")
        ):
            trusted_requests.append(item)
    for request in trusted_requests:
        reactions = api_list(
            f"repos/{REPO}/issues/comments/{request['id']}/reactions?per_page=100"
        )
        clean_reactions = [
            reaction
            for reaction in reactions
            if (reaction.get("user") or {}).get("login") == CODEX_LOGIN
            and reaction.get("content") == "+1"
        ]
        if clean_reactions:
            latest = max(
                (
                    reaction.get("created_at") or request.get("created_at")
                    for reaction in clean_reactions
                ),
                default=request.get("created_at"),
            )
            state = "blocking" if unresolved_review_threads(pr_number) else "clean"
            return {
                "state": state,
                "timestamp": latest,
                "request_timestamp": request.get("created_at"),
            }
    request_timestamp = max(
        (item.get("created_at") for item in trusted_requests if item.get("created_at")),
        default=None,
    )
    return {"state": "pending", "timestamp": None, "request_timestamp": request_timestamp}


def exact_codex_state(pr_number: int, sha: str) -> str:
    return str(exact_codex_evidence(pr_number, sha)["state"])


def exact_codex_clean(pr_number: int, sha: str) -> bool:
    return exact_codex_state(pr_number, sha) == "clean"


def trusted_candidate(pr: dict[str, Any]) -> bool:
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    author = (pr.get("user") or {}).get("login") or ""
    return bool(
        ((head.get("repo") or {}).get("full_name") == REPO)
        and ((base.get("repo") or {}).get("full_name") == REPO)
        and base.get("ref") == DEFAULT_BRANCH
        and author in ALLOWED_AUTHORS
        and (head.get("ref") or "").startswith(ALLOWED_PREFIXES)
        and not any(label.get("name") == "ai-no-merge" for label in pr.get("labels") or [])
    )


def trusted_source_issue(issue: dict[str, Any]) -> bool:
    login = (issue.get("user") or {}).get("login") or ""
    return not issue.get("pull_request") and login in TRUSTED_ISSUE_AUTHORS


def trusted_attestation_run(run: dict[str, Any], sha: str, *, allow_active: bool) -> bool:
    repository = run.get("repository") or {}
    actor = (run.get("actor") or {}).get("login") or ""
    path = str(run.get("path") or "")
    if repository.get("full_name") != REPO:
        return False
    if int(run.get("workflow_id") or 0) != trusted_workflow_id():
        return False
    if run.get("event") != "workflow_dispatch":
        return False
    if run.get("head_branch") != DEFAULT_BRANCH:
        return False
    if run.get("head_sha") != current_default_sha():
        return False
    if actor not in ALLOWED_AUTHORS:
        return False
    if path and TRUSTED_WORKFLOW_PATH not in path:
        return False
    if run.get("display_title") != f"Trusted checks {sha}":
        return False
    status = str(run.get("status") or "")
    if allow_active and status in ACTIVE_RUN_STATES:
        return True
    return status == "completed"


def trusted_runs_for_sha(sha: str) -> list[dict[str, Any]]:
    runs = api_key_pages(
        f"repos/{REPO}/actions/workflows/{trusted_workflow_id()}/runs"
        f"?branch={DEFAULT_BRANCH}&event=workflow_dispatch&per_page=100",
        "workflow_runs",
    )
    return [run for run in runs if trusted_attestation_run(run, sha, allow_active=True)]


def trusted_run_jobs(run_id: int) -> list[dict[str, Any]]:
    return api_key_pages(
        f"repos/{REPO}/actions/runs/{run_id}/jobs?filter=all&per_page=100",
        "jobs",
    )


def _complete_successful_job_set(
    jobs: list[dict[str, Any]], run_id: int, trusted_workflow_sha: str
) -> bool:
    by_name: dict[str, list[dict[str, Any]]] = {name: [] for name in ATTESTATION_JOB_NAMES}
    for job in jobs:
        name = str(job.get("name") or "")
        if name not in by_name:
            continue
        if int(job.get("run_id") or 0) != run_id:
            return False
        if job.get("head_sha") != trusted_workflow_sha:
            return False
        by_name[name].append(job)
    if any(len(matches) != 1 for matches in by_name.values()):
        return False
    return all(
        matches[0].get("status") == "completed"
        and matches[0].get("conclusion") == "success"
        for matches in by_name.values()
    )


def attestation_attempts(sha: str) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    for run in trusted_runs_for_sha(sha):
        run_id = int(run["id"])
        status = str(run.get("status") or "")
        active = status in ACTIVE_RUN_STATES
        complete = False
        success = False
        if not active:
            trusted_workflow_sha = str(run.get("head_sha") or "")
            jobs = trusted_run_jobs(run_id)
            complete = bool(
                trusted_workflow_sha
                and _complete_successful_job_set(jobs, run_id, trusted_workflow_sha)
            )
            success = bool(status == "completed" and run.get("conclusion") == "success" and complete)
        attempt: dict[str, Any] = {
            "run_id": run_id,
            "active": active,
            "success": success,
            "complete": complete,
        }
        if run.get("updated_at"):
            attempt["updated_at"] = run["updated_at"]
        attempts.append(attempt)
    return sorted(attempts, key=lambda item: int(item["run_id"]))


def latest_successful_attestation_timestamp(attempts: list[dict[str, Any]]) -> str | None:
    return max(
        (
            str(item["updated_at"])
            for item in attempts
            if item.get("success") and item.get("updated_at")
        ),
        default=None,
    )


def _not_found(result: subprocess.CompletedProcess[str]) -> bool:
    combined = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode != 0 and ("http 404" in combined or "not found" in combined)


def _workflow_metadata(filename: str, *, optional: bool) -> dict[str, Any] | None:
    result = gh_result(
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{REPO}/actions/workflows/{filename}",
    )
    if result.returncode != 0:
        if optional and _not_found(result):
            return None
        raise RuntimeError(f"Could not inspect fixed workflow {filename}: {result.stderr.strip()}")
    metadata = json.loads(result.stdout)
    if metadata.get("state") != "active":
        raise RuntimeError(f"Fixed workflow {filename} is not active")
    expected_path = f".github/workflows/{filename}"
    if str(metadata.get("path") or "") != expected_path:
        raise RuntimeError(f"Fixed workflow {filename} has unexpected path")
    return metadata


def _content_blob_sha(path: str, ref: str) -> str:
    payload = api(f"repos/{REPO}/contents/{path}?ref={ref}")
    if payload.get("type") != "file" or not payload.get("sha"):
        raise RuntimeError(f"Could not resolve immutable file blob for {path}@{ref}")
    return str(payload["sha"])


def _workflow_definition_matches_default(
    filename: str, candidate_sha: str, stable_default_sha: str
) -> bool:
    candidate_sha = _require_exact_sha(candidate_sha)
    stable_default_sha = _require_exact_sha(stable_default_sha)
    path = f".github/workflows/{filename}"
    try:
        default_blob = _content_blob_sha(path, stable_default_sha)
        candidate_blob = _content_blob_sha(path, candidate_sha)
    except Exception:
        return False
    return candidate_blob == default_blob


def required_native_workflows() -> list[tuple[str, str, int]]:
    required: list[tuple[str, str, int]] = []
    for filename, display_name in NATIVE_WORKFLOW_SPECS:
        metadata = _workflow_metadata(filename, optional=False)
        assert metadata is not None
        required.append((filename, display_name, int(metadata["id"])))
    for filename, display_name in OPTIONAL_NATIVE_WORKFLOW_SPECS:
        metadata = _workflow_metadata(filename, optional=True)
        if metadata is not None:
            required.append((filename, display_name, int(metadata["id"])))
    return required


def _run_belongs_to_pr(run: dict[str, Any], pr_number: int) -> bool:
    pulls = run.get("pull_requests") or []
    return any(
        int(item.get("number") or 0) == pr_number
        and str(((item.get("base") or {}).get("ref") or DEFAULT_BRANCH)) == DEFAULT_BRANCH
        for item in pulls
    )


def _native_run_matches(
    run: dict[str, Any], sha: str, filename: str, workflow_id: int, pr_number: int
) -> bool:
    repository = run.get("repository") or {}
    head_repository = run.get("head_repository") or {}
    path = str(run.get("path") or "")
    return bool(
        int(run.get("workflow_id") or 0) == workflow_id
        and run.get("event") == "pull_request"
        and run.get("head_sha") == sha
        and repository.get("full_name") == REPO
        and (not head_repository or head_repository.get("full_name") == REPO)
        and (not path or path.endswith(f"/{filename}") or path == f".github/workflows/{filename}")
        and _run_belongs_to_pr(run, pr_number)
    )


def native_workflow_evidence(
    sha: str, pr_number: int | None = None
) -> tuple[bool, list[dict[str, Any]]]:
    _require_exact_sha(sha)
    pr_number = _require_positive_number("pr_number", pr_number)
    stable_default_sha = _require_exact_sha(current_default_sha())
    required = required_native_workflows()
    evidence: list[dict[str, Any]] = []
    clean = True
    for filename, display_name, workflow_id in required:
        definition_match = _workflow_definition_matches_default(
            filename, sha, stable_default_sha
        )
        if not definition_match:
            clean = False
            evidence.append(
                {
                    "workflow": filename,
                    "display_name": display_name,
                    "workflow_id": workflow_id,
                    "run_id": None,
                    "status": "untrusted-definition",
                    "conclusion": None,
                }
            )
            continue
        runs = api_key_pages(
            f"repos/{REPO}/actions/workflows/{workflow_id}/runs"
            f"?event=pull_request&head_sha={sha}&per_page=100",
            "workflow_runs",
        )
        candidates = [
            run
            for run in runs
            if _native_run_matches(run, sha, filename, workflow_id, pr_number)
        ]
        if not candidates:
            clean = False
            evidence.append(
                {
                    "workflow": filename,
                    "display_name": display_name,
                    "workflow_id": workflow_id,
                    "run_id": None,
                    "status": "missing",
                    "conclusion": None,
                }
            )
            continue
        latest = max(
            candidates,
            key=lambda run: (
                int(run.get("run_number") or 0),
                int(run.get("run_attempt") or 0),
                int(run.get("id") or 0),
            ),
        )
        status = str(latest.get("status") or "")
        conclusion = latest.get("conclusion")
        success = status == "completed" and conclusion == "success"
        clean = clean and success
        evidence.append(
            {
                "workflow": filename,
                "display_name": display_name,
                "workflow_id": workflow_id,
                "run_id": int(latest.get("id") or 0),
                "status": status,
                "conclusion": conclusion,
                "updated_at": latest.get("updated_at"),
            }
        )
    final_default_sha = _require_exact_sha(current_default_sha())
    if final_default_sha != stable_default_sha:
        raise RuntimeError("Default branch moved during complete native workflow evidence validation")
    return clean, evidence


def changed_paths(pr: dict[str, Any]) -> list[str] | None:
    expected = int(pr.get("changed_files") or 0)
    if expected > MAX_CHANGED_FILES:
        return None
    files = api_list(f"repos/{REPO}/pulls/{pr['number']}/files?per_page={MAX_CHANGED_FILES}")
    if len(files) != expected:
        return None
    paths: set[str] = set()
    for item in files:
        if item.get("filename"):
            paths.add(str(item["filename"]))
        if item.get("previous_filename"):
            paths.add(str(item["previous_filename"]))
    return sorted(paths)


def source_and_scope(
    pr: dict[str, Any],
) -> tuple[int | None, dict[str, Any] | None, list[str], str | None]:
    issue_number = parse_issue_number(pr.get("body") or "")
    if not issue_number:
        return None, None, [], "MISSING_TRUSTED_SOURCE_ISSUE"
    issue = api(f"repos/{REPO}/issues/{issue_number}")
    if not trusted_source_issue(issue):
        return issue_number, issue, [], "UNTRUSTED_SOURCE_ISSUE"
    changed = changed_paths(pr)
    if changed is None:
        return issue_number, issue, [], "INCOMPLETE_CHANGED_FILE_EVIDENCE"
    issue_body = issue.get("body") or ""
    if not scope_is_authorized(changed, issue_body):
        return issue_number, issue, changed, "UNAUTHORIZED_CHANGED_PATH"
    if any(is_protected(path) for path in changed) and not protected_scope_is_authorized(
        changed, issue_body
    ):
        return issue_number, issue, changed, "UNAUTHORIZED_PROTECTED_PATH"
    return issue_number, issue, changed, None


def candidate_pulls() -> list[dict[str, Any]]:
    pulls = api(f"repos/{REPO}/pulls?state=open&per_page=50")
    return [
        pr
        for pr in sorted(pulls, key=lambda item: int(item["number"]))
        if trusted_candidate(pr)
    ][:MAX_CANDIDATES]


def _sanitized_check_evidence(sha: str) -> str:
    payload = api(f"repos/{REPO}/commits/{sha}/check-runs?per_page=100")
    checks = [
        {
            "name": str(item.get("name") or ""),
            "status": str(item.get("status") or ""),
            "conclusion": str(item.get("conclusion") or ""),
            "app": str(((item.get("app") or {}).get("slug") or "")),
        }
        for item in payload.get("check_runs") or []
    ]
    return json.dumps(
        sorted(checks, key=lambda item: (item["name"], item["app"])),
        sort_keys=True,
        separators=(",", ":"),
    )


def internal_stop_record_path(pr_number: int, sha: str, reason: str) -> str:
    pr_number = _require_positive_number("pr_number", pr_number)
    sha = _require_exact_sha(sha)
    if not SAFE_REASON.fullmatch(reason):
        raise ValueError("internal stop reason is not path-safe")
    return f"{INTERNAL_STOP_ROOT}/pr-{pr_number}/{sha}/{reason}.json"


def human_notice_record_path(pr_number: int, sha: str, reason: str) -> str:
    pr_number = _require_positive_number("pr_number", pr_number)
    sha = _require_exact_sha(sha)
    if reason not in HUMAN_ONLY_REASONS:
        raise ValueError("reason is not an allowed human-only notice family")
    return f"{INTERNAL_STOP_ROOT}/pr-{pr_number}/{sha}/{reason}.notice.json"


def _audit_record_path(pr_number: int, sha: str, reason: str) -> str:
    if reason in HUMAN_ONLY_REASONS:
        return human_notice_record_path(pr_number, sha, reason)
    return internal_stop_record_path(pr_number, sha, reason)


def self_resolution_audit(
    pr: dict[str, Any],
    issue_number: int | None,
    reason: str,
    *,
    human_notice_context: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, str]:
    pr_number = _require_positive_number("pr_number", int(pr["number"]))
    sha = _require_exact_sha(str((pr.get("head") or {}).get("sha") or ""))
    if not SAFE_REASON.fullmatch(reason):
        raise ValueError("audit reason is not path-safe")

    repository = api(f"repos/{REPO}")
    current_pr = _live_pr(pr_number, sha)
    changed = changed_paths(current_pr)
    attempts = attestation_attempts(sha)
    native_clean, native_evidence = native_workflow_evidence(sha, pr_number)
    checks = _sanitized_check_evidence(sha)
    codex = exact_codex_evidence(pr_number, sha)
    unresolved = unresolved_review_threads(pr_number)
    permission = api(f"repos/{REPO}/collaborators/{AUTOMATION_OWNER}/permission").get("permission")
    workflow_states: list[str] = []
    for workflow_name in AUDIT_WORKFLOWS:
        metadata = api(f"repos/{REPO}/actions/workflows/{workflow_name}")
        workflow_states.append(
            f"{workflow_name}:{metadata.get('state', 'unknown')}:{metadata.get('id', 'unknown')}"
        )

    issue_state = "not-applicable"
    authorization_state = "not-applicable"
    issue_trusted: bool | None = None
    all_paths_authorized: bool | None = None
    protected_paths_authorized: bool | None = None
    if issue_number:
        issue = api(f"repos/{REPO}/issues/{issue_number}")
        issue_body = issue.get("body") or ""
        issue_trusted = trusted_source_issue(issue)
        issue_state = f"state={issue.get('state', 'unknown')},trusted_author={issue_trusted}"
        if changed is None:
            authorization_state = "incomplete-path-evidence"
        else:
            all_paths_authorized = scope_is_authorized(changed, issue_body)
            protected_paths_authorized = protected_scope_is_authorized(
                changed, issue_body
            )
            authorization_state = (
                f"all_paths={all_paths_authorized},"
                f"protected_paths={protected_paths_authorized}"
            )

    connected_notice_evidence = "not-applicable"
    if reason in HUMAN_ONLY_REASONS:
        if human_notice_context is None:
            raise RuntimeError("human-only audit requires a connected notice context")
        targets = tuple(human_notice_context.get("targets") or ())
        expected_attempted = tuple(human_notice_context.get("attempted") or ())
        expected_impossible = tuple(human_notice_context.get("impossible") or ())
        connected_attempted, connected_impossible = _connected_human_notice_evidence(
            reason, targets
        )
        if connected_attempted != expected_attempted:
            raise RuntimeError("Final audit attempted-path evidence changed")
        if connected_impossible != expected_impossible:
            raise RuntimeError("Final audit impossibility evidence changed")
        connected_notice_evidence = json.dumps(
            {
                "targets": targets,
                "attempted_connected_paths": connected_attempted,
                "impossibility_evidence": connected_impossible,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    elif human_notice_context is not None:
        raise RuntimeError("internal-stop audit received an unexpected human-only context")

    final_pr = _live_pr(pr_number, sha)
    mergeable = final_pr.get("mergeable")
    mergeable_state = str(final_pr.get("mergeable_state") or "unknown")
    if reason == "MERGE_NOT_READY" and mergeable is not False:
        raise RuntimeError("MERGE_NOT_READY is no longer supported by live mergeability")
    if reason == "UNAUTHORIZED_CHANGED_PATH" and not (
        issue_trusted is True
        and changed is not None
        and all_paths_authorized is False
    ):
        raise RuntimeError(
            "UNAUTHORIZED_CHANGED_PATH is no longer supported by fresh Issue authorization"
        )
    if reason == "UNAUTHORIZED_PROTECTED_PATH" and not (
        issue_trusted is True
        and changed is not None
        and any(is_protected(path) for path in changed)
        and all_paths_authorized is True
        and protected_paths_authorized is False
    ):
        raise RuntimeError(
            "UNAUTHORIZED_PROTECTED_PATH is no longer supported by fresh Issue authorization"
        )

    return {
        "issue": f"#{issue_number}" if issue_number else "unknown",
        "pull_request": f"#{pr_number}",
        "exact_head_sha": sha,
        "reason_code": reason,
        "repository_metadata": (
            f"visibility={repository.get('visibility', 'unknown')},"
            f"default_branch={repository.get('default_branch', 'unknown')},"
            "initial_and_final_head_confirmed=true"
        ),
        "workflow_run_and_job_evidence": json.dumps(attempts, sort_keys=True, separators=(",", ":")),
        "native_pull_request_workflow_evidence": json.dumps(
            {"clean": native_clean, "runs": native_evidence},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "check_evidence": checks,
        "changed_and_renamed_paths": (
            "incomplete" if changed is None else json.dumps(changed, separators=(",", ":"))
        ),
        "scope_and_authorization": f"issue={issue_state},authorization={authorization_state}",
        "review_and_provenance": (
            f"codex={codex['state']},codex_timestamp={codex['timestamp']},"
            f"trusted_request_timestamp={codex['request_timestamp']},"
            f"unresolved_threads={unresolved}"
        ),
        "mergeability": f"mergeable={mergeable},mergeable_state={mergeable_state}",
        "permissions_and_credentials": (
            f"automation_owner_permission={permission or 'unknown'},secret_values_not_requested=true"
        ),
        "alternative_connected_paths": ";".join(workflow_states),
        "human_only_connected_evidence": connected_notice_evidence,
        "idempotency": _audit_record_path(pr_number, sha, reason),
    }


def canonical_internal_stop_record(
    *,
    pr_number: int,
    issue_number: int | None,
    sha: str,
    reason: str,
    detail: str,
    audit: dict[str, str],
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "notification": False,
            "required_human_action": None,
            "reason_code": reason,
            "issue_number": issue_number,
            "pull_request_number": pr_number,
            "exact_head_sha": sha,
            "detail": detail,
            "audit": audit,
        },
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
    ) + "\n"


def canonical_human_notice_record(
    *,
    reason: str,
    issue_number: int,
    pr_number: int,
    exact_head_sha: str,
    attempted_connected_paths: tuple[str, ...],
    impossibility_evidence: tuple[str, ...],
    provider_ui_action: str,
    automatic_resume_condition: str,
    targets: tuple[str, ...],
    audit: dict[str, str],
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "notification": True,
            "reason_code": reason,
            "issue_number": issue_number,
            "pull_request_number": pr_number,
            "exact_head_sha": exact_head_sha,
            "attempted_connected_paths": list(attempted_connected_paths),
            "impossibility_evidence": list(impossibility_evidence),
            "provider_ui_action": provider_ui_action,
            "automatic_resume_condition": automatic_resume_condition,
            "targets": list(targets),
            "audit": audit,
        },
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
    ) + "\n"


def ensure_internal_stop_branch() -> None:
    ref_path = f"repos/{REPO}/git/ref/heads/{INTERNAL_STOP_BRANCH}"
    current = gh_result("api", "-H", "Accept: application/vnd.github+json", ref_path)
    if current.returncode == 0:
        return
    if not _not_found(current):
        raise RuntimeError(f"Could not inspect internal-stop branch: {current.stderr.strip()}")
    default_sha = current_default_sha()
    created = gh_result(
        "api",
        "--method",
        "POST",
        f"repos/{REPO}/git/refs",
        "-f",
        f"ref=refs/heads/{INTERNAL_STOP_BRANCH}",
        "-f",
        f"sha={default_sha}",
    )
    if created.returncode == 0:
        return
    raced = gh_result("api", "-H", "Accept: application/vnd.github+json", ref_path)
    if raced.returncode != 0:
        raise RuntimeError(f"Could not create internal-stop branch: {created.stderr.strip()}")


def _existing_internal_record(path: str) -> str | None:
    result = gh_result(
        "api",
        "-H",
        "Accept: application/vnd.github+json",
        f"repos/{REPO}/contents/{path}?ref={INTERNAL_STOP_BRANCH}",
    )
    if result.returncode == 0:
        payload = json.loads(result.stdout)
        encoded = str(payload.get("content") or "").replace("\n", "")
        return base64.b64decode(encoded).decode("utf-8")
    if _not_found(result):
        return None
    raise RuntimeError(f"Could not inspect audit record: {result.stderr.strip()}")


def _persist_exact_record(path: str, content: str, reason: str, pr_number: int) -> bool:
    ensure_internal_stop_branch()
    existing = _existing_internal_record(path)
    if existing is not None:
        if existing != content:
            raise RuntimeError("Existing deterministic audit record content does not match")
        return False
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    created = gh_result(
        "api",
        "--method",
        "PUT",
        f"repos/{REPO}/contents/{path}",
        "-f",
        f"message=Record {reason} for PR #{pr_number}",
        "-f",
        f"content={encoded}",
        "-f",
        f"branch={INTERNAL_STOP_BRANCH}",
    )
    if created.returncode == 0:
        return True
    raced = _existing_internal_record(path)
    if raced == content:
        return False
    raise RuntimeError(f"Could not persist deterministic audit record: {created.stderr.strip()}")


def persist_internal_stop_record(path: str, content: str, reason: str, pr_number: int) -> bool:
    return _persist_exact_record(path, content, reason, pr_number)


def persist_human_notice_record(path: str, content: str, reason: str, pr_number: int) -> bool:
    if reason not in HUMAN_ONLY_REASONS:
        raise ValueError("reason is not an allowed human-only notice family")
    return _persist_exact_record(path, content, reason, pr_number)


def _revalidate_stop_reason(
    pr_number: int,
    sha: str,
    issue_number: int | None,
    reason: str,
) -> dict[str, Any]:
    live = _live_pr(pr_number, sha)
    fresh_issue_number, _, _, fresh_scope_reason = source_and_scope(live)
    scope_reasons = {
        "MISSING_TRUSTED_SOURCE_ISSUE",
        "UNTRUSTED_SOURCE_ISSUE",
        "INCOMPLETE_CHANGED_FILE_EVIDENCE",
        "UNAUTHORIZED_CHANGED_PATH",
        "UNAUTHORIZED_PROTECTED_PATH",
    }
    if reason in scope_reasons:
        if fresh_issue_number != issue_number or fresh_scope_reason != reason:
            raise RuntimeError(
                f"{reason} is no longer supported immediately before stop mutation"
            )
        return _live_pr(pr_number, sha)
    if fresh_issue_number != issue_number or fresh_scope_reason is not None:
        raise RuntimeError(
            f"{reason} is no longer supported because source/scope evidence changed"
        )

    attempts = attestation_attempts(sha)
    successful_attestation = any(item["success"] for item in attempts)
    active_attestation = any(item["active"] for item in attempts)
    attempt_count = len({item["run_id"] for item in attempts})

    if reason == "TRUSTED_ATTESTATION_RETRY_EXHAUSTED":
        if successful_attestation or active_attestation or attempt_count < MAX_ATTESTATION_ATTEMPTS:
            raise RuntimeError(
                "TRUSTED_ATTESTATION_RETRY_EXHAUSTED is no longer supported by fresh attestation evidence"
            )
        return _live_pr(pr_number, sha)

    if not successful_attestation:
        raise RuntimeError(
            f"{reason} is no longer supported without a fresh successful trusted attestation"
        )

    native_clean, native_evidence = native_workflow_evidence(sha, pr_number)
    if reason == "NO_MEANINGFUL_PROGRESS":
        supported = False
        if not native_clean:
            anchor = max(
                (
                    str(item.get("updated_at") or "")
                    for item in native_evidence
                    if item.get("updated_at")
                ),
                default=None,
            )
            elapsed = minutes_since(anchor)
            supported = elapsed is not None and elapsed >= NO_PROGRESS_MINUTES
        else:
            codex = exact_codex_evidence(pr_number, sha)
            if codex["state"] == "pending":
                elapsed = minutes_since(codex.get("request_timestamp"))
                supported = elapsed is not None and elapsed >= NO_PROGRESS_MINUTES
            elif codex["state"] == "clean":
                final_live = _live_pr(pr_number, sha)
                if final_live.get("mergeable") not in {True, False}:
                    anchor = _evidence_anchor(
                        latest_successful_attestation_timestamp(attempts),
                        str(codex.get("timestamp") or "") or None,
                        *(
                            str(item.get("updated_at") or "") or None
                            for item in native_evidence
                        ),
                    )
                    elapsed = minutes_since(anchor)
                    supported = elapsed is not None and elapsed >= NO_PROGRESS_MINUTES
        if not supported:
            raise RuntimeError(
                "NO_MEANINGFUL_PROGRESS is no longer supported by fresh exact-head evidence"
            )
        return _live_pr(pr_number, sha)

    if reason == "BLOCKING_CODEX_REVIEW":
        codex = exact_codex_evidence(pr_number, sha)
        if not native_clean or codex["state"] != "blocking":
            raise RuntimeError(
                "BLOCKING_CODEX_REVIEW is no longer supported by fresh exact-head evidence"
            )
        return _live_pr(pr_number, sha)

    if reason == "MERGE_NOT_READY":
        codex = exact_codex_evidence(pr_number, sha)
        final_live = _live_pr(pr_number, sha)
        if not native_clean or codex["state"] != "clean" or final_live.get("mergeable") is not False:
            raise RuntimeError(
                "MERGE_NOT_READY is no longer supported by fresh exact-head evidence"
            )
        return final_live

    if reason in {"UNTRUSTED_EVIDENCE", "AMBIGUOUS_TECHNICAL_STATE"}:
        raise RuntimeError(
            f"{reason} has no deterministic current derivation and fails closed"
        )
    raise RuntimeError(f"Unsupported internal stop reason: {reason}")


def stop_report(
    pr: dict[str, Any],
    issue_number: int | None,
    reason: str,
    detail: str,
    close: bool = False,
) -> None:
    if reason in HUMAN_ONLY_REASONS:
        raise ValueError("human-only reasons must use the audited human-only formatter")
    sha = _require_exact_sha(str(pr["head"]["sha"]))
    pr_number = int(pr["number"])
    audit = self_resolution_audit(pr, issue_number, reason)
    _revalidate_stop_reason(pr_number, sha, issue_number, reason)
    path = internal_stop_record_path(pr_number, sha, reason)
    content = canonical_internal_stop_record(
        pr_number=pr_number,
        issue_number=issue_number,
        sha=sha,
        reason=reason,
        detail=detail,
        audit=audit,
    )
    _revalidate_stop_reason(pr_number, sha, issue_number, reason)
    persist_internal_stop_record(path, content, reason, pr_number)
    if close:
        _revalidate_stop_reason(pr_number, sha, issue_number, reason)
        gh("pr", "close", str(pr_number), "--repo", REPO)


def format_human_only_notice(
    *,
    reason: str,
    issue_number: int,
    pr_number: int,
    exact_head_sha: str,
    attempted_connected_paths: Iterable[str],
    impossibility_evidence: Iterable[str],
    provider_ui_action: str,
    automatic_resume_condition: str,
    targets: Iterable[str],
) -> str:
    if reason not in HUMAN_ONLY_REASONS:
        raise ValueError("reason is not an allowed human-only notice family")
    issue_number = _require_positive_number("issue_number", issue_number)
    pr_number = _require_positive_number("pr_number", pr_number)
    exact_head_sha = _require_exact_sha(exact_head_sha)
    attempted = _normalized_evidence(attempted_connected_paths, "attempted_connected_paths")
    evidence = _normalized_evidence(impossibility_evidence, "impossibility_evidence")
    target_list = _normalized_evidence(targets, "targets")
    expected_action = HUMAN_ONLY_ACTIONS[reason]
    if provider_ui_action.strip() != expected_action:
        raise ValueError("provider_ui_action is not the canonical reason-compatible action")
    if not automatic_resume_condition.strip():
        raise ValueError("automatic_resume_condition is required")
    if reason == "HUMAN_ONLY_ACCOUNT_LEVEL_REPOSITORY_CREATION_UI_UNAVAILABLE":
        canonical_targets = _canonical_repository_targets(target_list)
        if canonical_targets != target_list:
            raise ValueError("repository-creation notice targets must be canonical owner/name pairs")
    marker = f"{HUMAN_NOTICE_PREFIX}{reason}:{exact_head_sha}:{issue_number}:{pr_number} -->"
    attempted_text = "\n".join(f"  - `{item}`" for item in attempted)
    evidence_text = "\n".join(f"  - {item}" for item in evidence)
    targets_text = "\n".join(f"  - `{item}`" for item in target_list)
    return (
        f"{marker}\n## Audited human-only action required\n\n"
        "- notification: `true`\n"
        f"- reason_code: `{reason}`\n"
        f"- issue: `#{issue_number}`\n"
        f"- pull_request: `#{pr_number}`\n"
        f"- exact_head_sha: `{exact_head_sha}`\n"
        "- targets:\n"
        f"{targets_text}\n"
        "- attempted_connected_paths:\n"
        f"{attempted_text}\n"
        "- impossibility_evidence:\n"
        f"{evidence_text}\n"
        f"- required_provider_ui_action: {expected_action}\n"
        f"- automatic_resume_condition: {automatic_resume_condition.strip()}\n"
    )


def _validated_notice_destination(
    pr_number: int, issue_number: int, exact_head_sha: str
) -> dict[str, Any]:
    live = _live_pr(pr_number, exact_head_sha)
    if live.get("state") != "open":
        raise RuntimeError("human-only notice destination is not an open Pull Request")
    if parse_issue_number(live.get("body") or "") != issue_number:
        raise RuntimeError("human-only notice Issue linkage does not match the live Pull Request")
    issue = api(f"repos/{REPO}/issues/{issue_number}")
    if not trusted_source_issue(issue):
        raise RuntimeError("human-only notice source Issue is not trusted")
    if not trusted_candidate(live):
        raise RuntimeError("human-only notice destination is not a trusted same-repository candidate")
    return live


def _canonical_repository_target(target: str) -> str:
    if target != target.strip():
        raise ValueError("repository target must not contain surrounding whitespace")
    parts = target.split("/")
    if len(parts) != 2 or any(not part for part in parts):
        raise ValueError("repository target must be exactly one owner/name pair")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("repository target contains an unsafe path component")
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", part) for part in parts):
        raise ValueError("repository target contains unsupported characters")
    return f"{parts[0]}/{parts[1]}"


def _canonical_repository_targets(targets: tuple[str, ...]) -> tuple[str, str]:
    if len(targets) != 2:
        raise ValueError("repository-creation audit requires exactly two owner/name targets")
    canonical = tuple(_canonical_repository_target(target) for target in targets)
    if len({target.casefold() for target in canonical}) != 2:
        raise ValueError("repository-creation audit requires two distinct repositories")
    return canonical


def _connected_repository_creation_evidence(
    targets: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    canonical_targets = _canonical_repository_targets(targets)
    attempted: list[str] = []
    impossible: list[str] = []
    for target in canonical_targets:
        path = f"repos/{target}"
        attempted.append(f"GitHub API GET {path}")
        result = gh_result("api", "-H", "Accept: application/vnd.github+json", path)
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            if str(payload.get("full_name") or "") != target:
                raise RuntimeError(f"GitHub returned a mismatched repository for {target}")
            continue
        if _not_found(result):
            impossible.append(
                f"GitHub API returned HTTP 404 for {target}; the exact repository is absent or unavailable to the connected token."
            )
            continue
        raise RuntimeError(f"Connected GitHub repository query failed for {target}: {result.stderr.strip()}")
    if not impossible:
        raise RuntimeError("The account-level repository creation condition is not currently true")
    return tuple(attempted), tuple(impossible)


def _connected_human_notice_evidence(
    reason: str, targets: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if reason == "HUMAN_ONLY_ACCOUNT_LEVEL_REPOSITORY_CREATION_UI_UNAVAILABLE":
        return _connected_repository_creation_evidence(targets)
    raise RuntimeError(
        "No reason-specific connected provider evidence adapter is available; human-only publication fails closed"
    )


def human_only_notice(
    *,
    reason: str,
    issue_number: int,
    pr_number: int,
    exact_head_sha: str,
    attempted_connected_paths: Iterable[str],
    impossibility_evidence: Iterable[str],
    provider_ui_action: str,
    automatic_resume_condition: str,
    targets: Iterable[str],
) -> None:
    attempted_assertion = _normalized_evidence(
        attempted_connected_paths, "attempted_connected_paths"
    )
    impossibility_assertion = _normalized_evidence(
        impossibility_evidence, "impossibility_evidence"
    )
    target_list = _normalized_evidence(targets, "targets")
    connected_attempted, connected_impossible = _connected_human_notice_evidence(
        reason, target_list
    )
    if attempted_assertion != connected_attempted:
        raise RuntimeError("Caller attempted-path assertions do not match connected audit evidence")
    if impossibility_assertion != connected_impossible:
        raise RuntimeError("Caller impossibility assertions do not match connected audit evidence")

    body = format_human_only_notice(
        reason=reason,
        issue_number=issue_number,
        pr_number=pr_number,
        exact_head_sha=exact_head_sha,
        attempted_connected_paths=connected_attempted,
        impossibility_evidence=connected_impossible,
        provider_ui_action=provider_ui_action,
        automatic_resume_condition=automatic_resume_condition,
        targets=target_list,
    )
    live = _validated_notice_destination(pr_number, issue_number, exact_head_sha)
    notice_context = {
        "targets": target_list,
        "attempted": connected_attempted,
        "impossible": connected_impossible,
    }
    audit = self_resolution_audit(
        live,
        issue_number,
        reason,
        human_notice_context=notice_context,
    )
    record_path = human_notice_record_path(pr_number, exact_head_sha, reason)
    record = canonical_human_notice_record(
        reason=reason,
        issue_number=issue_number,
        pr_number=pr_number,
        exact_head_sha=exact_head_sha,
        attempted_connected_paths=connected_attempted,
        impossibility_evidence=connected_impossible,
        provider_ui_action=provider_ui_action.strip(),
        automatic_resume_condition=automatic_resume_condition.strip(),
        targets=target_list,
        audit=audit,
    )
    _validated_notice_destination(pr_number, issue_number, exact_head_sha)
    persist_human_notice_record(record_path, record, reason, pr_number)
    body += "- self_resolution_audit:\n" + "\n".join(
        f"  - {key}: `{value}`" for key, value in audit.items()
    ) + "\n"
    marker = body.splitlines()[0]
    expected_suffix = f":{issue_number}:{pr_number} -->"
    if not marker.startswith(HUMAN_NOTICE_PREFIX) or not marker.endswith(expected_suffix):
        raise ValueError("validated notice marker is not bound to the destination")

    final_attempted, final_impossible = _connected_human_notice_evidence(
        reason, target_list
    )
    if final_attempted != connected_attempted or final_impossible != connected_impossible:
        raise RuntimeError("Connected human-only condition changed after the final audit")
    comments = api_list(f"repos/{REPO}/issues/{pr_number}/comments?per_page=100")
    trusted_duplicate = any(
        (item.get("user") or {}).get("login") == ACTIONS_LOGIN
        and item.get("created_at") == item.get("updated_at")
        and marker in (item.get("body") or "")
        for item in comments
    )
    persisted_record = _existing_internal_record(record_path)
    if trusted_duplicate:
        if persisted_record != record:
            raise RuntimeError("trusted notice comment has no matching persisted exact audit record")
        return
    final_attempted, final_impossible = _connected_human_notice_evidence(
        reason, target_list
    )
    if final_attempted != connected_attempted or final_impossible != connected_impossible:
        raise RuntimeError("Connected human-only condition changed before publication")
    if persisted_record != record:
        raise RuntimeError("human-only audit record changed before publication")
    _validated_notice_destination(pr_number, issue_number, exact_head_sha)
    comment(pr_number, body)


def discover_targets() -> list[str]:
    targets: list[str] = []
    for observed in candidate_pulls():
        pr = api(f"repos/{REPO}/pulls/{observed['number']}")
        if pr["head"]["sha"] != observed["head"]["sha"] or not trusted_candidate(pr):
            continue
        _, _, _, scope_error = source_and_scope(pr)
        if scope_error:
            continue
        attempts = attestation_attempts(str(pr["head"]["sha"]))
        if any(item["success"] or item["active"] for item in attempts):
            continue
        if len({item["run_id"] for item in attempts}) >= MAX_ATTESTATION_ATTEMPTS:
            continue
        targets.append(str(pr["head"]["sha"]))
    return targets


def request_codex(pr_number: int, sha: str) -> None:
    marker = f"<!-- foundation-codex-request:{sha} -->"
    comments = api_list(f"repos/{REPO}/issues/{pr_number}/comments?per_page=100")
    if any(
        (item.get("user") or {}).get("login") == ACTIONS_LOGIN
        and marker in (item.get("body") or "")
        and item.get("created_at") == item.get("updated_at")
        for item in comments
    ):
        return
    comment(
        pr_number,
        f"{marker}\n@codex review\n\nReview exact head `{sha}`. Report blocking findings only.",
    )


def _evidence_anchor(*values: str | None) -> str | None:
    parsed = [(value, _parse_timestamp(value)) for value in values if value]
    parsed = [(value, timestamp) for value, timestamp in parsed if timestamp is not None]
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[1])[0]


def supervise() -> None:
    for observed in candidate_pulls():
        pr = api(f"repos/{REPO}/pulls/{observed['number']}")
        sha = str(pr["head"]["sha"])
        pr_number = int(pr["number"])
        if sha != observed["head"]["sha"] or not trusted_candidate(pr):
            continue

        issue_number, issue, _, scope_error = source_and_scope(pr)
        if scope_error == "MISSING_TRUSTED_SOURCE_ISSUE":
            stop_report(pr, None, scope_error, "PR body identifies no trusted source Issue.")
            continue
        if scope_error == "UNTRUSTED_SOURCE_ISSUE":
            stop_report(pr, issue_number, scope_error, "The referenced source is not a trusted owner-authored repository Issue.")
            continue
        if scope_error == "INCOMPLETE_CHANGED_FILE_EVIDENCE":
            stop_report(
                pr,
                issue_number,
                scope_error,
                f"Changed/renamed path evidence exceeded or did not match the bounded {MAX_CHANGED_FILES}-file snapshot.",
            )
            continue
        if scope_error in {"UNAUTHORIZED_CHANGED_PATH", "UNAUTHORIZED_PROTECTED_PATH"}:
            issue_body = (issue or {}).get("body") or ""
            auto_close = bool(
                E2E_AUTO_CLOSE_MARKER in issue_body
                or any(label.get("name") == "e2e-auto-close" for label in pr.get("labels") or [])
            )
            detail = (
                "Changed or renamed paths exceed the trusted Issue allowlist."
                if scope_error == "UNAUTHORIZED_CHANGED_PATH"
                else "Protected changed or renamed paths lack exact protected authorization."
            )
            stop_report(pr, issue_number, scope_error, detail, close=auto_close)
            continue

        attempts = attestation_attempts(sha)
        if not any(item["success"] for item in attempts):
            if (
                not any(item["active"] for item in attempts)
                and len({item["run_id"] for item in attempts}) >= MAX_ATTESTATION_ATTEMPTS
            ):
                stop_report(
                    pr,
                    issue_number,
                    "TRUSTED_ATTESTATION_RETRY_EXHAUSTED",
                    "Three fixed candidate-bound workflow attempts completed without one complete successful immutable run/job evidence set.",
                )
            continue

        native_clean, native_evidence = native_workflow_evidence(sha, pr_number)
        if not native_clean:
            anchor = max(
                (str(item.get("updated_at") or "") for item in native_evidence if item.get("updated_at")),
                default=None,
            )
            elapsed = minutes_since(anchor)
            if elapsed is not None and elapsed >= NO_PROGRESS_MINUTES:
                stop_report(
                    pr,
                    issue_number,
                    "NO_MEANINGFUL_PROGRESS",
                    "Fixed native pull-request workflow evidence remained incomplete or unsuccessful for the bounded interval.",
                )
            continue

        codex = exact_codex_evidence(pr_number, sha)
        if codex["state"] == "pending":
            request_codex(pr_number, sha)
            elapsed = minutes_since(codex.get("request_timestamp"))
            if elapsed is not None and elapsed >= NO_PROGRESS_MINUTES:
                stop_report(
                    pr,
                    issue_number,
                    "NO_MEANINGFUL_PROGRESS",
                    "No exact-SHA Codex evidence changed within the bounded interval measured from the immutable trusted request comment.",
                )
            continue
        if codex["state"] == "blocking":
            stop_report(
                pr,
                issue_number,
                "BLOCKING_CODEX_REVIEW",
                "Exact-head Codex evidence contains a blocking finding or unresolved review thread.",
            )
            continue

        current = api(f"repos/{REPO}/pulls/{pr_number}")
        if current["head"]["sha"] != sha or not trusted_candidate(current):
            continue
        if current.get("draft"):
            gh("pr", "ready", str(pr_number), "--repo", REPO)
            current = api(f"repos/{REPO}/pulls/{pr_number}")
            if current["head"]["sha"] != sha or not trusted_candidate(current):
                continue
        if current.get("mergeable") is False:
            stop_report(
                current,
                issue_number,
                "MERGE_NOT_READY",
                f"GitHub reports mergeable=false with state {current.get('mergeable_state', 'unknown')}.",
            )
            continue
        if current.get("mergeable") is not True:
            anchor = _evidence_anchor(
                latest_successful_attestation_timestamp(attempts),
                str(codex.get("timestamp") or "") or None,
                *(str(item.get("updated_at") or "") or None for item in native_evidence),
            )
            elapsed = minutes_since(anchor)
            if elapsed is not None and elapsed >= NO_PROGRESS_MINUTES:
                stop_report(
                    current,
                    issue_number,
                    "NO_MEANINGFUL_PROGRESS",
                    "Mergeability remained indeterminate for the bounded interval measured from the latest immutable clean evidence.",
                )
            continue

        final = _live_pr(pr_number, sha)
        if final.get("mergeable") is not True or not trusted_candidate(final):
            continue
        final_native_clean, _ = native_workflow_evidence(sha, pr_number)
        if not final_native_clean or not exact_codex_clean(pr_number, sha):
            continue

        scope_candidate = _live_pr(pr_number, sha)
        final_issue_number, _, _, final_scope_error = source_and_scope(scope_candidate)
        if final_issue_number != issue_number or final_scope_error:
            continue
        merge_candidate = _live_pr(pr_number, sha)
        if (
            merge_candidate.get("mergeable") is not True
            or not trusted_candidate(merge_candidate)
            or parse_issue_number(merge_candidate.get("body") or "") != issue_number
        ):
            continue
        gh(
            "api",
            "--method",
            "PUT",
            f"repos/{REPO}/pulls/{pr_number}/merge",
            "-f",
            "merge_method=squash",
            "-f",
            f"sha={sha}",
        )


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        print(json.dumps(discover_targets(), separators=(",", ":")))
        return 0
    supervise()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
