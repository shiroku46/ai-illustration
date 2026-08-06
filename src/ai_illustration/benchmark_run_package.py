"""Deterministic offline ComfyUI API workflow packages for model benchmarks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Iterable, Sequence

from .model_benchmark import (
    canonical_sha256,
    expand_matrix,
    validate_dependencies,
    validate_plan,
)
from .model_install_manifest import load_manifest, validate_manifest
from .naming import canonical_json, safe_relative_path

PACKAGE_KIND = "benchmark-run-package"
SCHEMA_VERSION = "1.0"
PACKAGE_MANIFEST = "benchmark-run-package.json"
MAX_SOURCE_BYTES = 16 * 1024 * 1024
EXPECTED_RUN_COUNT = 144


class RunPackageError(ValueError):
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


def _document_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _resolve_root(path: Path) -> Path:
    expanded = path.expanduser()
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if lexical.is_symlink():
        raise RunPackageError("WORKSPACE_SYMLINK", "workspace root must not be a symlink", "workspace_root")
    try:
        root = lexical.resolve(strict=True)
    except OSError as exc:
        raise RunPackageError("WORKSPACE_MISSING", str(exc), "workspace_root") from exc
    if not root.is_dir():
        raise RunPackageError("WORKSPACE_TYPE", "workspace root must be a directory", "workspace_root")
    return root


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


def _read_relative(root: Path, relative: str, field: str) -> bytes:
    try:
        safe = safe_relative_path(relative)
        lexical = root.joinpath(*safe.parts)
        if _has_symlink(lexical, root):
            raise ValueError("path contains a symlink")
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_file() or resolved.is_symlink():
            raise ValueError("path must be a regular non-symlink file")
        size = resolved.stat().st_size
        if size <= 0 or size > MAX_SOURCE_BYTES:
            raise ValueError(f"file size must be 1..{MAX_SOURCE_BYTES} bytes")
        payload = resolved.read_bytes()
        if len(payload) != size:
            raise ValueError("file changed while being read")
        return payload
    except (OSError, ValueError) as exc:
        raise RunPackageError("SOURCE_READ", str(exc), field) from exc


def _json_object(payload: bytes, field: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RunPackageError("SOURCE_JSON", str(exc), field) from exc
    if not isinstance(value, dict):
        raise RunPackageError("SOURCE_JSON", "JSON root must be an object", field)
    return value


def _single_node(workflow: dict[str, Any], class_type: str, field: str) -> dict[str, Any]:
    nodes = [
        node
        for node in workflow.values()
        if isinstance(node, dict) and node.get("class_type") == class_type
    ]
    if len(nodes) != 1:
        raise RunPackageError(
            "WORKFLOW_NODE_COUNT",
            f"exactly one {class_type} node is required",
            field,
        )
    return nodes[0]


def _prompt_node(workflow: dict[str, Any], title: str, field: str) -> dict[str, Any]:
    nodes = [
        node
        for node in workflow.values()
        if isinstance(node, dict)
        and node.get("class_type") == "CLIPTextEncode"
        and isinstance(node.get("_meta"), dict)
        and node["_meta"].get("title") == title
    ]
    if len(nodes) != 1:
        raise RunPackageError(
            "WORKFLOW_PROMPT_NODE",
            f"exactly one prompt node titled {title!r} is required",
            field,
        )
    return nodes[0]


def _inputs(node: dict[str, Any], field: str) -> dict[str, Any]:
    inputs = node.get("inputs")
    if not isinstance(inputs, dict):
        raise RunPackageError("WORKFLOW_INPUTS", "node inputs must be an object", field)
    return inputs


def _exact_setting(value: str) -> str:
    return value.replace("-", "_")


def _manifest_model_map(install_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    models = install_manifest.get("models")
    if not isinstance(models, list):
        raise RunPackageError("INSTALL_MODELS", "installation manifest models must be a list", "install_manifest.models")
    result: dict[str, dict[str, Any]] = {}
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("family"), str):
            raise RunPackageError("INSTALL_MODEL", "installation model entry is invalid", "install_manifest.models")
        family = model["family"]
        if family in result:
            raise RunPackageError("INSTALL_MODEL", f"duplicate installation family: {family}", "install_manifest.models")
        result[family] = model
    return result


def validate_cross_bindings(
    plan: dict[str, Any],
    install_manifest: dict[str, Any],
) -> list[dict[str, str]]:
    diagnostics = list(validate_plan(plan))
    if diagnostics:
        return _sorted(diagnostics)
    try:
        installation = _manifest_model_map(install_manifest)
    except RunPackageError as exc:
        return [exc.to_dict()]
    plan_families = {model["family"] for model in plan["models"]}
    if plan_families != set(installation):
        diagnostics.append(
            _diag(
                "FAMILY_BINDING",
                f"plan and installation families differ: plan={sorted(plan_families)}, install={sorted(installation)}",
                "models",
            )
        )
    for index, model in enumerate(plan["models"]):
        field = f"models[{index}]"
        installed = installation.get(model["family"])
        if installed is None:
            continue
        expected_ref = f"{model['profile_id']}@{model['profile_version']}"
        for name, expected in (
            ("profile_ref", expected_ref),
            ("profile_sha256", model["profile_sha256"]),
        ):
            if installed.get(name) != expected:
                diagnostics.append(
                    _diag(
                        "MODEL_BINDING",
                        f"installation {name} differs from benchmark plan",
                        f"{field}.{name}",
                    )
                )
        workflow = installed.get("workflow")
        if not isinstance(workflow, dict) or workflow.get("path") != model["workflow_path"] or workflow.get("sha256") != model["workflow_sha256"]:
            diagnostics.append(
                _diag(
                    "WORKFLOW_BINDING",
                    "installation workflow path or SHA-256 differs from benchmark plan",
                    f"{field}.workflow",
                )
            )
        settings = installed.get("benchmark_settings")
        if not isinstance(settings, dict):
            diagnostics.append(_diag("SETTINGS_BINDING", "installation settings are missing", field))
            continue
        expected_settings = {
            "width": model["native_width"],
            "height": model["native_height"],
            "steps": model["steps"],
            "cfg": model["cfg"],
            "sampler": _exact_setting(model["sampler"]),
            "scheduler": _exact_setting(model["scheduler"]),
            "prompt_format": model["prompt_format"],
        }
        for name, expected in expected_settings.items():
            if settings.get(name) != expected:
                diagnostics.append(
                    _diag(
                        "SETTINGS_BINDING",
                        f"{name} differs: plan={expected!r}, install={settings.get(name)!r}",
                        f"{field}.{name}",
                    )
                )
    return _sorted(diagnostics)


def _case_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {case["id"]: case for case in plan["prompt_cases"]}


def _plan_model_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {model["family"]: model for model in plan["models"]}


def bind_run_workflow(
    template: dict[str, Any],
    row: dict[str, Any],
    prompt_case: dict[str, Any],
    install_model: dict[str, Any],
) -> dict[str, Any]:
    workflow = json.loads(json.dumps(template, ensure_ascii=False))
    sampler = _inputs(_single_node(workflow, "KSampler", "sampler"), "sampler")
    latent = _inputs(_single_node(workflow, "EmptyLatentImage", "latent"), "latent")
    output = _inputs(_single_node(workflow, "SaveImage", "output"), "output")
    positive = _inputs(_prompt_node(workflow, "Positive prompt", "positive"), "positive")
    negative = _inputs(_prompt_node(workflow, "Negative prompt", "negative"), "negative")
    exact = install_model["benchmark_settings"]

    sampler.update(
        {
            "seed": row["seed"],
            "steps": exact["steps"],
            "cfg": exact["cfg"],
            "sampler_name": exact["sampler"],
            "scheduler": exact["scheduler"],
            "denoise": 1.0,
        }
    )
    latent.update(
        {
            "width": exact["width"],
            "height": exact["height"],
            "batch_size": 1,
        }
    )
    positive["text"] = prompt_case["positive_contract"]
    negative["text"] = prompt_case["negative_contract"]
    image_path = safe_relative_path(row["image_path"])
    output["filename_prefix"] = image_path.with_suffix("").as_posix()
    return workflow


def build_package(
    plan: dict[str, Any],
    install_manifest: dict[str, Any],
    *,
    workspace_root: Path,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    diagnostics = validate_cross_bindings(plan, install_manifest)
    if diagnostics:
        raise RunPackageError(
            "CROSS_BINDING",
            json.dumps(diagnostics, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            "plan",
        )
    root = _resolve_root(workspace_root)
    matrix = expand_matrix(plan)
    if len(matrix) != EXPECTED_RUN_COUNT:
        raise RunPackageError(
            "RUN_COUNT",
            f"expected {EXPECTED_RUN_COUNT} runs, found {len(matrix)}",
            "matrix",
        )
    plan_models = _plan_model_map(plan)
    install_models = _manifest_model_map(install_manifest)
    cases = _case_map(plan)
    template_cache: dict[str, dict[str, Any]] = {}
    template_bytes: dict[str, bytes] = {}
    for family, model in plan_models.items():
        payload = _read_relative(root, model["workflow_path"], f"models.{family}.workflow")
        if _sha256(payload) != model["workflow_sha256"]:
            raise RunPackageError(
                "WORKFLOW_SHA256",
                "template workflow checksum differs from the benchmark plan",
                f"models.{family}.workflow",
            )
        template_cache[family] = _json_object(payload, f"models.{family}.workflow")
        template_bytes[family] = payload

    files: dict[str, bytes] = {}
    runs: list[dict[str, Any]] = []
    for row in matrix:
        family = row["model_family"]
        prompt_case = cases[row["prompt_case_id"]]
        workflow = bind_run_workflow(
            template_cache[family],
            row,
            prompt_case,
            install_models[family],
        )
        payload = _document_bytes(workflow)
        relative = f"runs/{row['run_id']}.api.json"
        files[relative] = payload
        runs.append(
            {
                **row,
                "workflow_path": relative,
                "workflow_sha256": _sha256(payload),
                "template_workflow_path": plan_models[family]["workflow_path"],
                "template_workflow_sha256": _sha256(template_bytes[family]),
                "exact_comfyui_settings": {
                    key: install_models[family]["benchmark_settings"][key]
                    for key in (
                        "width",
                        "height",
                        "steps",
                        "cfg",
                        "sampler",
                        "scheduler",
                        "prompt_format",
                    )
                },
            }
        )
    file_inventory = [
        {"path": path, "sha256": _sha256(payload), "size_bytes": len(payload)}
        for path, payload in sorted(files.items())
    ]
    core = {
        "kind": PACKAGE_KIND,
        "schema_version": SCHEMA_VERSION,
        "plan_ref": plan["id"],
        "plan_version": plan["version"],
        "plan_sha256": canonical_sha256(plan),
        "install_manifest_ref": install_manifest["id"],
        "install_manifest_version": install_manifest["version"],
        "install_manifest_sha256": canonical_sha256(install_manifest),
        "run_count": len(runs),
        "runs": runs,
        "files": file_inventory,
        "network_effect": False,
        "prompt_queued": False,
        "automatic_ranking": False,
        "automatic_selection": False,
    }
    package_id = f"benchmark-runs-{hashlib.sha256(canonical_json(core)).hexdigest()[:20]}"
    manifest = {"id": package_id, **core}
    files[PACKAGE_MANIFEST] = _document_bytes(manifest)
    return manifest, files


def validate_package(
    package_root: Path,
    plan: dict[str, Any],
    install_manifest: dict[str, Any],
    *,
    workspace_root: Path,
) -> list[dict[str, str]]:
    try:
        expected_manifest, expected_files = build_package(
            plan,
            install_manifest,
            workspace_root=workspace_root,
        )
        root = package_root.expanduser().resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise ValueError("package root must be a regular directory")
    except (OSError, ValueError, RunPackageError) as exc:
        return [
            exc.to_dict()
            if isinstance(exc, RunPackageError)
            else _diag("PACKAGE_ROOT", str(exc), "package_root")
        ]
    diagnostics: list[dict[str, str]] = []
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    expected_paths = set(expected_files)
    missing = sorted(expected_paths - set(actual))
    extra = sorted(set(actual) - expected_paths)
    if missing:
        diagnostics.append(_diag("PACKAGE_MISSING", ", ".join(missing), "package_root"))
    if extra:
        diagnostics.append(_diag("PACKAGE_EXTRA", ", ".join(extra), "package_root"))
    for relative in sorted(expected_paths & set(actual)):
        path = actual[relative]
        if _has_symlink(path, root):
            diagnostics.append(_diag("PACKAGE_SYMLINK", "package path contains a symlink", relative))
            continue
        payload = path.read_bytes()
        if payload != expected_files[relative]:
            diagnostics.append(_diag("PACKAGE_BYTES", "package bytes differ", relative))
    manifest_path = root / PACKAGE_MANIFEST
    if manifest_path.is_file():
        try:
            actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            diagnostics.append(_diag("PACKAGE_MANIFEST", str(exc), PACKAGE_MANIFEST))
        else:
            if actual_manifest != expected_manifest:
                diagnostics.append(
                    _diag("PACKAGE_MANIFEST", "manifest object differs", PACKAGE_MANIFEST)
                )
    return _sorted(diagnostics)


def publish_package(destination: Path, files: dict[str, bytes]) -> None:
    parent = destination.parent.resolve()
    if not parent.is_dir() or parent.is_symlink():
        raise RunPackageError("DESTINATION_PARENT", "destination parent must be a regular directory", "destination")
    destination = parent / destination.name
    if destination.exists() or destination.is_symlink():
        raise RunPackageError("DESTINATION_EXISTS", "destination already exists", "destination")
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-", dir=parent))
    try:
        for relative, payload in sorted(files.items()):
            safe = safe_relative_path(relative)
            target = temporary.joinpath(*safe.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RunPackageError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise RunPackageError("LOAD_ERROR", "JSON root must be an object", str(path))
    return value


def _result(
    diagnostics: list[dict[str, str]],
    *,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": not diagnostics,
        "package_id": manifest.get("id") if manifest else None,
        "run_count": manifest.get("run_count") if manifest else 0,
        "plan_sha256": manifest.get("plan_sha256") if manifest else None,
        "install_manifest_sha256": (
            manifest.get("install_manifest_sha256") if manifest else None
        ),
        "network_effect": False,
        "prompt_queued": False,
        "automatic_ranking": False,
        "automatic_selection": False,
        "diagnostics": _sorted(diagnostics),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_illustration.benchmark_run_package"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "check"):
        command = sub.add_parser(name)
        command.add_argument("plan", type=Path)
        command.add_argument("install_manifest", type=Path)
        command.add_argument("--workspace-root", type=Path, required=True)
        command.add_argument("--reference-root", type=Path, required=True)
        command.add_argument("--package-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = _load_json(args.plan)
        install_manifest = load_manifest(args.install_manifest)
        diagnostics = validate_dependencies(
            plan,
            args.workspace_root,
            args.reference_root,
        )
        install_diagnostics, _ = validate_manifest(
            install_manifest,
            workspace_root=args.workspace_root,
        )
        diagnostics.extend(install_diagnostics)
        diagnostics.extend(validate_cross_bindings(plan, install_manifest))
        if diagnostics:
            result = _result(diagnostics)
        elif args.command == "prepare":
            manifest, files = build_package(
                plan,
                install_manifest,
                workspace_root=args.workspace_root,
            )
            publish_package(args.package_root, files)
            result = _result([], manifest=manifest)
        else:
            manifest, _ = build_package(
                plan,
                install_manifest,
                workspace_root=args.workspace_root,
            )
            package_diagnostics = validate_package(
                args.package_root,
                plan,
                install_manifest,
                workspace_root=args.workspace_root,
            )
            result = _result(package_diagnostics, manifest=manifest)
    except (OSError, ValueError, RunPackageError) as exc:
        diagnostic = (
            exc.to_dict()
            if isinstance(exc, RunPackageError)
            else _diag("ERROR", str(exc))
        )
        result = _result([diagnostic])
    sys.stdout.write(
        json.dumps(
            result,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
