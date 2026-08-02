#!/usr/bin/env python3
"""Fail closed when a public export contains secrets or private references."""
from __future__ import annotations
import argparse
import re
from pathlib import Path

TEXT_SUFFIXES = {
    ".md", ".py", ".yml", ".yaml", ".json", ".toml", ".txt", ".tmpl",
    ".gitignore", ".cfg", ".ini",
}
SKIP_PARTS = {".git", "__pycache__", ".pytest_cache"}
PATTERNS = {
    "private-repository-reference": re.compile(r"ai-dev-automation-(?:sandbox|e2e)(?!-foundation)", re.I),
    "private-notion-reference": re.compile(r"(?:notion\.so|app\.notion\.com)/(?:p/)?[0-9a-f]{20,}", re.I),
    "credential-value": re.compile(
        r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}"
    ),
    "github-token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "private-actions-url": re.compile(r"github\.com/[^/\s]+/[^/\s]+/actions/runs/\d+", re.I),
    "product-specific": re.compile(r"\b(?:TRPG|BOOTH|luluportal|競馬|黒百合)\b", re.I),
}
ALLOWED_SECRET_REFERENCES = {
    "${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
}

def iter_text_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.relative_to(root).as_posix() in {"scripts/public_export_guard.py", "tests/test_export_guard.py"}:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore"}:
            yield path

def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in iter_text_files(root):
        text = path.read_text(encoding="utf-8")
        cleaned = text
        for allowed in ALLOWED_SECRET_REFERENCES:
            cleaned = cleaned.replace(allowed, "")
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(cleaned):
                line = cleaned.count("\n", 0, match.start()) + 1
                findings.append(f"{path.relative_to(root)}:{line}: {name}")
    return findings

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    findings = scan(Path(args.root).resolve())
    if findings:
        print("\n".join(findings))
        return 1
    print("public export guard: clean")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
