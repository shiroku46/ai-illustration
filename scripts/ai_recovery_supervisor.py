#!/usr/bin/env python3
"""Pure deterministic recovery decision engine.

The engine distinguishes bounded technical recovery, audited non-notifying internal
stops, and the very small set of genuine human-only UI/identity operations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import re


class Action(str, Enum):
    NOOP = "NOOP"
    WAIT = "WAIT"
    RETRY_TRANSIENT = "RETRY_TRANSIENT"
    REQUEST_BOUNDED_FIX = "REQUEST_BOUNDED_FIX"
    RERUN_EXACT_SHA_CHECKS = "RERUN_EXACT_SHA_CHECKS"
    REQUEST_CODEX_REVIEW = "REQUEST_CODEX_REVIEW"
    RUN_SELF_RESOLUTION_AUDIT = "RUN_SELF_RESOLUTION_AUDIT"
    INTERNAL_STOP = "INTERNAL_STOP"
    MARK_READY = "MARK_READY"
    MERGE = "MERGE"
    CREATE_NEXT_PHASE = "CREATE_NEXT_PHASE"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    BLOCK_AND_CLOSE = "BLOCK_AND_CLOSE"


class Reason(str, Enum):
    DUPLICATE = "DUPLICATE_RECONCILIATION"
    HOLD = "AI_NO_MERGE_HOLD"
    AUDIT_REQUIRED = "SELF_RESOLUTION_AUDIT_REQUIRED"
    UNAUTHORIZED_PROTECTED = "UNAUTHORIZED_PROTECTED_PATH"
    MISSING_CHECKS = "MISSING_REQUIRED_CHECKS"
    UNTRUSTED_EVIDENCE = "UNTRUSTED_CHECK_EVIDENCE"
    CHECKS_RUNNING = "REQUIRED_CHECKS_RUNNING"
    TRANSIENT = "TRANSIENT_FAILURE_RETRY"
    COOLDOWN = "RETRY_COOLDOWN"
    EXHAUSTED = "INTERNAL_RETRY_BUDGET_EXHAUSTED"
    BOUNDED_FIX = "BOUNDED_DETERMINISTIC_FIX"
    AMBIGUOUS = "INTERNAL_AMBIGUOUS_FAILURE"
    AUTOMATABLE_PERMISSION = "INTERNAL_AUTOMATABLE_PERMISSION_OR_WORKFLOW_REPAIR"
    CODEX_REQUIRED = "CODEX_REVIEW_REQUIRED"
    CODEX_BLOCKER = "CODEX_BLOCKER"
    NO_PROGRESS = "INTERNAL_NO_PROGRESS_AFTER_AUDIT"
    MERGE_BLOCKED = "INTERNAL_MERGE_STATE_BLOCKED"
    READY = "READY_FOR_REVIEW"
    MERGE = "ALL_GATES_PASSED"
    NEXT_PHASE = "PREDECLARED_NEXT_PHASE"
    HUMAN_REPOSITORY_UI = "HUMAN_ONLY_ACCOUNT_LEVEL_REPOSITORY_CREATION_UI_UNAVAILABLE"
    HUMAN_CREDENTIAL_UI = "HUMAN_ONLY_CREDENTIAL_PROVIDER_UI_REQUIRED"
    HUMAN_DISCONNECTED_INTEGRATION = "HUMAN_ONLY_DISCONNECTED_INTEGRATION_RECONNECTION_UI_REQUIRED"
    NOOP = "NO_ACTION"


@dataclass(frozen=True)
class Check:
    context: str
    state: str
    sha: str
    producer: str
    run_id: str = ""
    failure_fingerprint: str = ""


@dataclass(frozen=True)
class CodexEvidence:
    sha: str = ""
    reviewed: bool = False
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoundedFixEvidence:
    sha: str
    run_id: str
    failure_fingerprint: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class NextPhase:
    name: str
    goal: str
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True)
class SelfResolutionAudit:
    """Immutable evidence that connected recovery paths were exhausted.

    Audit evidence is valid for one exact head SHA and one reason family only.
    Internal stops require a completed audit and at least one concrete attempted
    connected path. Human-only escalation additionally requires concrete
    impossibility evidence, one reason-compatible UI action, and an automatic-
    resumption condition.
    """

    completed: bool = False
    audited_sha: str = ""
    reason_family: str = ""
    attempted_connected_paths: tuple[str, ...] = ()
    impossibility_evidence: tuple[str, ...] = ()
    minimal_human_action: str = ""
    automatic_resume_condition: str = ""


@dataclass(frozen=True)
class Policy:
    required_checks: tuple[str, ...] = ("CI / validate", "Unit Tests / test")
    trusted_producers: tuple[str, ...] = ("github-actions[bot]",)
    retry_cooldown_seconds: int = 900
    max_retries: int = 3
    max_bounded_fix_paths: int = 4


@dataclass(frozen=True)
class State:
    issue_number: int
    pr_number: int
    head_sha: str
    checks: tuple[Check, ...] = ()
    codex: CodexEvidence = CodexEvidence()
    attempt_count: int = 0
    seconds_since_last_attempt: int | None = None
    last_action_key: str = ""
    transient_failure: bool = False
    deterministic_failure: bool = False
    concrete_failure_run_id: str = ""
    concrete_failure_fingerprint: str = ""
    bounded_fix: BoundedFixEvidence | None = None
    allowed_fix_paths: tuple[str, ...] = ()
    protected_paths_changed: tuple[str, ...] = ()
    protected_authorized_paths: tuple[str, ...] = ()
    risk_flags: tuple[str, ...] = ()
    ai_no_merge: bool = False
    draft: bool = True
    mergeable: bool = True
    next_phase: NextPhase | None = None
    no_progress_seconds: int = 0
    self_resolution_audit: SelfResolutionAudit = SelfResolutionAudit()


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: Reason
    explanation: str
    idempotency_key: str


HUMAN_ONLY_REASON_BY_RISK = {
    "account-level-repository-creation-ui-unavailable": Reason.HUMAN_REPOSITORY_UI,
    "account-level-app-connection-ui-unavailable": Reason.HUMAN_REPOSITORY_UI,
    "credential-provider-ui-required": Reason.HUMAN_CREDENTIAL_UI,
    "credential-acquisition-ui-required": Reason.HUMAN_CREDENTIAL_UI,
    "credential-renewal-ui-required": Reason.HUMAN_CREDENTIAL_UI,
    "mfa": Reason.HUMAN_CREDENTIAL_UI,
    "captcha": Reason.HUMAN_CREDENTIAL_UI,
    "hardware-key": Reason.HUMAN_CREDENTIAL_UI,
    "trusted-local-device": Reason.HUMAN_CREDENTIAL_UI,
    "provider-ui": Reason.HUMAN_CREDENTIAL_UI,
    "disconnected-integration-no-callable-reconnect": Reason.HUMAN_DISCONNECTED_INTEGRATION,
}

# These words alone never justify asking a person to intervene. They represent
# technical policy or implementation states that must be repaired automatically
# or converted to an audited non-notifying internal stop.
AUTOMATABLE_OR_INTERNAL_RISKS = {
    "secret",
    "permission",
    "workflow-permission",
    "repository-setting",
    "billing",
    "authentication",
    "deployment",
    "production",
    "destructive-data",
    "essential-ambiguity",
    "merge-conflict",
    "untrusted-evidence",
}

# Human-only actions are closed, canonical sentences. Exact equality prevents a
# valid provider-UI operation from being combined with a routine request such as
# Merge, Approve, Retry, Close, permission changes, billing, or deployment.
HUMAN_ACTION_BY_REASON = {
    Reason.HUMAN_REPOSITORY_UI: (
        "create or reconnect the named repository or github app installation in the account-level provider ui."
    ),
    Reason.HUMAN_CREDENTIAL_UI: (
        "complete the required credential, mfa, captcha, hardware-key, trusted-device, or provider verification in the provider ui."
    ),
    Reason.HUMAN_DISCONNECTED_INTEGRATION: (
        "reconnect the disconnected integration in the provider ui."
    ),
}


def _sorted_dict(value):
    if isinstance(value, dict):
        return {key: _sorted_dict(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple, set)):
        converted = [_sorted_dict(item) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _key(state: State, policy: Policy, action: Action, reason: Reason) -> str:
    payload = {
        "state": asdict(state),
        "policy": asdict(policy),
        "action": action.value,
        "reason": reason.value,
    }
    payload["state"]["last_action_key"] = ""
    canonical = json.dumps(_sorted_dict(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _decision(
    state: State,
    policy: Policy,
    action: Action,
    reason: Reason,
    explanation: str,
) -> Decision:
    key = _key(state, policy, action, reason)
    if state.last_action_key and state.last_action_key == key:
        return Decision(
            Action.NOOP,
            Reason.DUPLICATE,
            "Identical action was already applied.",
            key,
        )
    return Decision(action, reason, explanation, key)


def _nonempty_entries(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value and value.strip())


def _audit_is_bound(state: State, reason: Reason) -> bool:
    audit = state.self_resolution_audit
    exact_sha = bool(re.fullmatch(r"[0-9a-f]{40}", state.head_sha or ""))
    return bool(
        exact_sha
        and audit.completed
        and audit.audited_sha == state.head_sha
        and audit.reason_family == reason.value
        and _nonempty_entries(audit.attempted_connected_paths)
    )


def _human_action_matches_reason(reason: Reason, action: str) -> bool:
    normalized = " ".join(action.lower().split())
    return normalized == HUMAN_ACTION_BY_REASON.get(reason, "")


def _internal_stop_after_audit(
    state: State,
    policy: Policy,
    reason: Reason,
    explanation: str,
) -> Decision:
    if not _audit_is_bound(state, reason):
        return _decision(
            state,
            policy,
            Action.RUN_SELF_RESOLUTION_AUDIT,
            Reason.AUDIT_REQUIRED,
            "Run the mandatory repository, workflow, check, review, permission, "
            "and alternative-path self-resolution audit bound to the current exact "
            f"SHA and reason family `{reason.value}` before recording an internal stop.",
        )
    return _decision(state, policy, Action.INTERNAL_STOP, reason, explanation)


def _human_only_decision(
    state: State,
    policy: Policy,
    reason: Reason,
) -> Decision:
    audit = state.self_resolution_audit
    action = audit.minimal_human_action.strip()
    complete = bool(
        _audit_is_bound(state, reason)
        and _nonempty_entries(audit.impossibility_evidence)
        and _human_action_matches_reason(reason, action)
        and audit.automatic_resume_condition.strip()
    )
    if not complete:
        return _decision(
            state,
            policy,
            Action.RUN_SELF_RESOLUTION_AUDIT,
            Reason.AUDIT_REQUIRED,
            "Human-only notification is forbidden until a completed audit bound to "
            "the current exact SHA and selected reason family records concrete attempted "
            "connected paths, concrete impossibility evidence, the canonical minimal "
            "account/provider UI action, and the automatic-resumption condition.",
        )
    attempted = "; ".join(_nonempty_entries(audit.attempted_connected_paths))
    impossible = "; ".join(_nonempty_entries(audit.impossibility_evidence))
    explanation = (
        f"Issue #{state.issue_number}; PR #{state.pr_number}; exact SHA {state.head_sha}. "
        f"Attempted connected paths: {attempted}. Impossibility evidence: {impossible}. "
        f"Minimal human UI action: {action}. "
        f"Automatic resumption condition: {audit.automatic_resume_condition.strip()}."
    )
    return _decision(state, policy, Action.ESCALATE_HUMAN, reason, explanation)


def decide(state: State, policy: Policy = Policy()) -> Decision:
    if state.ai_no_merge:
        return _decision(
            state,
            policy,
            Action.WAIT,
            Reason.HOLD,
            "`ai-no-merge` is a hard hold.",
        )

    risks = set(state.risk_flags)
    human_reasons = sorted(
        {HUMAN_ONLY_REASON_BY_RISK[risk] for risk in risks if risk in HUMAN_ONLY_REASON_BY_RISK},
        key=lambda item: item.value,
    )
    if human_reasons:
        return _human_only_decision(state, policy, human_reasons[0])

    if risks & AUTOMATABLE_OR_INTERNAL_RISKS:
        return _internal_stop_after_audit(
            state,
            policy,
            Reason.AUTOMATABLE_PERMISSION,
            "A technical permission, workflow, authentication declaration, merge-state, "
            "or policy condition remains after bounded connected repair paths; record an "
            "internal stop without instructing the owner.",
        )

    changed = set(state.protected_paths_changed)
    authorized = set(state.protected_authorized_paths)
    if changed - authorized:
        return _decision(
            state,
            policy,
            Action.BLOCK_AND_CLOSE,
            Reason.UNAUTHORIZED_PROTECTED,
            "Protected paths are not covered by trusted Issue authorization.",
        )

    checks_by_context: dict[str, tuple[Check, ...]] = {}
    for context in policy.required_checks:
        matches = tuple(
            sorted(
                (
                    check
                    for check in state.checks
                    if check.sha == state.head_sha and check.context == context
                ),
                key=lambda check: (
                    check.producer,
                    check.run_id,
                    check.failure_fingerprint,
                    check.state,
                ),
            )
        )
        checks_by_context[context] = matches

    missing = [name for name, checks in checks_by_context.items() if not checks]
    if missing:
        return _decision(
            state,
            policy,
            Action.RERUN_EXACT_SHA_CHECKS,
            Reason.MISSING_CHECKS,
            f"Required checks are absent for current SHA: {', '.join(missing)}",
        )

    for name in policy.required_checks:
        checks = checks_by_context[name]
        if any(check.producer not in policy.trusted_producers for check in checks):
            return _decision(
                state,
                policy,
                Action.RERUN_EXACT_SHA_CHECKS,
                Reason.UNTRUSTED_EVIDENCE,
                f"Check {name} has an untrusted producer; replace it with fresh trusted exact-SHA evidence.",
            )
        if any(check.state in {"queued", "pending", "in_progress"} for check in checks):
            return _decision(
                state,
                policy,
                Action.WAIT,
                Reason.CHECKS_RUNNING,
                f"Check {name} is still running.",
            )

    failures = [
        check
        for name in policy.required_checks
        for check in checks_by_context[name]
        if check.state != "success"
    ]
    if failures:
        if state.transient_failure:
            if state.attempt_count >= policy.max_retries:
                return _internal_stop_after_audit(
                    state,
                    policy,
                    Reason.EXHAUSTED,
                    "The bounded transient retry budget is exhausted; retain one audited "
                    "non-notifying internal stop.",
                )
            if state.attempt_count > 0 and state.seconds_since_last_attempt is None:
                return _decision(
                    state,
                    policy,
                    Action.WAIT,
                    Reason.COOLDOWN,
                    "Elapsed-time evidence is missing; cooldown cannot be bypassed.",
                )
            if (state.seconds_since_last_attempt or 0) < policy.retry_cooldown_seconds:
                return _decision(
                    state,
                    policy,
                    Action.WAIT,
                    Reason.COOLDOWN,
                    "Retry cooldown has not elapsed.",
                )
            return _decision(
                state,
                policy,
                Action.RETRY_TRANSIENT,
                Reason.TRANSIENT,
                "Retry one bounded transient failure.",
            )

        if state.deterministic_failure and state.bounded_fix:
            fix = state.bounded_fix
            state_run_id = state.concrete_failure_run_id.strip()
            state_fingerprint = state.concrete_failure_fingerprint.strip()
            fix_run_id = fix.run_id.strip()
            fix_fingerprint = fix.failure_fingerprint.strip()
            valid_identity = bool(
                state_run_id
                and state_fingerprint
                and fix_run_id
                and fix_fingerprint
                and fix.sha == state.head_sha
                and fix_run_id == state_run_id
                and fix_fingerprint == state_fingerprint
            )
            paths = set(fix.paths)
            if (
                valid_identity
                and paths
                and len(paths) <= policy.max_bounded_fix_paths
                and paths <= set(state.allowed_fix_paths)
            ):
                return _decision(
                    state,
                    policy,
                    Action.REQUEST_BOUNDED_FIX,
                    Reason.BOUNDED_FIX,
                    "Concrete current failure is bound to a small allowlisted fix.",
                )
        return _internal_stop_after_audit(
            state,
            policy,
            Reason.AMBIGUOUS,
            "The deterministic failure is not safely repairable within the current "
            "bounded evidence and scope; retain an audited internal stop.",
        )

    if not state.codex.reviewed or state.codex.sha != state.head_sha:
        return _decision(
            state,
            policy,
            Action.REQUEST_CODEX_REVIEW,
            Reason.CODEX_REQUIRED,
            "Fresh Codex review is required for the current exact SHA.",
        )
    if state.codex.blockers:
        return _decision(
            state,
            policy,
            Action.REQUEST_BOUNDED_FIX,
            Reason.CODEX_BLOCKER,
            "Codex reported blocking findings on the current exact SHA.",
        )

    if state.no_progress_seconds >= 3600:
        return _internal_stop_after_audit(
            state,
            policy,
            Reason.NO_PROGRESS,
            "No meaningful progress remained after the completed repository, workflow, "
            "check, review, permission, and alternative-path audit.",
        )

    if state.draft:
        return _decision(
            state,
            policy,
            Action.MARK_READY,
            Reason.READY,
            "All evidence gates pass; mark the exact-SHA Draft ready.",
        )
    if state.mergeable:
        return _decision(
            state,
            policy,
            Action.MERGE,
            Reason.MERGE,
            "All exact-SHA gates pass; merge with expected-head-SHA protection.",
        )
    if state.next_phase and state.next_phase.goal and state.next_phase.allowed_paths:
        return _decision(
            state,
            policy,
            Action.CREATE_NEXT_PHASE,
            Reason.NEXT_PHASE,
            "A concrete predeclared next phase is available.",
        )
    return _internal_stop_after_audit(
        state,
        policy,
        Reason.MERGE_BLOCKED,
        "All evidence is clean but the merge state has no bounded automatic transition; "
        "retain an audited internal stop rather than asking the owner to press Merge.",
    )
