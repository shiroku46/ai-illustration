"""Validate identity-lock results and render deterministic owner-review sheets."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Iterable, Sequence

from .benchmark_results import BenchmarkResultsError, _parse_png
from .identity_lock import expand_matrix, load_plan, plan_sha256, validate_plan
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, canonical_json, content_identifier, safe_relative_path

KIND = "identity-lock-results"
SCHEMA_VERSION = "1.0"
STATES = frozenset({"succeeded", "failed"})
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_ERROR_MESSAGE = 2000
PACKAGE_MANIFEST = "identity-consistency-manifest.json"
SHEETS_DIR = "consistency-sheets"
PROFILE_REF_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")
FORBIDDEN_KEYS = frozenset(
    {
        "identity_score",
        "aesthetic_score",
        "score",
        "rank",
        "ranking",
        "winner",
        "recommendation",
        "recommended",
        "approval",
        "approved",
        "similarity_threshold",
        "automatic_approval",
        "automatic_promotion",
        "variant_promotion",
        "selected_strategy",
    }
)

TOP_FIELDS = frozenset(
    {"kind", "schema_version", "id", "version", "plan_ref", "plan_version", "plan_sha256", "results", "notes"}
)
TOP_REQUIRED = TOP_FIELDS - {"notes"}
COMMON_FIELDS = frozenset(
    {
        "run_id",
        "state",
        "model_family",
        "model_profile_ref",
        "model_profile_sha256",
        "workflow_sha256",
        "role",
        "candidate_id",
        "request_id",
        "identity_sha256",
        "strategy_id",
        "strategy_type",
        "pose",
        "expression",
        "control_sha256",
        "elapsed_ms",
        "peak_vram_mib",
    }
)
COMMON_REQUIRED = COMMON_FIELDS - {"peak_vram_mib"}
SUCCESS_FIELDS = COMMON_FIELDS | {"image_path", "image_sha256", "width", "height"}
SUCCESS_REQUIRED = COMMON_REQUIRED | {"image_path", "image_sha256", "width", "height"}
FAILURE_FIELDS = COMMON_FIELDS | {"error"}
FAILURE_REQUIRED = COMMON_REQUIRED | {"error"}
ERROR_FIELDS = frozenset({"code", "message"})


class IdentityLockResultsError(ValueError):
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


def document_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def results_sha256(results: dict[str, Any]) -> str:
    return hashlib.sha256(document_bytes(results)).hexdigest()


def _check_fields(value: Any, required: frozenset[str], allowed: frozenset[str], field: str) -> list[dict[str, str]]:
    if not isinstance(value, dict):
        return [_diag("OBJECT_REQUIRED", "must be an object", field)]
    diagnostics: list[dict[str, str]] = []
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - allowed)
    if missing:
        diagnostics.append(_diag("MISSING_FIELD", f"missing fields: {', '.join(missing)}", field))
    if unknown:
        diagnostics.append(_diag("UNKNOWN_FIELD", f"unknown fields: {', '.join(unknown)}", field))
    return diagnostics


def _token(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not TOKEN_RE.fullmatch(value):
        return [_diag("INVALID_TOKEN", "must be a lowercase ASCII token", field)]
    return []


def _checksum(value: Any, field: str, *, nullable: bool = False) -> list[dict[str, str]]:
    if nullable and value is None:
        return []
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        return [_diag("CHECKSUM", "must be 64 lowercase hexadecimal characters", field)]
    return []


def _nonnegative(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return [_diag("NONNEGATIVE_INTEGER", "must be a non-negative integer", field)]
    return []


def _positive(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > 8192:
        return [_diag("DIMENSION", "must be an integer from 1 to 8192", field)]
    return []


def _scan_forbidden(value: Any, field: str = "results") -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{field}.{key}"
            if key in FORBIDDEN_KEYS:
                diagnostics.append(_diag("AUTOMATIC_SELECTION_FORBIDDEN", f"field is forbidden: {key}", current))
            diagnostics.extend(_scan_forbidden(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            diagnostics.extend(_scan_forbidden(item, f"{field}[{index}]"))
    return diagnostics


def _validate_entry(entry: Any, index: int) -> list[dict[str, str]]:
    field = f"results[{index}]"
    if not isinstance(entry, dict):
        return [_diag("OBJECT_REQUIRED", "result entry must be an object", field)]
    state = entry.get("state")
    required = SUCCESS_REQUIRED if state == "succeeded" else FAILURE_REQUIRED if state == "failed" else COMMON_REQUIRED
    allowed = SUCCESS_FIELDS if state == "succeeded" else FAILURE_FIELDS if state == "failed" else COMMON_FIELDS
    diagnostics = _check_fields(entry, required, allowed, field)
    if state not in STATES:
        diagnostics.append(_diag("EXECUTION_STATE", "state must be succeeded or failed", f"{field}.state"))
    for name in ("run_id", "model_family", "candidate_id", "request_id", "strategy_id", "strategy_type", "pose", "expression"):
        diagnostics.extend(_token(entry.get(name), f"{field}.{name}"))
    profile_ref = entry.get("model_profile_ref")
    if not isinstance(profile_ref, str) or not PROFILE_REF_RE.fullmatch(profile_ref):
        diagnostics.append(_diag("MODEL_REFERENCE", "must use id@vNNN", f"{field}.model_profile_ref"))
    for name in ("model_profile_sha256", "workflow_sha256", "identity_sha256"):
        diagnostics.extend(_checksum(entry.get(name), f"{field}.{name}"))
    diagnostics.extend(_checksum(entry.get("control_sha256"), f"{field}.control_sha256", nullable=True))
    diagnostics.extend(_nonnegative(entry.get("elapsed_ms"), f"{field}.elapsed_ms"))
    if "peak_vram_mib" in entry:
        diagnostics.extend(_nonnegative(entry.get("peak_vram_mib"), f"{field}.peak_vram_mib"))
    if entry.get("role") not in {"boke", "tsukkomi"}:
        diagnostics.append(_diag("ROLE", "role must be boke or tsukkomi", f"{field}.role"))
    if state == "succeeded":
        path = entry.get("image_path")
        if not isinstance(path, str):
            diagnostics.append(_diag("UNSAFE_PATH", "image_path must be a POSIX relative path", f"{field}.image_path"))
        else:
            try:
                safe_relative_path(path)
            except (TypeError, ValueError) as exc:
                diagnostics.append(_diag("UNSAFE_PATH", str(exc), f"{field}.image_path"))
        diagnostics.extend(_checksum(entry.get("image_sha256"), f"{field}.image_sha256"))
        diagnostics.extend(_positive(entry.get("width"), f"{field}.width"))
        diagnostics.extend(_positive(entry.get("height"), f"{field}.height"))
    elif state == "failed":
        error = entry.get("error")
        diagnostics.extend(_check_fields(error, ERROR_FIELDS, ERROR_FIELDS, f"{field}.error"))
        if isinstance(error, dict):
            diagnostics.extend(_token(error.get("code"), f"{field}.error.code"))
            message = error.get("message")
            if not isinstance(message, str) or not message.strip() or len(message) > MAX_ERROR_MESSAGE:
                diagnostics.append(_diag("ERROR_MESSAGE", f"message must be 1..{MAX_ERROR_MESSAGE} characters", f"{field}.error.message"))
    return diagnostics


def validate_document(results: Any) -> list[dict[str, str]]:
    diagnostics = _check_fields(results, TOP_REQUIRED, TOP_FIELDS, "results")
    diagnostics.extend(_scan_forbidden(results))
    if not isinstance(results, dict):
        return _sorted(diagnostics)
    if results.get("kind") != KIND:
        diagnostics.append(_diag("KIND", f"kind must be {KIND}", "kind"))
    if results.get("schema_version") != SCHEMA_VERSION:
        diagnostics.append(_diag("SCHEMA_VERSION", "schema_version must be 1.0", "schema_version"))
    diagnostics.extend(_token(results.get("id"), "id"))
    if not isinstance(results.get("version"), str) or not VERSION_RE.fullmatch(results.get("version", "")):
        diagnostics.append(_diag("VERSION", "version must use vNNN", "version"))
    diagnostics.extend(_token(results.get("plan_ref"), "plan_ref"))
    if not isinstance(results.get("plan_version"), str) or not VERSION_RE.fullmatch(results.get("plan_version", "")):
        diagnostics.append(_diag("VERSION", "plan_version must use vNNN", "plan_version"))
    diagnostics.extend(_checksum(results.get("plan_sha256"), "plan_sha256"))
    if "notes" in results and (not isinstance(results.get("notes"), str) or not results["notes"].strip()):
        diagnostics.append(_diag("TEXT_REQUIRED", "notes must be non-empty when present", "notes"))
    entries = results.get("results")
    if not isinstance(entries, list) or not entries:
        diagnostics.append(_diag("RESULTS", "results must be a non-empty list", "results"))
    else:
        for index, entry in enumerate(entries):
            diagnostics.extend(_validate_entry(entry, index))
        run_ids = [entry.get("run_id") for entry in entries if isinstance(entry, dict) and isinstance(entry.get("run_id"), str)]
        if len(run_ids) != len(set(run_ids)):
            diagnostics.append(_diag("DUPLICATE_RUN", "run IDs must be unique", "results"))
    return _sorted(diagnostics)


def _root(path: Path) -> tuple[Path | None, list[dict[str, str]]]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        return None, [_diag("RESULT_ROOT_SYMLINK", "result root must not be a symlink", "result_root")]
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        return None, [_diag("RESULT_ROOT_MISSING", str(exc), "result_root")]
    if not resolved.is_dir():
        return None, [_diag("RESULT_ROOT_TYPE", "result root must be a directory", "result_root")]
    return resolved, []


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


def _read_image(root: Path, relative: str, field: str) -> tuple[bytes | None, list[dict[str, str]]]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        return None, [_diag("UNSAFE_PATH", str(exc), field)]
    lexical = root.joinpath(*safe.parts)
    if _has_symlink(lexical, root):
        return None, [_diag("IMAGE_SYMLINK", "image path contains a symlink", field)]
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        return None, [_diag("IMAGE_MISSING", str(exc), field)]
    if not resolved.is_file() or resolved.is_symlink():
        return None, [_diag("IMAGE_TYPE", "image must be a regular file", field)]
    try:
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_IMAGE_BYTES:
            return None, [_diag("IMAGE_SIZE", f"image size must be 1..{MAX_IMAGE_BYTES} bytes", field)]
        payload = resolved.read_bytes()
    except OSError as exc:
        return None, [_diag("IMAGE_READ", str(exc), field)]
    if len(payload) != size:
        return None, [_diag("IMAGE_CHANGED", "image changed while being read", field)]
    return payload, []


def _expected_common(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "model_family": row["model_family"],
        "model_profile_ref": row["model_profile_ref"],
        "model_profile_sha256": row["model_profile_sha256"],
        "workflow_sha256": row["workflow_sha256"],
        "role": row["role"],
        "candidate_id": row["candidate_id"],
        "request_id": row["request_id"],
        "identity_sha256": row["identity_sha256"],
        "strategy_id": row["strategy_id"],
        "strategy_type": row["strategy_type"],
        "pose": row["pose"],
        "expression": row["expression"],
        "control_sha256": row["control_sha256"],
    }


def validate_results(results: Any, plan: Any, result_root: Path) -> tuple[list[dict[str, str]], dict[str, bytes]]:
    diagnostics = validate_document(results)
    diagnostics.extend(validate_plan(plan))
    if diagnostics or not isinstance(results, dict) or not isinstance(plan, dict):
        return _sorted(diagnostics), {}
    if results.get("plan_ref") != plan.get("id"):
        diagnostics.append(_diag("PLAN_BINDING", "plan_ref does not match", "plan_ref"))
    if results.get("plan_version") != plan.get("version"):
        diagnostics.append(_diag("PLAN_BINDING", "plan_version does not match", "plan_version"))
    if results.get("plan_sha256") != plan_sha256(plan):
        diagnostics.append(_diag("PLAN_BINDING", "plan_sha256 does not match canonical plan", "plan_sha256"))

    expected = {row["run_id"]: row for row in expand_matrix(plan)}
    entries = results.get("results", [])
    actual = {
        entry["run_id"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("run_id"), str)
    }
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    if missing:
        diagnostics.append(_diag("MISSING_RUNS", f"missing run IDs: {', '.join(missing)}", "results"))
    if extra:
        diagnostics.append(_diag("EXTRA_RUNS", f"unexpected run IDs: {', '.join(extra)}", "results"))

    root, root_diagnostics = _root(result_root)
    diagnostics.extend(root_diagnostics)
    images: dict[str, bytes] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("run_id"), str):
            continue
        row = expected.get(entry["run_id"])
        if row is None:
            continue
        for name, value in _expected_common(row).items():
            if entry.get(name) != value:
                diagnostics.append(_diag("RUN_BINDING", f"{name} does not match identity-lock matrix", f"results[{index}].{name}"))
        if entry.get("state") != "succeeded" or root is None:
            continue
        if entry.get("image_path") != row["output_path"]:
            diagnostics.append(_diag("IMAGE_PATH_BINDING", f"expected {row['output_path']}", f"results[{index}].image_path"))
            continue
        payload, read_diagnostics = _read_image(root, str(entry.get("image_path", "")), f"results[{index}].image_path")
        diagnostics.extend(read_diagnostics)
        if payload is None:
            continue
        if hashlib.sha256(payload).hexdigest() != entry.get("image_sha256"):
            diagnostics.append(_diag("IMAGE_CHECKSUM", "image SHA-256 does not match", f"results[{index}].image_sha256"))
        try:
            width, height = _parse_png(payload)
        except BenchmarkResultsError as exc:
            diagnostics.append(_diag(exc.code, exc.message, f"results[{index}].image_path"))
            continue
        if width != entry.get("width") or height != entry.get("height"):
            diagnostics.append(_diag("IMAGE_DIMENSIONS", f"PNG dimensions are {width}x{height}", f"results[{index}]"))
        images[entry["run_id"]] = payload
    return _sorted(diagnostics), images


def _svg_text(x: int, y: int, text: str, *, size: int = 13, weight: str = "normal") -> str:
    return f'<text x="{x}" y="{y}" font-family="monospace" font-size="{size}" font-weight="{weight}" fill="#202020">{html.escape(text)}</text>'


def _sheet_svg(role: str, strategy: str, entries: list[dict[str, Any]], images: dict[str, bytes], poses: list[str], expressions: list[str]) -> bytes:
    cell_width = 300
    cell_height = 340
    left = 150
    header = 90
    width = left + len(expressions) * cell_width + 24
    height = header + len(poses) * cell_height + 24
    by_cell = {(entry["pose"], entry["expression"]): entry for entry in entries}
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f'<rect width="{width}" height="{height}" fill="#f5f1e8"/>',
        _svg_text(24, 30, f"ROLE: {role}", size=20, weight="bold"),
        _svg_text(24, 58, f"STRATEGY: {strategy}", size=17, weight="bold"),
    ]
    for column, expression in enumerate(expressions):
        parts.append(_svg_text(left + column * cell_width + 12, 80, expression, size=14, weight="bold"))
    for row_index, pose in enumerate(poses):
        y = header + row_index * cell_height
        parts.append(_svg_text(16, y + 28, pose, size=14, weight="bold"))
        for column, expression in enumerate(expressions):
            x = left + column * cell_width
            entry = by_cell[(pose, expression)]
            parts.append(f'<rect x="{x + 4}" y="{y + 4}" width="{cell_width - 8}" height="{cell_height - 8}" rx="8" fill="#fffdf7" stroke="#27231f" stroke-width="2"/>')
            if entry["state"] == "succeeded":
                encoded = base64.b64encode(images[entry["run_id"]]).decode("ascii")
                parts.append(f'<image x="{x + 16}" y="{y + 18}" width="{cell_width - 32}" height="{cell_width - 32}" preserveAspectRatio="xMidYMid meet" href="data:image/png;base64,{encoded}"/>')
                parts.append(_svg_text(x + 16, y + 302, f"OK {entry['elapsed_ms']} MS", size=12, weight="bold"))
            else:
                parts.append(f'<rect x="{x + 16}" y="{y + 18}" width="{cell_width - 32}" height="{cell_width - 32}" fill="#f4d8d8" stroke="#7a2020" stroke-width="2"/>')
                parts.append(_svg_text(x + 28, y + 70, "EXECUTION FAILED", size=16, weight="bold"))
                parts.append(_svg_text(x + 28, y + 98, f"CODE {entry['error']['code']}", size=12, weight="bold"))
                message = " ".join(entry["error"]["message"].split())[:64]
                parts.append(_svg_text(x + 28, y + 124, message, size=11))
                parts.append(_svg_text(x + 16, y + 302, f"ERROR {entry['elapsed_ms']} MS", size=12, weight="bold"))
            parts.append(_svg_text(x + 16, y + 324, entry["run_id"], size=9))
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def build_sheet_package(results: dict[str, Any], plan: dict[str, Any], images: dict[str, bytes]) -> tuple[dict[str, Any], dict[str, bytes]]:
    poses = sorted(plan["pose_targets"])
    expressions = sorted(plan["expression_targets"])
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in results["results"]:
        groups.setdefault((entry["role"], entry["strategy_id"]), []).append(entry)
    files: dict[str, bytes] = {}
    sheets: list[dict[str, Any]] = []
    for role, strategy in sorted(groups):
        entries = groups[(role, strategy)]
        svg = _sheet_svg(role, strategy, entries, images, poses, expressions)
        relative = f"{SHEETS_DIR}/{role}/{strategy}.svg"
        files[relative] = svg
        run_ids = [entry["run_id"] for entry in sorted(entries, key=lambda item: (item["pose"], item["expression"]))]
        sheets.append(
            {
                "role": role,
                "strategy_id": strategy,
                "path": relative,
                "sha256": hashlib.sha256(svg).hexdigest(),
                "size": len(svg),
                "run_ids": run_ids,
                "success_count": sum(entry["state"] == "succeeded" for entry in entries),
                "failure_count": sum(entry["state"] == "failed" for entry in entries),
            }
        )
    core = {
        "kind": "identity-consistency-sheet-package",
        "schema_version": "1.0",
        "plan_ref": plan["id"],
        "plan_version": plan["version"],
        "plan_sha256": plan_sha256(plan),
        "results_ref": results["id"],
        "results_version": results["version"],
        "results_sha256": results_sha256(results),
        "decision_policy": "owner-only",
        "pose_order": poses,
        "expression_order": expressions,
        "sheets": sheets,
    }
    manifest = {"id": content_identifier("identity-sheets", core), **core}
    files[PACKAGE_MANIFEST] = document_bytes(manifest)
    return manifest, files


def _output_target(path: Path) -> tuple[Path, Path]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.exists() or lexical.is_symlink():
        raise IdentityLockResultsError("OUTPUT_EXISTS", "output directory must not already exist", "output_dir")
    parent = lexical.parent
    if parent.is_symlink() or not parent.is_dir():
        raise IdentityLockResultsError("OUTPUT_PARENT", "output parent must be an existing non-symlink directory", "output_dir")
    try:
        parent_resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise IdentityLockResultsError("OUTPUT_PARENT", str(exc), "output_dir") from exc
    return lexical, parent_resolved


def publish_package(output_dir: Path, files: dict[str, bytes]) -> None:
    target, parent = _output_target(output_dir)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=parent))
    published = False
    try:
        for relative, payload in sorted(files.items()):
            safe = safe_relative_path(relative)
            destination = stage.joinpath(*safe.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
        if target.exists() or target.is_symlink():
            raise IdentityLockResultsError("OUTPUT_EXISTS", "output appeared during rendering", "output_dir")
        os.replace(stage, target)
        published = True
    except Exception:
        if not published and stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise


def _load_results(path: Path) -> dict[str, Any]:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if "\x00" in str(expanded) or ".." in expanded.parts or lexical.is_symlink():
        raise IdentityLockResultsError("RESULTS_PATH", "results path is unsafe or symlinked", "results")
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise IdentityLockResultsError("RESULTS_MISSING", str(exc), "results") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise IdentityLockResultsError("RESULTS_TYPE", "results must be a regular file", "results")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityLockResultsError("RESULTS_JSON", str(exc), "results") from exc
    if not isinstance(value, dict):
        raise IdentityLockResultsError("RESULTS_OBJECT", "results root must be an object", "results")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate identity-lock results and render deterministic consistency sheets")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("results-check", "render-sheets"):
        command = sub.add_parser(name)
        command.add_argument("results", type=Path)
        command.add_argument("plan", type=Path)
        command.add_argument("--result-root", type=Path, required=True)
        if name == "render-sheets":
            command.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        results = _load_results(args.results)
        plan = load_plan(args.plan)
        diagnostics, images = validate_results(results, plan, args.result_root)
        manifest = None
        if not diagnostics and args.command == "render-sheets":
            manifest, files = build_sheet_package(results, plan, images)
            publish_package(args.output_dir, files)
        output: dict[str, Any] = {
            "ok": not diagnostics,
            "diagnostics": diagnostics,
            "results_id": results.get("id"),
            "results_sha256": results_sha256(results),
            "run_count": len(results.get("results", [])) if isinstance(results.get("results"), list) else 0,
        }
        if manifest is not None:
            output["package_id"] = manifest["id"]
            output["sheet_count"] = len(manifest["sheets"])
            output["manifest_sha256"] = hashlib.sha256(document_bytes(manifest)).hexdigest()
    except (IdentityLockResultsError, ValueError, OSError) as exc:
        diagnostic = exc.to_dict() if isinstance(exc, IdentityLockResultsError) else _diag("ERROR", str(exc))
        output = {"ok": False, "diagnostics": [diagnostic]}
    print(json.dumps(output, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
