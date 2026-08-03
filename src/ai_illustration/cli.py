"""Command-line interface for manifests, catalogs, adapters, local review, variants, exports, and paper theater."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .adapters import AdapterError, ComfyUIAdapter, load_json_object
from .audio_preview import AudioPreviewError, build_audio_preview_package, check_audio_preview_package
from .catalog import catalog_listing, evaluate_compatibility, load_catalog, validate_hardware_profile
from .composition import CompositionError, build_composition_job_package, check_composition_job_package
from .exporter import ExportError, build_export_package, check_export_package
from .frame_renderer import FrameRenderError, build_frame_render_package, check_frame_render_package
from .models import load_manifest
from .paper_theater import PaperTheaterError, check_scene_plan, plan_scene
from .preview import PreviewError, build_preview_package, check_preview_package
from .render_plan import RenderPlanError, build_render_plan_package, check_render_plan_package
from .review_ui import ReviewUIError, run_review_ui
from .validation import validate_path
from .variants import VariantError, check_variant_set, load_json_object as load_variant_json, plan_variant_set


def _root_args(parser: argparse.ArgumentParser, *names: str) -> None:
    for name in names:
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, type=Path, required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-illustration-manifest")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("path", type=Path)
    for command in ("catalog-validate", "catalog-list"):
        sub = commands.add_parser(command)
        sub.add_argument("path", type=Path)
    compatibility = commands.add_parser("catalog-compat")
    compatibility.add_argument("catalog", type=Path)
    compatibility.add_argument("hardware", type=Path)

    adapter_check = commands.add_parser("adapter-check")
    adapter_check.add_argument("workflow", type=Path)
    adapter_plan = commands.add_parser("adapter-plan")
    adapter_plan.add_argument("request", type=Path)
    adapter_plan.add_argument("workflow", type=Path)
    adapter_plan.add_argument("--bindings", type=Path)
    adapter_plan.add_argument("--endpoint", default="http://127.0.0.1:8188")

    review = commands.add_parser("review-ui")
    review.add_argument("manifest_root", type=Path)
    review.add_argument("--asset-root", type=Path, required=True)
    review.add_argument("--port", type=int, default=8765)

    variant_plan = commands.add_parser("variant-plan")
    variant_plan.add_argument("manifest_root", type=Path)
    variant_plan.add_argument("--source-candidate", required=True)
    variant_plan.add_argument("--matrix", type=Path, required=True)
    variant_plan.add_argument("--intent", choices=("evaluation", "production"), required=True)
    variant_check = commands.add_parser("variant-check")
    variant_check.add_argument("variant_set", type=Path)
    _root_args(variant_check, "manifest_root")

    export = commands.add_parser("variant-export")
    export.add_argument("variant_set", type=Path)
    _root_args(export, "manifest_root", "source_root", "output_root")
    export.add_argument("--approval-root", type=Path)
    export.add_argument("--write", action="store_true")
    export_check = commands.add_parser("export-check")
    export_check.add_argument("package_manifest", type=Path)
    _root_args(export_check, "output_root")

    paper = commands.add_parser("paper-plan")
    paper.add_argument("cue_sheet", type=Path)
    _root_args(paper, "package_root")
    paper.add_argument("--write", type=Path)
    paper_check = commands.add_parser("paper-check")
    paper_check.add_argument("scene_plan", type=Path)
    _root_args(paper_check, "package_root")

    preview = commands.add_parser("preview-plan")
    preview.add_argument("scene_plan", type=Path)
    _root_args(preview, "package_root", "output_root")
    preview.add_argument("--width", type=int, required=True)
    preview.add_argument("--height", type=int, required=True)
    preview.add_argument("--write", action="store_true")
    preview_check = commands.add_parser("preview-check")
    preview_check.add_argument("preview_manifest", type=Path)
    _root_args(preview_check, "output_root", "package_root")

    audio = commands.add_parser("audio-preview-plan")
    audio.add_argument("preview_manifest", type=Path)
    _root_args(audio, "preview_root", "package_root", "audio_root", "output_root")
    audio.add_argument("--audio", required=True)
    audio.add_argument("--offset-ms", type=int, required=True)
    audio.add_argument("--duration-policy", choices=("exact", "audio-at-least-scene", "scene-at-least-audio"), required=True)
    audio.add_argument("--audio-license-status", choices=("unreviewed", "reviewing", "approved", "rejected"), required=True)
    audio.add_argument("--write", action="store_true")
    audio_check = commands.add_parser("audio-preview-check")
    audio_check.add_argument("audio_preview_manifest", type=Path)
    _root_args(audio_check, "output_root", "preview_root", "package_root", "audio_root")

    render = commands.add_parser("render-plan")
    render.add_argument("audio_preview_manifest", type=Path)
    _root_args(render, "audio_preview_root", "preview_root", "package_root", "audio_root", "output_root")
    render.add_argument("--fps-num", type=int, required=True)
    render.add_argument("--fps-den", type=int, required=True)
    render.add_argument("--write", action="store_true")
    render_check = commands.add_parser("render-plan-check")
    render_check.add_argument("render_plan_manifest", type=Path)
    _root_args(render_check, "output_root", "audio_preview_root", "preview_root", "package_root", "audio_root")

    composition = commands.add_parser("composition-job")
    composition.add_argument("render_plan_manifest", type=Path)
    composition.add_argument("composition_profile", type=Path)
    _root_args(composition, "render_plan_root", "audio_preview_root", "preview_root", "package_root", "audio_root", "output_root")
    composition.add_argument("--write", action="store_true")
    composition_check = commands.add_parser("composition-job-check")
    composition_check.add_argument("renderer_job_manifest", type=Path)
    _root_args(composition_check, "output_root", "render_plan_root", "audio_preview_root", "preview_root", "package_root", "audio_root")

    frame = commands.add_parser("frame-render")
    frame.add_argument("renderer_job_manifest", type=Path)
    _root_args(frame, "renderer_job_root", "render_plan_root", "audio_preview_root", "preview_root", "package_root", "audio_root", "output_root")
    frame.add_argument("--write", action="store_true")
    frame_check = commands.add_parser("frame-render-check")
    frame_check.add_argument("frame_render_manifest", type=Path)
    _root_args(frame_check, "output_root", "renderer_job_root", "render_plan_root", "audio_preview_root", "preview_root", "package_root", "audio_root")
    return parser


def _emit(payload: dict[str, object], summary: str, ok: bool) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    print(summary, file=sys.stderr)
    return 0 if ok else 1


def _error(exc: Exception, label: str) -> int:
    if hasattr(exc, "to_dict") and hasattr(exc, "code"):
        return _emit({"ok": False, "diagnostics": [exc.to_dict()]}, f"{label} failed: {exc.code}", False)
    return _emit({"ok": False, "diagnostics": [{"code": f"{label.upper().replace('-', '_')}_ERROR", "message": str(exc), "field": ""}]}, f"{label} failed", False)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command
    try:
        if command == "validate":
            diagnostics = validate_path(args.path)
            return _emit({"ok": not diagnostics, "diagnostic_count": len(diagnostics), "diagnostics": [item.to_dict() for item in diagnostics]}, "validation succeeded" if not diagnostics else f"validation failed with {len(diagnostics)} diagnostic(s)", not diagnostics)

        if command in {"variant-plan", "variant-check"}:
            plan = plan_variant_set(args.manifest_root, args.source_candidate, load_variant_json(args.matrix), args.intent) if command == "variant-plan" else check_variant_set(args.variant_set, args.manifest_root)
            return _emit({"ok": True, "variant_set": plan}, f"validated {len(plan['variants'])} reviewed variant(s)", True)

        if command == "variant-export":
            result = build_export_package(args.variant_set, args.manifest_root, args.source_root, args.output_root, approval_root=args.approval_root, write=args.write)
            return _emit(result, f"{'materialized' if args.write else 'planned'} {len(result['package']['items'])} verified variant export(s)", True)
        if command == "export-check":
            result = check_export_package(args.package_manifest, args.output_root)
            return _emit(result, f"verified export package with {result['file_count']} file(s)", True)

        if command == "paper-plan":
            result = plan_scene(args.cue_sheet, args.package_root, write_path=args.write)
            return _emit(result, f"planned {len(result['scene_plan']['segments'])} paper-theater segment(s)", True)
        if command == "paper-check":
            result = check_scene_plan(args.scene_plan, args.package_root)
            return _emit(result, f"verified scene plan with {result['segment_count']} segment(s)", True)

        if command == "preview-plan":
            result = build_preview_package(args.scene_plan, args.package_root, args.output_root, width=args.width, height=args.height, write=args.write)
            return _emit(result, f"{'materialized' if args.write else 'planned'} offline preview with {result['file_count']} file(s)", True)
        if command == "preview-check":
            result = check_preview_package(args.preview_manifest, args.output_root, args.package_root)
            return _emit(result, f"verified offline preview with {result['segment_count']} segment(s)", True)

        if command == "audio-preview-plan":
            result = build_audio_preview_package(args.preview_manifest, args.preview_root, args.package_root, args.audio, args.audio_root, args.output_root, offset_ms=args.offset_ms, duration_policy=args.duration_policy, audio_license_status=args.audio_license_status, write=args.write)
            return _emit(result, f"{'materialized' if args.write else 'planned'} WAV-bound offline preview with {result['file_count']} file(s)", True)
        if command == "audio-preview-check":
            result = check_audio_preview_package(args.audio_preview_manifest, args.output_root, args.preview_root, args.package_root, args.audio_root)
            return _emit(result, f"verified WAV-bound offline preview with {result['segment_count']} segment(s)", True)

        if command == "render-plan":
            result = build_render_plan_package(args.audio_preview_manifest, args.audio_preview_root, args.preview_root, args.package_root, args.audio_root, args.output_root, fps_num=args.fps_num, fps_den=args.fps_den, write=args.write)
            return _emit(result, f"{'materialized' if args.write else 'planned'} renderer-neutral plan with {result['render_plan']['frame_count']} frame(s)", True)
        if command == "render-plan-check":
            result = check_render_plan_package(args.render_plan_manifest, args.output_root, args.audio_preview_root, args.preview_root, args.package_root, args.audio_root)
            return _emit(result, f"verified render plan with {result['frame_count']} frame(s)", True)

        if command == "composition-job":
            result = build_composition_job_package(args.render_plan_manifest, args.composition_profile, args.render_plan_root, args.audio_preview_root, args.preview_root, args.package_root, args.audio_root, args.output_root, write=args.write)
            return _emit(result, f"{'materialized' if args.write else 'planned'} composition-bound renderer job with {result['renderer_job']['span_count']} span(s)", True)
        if command == "composition-job-check":
            result = check_composition_job_package(args.renderer_job_manifest, args.output_root, args.render_plan_root, args.audio_preview_root, args.preview_root, args.package_root, args.audio_root)
            return _emit(result, f"verified composition-bound renderer job with {result['span_count']} span(s)", True)

        if command == "frame-render":
            result = build_frame_render_package(args.renderer_job_manifest, args.renderer_job_root, args.render_plan_root, args.audio_preview_root, args.preview_root, args.package_root, args.audio_root, args.output_root, write=args.write)
            return _emit(result, f"{'materialized' if args.write else 'planned'} deterministic RGBA frame package with {result['frame_render']['frame_count']} frame(s)", True)
        if command == "frame-render-check":
            result = check_frame_render_package(args.frame_render_manifest, args.output_root, args.renderer_job_root, args.render_plan_root, args.audio_preview_root, args.preview_root, args.package_root, args.audio_root)
            return _emit(result, f"verified deterministic RGBA frame package with {result['frame_count']} frame(s)", True)

        if command == "review-ui":
            run_review_ui(args.manifest_root, args.asset_root, args.port)
            return 0

        if command in {"adapter-check", "adapter-plan"}:
            adapter = ComfyUIAdapter()
            workflow = load_json_object(args.workflow)
            if command == "adapter-check":
                return _emit({"ok": True, "adapter_id": adapter.adapter_id, "workflow": adapter.check_workflow(workflow)}, "adapter workflow validation succeeded", True)
            bindings = load_json_object(args.bindings or args.workflow.with_name("bindings.json"))
            plan = adapter.plan(load_json_object(args.request), workflow, bindings, endpoint=args.endpoint)
            return _emit({"ok": True, "plan": plan.to_dict()}, "adapter dry-run plan created", True)

        catalog_path = args.path if command != "catalog-compat" else args.catalog
        profiles, diagnostics = load_catalog(catalog_path)
        if command == "catalog-validate":
            return _emit({"ok": not diagnostics, "profile_count": len(profiles), "diagnostic_count": len(diagnostics), "diagnostics": [item.to_dict() for item in diagnostics]}, "catalog validation succeeded" if not diagnostics else "catalog validation failed", not diagnostics)
        if diagnostics:
            return _emit({"ok": False, "diagnostic_count": len(diagnostics), "diagnostics": [item.to_dict() for item in diagnostics]}, "catalog validation failed", False)
        if command == "catalog-list":
            listing = catalog_listing(profiles)
            return _emit({"ok": True, "profile_count": len(listing), "profiles": listing}, f"listed {len(listing)} profile(s)", True)
        hardware = load_manifest(args.hardware).data
        hardware_diagnostics = validate_hardware_profile(args.hardware, hardware)
        if hardware_diagnostics:
            return _emit({"ok": False, "diagnostic_count": len(hardware_diagnostics), "diagnostics": [item.to_dict() for item in hardware_diagnostics]}, "hardware validation failed", False)
        results = [evaluate_compatibility(profile, hardware).to_dict() for profile in profiles]
        return _emit({"ok": True, "hardware_id": hardware["id"], "results": results}, f"evaluated {len(results)} profile(s)", True)

    except (VariantError, ExportError, PaperTheaterError, PreviewError, AudioPreviewError, RenderPlanError, CompositionError, FrameRenderError, AdapterError, ReviewUIError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return _error(exc, command)


if __name__ == "__main__":
    raise SystemExit(main())
