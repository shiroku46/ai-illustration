"""Read-only sanitized handoff snapshots for local benchmark checkpoints."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence

from .benchmark_execute import RESULTS_FILE, BenchmarkExecutionError, status

DEFAULT_LIMIT = 3
MAX_LIMIT = 144
MAX_TEXT_CHARS = 2000
QUOTED_WINDOWS_PATH_RE = re.compile(r"(?i)([\"'])[a-z]:\\.*?\1")
WINDOWS_PATH_RE = re.compile(r"(?i)\b[a-z]:\\[^\s\"']+")
QUOTED_POSIX_PATH_RE = re.compile(r"([\"'])/(?:[^\r\n]*?)\1")
POSIX_PATH_RE = re.compile(r"(?<![:A-Za-z0-9_])/(?:[^\s\"']*)")
SECRET_VALUE_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{8,})"
)


def _sanitize_text(value: Any) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    text = SECRET_VALUE_RE.sub("[redacted]", text)
    text = QUOTED_WINDOWS_PATH_RE.sub("[path]", text)
    text = WINDOWS_PATH_RE.sub("[path]", text)
    text = QUOTED_POSIX_PATH_RE.sub("[path]", text)
    text = POSIX_PATH_RE.sub("[path]", text)
    return text[:MAX_TEXT_CHARS]


def _load_results(results_root: Path) -> dict[str, Any] | None:
    lexical = results_root.expanduser()
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    if lexical.is_symlink():
        raise BenchmarkExecutionError(
            "root-symlink", "results root must not be a symlink", "results_root"
        )
    if not lexical.exists():
        return None
    try:
        root = lexical.resolve(strict=True)
    except OSError as exc:
        raise BenchmarkExecutionError("root-missing", str(exc), "results_root") from exc
    if not root.is_dir() or root.is_symlink():
        raise BenchmarkExecutionError(
            "root-type", "results root must be a regular directory", "results_root"
        )
    path = root / RESULTS_FILE
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise BenchmarkExecutionError(
            "results-file", "aggregate results must be a regular file", "results"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BenchmarkExecutionError("results-read", str(exc), "results") from exc
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise BenchmarkExecutionError(
            "results-object", "aggregate results document is invalid", "results"
        )
    return value


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {
        "run_id": entry["run_id"],
        "state": entry["state"],
        "model_family": entry["model_family"],
        "model_profile_ref": entry["model_profile_ref"],
        "seed": entry["seed"],
        "prompt_case_id": entry["prompt_case_id"],
        "elapsed_ms": entry["elapsed_ms"],
    }
    if entry["state"] == "failed":
        error = entry.get("error")
        if not isinstance(error, dict):
            raise BenchmarkExecutionError(
                "results-error", "failed run is missing its error object", entry["run_id"]
            )
        output["error"] = {
            "code": _sanitize_text(error.get("code")),
            "message": _sanitize_text(error.get("message")),
        }
    else:
        output.update(
            {
                "image_sha256": entry.get("image_sha256"),
                "width": entry.get("width"),
                "height": entry.get("height"),
            }
        )
    return output


def snapshot(
    package_root: Path,
    plan_path: Path,
    install_manifest_path: Path,
    workspace_root: Path,
    results_root: Path,
    *,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise BenchmarkExecutionError(
            "handoff-limit", f"limit must be 1..{MAX_LIMIT}", "limit"
        )

    progress = status(
        package_root,
        plan_path,
        install_manifest_path,
        workspace_root,
        results_root,
    )
    document = _load_results(results_root)
    entries = [] if document is None else document["results"]
    if len(entries) != progress["succeeded"] + progress["failed"]:
        raise BenchmarkExecutionError(
            "handoff-count", "aggregate result count differs from verified status", "results"
        )

    reported = [_summary(entry) for entry in entries[:limit]]
    return {
        "ok": True,
        "package_id": progress["package_id"],
        "run_count": progress["run_count"],
        "succeeded": progress["succeeded"],
        "failed": progress["failed"],
        "pending": progress["pending"],
        "complete": progress["complete"],
        "next_run_id": progress["next_run_id"],
        "reported_count": len(reported),
        "limit": limit,
        "runs": reported,
        "network_contacted": False,
        "prompt_queued": False,
        "automatic_ranking": False,
        "automatic_selection": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_illustration.benchmark_handoff",
        description="Emit a sanitized read-only benchmark handoff snapshot",
    )
    parser.add_argument("package_root", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("install_manifest", type=Path)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = snapshot(
            args.package_root,
            args.plan,
            args.install_manifest,
            args.workspace_root,
            args.results_root,
            limit=args.limit,
        )
    except (BenchmarkExecutionError, OSError, ValueError) as exc:
        result = {
            "ok": False,
            "diagnostics": [
                {
                    "code": _sanitize_text(
                        getattr(exc, "code", exc.__class__.__name__.lower())
                    ),
                    "message": _sanitize_text(getattr(exc, "message", str(exc))),
                    "field": _sanitize_text(getattr(exc, "field", "")),
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
