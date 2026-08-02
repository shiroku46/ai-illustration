"""Command-line interface for manifests, catalogs, and dry-run adapters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .adapters import AdapterError, ComfyUIAdapter, load_json_object
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
    adapter_check = subparsers.add_parser("adapter-check", help="validate a ComfyUI API-format workflow without execution")
    adapter_check.add_argument("workflow", type=Path)
    adapter_plan = subparsers.add_parser("adapter-plan", help="create a deterministic ComfyUI dry-run execution plan")
    adapter_plan.add_argument("request", type=Path)
    adapter_plan.add_argument("workflow", type=Path)
    adapter_plan.add_argument("--bindings", type=Path)
    adapter_plan.add_argument("--endpoint", default="http://127.0.0.1:8188")
    return parser


def _emit(payload: dict[str, object], summary: str, ok: bool) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    print(summary, file=sys.stderr)
    return 0 if ok else 1


def _adapter_error(exc: AdapterError) -> int:
    return _emit({"ok": False, "diagnostics": [exc.to_dict()]}, f"adapter validation failed: {exc.code}", False)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        diagnostics = validate_path(args.path)
        return _emit(
            {"ok": not diagnostics, "diagnostic_count": len(diagnostics), "diagnostics": [item.to_dict() for item in diagnostics]},
            "validation succeeded" if not diagnostics else f"validation failed with {len(diagnostics)} diagnostic(s)",
            not diagnostics,
        )

    if args.command in {"adapter-check", "adapter-plan"}:
        adapter = ComfyUIAdapter()
        try:
            workflow = load_json_object(args.workflow)
            if args.command == "adapter-check":
                summary = adapter.check_workflow(workflow)
                return _emit({"ok": True, "adapter_id": adapter.adapter_id, "workflow": summary}, "adapter workflow validation succeeded", True)
            request = load_json_object(args.request)
            bindings_path = args.bindings or args.workflow.with_name("bindings.json")
            bindings = load_json_object(bindings_path)
            plan = adapter.plan(request, workflow, bindings, endpoint=args.endpoint)
            return _emit({"ok": True, "plan": plan.to_dict()}, "adapter dry-run plan created", True)
        except AdapterError as exc:
            return _adapter_error(exc)

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
