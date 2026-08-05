"""Dedicated CLI for deterministic local ComfyUI smoke-test preparation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any, Sequence

from .adapters.base import AdapterError
from .comfyui_smoke_common import (
    BINDINGS_FILE, EXECUTION_FILE, MANIFEST_FILE, MODEL_FILE, PROFILE_STATES, REQUEST_FILE,
    TOOL_FILE, WORKFLOW_FILE, SmokeError, _json_bytes, inspect_workflow,
)
from .comfyui_smoke_bundle import prepare_bundle
from .comfyui_smoke_check import check_bundle


def _inspection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sampler-node")
    parser.add_argument("--checkpoint-node")
    parser.add_argument("--size-node")
    parser.add_argument("--positive-node")
    parser.add_argument("--negative-node")
    parser.add_argument("--output-node", action="append", default=[])
    parser.add_argument("--seed", type=int)
    parser.add_argument("--steps", type=int)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--positive-prompt")
    parser.add_argument("--negative-prompt")


def _inspection_options(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "sampler_node": args.sampler_node,
        "checkpoint_node": args.checkpoint_node,
        "size_node": args.size_node,
        "positive_node": args.positive_node,
        "negative_node": args.negative_node,
        "output_nodes": args.output_node,
        "seed": args.seed,
        "steps": args.steps,
        "width": args.width,
        "height": args.height,
        "positive_prompt": args.positive_prompt,
        "negative_prompt": args.negative_prompt,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ai_illustration.comfyui_smoke")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("workflow", type=Path)
    _inspection_arguments(inspect_parser)

    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("workflow", type=Path)
    prepare_parser.add_argument("--output-root", type=Path, required=True)
    prepare_parser.add_argument("--profile-state", choices=sorted(PROFILE_STATES), default="reviewing")
    prepare_parser.add_argument("--review-date", required=True)
    prepare_parser.add_argument("--tool-evidence-url")
    prepare_parser.add_argument("--model-evidence-url")
    prepare_parser.add_argument("--confirm-tool-license", action="store_true")
    prepare_parser.add_argument("--confirm-model-license", action="store_true")
    prepare_parser.add_argument("--confirm-commercial-use", action="store_true")
    prepare_parser.add_argument("--tool-id", default="comfyui-local-api")
    prepare_parser.add_argument("--model-id")
    prepare_parser.add_argument("--request-id")
    prepare_parser.add_argument("--endpoint", default="http://127.0.0.1:8188")
    prepare_parser.add_argument("--minimum-vram-gb", type=int, default=0)
    prepare_parser.add_argument("--minimum-ram-gb", type=int, default=0)
    prepare_parser.add_argument("--write", action="store_true")
    _inspection_arguments(prepare_parser)

    check_parser = sub.add_parser("check")
    check_parser.add_argument("manifest", type=Path)
    check_parser.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_workflow(args.workflow, **_inspection_options(args))
        elif args.command == "prepare":
            result = prepare_bundle(
                args.workflow,
                args.output_root,
                profile_state=args.profile_state,
                review_date=args.review_date,
                tool_evidence_url=args.tool_evidence_url,
                model_evidence_url=args.model_evidence_url,
                confirm_tool_license=args.confirm_tool_license,
                confirm_model_license=args.confirm_model_license,
                confirm_commercial_use=args.confirm_commercial_use,
                tool_id=args.tool_id,
                model_id=args.model_id,
                request_id=args.request_id,
                endpoint=args.endpoint,
                minimum_vram_gb=args.minimum_vram_gb,
                minimum_ram_gb=args.minimum_ram_gb,
                write=args.write,
                **_inspection_options(args),
            )
        else:
            result = check_bundle(args.manifest, args.output_root)
    except (SmokeError, AdapterError) as exc:
        code = getattr(exc, "code", exc.__class__.__name__.upper())
        message = getattr(exc, "message", str(exc))
        field = getattr(exc, "field", "")
        result = {
            "ok": False,
            "diagnostics": [{"code": str(code), "message": str(message), "field": str(field)}],
            "network_contacted": False,
            "external_process_started": False,
        }
    sys.stdout.buffer.write(_json_bytes(result))
    print(
        f"ComfyUI smoke {args.command}: {'ok' if result.get('ok') else 'failed'}",
        file=sys.stderr,
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
