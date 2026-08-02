"""Command-line interface for local manifest and tool-catalog validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .catalog import catalog_listing, evaluate_compatibility, load_catalog, validate_hardware_profile
from .models import load_manifest
from .validation import validate_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-illustration-manifest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="validate one manifest JSON file or a directory")
    validate.add_argument("path", type=Path)
    catalog_validate = subparsers.add_parser("catalog-validate", help="validate one tool profile or a directory")
    catalog_validate.add_argument("path", type=Path)
    catalog_list = subparsers.add_parser("catalog-list", help="list validated tool profiles deterministically")
    catalog_list.add_argument("path", type=Path)
    catalog_compat = subparsers.add_parser("catalog-compat", help="compare tool profiles with one hardware profile")
    catalog_compat.add_argument("catalog", type=Path)
    catalog_compat.add_argument("hardware", type=Path)
    return parser


def _emit(payload: dict[str, object], summary: str, ok: bool) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    print(summary, file=sys.stderr)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        diagnostics = validate_path(args.path)
        return _emit(
            {"ok": not diagnostics, "diagnostic_count": len(diagnostics), "diagnostics": [item.to_dict() for item in diagnostics]},
            "validation succeeded" if not diagnostics else f"validation failed with {len(diagnostics)} diagnostic(s)",
            not diagnostics,
        )

    catalog_path = args.path if args.command != "catalog-compat" else args.catalog
    profiles, diagnostics = load_catalog(catalog_path)
    if args.command == "catalog-validate":
        return _emit(
            {"ok": not diagnostics, "profile_count": len(profiles), "diagnostic_count": len(diagnostics), "diagnostics": [item.to_dict() for item in diagnostics]},
            "catalog validation succeeded" if not diagnostics else f"catalog validation failed with {len(diagnostics)} diagnostic(s)",
            not diagnostics,
        )
    if diagnostics:
        return _emit(
            {"ok": False, "diagnostic_count": len(diagnostics), "diagnostics": [item.to_dict() for item in diagnostics]},
            f"catalog validation failed with {len(diagnostics)} diagnostic(s)",
            False,
        )
    if args.command == "catalog-list":
        listing = catalog_listing(profiles)
        return _emit({"ok": True, "profile_count": len(listing), "profiles": listing}, f"listed {len(listing)} profile(s)", True)

    try:
        hardware = load_manifest(args.hardware).data
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return _emit(
            {"ok": False, "diagnostic_count": 1, "diagnostics": [{"code": "LOAD_ERROR", "message": str(exc), "document": str(args.hardware), "field": "", "severity": "error"}]},
            "hardware profile could not be loaded",
            False,
        )
    hardware_diagnostics = validate_hardware_profile(args.hardware, hardware)
    if hardware_diagnostics:
        return _emit(
            {"ok": False, "diagnostic_count": len(hardware_diagnostics), "diagnostics": [item.to_dict() for item in hardware_diagnostics]},
            f"hardware validation failed with {len(hardware_diagnostics)} diagnostic(s)",
            False,
        )
    results = [evaluate_compatibility(profile, hardware).to_dict() for profile in profiles]
    return _emit({"ok": True, "hardware_id": hardware["id"], "results": results}, f"evaluated {len(results)} profile(s)", True)


if __name__ == "__main__":
    raise SystemExit(main())
