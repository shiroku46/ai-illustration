"""Bounded, resumable execution of deterministic local ComfyUI benchmark packages."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Sequence

from .adapters.base import AdapterError
from .adapters.comfyui import sanitize_loopback_endpoint
from .adapters.comfyui_execution_package import history_outputs
from .adapters.comfyui_http import ComfyUIHttpClient, HttpLimits
from .adapters.comfyui_png import decode_comfyui_png
from .benchmark_readiness import run_runtime_preflight
from .benchmark_results import validate_document as validate_results_document
from .benchmark_run_package import (
    PACKAGE_MANIFEST,
    build_package,
    validate_package,
)
from .model_benchmark import canonical_sha256
from .model_install_manifest import load_manifest
from .naming import canonical_json, safe_relative_path

RESULTS_FILE = "model-benchmark-results.v001.json"
JOURNAL_DIR = "journal"
IMAGE_DIR = "images"
JOURNAL_KIND = "benchmark-run-journal"
SCHEMA_VERSION = "1.0"
MAX_RUNS = 144
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_ERROR_CHARS = 2000
MAX_PROMPT_ID_CHARS = 128
DEFAULT_QUEUE_RESPONSE_BYTES = 1024 * 1024
DEFAULT_HISTORY_RESPONSE_BYTES = 8 * 1024 * 1024
DEFAULT_PNG_BYTES = 128 * 1024 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60
DEFAULT_RUN_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
ERROR_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class BenchmarkExecutionError(ValueError):
    def __init__(self, code: str, message: str, field: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _diag(code: str, message: str, field: str = "") -> dict[str, str]:
    return {"code": code, "message": message, "field": field}


def _sorted(values: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    unique = {
        (item.get("field", ""), item.get("code", ""), item.get("message", "")): {
            "code": item.get("code", ""),
            "message": item.get("message", ""),
            "field": item.get("field", ""),
        }
        for item in values
    }
    return [unique[key] for key in sorted(unique)]


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, field: str, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("must be a regular non-symlink file")
        size = path.stat().st_size
        if size <= 0 or size > maximum:
            raise ValueError(f"size must be 1..{maximum} bytes")
        payload = path.read_bytes()
        if len(payload) != size:
            raise ValueError("file changed while being read")
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BenchmarkExecutionError("json-read", str(exc), field) from exc
    if not isinstance(value, dict):
        raise BenchmarkExecutionError("json-object", "JSON root must be an object", field)
    return value


def _resolve_directory(path: Path, field: str, *, create: bool = False) -> Path:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        raise BenchmarkExecutionError("root-symlink", "directory must not be a symlink", field)
    if create and not lexical.exists():
        lexical.mkdir(parents=True, exist_ok=True)
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise BenchmarkExecutionError("root-missing", str(exc), field) from exc
    if not resolved.is_dir() or resolved.is_symlink():
        raise BenchmarkExecutionError("root-type", "must be a regular directory", field)
    return resolved


def _has_symlink(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop:
            return False
        if current.parent == current:
            return True
        current = current.parent


def _contained_file(root: Path, relative: str, field: str) -> Path:
    try:
        safe = safe_relative_path(relative)
        lexical = root.joinpath(*safe.parts)
        if _has_symlink(lexical, root):
            raise ValueError("path contains a symlink")
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise BenchmarkExecutionError("file-location", str(exc), field) from exc
    if resolved.is_symlink() or not resolved.is_file():
        raise BenchmarkExecutionError("file-type", "must be a regular non-symlink file", field)
    return resolved


def _atomic_write(path: Path, payload: bytes, *, replace: bool) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise BenchmarkExecutionError("write-symlink", "output directory must not be a symlink", str(parent))
    if path.is_symlink():
        raise BenchmarkExecutionError("write-symlink", "output path must not be a symlink", str(path))
    if path.exists() and not replace:
        if path.is_file() and path.read_bytes() == payload:
            return
        raise BenchmarkExecutionError("write-conflict", "refusing to overwrite different bytes", str(path))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = Path(temporary_name)
        if temporary.is_symlink() or not temporary.is_file():
            raise BenchmarkExecutionError("write-temp", "temporary output is invalid", str(temporary))
        os.replace(temporary, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _safe_error_code(value: Any) -> str:
    raw = str(value or "execution-error").strip().lower().replace("_", "-")
    raw = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")
    raw = re.sub(r"-+", "-", raw)
    if not raw:
        raw = "execution-error"
    if not ERROR_CODE_RE.fullmatch(raw):
        raw = "execution-error"
    return raw[:120].rstrip("-") or "execution-error"


def _safe_error_message(value: Any) -> str:
    text = " ".join(str(value or "execution failed").replace("\x00", " ").split())
    if not text:
        text = "execution failed"
    return text[:MAX_ERROR_CHARS]


def _prompt_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_PROMPT_ID_CHARS
        or any(not (ch.isalnum() or ch in "_-") for ch in value)
    ):
        raise BenchmarkExecutionError("prompt-id", "invalid prompt ID", "prompt_id")
    return value


def _save_nodes(workflow: dict[str, Any]) -> list[str]:
    nodes = sorted(
        node_id
        for node_id, node in workflow.items()
        if isinstance(node_id, str)
        and isinstance(node, dict)
        and node.get("class_type") == "SaveImage"
    )
    if len(nodes) != 1:
        raise BenchmarkExecutionError(
            "output-node-count",
            "benchmark workflow must contain exactly one SaveImage node",
            "workflow",
        )
    return nodes


def _package_context(
    package_root: Path,
    plan_path: Path,
    install_manifest_path: Path,
    workspace_root: Path,
) -> tuple[Path, dict[str, Any], dict[str, Any], dict[str, Any], str]:
    workspace = _resolve_directory(workspace_root, "workspace_root")
    package = _resolve_directory(package_root, "package_root")
    plan = _load_json(plan_path.resolve(strict=True), "plan")
    try:
        install = load_manifest(install_manifest_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise BenchmarkExecutionError("install-manifest", str(exc), "install_manifest") from exc
    diagnostics = validate_package(package, plan, install, workspace_root=workspace)
    if diagnostics:
        raise BenchmarkExecutionError(
            "package-invalid",
            json.dumps(_sorted(diagnostics), ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            "package_root",
        )
    expected_manifest, _expected_files = build_package(plan, install, workspace_root=workspace)
    package_manifest_path = _contained_file(package, PACKAGE_MANIFEST, "package_manifest")
    package_sha = _sha256(package_manifest_path.read_bytes())
    return package, plan, install, expected_manifest, package_sha


def _result_id(plan: dict[str, Any], package_manifest: dict[str, Any]) -> str:
    identity = {
        "plan_ref": plan["id"],
        "plan_version": plan["version"],
        "plan_sha256": canonical_sha256(plan),
        "package_id": package_manifest["id"],
    }
    return f"benchmark-results-{hashlib.sha256(canonical_json(identity)).hexdigest()[:20]}"


def _results_document(
    plan: dict[str, Any],
    package_manifest: dict[str, Any],
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    document = {
        "kind": "model-benchmark-results",
        "schema_version": "1.0",
        "id": _result_id(plan, package_manifest),
        "version": "v001",
        "plan_ref": plan["id"],
        "plan_version": plan["version"],
        "plan_sha256": canonical_sha256(plan),
        "results": entries,
        "notes": "Local deterministic benchmark execution journal; no aesthetic ranking or automatic selection.",
    }
    diagnostics = validate_results_document(document)
    if diagnostics:
        raise BenchmarkExecutionError(
            "results-invalid",
            json.dumps(diagnostics, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            "results",
        )
    return document


def _journal_path(results_root: Path, run_id: str) -> Path:
    return results_root / JOURNAL_DIR / f"{run_id}.json"


def _image_path(results_root: Path, run_id: str) -> Path:
    return results_root / IMAGE_DIR / f"{run_id}.png"


def _journal_document(
    *,
    package_manifest: dict[str, Any],
    package_sha: str,
    run: dict[str, Any],
    prompt_id: str | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": JOURNAL_KIND,
        "schema_version": SCHEMA_VERSION,
        "package_id": package_manifest["id"],
        "package_manifest_sha256": package_sha,
        "run_id": run["run_id"],
        "run_workflow_path": run["workflow_path"],
        "run_workflow_sha256": run["workflow_sha256"],
        "prompt_id": prompt_id,
        "result": result,
    }


def _verify_journal(
    path: Path,
    *,
    results_root: Path,
    plan: dict[str, Any],
    package_manifest: dict[str, Any],
    package_sha: str,
    run: dict[str, Any],
) -> dict[str, Any]:
    journal = _load_json(path, f"journal.{run['run_id']}")
    expected_keys = {
        "kind",
        "schema_version",
        "package_id",
        "package_manifest_sha256",
        "run_id",
        "run_workflow_path",
        "run_workflow_sha256",
        "prompt_id",
        "result",
    }
    if set(journal) != expected_keys:
        raise BenchmarkExecutionError("journal-schema", "journal fields differ from the exact contract", str(path))
    expected = {
        "kind": JOURNAL_KIND,
        "schema_version": SCHEMA_VERSION,
        "package_id": package_manifest["id"],
        "package_manifest_sha256": package_sha,
        "run_id": run["run_id"],
        "run_workflow_path": run["workflow_path"],
        "run_workflow_sha256": run["workflow_sha256"],
    }
    for key, value in expected.items():
        if journal.get(key) != value:
            raise BenchmarkExecutionError("journal-binding", f"journal {key} changed", str(path))
    prompt = journal.get("prompt_id")
    if prompt is not None:
        _prompt_id(prompt)
    result = journal.get("result")
    if not isinstance(result, dict):
        raise BenchmarkExecutionError("journal-result", "journal result must be an object", str(path))
    probe = _results_document(plan, package_manifest, [result])
    _ = probe
    expected_common = {
        "run_id": run["run_id"],
        "model_family": run["model_family"],
        "model_profile_ref": run["model_profile_ref"],
        "model_profile_sha256": run["model_profile_sha256"],
        "workflow_sha256": run["template_workflow_sha256"],
        "seed": run["seed"],
        "prompt_case_id": run["prompt_case_id"],
        "role_scope": run["role_scope"],
        "settings": run["settings"],
    }
    for key, value in expected_common.items():
        if result.get(key) != value:
            raise BenchmarkExecutionError("journal-result-binding", f"result {key} changed", str(path))
    if result.get("state") == "succeeded":
        expected_relative = f"{IMAGE_DIR}/{run['run_id']}.png"
        if result.get("image_path") != expected_relative:
            raise BenchmarkExecutionError("image-binding", "result image path changed", str(path))
        image = _contained_file(results_root, expected_relative, f"image.{run['run_id']}")
        payload = image.read_bytes()
        if _sha256(payload) != result.get("image_sha256"):
            raise BenchmarkExecutionError("image-sha256", "stored image checksum changed", str(image))
        exact = run["exact_comfyui_settings"]
        decoded = decode_comfyui_png(
            payload,
            expected_width=int(exact["width"]),
            expected_height=int(exact["height"]),
        )
        if result.get("width") != decoded.width or result.get("height") != decoded.height:
            raise BenchmarkExecutionError("image-dimensions", "stored image dimensions changed", str(image))
    elif result.get("state") != "failed":
        raise BenchmarkExecutionError("journal-state", "journal state must be succeeded or failed", str(path))
    return result


def _journal_entries(
    *,
    results_root: Path,
    plan: dict[str, Any],
    package_manifest: dict[str, Any],
    package_sha: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    by_run: dict[str, dict[str, Any]] = {}
    expected_ids = {run["run_id"] for run in package_manifest["runs"]}
    journal_root = results_root / JOURNAL_DIR
    if journal_root.exists():
        if journal_root.is_symlink() or not journal_root.is_dir():
            raise BenchmarkExecutionError("journal-root", "journal root must be a regular directory", str(journal_root))
        extra = sorted(
            path.name
            for path in journal_root.glob("*.json")
            if path.stem not in expected_ids
        )
        if extra:
            raise BenchmarkExecutionError("journal-extra", f"unexpected journal files: {extra}", str(journal_root))
    for run in package_manifest["runs"]:
        path = _journal_path(results_root, run["run_id"])
        if not path.exists():
            continue
        result = _verify_journal(
            path,
            results_root=results_root,
            plan=plan,
            package_manifest=package_manifest,
            package_sha=package_sha,
            run=run,
        )
        entries.append(result)
        by_run[run["run_id"]] = result
    return entries, by_run


def _write_results(
    results_root: Path,
    plan: dict[str, Any],
    package_manifest: dict[str, Any],
    entries: list[dict[str, Any]],
) -> None:
    if not entries:
        return
    document = _results_document(plan, package_manifest, entries)
    _atomic_write(results_root / RESULTS_FILE, _json_bytes(document), replace=True)


def status(
    package_root: Path,
    plan_path: Path,
    install_manifest_path: Path,
    workspace_root: Path,
    results_root: Path,
) -> dict[str, Any]:
    package, plan, _install, package_manifest, package_sha = _package_context(
        package_root,
        plan_path,
        install_manifest_path,
        workspace_root,
    )
    _ = package
    results = _resolve_directory(results_root, "results_root", create=True)
    entries, by_run = _journal_entries(
        results_root=results,
        plan=plan,
        package_manifest=package_manifest,
        package_sha=package_sha,
    )
    succeeded = sum(1 for entry in entries if entry["state"] == "succeeded")
    failed = sum(1 for entry in entries if entry["state"] == "failed")
    pending_runs = [run for run in package_manifest["runs"] if run["run_id"] not in by_run]
    return {
        "ok": True,
        "package_id": package_manifest["id"],
        "run_count": len(package_manifest["runs"]),
        "succeeded": succeeded,
        "failed": failed,
        "pending": len(pending_runs),
        "complete": not pending_runs,
        "next_run_id": pending_runs[0]["run_id"] if pending_runs else None,
        "network_contacted": False,
        "prompt_queued": False,
        "automatic_ranking": False,
        "automatic_selection": False,
    }


def _failure_result(run: dict[str, Any], elapsed_ms: int, exc: Exception) -> dict[str, Any]:
    code = _safe_error_code(getattr(exc, "code", exc.__class__.__name__))
    message = _safe_error_message(getattr(exc, "message", str(exc)))
    return {
        "run_id": run["run_id"],
        "state": "failed",
        "model_family": run["model_family"],
        "model_profile_ref": run["model_profile_ref"],
        "model_profile_sha256": run["model_profile_sha256"],
        "workflow_sha256": run["template_workflow_sha256"],
        "seed": run["seed"],
        "prompt_case_id": run["prompt_case_id"],
        "role_scope": run["role_scope"],
        "settings": run["settings"],
        "elapsed_ms": max(0, elapsed_ms),
        "error": {"code": code, "message": message},
    }


def _success_result(
    run: dict[str, Any],
    elapsed_ms: int,
    image_payload: bytes,
    width: int,
    height: int,
) -> dict[str, Any]:
    return {
        "run_id": run["run_id"],
        "state": "succeeded",
        "model_family": run["model_family"],
        "model_profile_ref": run["model_profile_ref"],
        "model_profile_sha256": run["model_profile_sha256"],
        "workflow_sha256": run["template_workflow_sha256"],
        "seed": run["seed"],
        "prompt_case_id": run["prompt_case_id"],
        "role_scope": run["role_scope"],
        "settings": run["settings"],
        "elapsed_ms": max(0, elapsed_ms),
        "image_path": f"{IMAGE_DIR}/{run['run_id']}.png",
        "image_sha256": _sha256(image_payload),
        "width": width,
        "height": height,
    }


def _execute_one(
    *,
    package_root: Path,
    results_root: Path,
    package_manifest: dict[str, Any],
    package_sha: str,
    run: dict[str, Any],
    client: Any,
    run_timeout_seconds: float,
    poll_interval_seconds: float,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    workflow_path = _contained_file(package_root, run["workflow_path"], f"workflow.{run['run_id']}")
    payload = workflow_path.read_bytes()
    if _sha256(payload) != run["workflow_sha256"]:
        raise BenchmarkExecutionError("workflow-sha256", "run workflow checksum changed", str(workflow_path))
    try:
        workflow = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkExecutionError("workflow-json", str(exc), str(workflow_path)) from exc
    if not isinstance(workflow, dict):
        raise BenchmarkExecutionError("workflow-object", "workflow root must be an object", str(workflow_path))
    output_nodes = _save_nodes(workflow)
    started = clock()
    deadline = started + run_timeout_seconds

    def remaining() -> float:
        value = deadline - clock()
        if value <= 0:
            raise BenchmarkExecutionError("run-timeout", "benchmark run exceeded its timeout", run["run_id"])
        return min(float(DEFAULT_REQUEST_TIMEOUT_SECONDS), value)

    prompt_id = _prompt_id(client.queue_prompt(workflow, timeout_seconds=remaining()))
    descriptors: list[dict[str, str]] | None = None
    while descriptors is None:
        history = client.history(prompt_id, timeout_seconds=remaining())
        try:
            descriptors = history_outputs(history, prompt_id, output_nodes, 1)
        except AdapterError as exc:
            raise BenchmarkExecutionError(
                _safe_error_code(exc.code),
                _safe_error_message(exc.message),
                exc.field,
            ) from exc
        if descriptors is None:
            delay = min(poll_interval_seconds, max(0.0, deadline - clock()))
            if delay <= 0:
                raise BenchmarkExecutionError("run-timeout", "benchmark run exceeded its timeout", run["run_id"])
            sleeper(delay)
    if len(descriptors) != 1:
        raise BenchmarkExecutionError("image-count", "benchmark run must produce exactly one image", run["run_id"])
    descriptor = descriptors[0]
    image_payload = client.image(
        descriptor["filename"],
        descriptor["subfolder"],
        timeout_seconds=remaining(),
    )
    exact = run["exact_comfyui_settings"]
    decoded = decode_comfyui_png(
        image_payload,
        expected_width=int(exact["width"]),
        expected_height=int(exact["height"]),
    )
    image_path = _image_path(results_root, run["run_id"])
    _atomic_write(image_path, image_payload, replace=False)
    elapsed_ms = int(max(0.0, clock() - started) * 1000)
    result = _success_result(run, elapsed_ms, image_payload, decoded.width, decoded.height)
    journal = _journal_document(
        package_manifest=package_manifest,
        package_sha=package_sha,
        run=run,
        prompt_id=prompt_id,
        result=result,
    )
    _atomic_write(_journal_path(results_root, run["run_id"]), _json_bytes(journal), replace=True)
    return result


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
    max_runs: int = MAX_RUNS,
    retry_failed: bool = False,
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    readiness_check: Callable[..., dict[str, Any]] = run_runtime_preflight,
    client_factory: Callable[..., Any] = ComfyUIHttpClient,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if execute is not True:
        raise BenchmarkExecutionError("execute-acknowledgement", "run requires explicit --execute", "execute")
    if not isinstance(max_runs, int) or isinstance(max_runs, bool) or not 1 <= max_runs <= MAX_RUNS:
        raise BenchmarkExecutionError("max-runs", f"max_runs must be 1..{MAX_RUNS}", "max_runs")
    if not isinstance(run_timeout_seconds, (int, float)) or isinstance(run_timeout_seconds, bool) or not 1 <= run_timeout_seconds <= 3600:
        raise BenchmarkExecutionError("run-timeout", "run timeout must be 1..3600 seconds", "run_timeout_seconds")
    if not isinstance(poll_interval_seconds, (int, float)) or isinstance(poll_interval_seconds, bool) or not 0.05 <= poll_interval_seconds <= 10:
        raise BenchmarkExecutionError("poll-interval", "poll interval must be 0.05..10 seconds", "poll_interval_seconds")
    sanitized = sanitize_loopback_endpoint(endpoint)
    package, plan, _install, package_manifest, package_sha = _package_context(
        package_root,
        plan_path,
        install_manifest_path,
        workspace_root,
    )
    results = _resolve_directory(results_root, "results_root", create=True)
    entries, by_run = _journal_entries(
        results_root=results,
        plan=plan,
        package_manifest=package_manifest,
        package_sha=package_sha,
    )
    candidate_runs = []
    for item in package_manifest["runs"]:
        existing = by_run.get(item["run_id"])
        if existing is None or (retry_failed and existing["state"] == "failed"):
            candidate_runs.append(item)
    if not candidate_runs:
        _write_results(results, plan, package_manifest, entries)
        snapshot = status(package, plan_path, install_manifest_path, workspace_root, results)
        snapshot.update({"attempted": 0, "network_contacted": False, "prompt_queued": False})
        return snapshot

    readiness = readiness_check(
        install_manifest_path,
        workspace_root=workspace_root,
        comfyui_root=comfyui_root,
        endpoint=sanitized,
    )
    if not isinstance(readiness, dict) or readiness.get("ready") is not True:
        diagnostics = readiness.get("diagnostics", []) if isinstance(readiness, dict) else []
        raise BenchmarkExecutionError(
            "readiness",
            json.dumps(diagnostics, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            "comfyui",
        )
    client = client_factory(
        sanitized,
        HttpLimits(
            queue_response_bytes=DEFAULT_QUEUE_RESPONSE_BYTES,
            history_response_bytes=DEFAULT_HISTORY_RESPONSE_BYTES,
            png_bytes=DEFAULT_PNG_BYTES,
            request_timeout_seconds=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        ),
    )
    attempted = 0
    queued = False
    for item in candidate_runs[:max_runs]:
        attempted += 1
        started = clock()
        old = by_run.get(item["run_id"])
        if old is not None and old["state"] == "failed" and retry_failed:
            old_journal = _journal_path(results, item["run_id"])
            if old_journal.exists() and old_journal.is_file() and not old_journal.is_symlink():
                old_journal.unlink()
        try:
            result = _execute_one(
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
            )
            queued = True
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            elapsed_ms = int(max(0.0, clock() - started) * 1000)
            result = _failure_result(item, elapsed_ms, exc)
            journal = _journal_document(
                package_manifest=package_manifest,
                package_sha=package_sha,
                run=item,
                prompt_id=None,
                result=result,
            )
            _atomic_write(_journal_path(results, item["run_id"]), _json_bytes(journal), replace=True)
        entries, by_run = _journal_entries(
            results_root=results,
            plan=plan,
            package_manifest=package_manifest,
            package_sha=package_sha,
        )
        _write_results(results, plan, package_manifest, entries)
    snapshot = status(package, plan_path, install_manifest_path, workspace_root, results)
    snapshot.update(
        {
            "attempted": attempted,
            "network_contacted": True,
            "prompt_queued": queued,
            "readiness_checked": True,
        }
    )
    return snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_illustration.benchmark_execute",
        description="Run or inspect the exact resumable local model benchmark",
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
            command.add_argument("--max-runs", type=int, default=MAX_RUNS)
            command.add_argument("--retry-failed", action="store_true")
            command.add_argument("--run-timeout-seconds", type=float, default=DEFAULT_RUN_TIMEOUT_SECONDS)
            command.add_argument("--poll-interval-ms", type=int, default=int(DEFAULT_POLL_INTERVAL_SECONDS * 1000))
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
                max_runs=args.max_runs,
                retry_failed=args.retry_failed,
                run_timeout_seconds=args.run_timeout_seconds,
                poll_interval_seconds=args.poll_interval_ms / 1000,
            )
    except (BenchmarkExecutionError, AdapterError, OSError, ValueError) as exc:
        result = {
            "ok": False,
            "diagnostics": [
                _diag(
                    _safe_error_code(getattr(exc, "code", exc.__class__.__name__)),
                    _safe_error_message(getattr(exc, "message", str(exc))),
                    str(getattr(exc, "field", "")),
                )
            ],
            "network_contacted": False,
            "prompt_queued": False,
            "automatic_ranking": False,
            "automatic_selection": False,
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
