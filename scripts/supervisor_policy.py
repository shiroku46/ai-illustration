#!/usr/bin/env python3
"""Small policy helpers shared by tests and trusted runtime."""
from __future__ import annotations

from pathlib import PurePosixPath
import re

PROTECTED_PREFIXES = (".github/", "bootstrap/")
PROTECTED_EXACT = {
    "SECURITY.md",
    "scripts/supervisor_final_guard.py",
    "scripts/supervisor_policy.py",
    "scripts/supervisor_runtime.py",
    "scripts/supervisor_queue_recovery.py",
    "scripts/supervisor_queue_recovery_v2.py",
    "scripts/supervisor_queue_recovery_v3.py",
    "scripts/ai_recovery_supervisor.py",
}
ALLOWED_SCOPE_HEADINGS = frozenset(
    {
        "allowed paths",
        "allowed scope",
        "exact scope",
        "exact authorized scope",
    }
)


def normalize_path(path: str) -> str:
    normalized = str(PurePosixPath(path.strip().strip("`")))
    if normalized in {"", "."} or normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError("authorized paths must be repository-relative")
    return normalized


def is_protected(path: str) -> bool:
    normalized = normalize_path(path)
    return normalized in PROTECTED_EXACT or normalized.startswith(PROTECTED_PREFIXES)


def parse_issue_number(pr_body: str) -> int | None:
    match = re.search(r"(?im)\b(?:closes|fixes|resolves)\s+#(\d+)\b", pr_body or "")
    return int(match.group(1)) if match else None


def _bullet_path(raw: str) -> str | None:
    line = raw.strip()
    if not line.startswith("- "):
        return None
    value = line[2:].strip()
    if value.startswith("`") and "`" in value[1:]:
        value = value[1:].split("`", 1)[0]
    elif any(character.isspace() for character in value):
        return None
    try:
        return normalize_path(value)
    except ValueError:
        return None


def protected_authorized_paths(issue_body: str) -> set[str]:
    marker = "<!-- foundation-protected-authorization"
    end = "-->"
    if marker not in (issue_body or ""):
        return set()
    block = issue_body.split(marker, 1)[1].split(end, 1)[0]
    paths: set[str] = set()
    in_paths = False
    for raw in block.splitlines():
        line = raw.strip()
        if line == "paths:":
            in_paths = True
            continue
        if in_paths:
            path = _bullet_path(raw)
            if path:
                paths.add(path)
                continue
            if line and not line.startswith("#"):
                in_paths = False
    return paths


def declared_paths(issue_body: str) -> set[str]:
    """Return only paths from ordinary allowlist headings.

    Protected-authorization paths are intentionally excluded so protected changes
    must be independently present in both the ordinary allowlist and the stricter
    protected contract.
    """
    paths: set[str] = set()
    in_scope = False
    for raw in (issue_body or "").splitlines():
        line = raw.strip()
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            in_scope = heading.group(1).strip().lower() in ALLOWED_SCOPE_HEADINGS
            continue
        if in_scope and line.startswith("<!--"):
            in_scope = False
            continue
        if in_scope:
            path = _bullet_path(raw)
            if path:
                paths.add(path)
    return paths


def _matches(path: str, pattern: str) -> bool:
    normalized = normalize_path(path)
    authorized = normalize_path(pattern)
    if authorized.endswith("/**"):
        prefix = authorized[:-3].rstrip("/")
        return bool(prefix) and (
            normalized == prefix or normalized.startswith(f"{prefix}/")
        )
    if any(character in authorized for character in "*?["):
        return False
    return normalized == authorized


def scope_is_authorized(changed_paths, issue_body: str) -> bool:
    patterns = declared_paths(issue_body)
    changed = {normalize_path(path) for path in changed_paths}
    return bool(changed) and bool(patterns) and all(
        any(_matches(path, pattern) for pattern in patterns) for path in changed
    )


def protected_scope_is_authorized(changed_paths, issue_body: str) -> bool:
    patterns = protected_authorized_paths(issue_body)
    protected = {normalize_path(path) for path in changed_paths if is_protected(path)}
    return all(any(_matches(path, pattern) for pattern in patterns) for path in protected)


# Backward-compatible name used by earlier tests and consumers.
def authorized_paths(issue_body: str) -> set[str]:
    return protected_authorized_paths(issue_body)
