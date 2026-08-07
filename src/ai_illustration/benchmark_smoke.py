"""Stratified first-batch execution for the deterministic model benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Callable, Sequence

from .adapters.base import AdapterError
from .adapters.comfyui import sanitize_loopback_endpoint
from .adapters.comfyui_http import ComfyUIHttpClient, HttpLimits
from .benchmark_readiness import run_runtime_preflight
from . import benchmark_execute as be

SMOKE_SEED = 101
SMOKE_PROMPT_CASE = "front-full-body-neutral"
EXPECTED_FAMILIES = 3


def select_smoke_runs(package_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    runs = package_manifest.get("runs")
    if not isinstance(runs, list):
        raise be.BenchmarkExecutionError(
            "smoke-package", "package runs must be a list", "package.runs"
        )
    matches: dict[str, dict[str, Any]] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("seed") != SMOKE_SEED or run.get("prompt_case_id") != SMOKE_PROMPT_CASE:
            continue
        family = run.get("model_family")
        if not isinstance(family, str) or not family:
            raise be.BenchmarkExecutionError(
                "smoke-family", "smoke run has no model family", "package.runs"
            )
        if family in matches:
            raise be.BenchmarkExecutionError(
                "smoke-duplicate", f"multiple baseline smoke runs for {family}", "package.runs"
            )
        matches[family] = run
    if len(matches) != EXPECTED_FAMILIES:
        raise be.BenchmarkExecutionError(
            "smoke-coverage",
            f"expected {EXPECTED_FAMILIES} model families at seed={SMOKE_SEED} case={SMOKE_PROMPT_CASE}; found {len(matches)}",
            "package.runs",
        )
    return [matches[family] for family in sorted(matches)]


def _smoke_snapshot(
    package_root: Path,
    plan_path: Path,
    install_manifest_path: Path,
    workspace_root: Path,
    results_root: Path,
) -> tuple[dict[str, Any], tuple[Any, ...]]:
    package, plan, install, package_manifest, package_sha = be._package_context(
        package_root,
        plan_path,
        install_manifest_path,
        workspace_root,
    )
    selected = select_smoke_runs(package_manifest)
    results = be._existing_directory(results_root, "results_root")
    if results is None:
        entries: list[dict[str, Any]] = []
        by_run: dict[str, dict[str, Any]] = {}
    else:
        entries, by_run = be._journal_entries(
            results_root=results,
            plan=plan,
            package_manifest=package_manifest,
            package_sha=package_sha,
        )
        be._verify_aggregate(results, plan, package_manifest, entries)
    states = []
    for run in selected:
        existing = by_run.get(run["run_id"])
        states.append(
            {
                "run_id": run["run_id"],
                "model_family": run["model_family"],
                "seed": run["seed"],
                "prompt_case_id": run["prompt_case_id"],
                "state": existing["state"] if existing is not None else "pending",
            }
        )
    smoke_succeeded = sum(item["state"] == "succeeded" for item in states)
    smoke_failed = sum(item["state"] == "failed" for item in states)
    smoke_pending = sum(item["state"] == "pending" for item in states)
    aggregate_succeeded = sum(entry["state"] == "succeeded" for entry in entries)
    aggregate_failed = sum(entry["state"] == "failed" for entry in entries)
    output = {
        "ok": True,
        "package_id": package_manifest["id"],
        "smoke_seed": SMOKE_SEED,
        "smoke_prompt_case_id": SMOKE_PROMPT_CASE,
        "smoke_run_count": len(selected),
        "smoke_succeeded": smoke_succeeded,
        "smoke_failed": smoke_failed,
        "smoke_pending": smoke_pending,
        "smoke_complete": smoke_pending == 0,
        "smoke_runs": states,
        "run_count": len(package_manifest["runs"]),
        "succeeded": aggregate_succeeded,
        "failed": aggregate_failed,
        "pending": len(package_manifest["runs"]) - aggregate_succeeded - aggregate_failed,
        "network_contacted": False,
        "prompt_queued": False,
        "automatic_ranking": False,
        "automatic_selection": False,
    }
    context = (
        package,
        plan,
        install,
        package_manifest,
        package_sha,
        results,
        entries,
        by_run,
        selected,
    )
    return output, context


def status(
    package_root: Path,
    plan_path: Path,
    install_manifest_path: Path,
    workspace_root: Path,
    results_root: Path,
) -> dict[str, Any]:
    output, _context = _smoke_snapshot(
        package_root,
        plan_path,
        install_manifest_path,
        workspace_root,
        results_root,
    )
    return output


def run(
    package_root: Path,
    plan_path: Path,
    install_manifest_path: Path,
    workspace_root: Path,
    results_root: Path,
    comfyui_root: Path,
    *,
    endpoint: str,
    execute: bool,
    retry_failed: bool = False,
    run_timeout_seconds: float = be.DEFAULT_RUN_TIMEOUT_SECONDS,
    poll_interval_seconds: float = be.DEFAULT_POLL_INTERVAL_SECONDS,
    readiness_check: Callable[..., dict[str, Any]] = run_runtime_preflight,
    client_factory: Callable[..., Any] = ComfyUIHttpClient,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if execute is not True:
        raise be.BenchmarkExecutionError(
            "execute-acknowledgement", "smoke run requires explicit --execute", "execute"
        )
    if (
        not isinstance(run_timeout_seconds, (int, float))
        or isinstance(run_timeout_seconds, bool)
        or not 1 <= run_timeout_seconds <= 3600
    ):
        raise be.BenchmarkExecutionError(
            "run-timeout", "run timeout must be 1..3600 seconds", "run_timeout_seconds"
        )
    if (
        not isinstance(poll_interval_seconds, (int, float))
        or isinstance(poll_interval_seconds, bool)
        or not 0.05 <= poll_interval_seconds <= 10
    ):
        raise be.BenchmarkExecutionError(
            "poll-interval", "poll interval must be 0.05..10 seconds", "poll_interval_seconds"
        )

    sanitized = sanitize_loopback_endpoint(endpoint)
    _snapshot, context = _smoke_snapshot(
        package_root,
        plan_path,
        install_manifest_path,
        workspace_root,
        results_root,
    )
    (
        package,
        plan,
        _install,
        package_manifest,
        package_sha,
        existing_results,
        entries,
        by_run,
        selected,
    ) = context
    results = existing_results or be._resolve_directory(
        results_root, "results_root", create=True
    )
    candidates = [
        item
        for item in selected
        if item["run_id"] not in by_run
        or (retry_failed and by_run[item["run_id"]]["state"] == "failed")
    ]
    if not candidates:
        be._write_results(results, plan, package_manifest, entries)
        output = status(
            package,
            plan_path,
            install_manifest_path,
            workspace_root,
            results,
        )
        output.update(
            {
                "attempted": 0,
                "network_contacted": False,
                "prompt_queued": False,
            }
        )
        return output

    readiness = readiness_check(
        install_manifest_path,
        workspace_root=workspace_root,
        comfyui_root=comfyui_root,
        endpoint=sanitized,
    )
    if not isinstance(readiness, dict) or readiness.get("ready") is not True:
        diagnostics = readiness.get("diagnostics", []) if isinstance(readiness, dict) else []
        raise be.BenchmarkExecutionError(
            "readiness",
            json.dumps(diagnostics, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            "comfyui",
        )

    raw_client = client_factory(
        sanitized,
        HttpLimits(
            queue_response_bytes=be.DEFAULT_QUEUE_RESPONSE_BYTES,
            history_response_bytes=be.DEFAULT_HISTORY_RESPONSE_BYTES,
            png_bytes=be.DEFAULT_PNG_BYTES,
            request_timeout_seconds=be.DEFAULT_REQUEST_TIMEOUT_SECONDS,
        ),
    )
    client = be._TrackingClient(raw_client)
    attempted = 0
    for item in candidates:
        attempted += 1
        started = clock()
        attempt_state: dict[str, Any] = {}
        try:
            be._execute_one(
                package_root=package,
                results_root=results,
                package_manifest=package_manifest,
                package_sha=package_sha,
                run=item,
                client=client,
                run_timeout_seconds=float(run_timeout_seconds),
                poll_interval_seconds=float(poll_interval_seconds),
                clock=clock,
                sleeper=sleeper,
                attempt_state=attempt_state,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            elapsed_ms = int(max(0.0, clock() - started) * 1000)
            result = be._failure_result(item, elapsed_ms, exc)
            journal = be._journal_document(
                package_manifest=package_manifest,
                package_sha=package_sha,
                run=item,
                prompt_id=attempt_state.get("prompt_id"),
                result=result,
            )
            be._atomic_write(
                be._journal_path(results, item["run_id"]),
                be._json_bytes(journal),
                replace=True,
            )
        entries, by_run = be._journal_entries(
            results_root=results,
            plan=plan,
            package_manifest=package_manifest,
            package_sha=package_sha,
        )
        be._write_results(results, plan, package_manifest, entries)

    output = status(
        package,
        plan_path,
        install_manifest_path,
        workspace_root,
        results,
    )
    output.update(
        {
            "attempted": attempted,
            "network_contacted": True,
            "prompt_queued": client.prompt_queued,
            "readiness_checked": True,
        }
    )
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_illustration.benchmark_smoke",
        description="Run one identical baseline benchmark case per model family",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "run"):
        command = sub.add_parser(name)
        command.add_argument("package_root", type=Path)
        command.add_argument("plan", type=Path)
        command.add_argument("install_manifest", type=Path)
        command.add_argument("--workspace-root", type=Path, required=True)
        command.add_argument("--results-root", type=Path, required=True)
        if name == "run":
            command.add_argument("--comfyui-root", type=Path, required=True)
            command.add_argument("--endpoint", default="http://127.0.0.1:8188")
            command.add_argument("--execute", action="store_true")
            command.add_argument("--retry-failed", action="store_true")
            command.add_argument(
                "--run-timeout-seconds",
                type=float,
                default=be.DEFAULT_RUN_TIMEOUT_SECONDS,
            )
            command.add_argument(
                "--poll-interval-ms",
                type=int,
                default=int(be.DEFAULT_POLL_INTERVAL_SECONDS * 1000),
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            result = status(
                args.package_root,
                args.plan,
                args.install_manifest,
                args.workspace_root,
                args.results_root,
            )
        else:
            result = run(
                args.package_root,
                args.plan,
                args.install_manifest,
                args.workspace_root,
                args.results_root,
                args.comfyui_root,
                endpoint=args.endpoint,
                execute=args.execute,
                retry_failed=args.retry_failed,
                run_timeout_seconds=args.run_timeout_seconds,
                poll_interval_seconds=args.poll_interval_ms / 1000,
            )
    except (be.BenchmarkExecutionError, AdapterError, OSError, ValueError) as exc:
        result = {
            "ok": False,
            "diagnostics": [
                {
                    "code": be._safe_error_code(
                        getattr(exc, "code", exc.__class__.__name__)
                    ),
                    "message": be._safe_error_message(
                        getattr(exc, "message", str(exc))
                    ),
                    "field": str(getattr(exc, "field", "")),
                }
            ],
            "network_contacted": False,
            "prompt_queued": False,
            "automatic_ranking": False,
            "automatic_selection": False,
        }
    sys.stdout.write(
        json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
