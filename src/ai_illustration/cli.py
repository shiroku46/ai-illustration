"""Command-line interface for manifests, catalogs, adapters, local review, variants, and exports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .adapters import AdapterError, ComfyUIAdapter, load_json_object
from .catalog import catalog_listing, evaluate_compatibility, load_catalog, validate_hardware_profile
from .exporter import ExportError, build_export_package, check_export_package
from .models import load_manifest
from .review_ui import ReviewUIError, run_review_ui
from .validation import validate_path
from .variants import VariantError, check_variant_set, load_json_object as load_variant_json, plan_variant_set


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
    review_ui = subparsers.add_parser("review-ui", help="start the read-only candidate review UI on 127.0.0.1")
    review_ui.add_argument("manifest_root", type=Path)
    review_ui.add_argument("--asset-root", type=Path, required=True)
    review_ui.add_argument("--port", type=int, default=8765)
    variant_plan = subparsers.add_parser("variant-plan", help="create a deterministic reviewed variant-set plan")
    variant_plan.add_argument("manifest_root", type=Path)
    variant_plan.add_argument("--source-candidate", required=True)
    variant_plan.add_argument("--matrix", type=Path, required=True)
    variant_plan.add_argument("--intent", choices=("evaluation", "production"), required=True)
    variant_check = subparsers.add_parser("variant-check", help="validate a variant-set against its source manifests")
    variant_check.add_argument("variant_set", type=Path)
    variant_check.add_argument("--manifest-root", type=Path, required=True)
    variant_export = subparsers.add_parser("variant-export", help="plan or write a verified local variant export package")
    variant_export.add_argument("variant_set", type=Path)
    variant_export.add_argument("--manifest-root", type=Path, required=True)
    variant_export.add_argument("--source-root", type=Path, required=True)
    variant_export.add_argument("--output-root", type=Path, required=True)
    variant_export.add_argument(
        "--approval-root",
        type=Path,
        help="production only: one canonical byte-bound accept review JSON per variant ID",
    )
    variant_export.add_argument("--write", action="store_true")
    export_check = subparsers.add_parser("export-check", help="verify a materialized variant export package")
    export_check.add_argument("package_manifest", type=Path)
    export_check.add_argument("--output-root", type=Path, required=True)
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

    if args.command in {"variant-plan", "variant-check"}:
        try:
            if args.command == "variant-plan":
                matrix = load_variant_json(args.matrix)
                plan = plan_variant_set(args.manifest_root, args.source_candidate, matrix, args.intent)
                summary = f"planned {len(plan['variants'])} reviewed variant(s)"
            else:
                plan = check_variant_set(args.variant_set, args.manifest_root)
                summary = f"validated {len(plan['variants'])} reviewed variant(s)"
            return _emit({"ok": True, "variant_set": plan}, summary, True)
        except VariantError as exc:
            return _emit(
                {"ok": False, "diagnostics": [exc.to_dict()]},
                f"variant validation failed: {exc.code}",
                False,
            )

    if args.command in {"variant-export", "export-check"}:
        try:
            if args.command == "variant-export":
                result = build_export_package(
                    args.variant_set,
                    args.manifest_root,
                    args.source_root,
                    args.output_root,
                    approval_root=args.approval_root,
                    write=args.write,
                )
                action = "materialized" if args.write else "planned"
                return _emit(result, f"{action} {len(result['package']['items'])} verified variant export(s)", True)
            result = check_export_package(args.package_manifest, args.output_root)
            return _emit(result, f"verified export package with {result['file_count']} file(s)", True)
        except ExportError as exc:
            return _emit(
                {"ok": False, "diagnostics": [exc.to_dict()]},
                f"export validation failed: {exc.code}",
                False,
            )
        except (OSError, ValueError) as exc:
            return _emit(
                {"ok": False, "diagnostics": [{"code": "EXPORT_ERROR", "message": str(exc), "field": ""}]},
                "export operation failed",
                False,
            )

    if args.command == "review-ui":
        try:
            run_review_ui(args.manifest_root, args.asset_root, args.port)
            return 0
        except (OSError, ReviewUIError) as exc:
            return _emit(
                {"ok": False, "diagnostics": [{"code": "REVIEW_UI_ERROR", "message": str(exc)}]},
                "review UI could not start",
                False,
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
