#!/usr/bin/env python3
"""Validate GitHub-only runtime, optional-provider isolation and Bootstrap parity."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from scripts.foundation_product_checks import (
        CONFIG_PATH as PRODUCT_CHECKS_PATH,
        ProductCheckConfigError,
        parse_product_checks,
    )
except ModuleNotFoundError:
    from foundation_product_checks import (
        CONFIG_PATH as PRODUCT_CHECKS_PATH,
        ProductCheckConfigError,
        parse_product_checks,
    )

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"uses:\s*[^@\s]+@[0-9a-f]{40}\s*$")
REQUIRED = {
    "README.md", "LICENSE", "SECURITY.md", "AGENTS.md", "CLAUDE.md",
    "docs/PROJECT_STARTUP.md", "docs/MINIMUM_SAFETY_PROFILE.md",
    "docs/OPERATING_RULES.md", "docs/PUBLIC_SECURITY_MODEL.md",
    "scripts/public_export_guard.py", "scripts/validate_repository.py",
    "scripts/queue_failure_classifier.py", "scripts/queue_issue_hydration.py",
    "scripts/queue_retry_identity.py", "scripts/queue_event_guard.py",
    "scripts/foundation_product_checks.py",
    "scripts/github_api_governor.py", "scripts/github_coordinator_supervisor.py",
    "scripts/supervisor_policy.py", "scripts/foundation_drift.py",
    PRODUCT_CHECKS_PATH,
    ".github/workflows/ci.yml", ".github/workflows/unit-tests.yml",
    ".github/workflows/claude-queue.yml",
    ".github/workflows/claude-queue-comment-bridge.yml",
    ".github/workflows/ci-reconcile.yml", ".github/workflows/supervisor.yml",
    ".github/ISSUE_TEMPLATE/ai-task.yml", ".github/pull_request_template.md",
}
FOUNDATION_ONLY = {"bootstrap/generator.py"}
GENERATED_TARGET_MARKER = "<!-- ai-dev-automation-foundation:generated-target -->"


class ValidationError(AssertionError):
    pass


def text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require(content: str, values: tuple[str, ...], context: str) -> None:
    for value in values:
        if value not in content:
            raise ValidationError(f"{context} missing invariant: {value}")


def job(content: str, name: str, following: str | None = None) -> str:
    marker = f"\n  {name}:\n"
    if marker not in content:
        raise ValidationError(f"missing workflow job: {name}")
    block = content.split(marker, 1)[1]
    if following:
        block = block.split(f"\n  {following}:\n", 1)[0]
    return block


def validate() -> None:
    checklist = ROOT / "INSTALL_CHECKLIST.md"
    generated_target = (
        checklist.is_file()
        and GENERATED_TARGET_MARKER in checklist.read_text(encoding="utf-8")
    )
    required = REQUIRED if generated_target else REQUIRED | FOUNDATION_ONLY
    missing = sorted(path for path in required if not (ROOT / path).is_file())
    if missing:
        raise ValidationError("missing files: " + ", ".join(missing))
    try:
        parse_product_checks((ROOT / PRODUCT_CHECKS_PATH).read_bytes())
    except ProductCheckConfigError as exc:
        raise ValidationError(f"product check configuration is invalid: {exc}") from exc

    if generated_target:
        lock_path = ROOT / "FOUNDATION.lock.json"
        if lock_path.is_symlink() or not lock_path.is_file():
            raise ValidationError("generated target lock is missing or unsafe")
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError("generated target lock is malformed") from exc
        if not isinstance(lock, dict) or lock.get("schema_version") != 1:
            raise ValidationError("generated target lock schema is invalid")
        if lock.get("generator_version") != "2.0.0":
            raise ValidationError("generated target generator version is unsupported")
        if lock.get("source_repository") != "shiroku46/ai-dev-automation-foundation":
            raise ValidationError("generated target source repository is invalid")
        if re.fullmatch(r"[0-9a-f]{40}", str(lock.get("source_sha") or "")) is None:
            raise ValidationError("generated target source SHA is invalid")
        if lock.get("installation_mode") not in {"new-repository", "existing-product"}:
            raise ValidationError("generated target installation mode is invalid")
        if not isinstance(lock.get("installed_at"), str) or not lock["installed_at"]:
            raise ValidationError("generated target installation time is invalid")
        managed = lock.get("managed_files")
        if not isinstance(managed, list):
            raise ValidationError("generated target managed file list is invalid")
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in managed:
            if not isinstance(item, dict):
                raise ValidationError("generated target managed file entry is invalid")
            relative = item.get("path")
            digest = item.get("sha256")
            if (
                not isinstance(relative, str)
                or re.fullmatch(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*[\x00-\x1f\\])[^:]+$", relative) is None
                or relative in seen
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise ValidationError("generated target managed file identity is invalid")
            seen.add(relative)
            normalized.append((relative, digest))
        if normalized != sorted(normalized):
            raise ValidationError("generated target managed files are not sorted")
        preserved = {"README.md", "LICENSE", "AGENTS.md", "CLAUDE.md", "SECURITY.md"}
        target_owned = {PRODUCT_CHECKS_PATH}
        lock_required = (REQUIRED - preserved - target_owned) | {"INSTALL_CHECKLIST.md"}
        if not lock_required.issubset(seen):
            raise ValidationError("generated target lock omits required managed files")
        for relative, expected_digest in normalized:
            path = ROOT / relative
            if path.is_symlink() or not path.is_file():
                raise ValidationError(f"generated target managed file is missing or unsafe: {relative}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected_digest:
                raise ValidationError(f"generated target managed file drifted: {relative}")

    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        content = path.read_text(encoding="utf-8")
        require(content, ("name:", "\non:\n", "\njobs:\n"), str(path))
        if "\t" in content or "pull_request_target" in content:
            raise ValidationError(f"unsafe workflow syntax: {path}")
        for line in content.splitlines():
            if line.strip().removeprefix("- ").startswith("uses:") and not PIN.search(line):
                raise ValidationError(f"unpinned action: {path}: {line.strip()}")

    for name in ("ci.yml", "unit-tests.yml"):
        content = text(f".github/workflows/{name}")
        require(content, ("pull_request:", "contents: read", "persist-credentials: false"), name)
        for forbidden in ("secrets.", "id-token: write", "contents: write", "actions: write", "checks: write"):
            if forbidden in content:
                raise ValidationError(f"candidate check has forbidden capability: {name}: {forbidden}")

    supervisor = text(".github/workflows/supervisor.yml")
    require(supervisor, (
        'workflows: ["CI", "Unit Tests"]', "issue_comment:", "schedule:",
        "ref: ${{ github.event.repository.default_branch }}", "persist-credentials: false",
        "actions: read", "issues: read", "pull-requests: write", "contents: write",
        "python -m scripts.github_coordinator_supervisor",
    ), "GitHub coordinator supervisor")
    for forbidden in ("secrets.", "id-token: write", "actions: write", "issues: write", "anthropic", "codex", "supervisor_queue_recovery"):
        if forbidden.lower() in supervisor.lower():
            raise ValidationError(f"Supervisor retains provider/write capability: {forbidden}")

    reconcile = text(".github/workflows/ci-reconcile.yml")
    require(reconcile, (
        'workflows: ["CI", "Unit Tests", "Claude Issue Queue"]', "schedule:",
        "read-only compatibility observation", "queue_recovery:",
        "actions: write", "contents: write", "issues: read", "pull-requests: write",
        "queue-complete-", "queue-wip-", '["git", "apply", "--check"', '["git", "commit-tree"',
        "should_auto_retry", "max_retries = 3", "candidate_execution_with_write_token: `false`",
        "provider_invocation: false", "human_action_required: false",
    ), "CI reconciliation and bounded Queue recovery")
    observe = job(reconcile, "observe", "queue_recovery")
    require(observe, ("actions: read", "contents: read", "pull-requests: read"), "read-only CI observation")
    for forbidden in ("actions: write", "contents: write", "issues: write", "pull-requests: write", "secrets.", "id-token: write"):
        if forbidden in observe:
            raise ValidationError(f"read-only CI observation can mutate: {forbidden}")
    recovery = job(reconcile, "queue_recovery")
    for forbidden in ("secrets.", "id-token: write", "anthropics/", "claude-code-action"):
        if forbidden.lower() in recovery.lower():
            raise ValidationError(f"Queue reconciliation has forbidden provider capability: {forbidden}")
    require(recovery, (
        "actions: write", "contents: write", "issues: read", "pull-requests: write",
        "candidate.patch", "checkpoint.json", "scope_is_authorized",
        "protected_scope_is_authorized", "notification", "human_action_required",
        "automation-stops/queue-v4", "internal-stop.json", "exhausted.json",
    ), "bounded Queue recovery job")

    queue = text(".github/workflows/claude-queue.yml")
    require(queue, (
        "from scripts.queue_event_guard import resolve_queue_event",
        "COMMENT_ISSUE:", "COMMENT_BODY:", "COMMENT_IS_PR:",
        "decision = resolve_queue_event(", "check_tool_permission_contract", "contract_ok",
        "Optional provider missing secret", "credential-isolated route unavailable",
        "provider_invocation: false", "repository_credentials_exposed: false",
        "repository_write: false", "notification: false", "human_action_required: false",
        "exit 1", "publication_route: GitHub-direct coordinator", "request_fingerprint:",
        "retry_attempt:", "automation-stops/queue-v4/issue-", "automation-internal-stops",
        'record.get("next_automatic_action") == "dispatch one optional Queue retry"',
        "should_auto_retry(failure_class, attempt - 1, 3)",
    ), "optional Queue")
    for forbidden in ("EVENT_PATH", "github.event_path"):
        if forbidden in queue:
            raise ValidationError(f"optional Queue still reads an event payload file: {forbidden}")
    if "\n  issues:\n" in queue or "workflow_run:" in queue or "schedule:" in queue:
        raise ValidationError("optional Queue has an ordinary automatic trigger")
    prepare = job(queue, "prepare", "implement")
    require(prepare, (
        "resolve_queue_event", "decision.issue_number", "decision.allowed",
        "decision.automated_retry", "decision.fingerprint", "decision.retry_attempt",
        'owner = os.environ["OWNER"].casefold()',
        "def api(path):", "def api_pages(path):", "def trigger_identity(issue):",
        "def request_fingerprint(issue, base_sha):",
        "request_fingerprint(issue, base_sha)",
    ), "optional Queue dispatch guard")
    for forbidden in ("contents: write", "issues: write", "pull-requests: write", "id-token: write"):
        if forbidden in prepare:
            raise ValidationError(f"Queue dispatch guard can write or obtain OIDC: {forbidden}")
    implement = job(queue, "implement", "verify")
    require(implement, (
        "timeout-minutes: 5", "permissions: {}", "Optional provider missing secret",
        "credential-isolated route unavailable", "provider_invocation: false",
        "repository_credentials_exposed: false", "repository_write: false",
        "notification: false", "human_action_required: false", "exit 1",
    ), "credential-isolated optional route")
    for forbidden in (
        "continue-on-error: true", "secrets.", "id-token: write", "anthropics/",
        "actions/checkout@", "GH_TOKEN", "github.token", "persist-credentials",
        "remote.origin", "contents: write", "issues: write", "pull-requests: write",
        "queue-checkpoint", "candidate.patch", "checkpoint.json", "upload-artifact@",
    ):
        if forbidden in implement:
            raise ValidationError(f"credential-isolated optional route retains forbidden capability: {forbidden}")
    verify = job(queue, "verify", "publish")
    for forbidden in ("secrets.", "id-token: write", "contents: write", "pull-requests: write"):
        if forbidden in verify:
            raise ValidationError(f"verification job has forbidden capability: {forbidden}")
    publish = job(queue, "publish", "finalize")
    require(publish, ("contents: read", "repository_write: false"), "artifact publication handoff")
    for forbidden in ("contents: write", "pull-requests: write", "secrets.", "id-token: write", "anthropics/"):
        if forbidden in publish:
            raise ValidationError(f"optional publication handoff can mutate: {forbidden}")

    runtime = text("scripts/github_coordinator_supervisor.py")
    require(runtime, (
        "foundation-coordinator-review", "foundation-protected-authorization",
        "ai-no-merge", "workflow differs from the default-branch definition",
        "review evidence changed during evaluation",
        "expected-head merge was rejected", "human_action_required",
    ), "coordinator runtime")
    classifier = text("scripts/queue_failure_classifier.py")
    require(classifier, (
        "optional_provider_explicitly_enabled", "credential_ui_only_proven",
        "optional provider route unavailable; continue GitHub-direct work",
    ), "provider failure policy")


    if not generated_target:
        retired = (
            "scripts/ai_recovery_supervisor.py",
            "scripts/supervisor_final_guard.py",
            "scripts/supervisor_runtime.py",
            "scripts/supervisor_queue_recovery.py",
            "scripts/supervisor_queue_recovery_v2.py",
            "scripts/supervisor_queue_recovery_v3.py",
        )
        present = [relative for relative in retired if (ROOT / relative).exists()]
        if present:
            raise ValidationError("retired runtime files remain: " + ", ".join(present))

    issue_template = text(".github/ISSUE_TEMPLATE/ai-task.yml")
    require(issue_template, ("GitHub-only", "Risk tier", "Allowed paths", "Required checks", "Prohibited effects", "Rollback"), "Issue template")
    pr_template = text(".github/pull_request_template.md")
    require(pr_template, ("implementation_route", "github-direct", "exact_head_sha", "review_route", "github-coordinator", "review_state", "unresolved_review_threads", "human_action_required"), "PR template")

    if not generated_target:
        generator = text("bootstrap/generator.py")
        require(generator, (
            "MANAGED_FILES", "INSTALL_MODES", "PRESERVE_IF_PRESENT", "plan_render",
            "Bootstrap collisions", "FOUNDATION.lock.json", "source_sha",
            "destination.write_bytes(sources[relative])", "scripts/foundation_drift.py",
            "scripts/queue_issue_hydration.py", "scripts/queue_retry_identity.py",
            "scripts/queue_event_guard.py", "scripts/foundation_product_checks.py",
            "scripts/github_api_governor.py",
            "scripts/github_coordinator_supervisor.py", "scripts/supervisor_policy.py",
            ".github/workflows/supervisor.yml", "Codex and Claude setup is optional",
            "Non-destructive publication", "BOOTSTRAP_WORKFLOW_TOKEN",
        ), "Bootstrap")


def main() -> int:
    try:
        validate()
        print("repository validation passed")
        return 0
    except (OSError, ValidationError, ValueError) as exc:
        print(f"repository validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
