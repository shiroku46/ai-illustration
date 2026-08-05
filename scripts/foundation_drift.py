#!/usr/bin/env python3
"""Inspect a generated target against its Foundation lock without mutation."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

LOCK_FILE = "FOUNDATION.lock.json"
SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))(?!.*[\x00-\x1f\\])[^:]+$")


class LockError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_lock(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise LockError(f"lock is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LockError(f"lock is malformed: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise LockError("unsupported lock schema")
    if not _SHA_RE.fullmatch(str(value.get("source_sha") or "")):
        raise LockError("lock source_sha is invalid")
    if not isinstance(value.get("source_repository"), str) or not value["source_repository"]:
        raise LockError("lock source_repository is invalid")
    files = value.get("managed_files")
    if not isinstance(files, list):
        raise LockError("lock managed_files is invalid")
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, Mapping):
            raise LockError("managed file entry is invalid")
        relative = item.get("path")
        digest = item.get("sha256")
        if (
            not isinstance(relative, str)
            or not _SAFE_PATH_RE.fullmatch(relative)
            or relative in seen
            or not isinstance(digest, str)
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise LockError("managed file identity is invalid")
        seen.add(relative)
        normalized.append({"path": relative, "sha256": digest})
    if normalized != sorted(normalized, key=lambda item: item["path"]):
        raise LockError("managed files are not sorted")
    value = dict(value)
    value["managed_files"] = normalized
    return value


def hashes(lock: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item["path"]): str(item["sha256"])
        for item in lock.get("managed_files", [])
        if isinstance(item, Mapping)
    }


def inspect(root: Path, expected: Mapping[str, Any] | None = None) -> dict[str, Any]:
    root = root.expanduser().absolute()
    current_lock = load_lock(root / LOCK_FILE)
    current = hashes(current_lock)
    wanted = hashes(expected) if expected is not None else current
    entries: list[dict[str, str | None]] = []
    blocked = False
    drift = False

    for relative in sorted(set(current) | set(wanted)):
        destination = root / relative
        old_digest = current.get(relative)
        expected_digest = wanted.get(relative)
        actual_digest: str | None = None
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                state = "collision"
                blocked = True
            else:
                actual_digest = sha256_file(destination)
                if old_digest is None:
                    state = "collision"
                    blocked = True
                elif actual_digest != old_digest:
                    state = "collision" if expected_digest != old_digest else "modified"
                    blocked = blocked or state == "collision"
                    drift = True
                elif expected_digest is None:
                    state = "stale"
                    drift = True
                elif expected_digest != old_digest:
                    state = "stale"
                    drift = True
                else:
                    state = "unchanged"
        else:
            if old_digest is None and expected_digest is not None:
                state = "new"
            else:
                state = "missing"
            drift = True
        entries.append({
            "path": relative,
            "state": state,
            "locked_sha256": old_digest,
            "expected_sha256": expected_digest,
            "actual_sha256": actual_digest,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "source_repository": current_lock["source_repository"],
        "source_sha": current_lock["source_sha"],
        "expected_source_sha": expected.get("source_sha") if expected else current_lock["source_sha"],
        "status": "blocked" if blocked else ("drift" if drift else "clean"),
        "entries": entries,
        "human_action_required": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--expected-lock")
    args = parser.parse_args()
    try:
        expected = load_lock(Path(args.expected_lock)) if args.expected_lock else None
        report = inspect(Path(args.root), expected)
    except (OSError, LockError) as exc:
        print(json.dumps({
            "status": "invalid",
            "reason": str(exc),
            "human_action_required": False,
        }, indent=2, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "clean" else (2 if report["status"] == "blocked" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
