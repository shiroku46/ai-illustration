#!/usr/bin/env python3
"""Bind trusted attestation, authorization, native evidence, and merge."""
from __future__ import annotations

from scripts import supervisor_runtime as runtime

_native_workflow_evidence = runtime.native_workflow_evidence
_original_gh = runtime.gh
_original_current_default_sha = runtime.current_default_sha
_original_request_codex = runtime.request_codex
_verified_gate: tuple[str, int, str, int] | None = None


def _exact_live_default_sha() -> str:
    sha = str(_original_current_default_sha())
    if not runtime.EXACT_SHA.fullmatch(sha):
        raise RuntimeError("Default branch did not resolve to one exact SHA")
    return sha


def _require_unchanged_default(expected_sha: str) -> str:
    live_sha = _exact_live_default_sha()
    if live_sha != expected_sha:
        raise RuntimeError("Default branch moved during final evidence validation")
    return expected_sha


def _authorized_source_issue(live_pr: dict, candidate_sha: str) -> int:
    live_head = str((live_pr.get("head") or {}).get("sha") or "")
    if not isinstance(live_pr.get("labels"), list):
        raise RuntimeError("Live Pull Request omitted explicit label evidence")
    if live_head != candidate_sha or not runtime.trusted_candidate(live_pr):
        raise RuntimeError("Live Pull Request no longer matches the trusted candidate")
    issue_number, _, _, scope_error = runtime.source_and_scope(live_pr)
    if scope_error or not isinstance(issue_number, int) or issue_number <= 0:
        raise RuntimeError("Live source and scope authorization no longer passes")
    return issue_number


def request_codex_exact_head(pr_number: int, sha: str) -> None:
    """Dispatch one idempotent provider review request bound to the exact head."""
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("Pull Request number must be a positive integer")
    if not runtime.EXACT_SHA.fullmatch(sha):
        raise ValueError("Codex review request requires one exact candidate SHA")
    _original_request_codex(pr_number, sha)


def guarded_native_workflow_evidence(sha: str, pr_number: int):
    """Evaluate every trust input against one immutable default SHA."""
    global _verified_gate
    _verified_gate = None
    default_sha = _exact_live_default_sha()
    previous_current_default_sha = runtime.current_default_sha
    runtime.current_default_sha = lambda: _require_unchanged_default(default_sha)
    try:
        attempts = runtime.attestation_attempts(sha)
        if not any(item["success"] for item in attempts):
            return False, []
        clean, evidence = _native_workflow_evidence(sha, pr_number)
        if not clean:
            return False, evidence
        live_pr = runtime.api(f"repos/{runtime.REPO}/pulls/{pr_number}")
        issue_number = _authorized_source_issue(live_pr, sha)
        _require_unchanged_default(default_sha)
        _verified_gate = (sha, pr_number, default_sha, issue_number)
        return True, evidence
    except (RuntimeError, ValueError):
        _verified_gate = None
        return False, []
    finally:
        runtime.current_default_sha = previous_current_default_sha


def _merge_identity(args: tuple[str, ...]) -> tuple[str, int] | None:
    if not args or args[0] != "api":
        return None
    method = None
    path = None
    candidate_sha = None
    for index, value in enumerate(args):
        if value == "--method" and index + 1 < len(args):
            method = args[index + 1]
        if value.startswith(f"repos/{runtime.REPO}/pulls/") and value.endswith("/merge"):
            path = value
        if value == "-f" and index + 1 < len(args):
            field = args[index + 1]
            if field.startswith("sha="):
                candidate_sha = field.removeprefix("sha=")
    if method != "PUT" or path is None:
        return None
    if candidate_sha is None or not runtime.EXACT_SHA.fullmatch(candidate_sha):
        raise RuntimeError("Merge call omitted its exact candidate SHA")
    prefix = f"repos/{runtime.REPO}/pulls/"
    number_text = path.removeprefix(prefix).removesuffix("/merge")
    if not number_text.isdigit() or int(number_text) <= 0:
        raise RuntimeError("Merge call omitted its fixed Pull Request number")
    return candidate_sha, int(number_text)


def guarded_gh(*args: str) -> str:
    """Consume and recheck every trust-relevant input immediately before merge."""
    global _verified_gate
    identity = _merge_identity(args)
    if identity is None:
        return _original_gh(*args)
    if _verified_gate is None:
        raise RuntimeError("Merge call has no verified final evidence gate")
    gate = _verified_gate
    _verified_gate = None
    candidate_sha, pr_number = identity
    verified_sha, verified_pr, default_sha, verified_issue = gate
    if (candidate_sha, pr_number) != (verified_sha, verified_pr):
        raise RuntimeError("Merge call does not match the verified candidate gate")
    live_pr = runtime.api(f"repos/{runtime.REPO}/pulls/{pr_number}")
    if (
        live_pr.get("state") != "open"
        or live_pr.get("draft") is not False
        or live_pr.get("mergeable") is not True
    ):
        raise RuntimeError("Live Pull Request no longer matches the trusted merge gate")
    live_issue = _authorized_source_issue(live_pr, candidate_sha)
    if live_issue != verified_issue:
        raise RuntimeError("Live trusted source Issue no longer matches the verified gate")
    _require_unchanged_default(default_sha)
    return _original_gh(*args)


def main() -> int:
    runtime.request_codex = request_codex_exact_head
    runtime.native_workflow_evidence = guarded_native_workflow_evidence
    runtime.gh = guarded_gh
    return runtime.main()


if __name__ == "__main__":
    raise SystemExit(main())
