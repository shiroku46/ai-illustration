#!/usr/bin/env python3
"""Default-branch-controlled GitHub-only Pull Request supervisor.

The supervisor never checks out or executes Pull Request code. It reads bounded
same-repository GitHub evidence and performs only: mark one exact Draft PR ready,
then after a complete fresh evaluation merge that exact expected head.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence

from scripts.foundation_product_checks import (
    CONFIG_PATH as PRODUCT_CHECKS_PATH,
    ProductCheckConfigError,
    parse_product_checks,
)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
CLOSES_RE = re.compile(r"(?i)\b(?:closes|fixes|resolves)\s+#([1-9][0-9]*)\b")
SAFE_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*[\x00-\x1f\\])[^:]+$")
PROTECTED_RE = re.compile(
    r"<!--\s*foundation-protected-authorization(?:-amendment)?\s*\n(.*?)\n\s*-->",
    re.DOTALL,
)
GENERAL_REVIEW_RE = re.compile(
    r"<!-- foundation-coordinator-review:([0-9a-f]{40}):(clean|blocked) -->"
)
PASS_REVIEW_RE = re.compile(
    r"<!-- foundation-coordinator-review:([0-9a-f]{40}):(scope-security|correctness-race):(clean|blocked) -->"
)
ALLOWED_HEADINGS = {
    "allowed paths", "allowed scope", "exact scope", "exact authorized scope",
    "exact allowed paths", "phase 1 allowed paths", "phase 2 allowed paths",
    "additional allowed paths",
}
PROTECTED_EXACT = {
    PRODUCT_CHECKS_PATH,
    "SECURITY.md", "AGENTS.md", "CLAUDE.md",
    ".github/ISSUE_TEMPLATE/ai-task.yml", ".github/pull_request_template.md",
    "docs/MINIMUM_SAFETY_PROFILE.md", "docs/OPERATING_RULES.md",
    "docs/PROJECT_STARTUP.md", "docs/PUBLIC_SECURITY_MODEL.md",
    "scripts/github_coordinator_supervisor.py", "scripts/queue_failure_classifier.py",
    "scripts/supervisor_policy.py", "scripts/supervisor_runtime.py",
    "scripts/supervisor_final_guard.py", "scripts/validate_repository.py",
}
PROTECTED_PREFIXES = (".github/workflows/", "bootstrap/", "automation/")
PROTECTED_GLOBS = ("scripts/supervisor_*.py", "scripts/github_*.py")
CHECK_WORKFLOWS = {"CI": ".github/workflows/ci.yml", "Unit Tests": ".github/workflows/unit-tests.yml"}
REQUIRED_CHECKS = ("CI", "Unit Tests")
PASSING = {"success", "neutral", "skipped"}
MAX_ITEMS = 3000
MAX_PAGES = 20


class SupervisorError(RuntimeError):
    """A fail-closed result with no mutation."""


@dataclass(frozen=True)
class ImmutableComment:
    comment_id: int
    login: str
    created_at: str
    updated_at: str
    body: str


@dataclass(frozen=True)
class TaskScope:
    risk: str
    paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    checks: tuple[str, ...]
    authorization_comments: tuple[ImmutableComment, ...]


@dataclass(frozen=True)
class ReviewEvidence:
    state: str
    unresolved_threads: int
    marker_ids: tuple[int, ...]
    thread_snapshot: tuple[tuple[str, bool], ...]


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    head_sha: str
    risk: str
    unresolved_threads: int


class Client(Protocol):
    def repository(self) -> Mapping[str, Any]: ...
    def default_branch_sha(self, branch: str) -> str: ...
    def pull(self, number: int) -> Mapping[str, Any]: ...
    def issue(self, number: int) -> Mapping[str, Any]: ...
    def issue_comments(self, number: int) -> Sequence[Mapping[str, Any]]: ...
    def pull_files(self, number: int) -> Sequence[Mapping[str, Any]]: ...
    def open_pulls(self) -> Sequence[Mapping[str, Any]]: ...
    def workflow_runs(self, head_sha: str) -> Sequence[Mapping[str, Any]]: ...
    def review_threads(self, number: int) -> Sequence[Mapping[str, Any]]: ...
    def file_content(self, path: str, ref: str) -> bytes: ...
    def file_blob(self, path: str, ref: str) -> str: ...
    def mark_ready(self, node_id: str) -> None: ...
    def merge(self, number: int, head_sha: str) -> None: ...


def _text(value: Any, where: str, limit: int = 50000) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise SupervisorError(f"{where} is invalid")
    return value


def _positive(value: Any, where: str) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SupervisorError(f"{where} is invalid")
    return value


def _sha(value: Any, where: str) -> str:
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise SupervisorError(f"{where} is not an exact SHA")
    return value


def _login(value: Any) -> str:
    if not isinstance(value, str) or not LOGIN_RE.fullmatch(value):
        raise SupervisorError("trusted login is invalid")
    return value


def _path(value: str) -> str:
    value = value.strip().strip("`")
    if not value or not SAFE_PATH_RE.fullmatch(value):
        raise SupervisorError("authorization contains an unsafe path")
    if any(ch in value for ch in "?[]") or ("*" in value and not value.endswith("/**")):
        raise SupervisorError("only exact paths and bounded /** are supported")
    return value


def path_allowed(path: str, patterns: Sequence[str]) -> bool:
    if not SAFE_PATH_RE.fullmatch(path):
        return False
    for pattern in patterns:
        if pattern.endswith("/**"):
            prefix = pattern[:-3].rstrip("/")
            if prefix and (path == prefix or path.startswith(prefix + "/")):
                return True
        elif path == pattern:
            return True
    return False


def is_protected_path(path: str) -> bool:
    return (
        path in PROTECTED_EXACT
        or path.startswith(PROTECTED_PREFIXES)
        or any(fnmatch.fnmatchcase(path, pattern) for pattern in PROTECTED_GLOBS)
    )


def _heading_paths(body: str) -> set[str]:
    paths: set[str] = set()
    active = False
    for raw in body.splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*$", raw.strip())
        if match:
            active = match.group(1).strip().casefold() in ALLOWED_HEADINGS
            continue
        if active and raw.strip().startswith("<!--"):
            active = False
        elif active and raw.strip().startswith("- "):
            paths.add(_path(raw.strip()[2:]))
    return paths


def _simple_block(raw: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    list_key: str | None = None
    for line in raw.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            if list_key is None:
                raise SupervisorError("authorization list item has no key")
            result.setdefault(list_key, []).append(line.lstrip()[2:].strip())
            continue
        if ":" not in line:
            raise SupervisorError("authorization line is invalid")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key or key in result:
            raise SupervisorError("authorization key is missing or duplicate")
        result[key] = value if value else []
        list_key = None if value else key
    return result


def _trusted_immutable_comments(
    comments: Sequence[Mapping[str, Any]], trusted_logins: Iterable[str]
) -> tuple[ImmutableComment, ...]:
    trusted = {login.casefold() for login in trusted_logins}
    result: list[ImmutableComment] = []
    seen: set[int] = set()
    for raw in comments:
        login = str(((raw.get("user") or raw.get("author") or {}).get("login")) or "")
        created = str(raw.get("created_at") or raw.get("createdAt") or "")
        updated = str(raw.get("updated_at") or raw.get("updatedAt") or "")
        comment_id = raw.get("id")
        body = str(raw.get("body") or "")
        if (
            login.casefold() not in trusted or not created or created != updated
            or isinstance(comment_id, bool) or not isinstance(comment_id, int)
            or comment_id <= 0 or comment_id in seen
            or "foundation-protected-authorization-amendment" not in body
        ):
            continue
        seen.add(comment_id)
        result.append(ImmutableComment(comment_id, login, created, updated, _text(body, "comment")))
    return tuple(sorted(result, key=lambda item: item.comment_id))


def parse_task_scope(
    issue_body: str,
    source_comments: Sequence[Mapping[str, Any]],
    trusted_logins: Iterable[str],
) -> TaskScope:
    paths = _heading_paths(issue_body)
    if not paths:
        raise SupervisorError("Issue has no trusted path scope")
    comments = _trusted_immutable_comments(source_comments, trusted_logins)
    protected: set[str] = set()
    blocks = list(PROTECTED_RE.findall(issue_body))
    for comment in comments:
        amendments = PROTECTED_RE.findall(comment.body)
        blocks.extend(amendments)
        for amendment in amendments:
            parsed = _simple_block(amendment)
            values = parsed.get("paths")
            if isinstance(values, list):
                paths.update(_path(item) for item in values)
    for raw in blocks:
        parsed = _simple_block(raw)
        required = {"category", "paths", "operation", "prohibited", "validation", "rollback"}
        if not required.issubset(parsed) or not isinstance(parsed.get("paths"), list):
            raise SupervisorError("protected authorization is incomplete")
        protected.update(_path(item) for item in parsed["paths"])
    risk = "protected" if blocks or any(is_protected_path(path.rstrip("/**")) for path in paths) else "standard"
    if risk == "protected" and not protected:
        raise SupervisorError("protected paths lack explicit authorization")
    for path in paths:
        if is_protected_path(path.rstrip("/**")) and not path_allowed(path.rstrip("/**"), tuple(protected)):
            raise SupervisorError("protected path is not independently authorized")
    return TaskScope(risk, tuple(sorted(paths)), tuple(sorted(protected)), REQUIRED_CHECKS, comments)


def source_issue_number(pr_body: str) -> int:
    values = {int(item) for item in CLOSES_RE.findall(pr_body)}
    if len(values) != 1:
        raise SupervisorError("PR must bind exactly one source Issue")
    return next(iter(values))


def changed_paths(files: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    values: set[str] = set()
    for item in files:
        current = _text(item.get("filename"), "filename", 500)
        if not SAFE_PATH_RE.fullmatch(current):
            raise SupervisorError("unsafe changed path")
        values.add(current)
        previous = item.get("previous_filename")
        if previous is not None:
            previous = _text(previous, "previous filename", 500)
            if not SAFE_PATH_RE.fullmatch(previous):
                raise SupervisorError("unsafe previous path")
            values.add(previous)
    if not values or len(values) > MAX_ITEMS:
        raise SupervisorError("changed path set is empty or excessive")
    return tuple(sorted(values))


def review_evidence(
    comments: Sequence[Mapping[str, Any]],
    threads: Sequence[Mapping[str, Any]],
    head_sha: str,
    risk: str,
    trusted_logins: Iterable[str],
) -> ReviewEvidence:
    trusted = {value.casefold() for value in trusted_logins}
    thread_state: list[tuple[str, bool]] = []
    seen_threads: set[str] = set()
    for thread in threads:
        thread_id = str(thread.get("id") or "")
        resolved = thread.get("isResolved")
        if not thread_id or thread_id in seen_threads or not isinstance(resolved, bool):
            raise SupervisorError("thread evidence is malformed")
        seen_threads.add(thread_id)
        thread_state.append((thread_id, resolved))
    unresolved = sum(not resolved for _, resolved in thread_state)
    general = {"clean": 0, "blocked": 0}
    passes = {name: {"clean": 0, "blocked": 0} for name in ("scope-security", "correctness-race")}
    marker_ids: list[int] = []
    for raw in comments:
        login = str(((raw.get("user") or raw.get("author") or {}).get("login")) or "")
        created = str(raw.get("created_at") or raw.get("createdAt") or "")
        updated = str(raw.get("updated_at") or raw.get("updatedAt") or "")
        comment_id = raw.get("id")
        body = str(raw.get("body") or "")
        if (
            login.casefold() not in trusted or not created or created != updated
            or isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0
        ):
            continue
        spans: list[tuple[int, int]] = []
        for match in GENERAL_REVIEW_RE.finditer(body):
            if match.group(1) == head_sha:
                general[match.group(2)] += 1; spans.append(match.span())
        for match in PASS_REVIEW_RE.finditer(body):
            if match.group(1) == head_sha:
                passes[match.group(2)][match.group(3)] += 1; spans.append(match.span())
        if spans:
            remainder = body
            for start, end in reversed(spans):
                remainder = remainder[:start] + remainder[end:]
            if len(remainder.strip()) < 20:
                continue
            marker_ids.append(comment_id)
    if unresolved:
        state = "pending"
    elif general["blocked"] or any(value["blocked"] for value in passes.values()):
        state = "blocked"
    elif risk == "protected":
        state = "clean" if (
            general["clean"] == 0 and passes["scope-security"]["clean"] == 1
            and passes["correctness-race"]["clean"] == 1
        ) else ("pending" if marker_ids else "required")
    else:
        state = "clean" if general["clean"] == 1 and not any(value["clean"] for value in passes.values()) else ("pending" if marker_ids else "required")
    return ReviewEvidence(state, unresolved, tuple(sorted(marker_ids)), tuple(sorted(thread_state)))


def _pr_identity(pr: Mapping[str, Any], repo: str, default_branch: str) -> tuple[int, str]:
    number = _positive(pr.get("number"), "PR number")
    head, base = pr.get("head") or {}, pr.get("base") or {}
    if pr.get("state") != "open" or str(base.get("ref") or "") != default_branch:
        raise SupervisorError("PR state or base is invalid")
    if str(((head.get("repo") or {}).get("full_name")) or "").casefold() != repo.casefold():
        raise SupervisorError("PR head is not same-repository")
    if str(((base.get("repo") or {}).get("full_name")) or "").casefold() != repo.casefold():
        raise SupervisorError("PR base is not same-repository")
    return number, _sha(head.get("sha"), "PR head")


def _check_snapshot(
    runs: Sequence[Mapping[str, Any]],
    head: str,
    repo: str,
    number: int,
    required_checks: Sequence[str],
) -> tuple[tuple[str, str, int], ...]:
    required = tuple(required_checks)
    if not required or len(required) != len(set(required)):
        raise SupervisorError("required check identity is invalid")
    required_set = set(required)
    latest: dict[str, tuple[str, int, str]] = {}
    for run in runs:
        if run.get("event") != "pull_request" or run.get("head_sha") != head:
            continue
        if str(((run.get("repository") or {}).get("full_name")) or "").casefold() != repo.casefold():
            continue
        prs = run.get("pull_requests")
        if not isinstance(prs, list) or not any(
            isinstance(item, dict) and item.get("number") == number for item in prs
        ):
            continue
        name = str(run.get("name") or "")
        if name not in required_set:
            continue
        run_id = run.get("id")
        updated = str(run.get("updated_at") or run.get("created_at") or "")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0 or not updated:
            raise SupervisorError("workflow evidence is malformed")
        state = str(run.get("conclusion") or "pending") if run.get("status") == "completed" else "pending"
        candidate = (updated, run_id, state)
        if name not in latest or candidate[:2] > latest[name][:2]:
            latest[name] = candidate
    return tuple(
        (name, latest.get(name, ("", 0, "missing"))[2], latest.get(name, ("", 0, "missing"))[1])
        for name in required
    )


def _authorization_snapshot(scope: TaskScope) -> tuple[tuple[int, str, str, str, str], ...]:
    return tuple((item.comment_id, item.login, item.created_at, item.updated_at, item.body) for item in scope.authorization_comments)


def _overlaps(client: Client, repo: str, number: int, candidate: Sequence[str]) -> tuple[tuple[int, tuple[str, ...]], ...]:
    result = []
    for other in client.open_pulls():
        other_number = _positive(other.get("number"), "open PR number")
        if other_number == number:
            continue
        other_repo = str((((other.get("head") or {}).get("repo") or {}).get("full_name")) or "")
        if other_repo.casefold() != repo.casefold():
            continue
        overlap = tuple(sorted(set(candidate) & set(changed_paths(client.pull_files(other_number)))))
        if overlap:
            result.append((other_number, overlap))
    return tuple(sorted(result))



def _product_checks(client: Client, ref: str, where: str):
    raw = client.file_content(PRODUCT_CHECKS_PATH, ref)
    try:
        return raw, parse_product_checks(raw)
    except ProductCheckConfigError as exc:
        raise SupervisorError(f"{where} product check configuration is invalid") from exc

def evaluate(client: Client, repo: str, pr_number: int, automation_owner: str | None = None) -> Decision:
    if not REPO_RE.fullmatch(repo):
        raise SupervisorError("repository identity is invalid")
    repository = client.repository()
    default_branch = _text(repository.get("default_branch"), "default branch", 200)
    owner = _login(str(((repository.get("owner") or {}).get("login")) or repo.split("/", 1)[0]))
    trusted = {owner}
    if automation_owner:
        trusted.add(_login(automation_owner))
    default_sha = client.default_branch_sha(default_branch)
    pr = client.pull(pr_number)
    number, head = _pr_identity(pr, repo, default_branch)
    default_product_raw, default_product_checks = _product_checks(client, default_sha, "default")
    candidate_product_raw, candidate_product_checks = _product_checks(client, head, "candidate")
    configured_workflows = {**CHECK_WORKFLOWS, **{item.name: item.workflow for item in default_product_checks}}
    required_checks = tuple(configured_workflows)
    product_check_names = {item.name for item in default_product_checks}
    candidate_product_blobs = tuple(
        (item.workflow, client.file_blob(item.workflow, head))
        for item in candidate_product_checks
    )
    pr_body = _text(pr.get("body"), "PR body")
    source_number = source_issue_number(pr_body)
    issue = client.issue(source_number)
    issue_body = _text(issue.get("body"), "Issue body")
    issue_updated = str(issue.get("updated_at") or "")
    issue_author = str(((issue.get("user") or {}).get("login")) or "")
    if issue.get("state") != "open" or issue_author.casefold() not in {item.casefold() for item in trusted}:
        raise SupervisorError("source Issue is not open and trusted")
    scope = parse_task_scope(issue_body, client.issue_comments(source_number), trusted)
    auth_snapshot = _authorization_snapshot(scope)
    files = changed_paths(client.pull_files(number))
    if any(not path_allowed(path, scope.paths) for path in files):
        raise SupervisorError("PR paths exceed the source Issue")
    if any(is_protected_path(path) and not path_allowed(path, scope.protected_paths) for path in files):
        raise SupervisorError("protected path lacks authorization")
    overlap = _overlaps(client, repo, number, files)
    if overlap:
        raise SupervisorError(f"live Pull Request #{overlap[0][0]} overlaps the candidate path set")
    workflow_blobs = tuple(
        (name, workflow_path, client.file_blob(workflow_path, head), client.file_blob(workflow_path, default_sha))
        for name, workflow_path in configured_workflows.items()
    )
    for name, _workflow_path, candidate_blob, default_blob in workflow_blobs:
        if candidate_blob != default_blob:
            raise SupervisorError(f"{name} workflow differs from the default-branch definition")
    checks = _check_snapshot(client.workflow_runs(head), head, repo, number, required_checks)
    if any(
        state != "success" if name in product_check_names else state not in PASSING
        for name, state, _ in checks
    ):
        raise SupervisorError("required exact-head checks are not all successful")
    review = review_evidence(client.issue_comments(number), client.review_threads(number), head, scope.risk, trusted)
    if review.state != "clean":
        raise SupervisorError(f"coordinator review is {review.state}")
    final_issue = client.issue(source_number)
    if final_issue.get("state") != "open" or str(final_issue.get("body") or "") != issue_body or str(final_issue.get("updated_at") or "") != issue_updated:
        raise SupervisorError("source Issue changed during evaluation")
    if _authorization_snapshot(parse_task_scope(issue_body, client.issue_comments(source_number), trusted)) != auth_snapshot:
        raise SupervisorError("source authorization comments changed during evaluation")
    if client.default_branch_sha(default_branch) != default_sha:
        raise SupervisorError("default branch changed during evaluation")
    if client.file_content(PRODUCT_CHECKS_PATH, default_sha) != default_product_raw:
        raise SupervisorError("default product check configuration changed during evaluation")
    if client.file_content(PRODUCT_CHECKS_PATH, head) != candidate_product_raw:
        raise SupervisorError("candidate product check configuration changed during evaluation")
    if tuple(
        (item.workflow, client.file_blob(item.workflow, head))
        for item in candidate_product_checks
    ) != candidate_product_blobs:
        raise SupervisorError("candidate product workflow definitions changed during evaluation")
    if tuple(
        (name, workflow_path, client.file_blob(workflow_path, head), client.file_blob(workflow_path, default_sha))
        for name, workflow_path in configured_workflows.items()
    ) != workflow_blobs:
        raise SupervisorError("product workflow definitions changed during evaluation")
    if changed_paths(client.pull_files(number)) != files or _overlaps(client, repo, number, files) != overlap:
        raise SupervisorError("path or collision state changed during evaluation")
    if _check_snapshot(client.workflow_runs(head), head, repo, number, required_checks) != checks:
        raise SupervisorError("exact-head check evidence changed during evaluation")
    if review_evidence(client.issue_comments(number), client.review_threads(number), head, scope.risk, trusted) != review:
        raise SupervisorError("coordinator review evidence changed during evaluation")
    final_pr = client.pull(number)
    _, final_head = _pr_identity(final_pr, repo, default_branch)
    if final_head != head or str(final_pr.get("body") or "") != pr_body or final_pr.get("mergeable") is not True:
        raise SupervisorError("Pull Request identity, body, head, or mergeability changed")
    if "ai-no-merge" in {str(item.get("name") or "") for item in final_pr.get("labels") or []}:
        raise SupervisorError("Pull Request has ai-no-merge hold")
    return Decision("mark_ready" if final_pr.get("draft") is True else "merge", "all gates clean", head, scope.risk, 0)


def supervise(client: Client, repo: str, pr_number: int, automation_owner: str | None = None) -> Decision:
    decision = evaluate(client, repo, pr_number, automation_owner)
    if decision.action == "mark_ready":
        pr = client.pull(pr_number)
        if _sha((pr.get("head") or {}).get("sha"), "ready head") != decision.head_sha:
            raise SupervisorError("head moved before Ready")
        client.mark_ready(_text(pr.get("node_id"), "node id", 200))
        decision = evaluate(client, repo, pr_number, automation_owner)
    if decision.action != "merge":
        raise SupervisorError("post-Ready evaluation is not merge-ready")
    client.merge(pr_number, decision.head_sha)
    return decision


class GhClient:
    REVIEW_QUERY = "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){nameWithOwner pullRequest(number:$number){number reviewThreads(first:100,after:$cursor){nodes{id isResolved} pageInfo{hasNextPage endCursor}}}}}"
    READY_MUTATION = "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{id}}}"

    def __init__(self, repo: str) -> None:
        if not REPO_RE.fullmatch(repo):
            raise SupervisorError("repository identity is invalid")
        self.repo = repo
        self.owner, self.name = repo.split("/", 1)

    def _run(self, args: Sequence[str], stdin: str | None = None) -> Any:
        completed = subprocess.run(["gh", *args], input=stdin, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if completed.returncode != 0:
            raise SupervisorError("GitHub API request failed")
        return json.loads(completed.stdout or "null")

    def _api(self, path: str, method: str = "GET", payload: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith(f"repos/{self.repo}"):
            raise SupervisorError("API path escaped repository")
        args = ["api", "--method", method, path]
        stdin = None
        if payload is not None:
            args += ["--input", "-"]; stdin = json.dumps(payload, separators=(",", ":"))
        return self._run(args, stdin)

    def _pages(self, path: str) -> list[Mapping[str, Any]]:
        pages = self._run(["api", "--paginate", "--slurp", path])
        if not isinstance(pages, list) or len(pages) > MAX_PAGES:
            raise SupervisorError("pagination exceeded")
        values: list[Mapping[str, Any]] = []
        for page in pages:
            if isinstance(page, list): values.extend(item for item in page if isinstance(item, dict))
            elif isinstance(page, dict) and isinstance(page.get("workflow_runs"), list): values.extend(item for item in page["workflow_runs"] if isinstance(item, dict))
            else: raise SupervisorError("pagination response malformed")
        return values

    def repository(self): return self._api(f"repos/{self.repo}")
    def default_branch_sha(self, branch): return _sha(self._api(f"repos/{self.repo}/commits/{urllib.parse.quote(branch, safe='')}").get("sha"), "default SHA")
    def pull(self, number): return self._api(f"repos/{self.repo}/pulls/{number}")
    def issue(self, number): return self._api(f"repos/{self.repo}/issues/{number}")
    def issue_comments(self, number): return self._pages(f"repos/{self.repo}/issues/{number}/comments?per_page=100")
    def pull_files(self, number): return self._pages(f"repos/{self.repo}/pulls/{number}/files?per_page=100")
    def open_pulls(self): return self._pages(f"repos/{self.repo}/pulls?state=open&per_page=100")
    def workflow_runs(self, head_sha): return self._pages(f"repos/{self.repo}/actions/runs?event=pull_request&head_sha={head_sha}&per_page=100")
    def review_threads(self, number):
        cursor = None; nodes = []
        for _ in range(MAX_PAGES):
            args = ["api", "graphql", "-f", f"query={self.REVIEW_QUERY}", "-F", f"owner={self.owner}", "-F", f"name={self.name}", "-F", f"number={number}"]
            if cursor: args += ["-F", f"cursor={cursor}"]
            response = self._run(args)
            connection = (((response.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {}
            page_nodes, page_info = connection.get("nodes"), connection.get("pageInfo") or {}
            if not isinstance(page_nodes, list) or not isinstance(page_info.get("hasNextPage"), bool): raise SupervisorError("thread response malformed")
            nodes.extend(page_nodes)
            if not page_info["hasNextPage"]: return nodes
            cursor = page_info.get("endCursor")
            if not isinstance(cursor, str) or not cursor: raise SupervisorError("thread cursor invalid")
        raise SupervisorError("thread pagination exceeded")
    def file_content(self, path, ref):
        value = self._api(f"repos/{self.repo}/contents/{urllib.parse.quote(path, safe='/')}?ref={ref}")
        if value.get("type") != "file" or value.get("encoding") != "base64":
            raise SupervisorError("configuration content response is invalid")
        encoded = str(value.get("content") or "").replace("\n", "")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SupervisorError("configuration content encoding is invalid") from exc
    def file_blob(self, path, ref):
        value = self._api(f"repos/{self.repo}/contents/{urllib.parse.quote(path, safe='/')}?ref={ref}")
        return _sha(value.get("sha"), "file blob")
    def mark_ready(self, node_id): self._run(["api", "graphql", "-f", f"query={self.READY_MUTATION}", "-F", f"id={node_id}"])
    def merge(self, number, head_sha):
        result = self._api(f"repos/{self.repo}/pulls/{number}/merge", "PUT", {"sha": head_sha, "merge_method": "merge"})
        if result.get("merged") is not True: raise SupervisorError("expected-head merge was rejected")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--pr-number", action="append", type=int); args = parser.parse_args(argv)
    repo = os.environ.get("GITHUB_REPOSITORY", ""); owner = os.environ.get("AUTOMATION_OWNER") or None
    try:
        client = GhClient(repo)
        numbers = args.pr_number or [_positive(item.get("number"), "PR number") for item in client.open_pulls()]
        results = []
        for number in numbers:
            try:
                decision = supervise(client, repo, number, owner)
                results.append({"pr_number": number, "action": decision.action, "head_sha": decision.head_sha, "review_route": "github-coordinator", "review_state": "clean", "human_action_required": False})
            except SupervisorError as exc:
                results.append({"pr_number": number, "action": "blocked", "reason": str(exc), "review_route": "github-coordinator", "human_action_required": False})
        print(json.dumps(results, sort_keys=True)); return 0
    except (SupervisorError, OSError, json.JSONDecodeError) as exc:
        print(f"github-coordinator-supervisor: blocked: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
