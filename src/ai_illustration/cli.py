"""Command-line interface for local manifest validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .validation import validate_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-illustration-manifest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate one JSON file or a directory")
    validate.add_argument("path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    diagnostics = validate_path(args.path)
    payload = {"ok": not diagnostics, "diagnostic_count": len(diagnostics), "diagnostics": [item.to_dict() for item in diagnostics]}
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    print("validation succeeded" if not diagnostics else f"validation failed with {len(diagnostics)} diagnostic(s)", file=sys.stderr)
    return 0 if not diagnostics else 1


if __name__ == "__main__":
    raise SystemExit(main())
