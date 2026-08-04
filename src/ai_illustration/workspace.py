"""Owner-facing pipeline status and deterministic offline progress dashboard."""

from __future__ import annotations

import argparse
import html
from pathlib import Path
import shutil
import sys
from typing import Any, Sequence

from .naming import content_identifier, safe_relative_path
from .workspace_checks import evaluate_checks
from .workspace_common import (
    DASHBOARD_CSS,
    DASHBOARD_DATA,
    DASHBOARD_HTML,
    DASHBOARD_JS,
    DASHBOARD_MANIFEST,
    WorkspaceError,
    json_bytes,
    load_workspace,
    sha256,
)

MAX_DASHBOARD_FILES_BYTES = 16 * 1024 * 1024


def workspace_status(workspace_path: Path) -> dict[str, Any]:
    workspace, payload, resolved, checks = load_workspace(workspace_path)
    evaluation = evaluate_checks(checks, resolved.parent)
    return {
        "ok": True,
        "workspace": {
            "id": workspace["id"],
            "name": resolved.name,
            "sha256": sha256(payload),
            "project_name": workspace["project_name"],
        },
        "complete": evaluation["complete"],
        "counts": evaluation["counts"],
        "checks": evaluation["checks"],
        "next": evaluation["next"],
        "network_contacted": False,
        "external_process_started": False,
    }


def _html_bytes(project_name: str) -> bytes:
    escaped = html.escape(project_name)
    csp = (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'none'; media-src 'none'; "
        "connect-src 'none'; object-src 'none'; frame-src 'none'; font-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escaped} — AI Illustration Workspace</title>
<link rel="stylesheet" href="{DASHBOARD_CSS}">
</head>
<body>
<main>
<header><p class="eyebrow">AI Illustration Workspace</p><h1>{escaped}</h1><div id="summary"></div></header>
<section id="next" aria-labelledby="next-title"><h2 id="next-title">次の操作</h2><div id="next-content"></div></section>
<section aria-labelledby="checks-title"><h2 id="checks-title">工程</h2><div id="checks" class="grid"></div></section>
</main>
<script src="{DASHBOARD_DATA}"></script>
<script src="{DASHBOARD_JS}"></script>
</body>
</html>
""".encode("utf-8")


def _css_bytes() -> bytes:
    return b"""*{box-sizing:border-box}html{background:#f4f1ea;color:#24221f;font-family:system-ui,-apple-system,sans-serif}body{margin:0}main{max-width:1180px;margin:auto;padding:40px 24px 72px}header{border-bottom:2px solid #24221f;padding-bottom:24px}.eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:12px;margin:0 0 8px}h1{font-size:clamp(32px,6vw,72px);line-height:.95;margin:0 0 20px}h2{font-size:20px;margin:36px 0 14px}.summary{display:flex;gap:18px;flex-wrap:wrap}.summary span{background:#fff;border:1px solid #b7b1a7;padding:8px 12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}.card{background:#fff;border:1px solid #b7b1a7;padding:18px;min-height:190px}.card h3{margin:0 0 8px;font-size:18px}.meta{font-size:12px;color:#645f57;word-break:break-all}.badge{display:inline-block;border:1px solid currentColor;padding:3px 8px;font-size:12px;font-weight:700;text-transform:uppercase}.complete{color:#17633b}.not-started{color:#7b5512}.blocked{color:#655f57}.invalid{color:#9a261e}.diagnostic{font-size:13px;background:#f7f5f1;padding:8px;margin-top:10px;white-space:pre-wrap}.next-box{background:#24221f;color:#fff;padding:18px}.argv{font-family:ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:#111;padding:10px;margin-top:10px}button{font:inherit}noscript{color:#9a261e}\n"""


def _js_bytes() -> bytes:
    return b"""(()=>{'use strict';const data=window.__AI_ILLUSTRATION_WORKSPACE__;const esc=v=>String(v??'');const summary=document.getElementById('summary');summary.className='summary';for(const [key,value] of Object.entries(data.counts)){const node=document.createElement('span');node.textContent=`${key}: ${value}`;summary.appendChild(node)}const next=document.getElementById('next-content');if(!data.next){next.className='next-box';next.textContent='すべての宣言済み工程が完了しています。'}else{next.className='next-box';const title=document.createElement('strong');title.textContent=`${data.next.check_id} (${data.next.status})`;next.appendChild(title);const action=data.next.action;if(action){const p=document.createElement('p');p.textContent=action.label;next.appendChild(p);if(action.type==='command'){const code=document.createElement('div');code.className='argv';code.textContent=action.argv.join(' ');next.appendChild(code)}}else{const p=document.createElement('p');p.textContent='Workspaceに次の操作が宣言されていません。';next.appendChild(p)}}const grid=document.getElementById('checks');for(const check of data.checks){const card=document.createElement('article');card.className='card';const h=document.createElement('h3');h.textContent=check.id;card.appendChild(h);const badge=document.createElement('span');badge.className=`badge ${check.status}`;badge.textContent=check.status;card.appendChild(badge);for(const text of [`kind: ${check.kind}`,`artifact: ${check.artifact}`,check.result_id?`result: ${check.result_id}`:'']){if(!text)continue;const p=document.createElement('p');p.className='meta';p.textContent=text;card.appendChild(p)}for(const item of check.diagnostics){const d=document.createElement('div');d.className='diagnostic';d.textContent=`${item.code}: ${item.message}`;card.appendChild(d)}grid.appendChild(card)}})();\n"""


def _dashboard_expected(workspace_path: Path) -> tuple[dict[str, Any], dict[str, bytes], set[Path]]:
    workspace, workspace_bytes, resolved, checks = load_workspace(workspace_path)
    status = workspace_status(resolved)
    data_bytes = b"window.__AI_ILLUSTRATION_WORKSPACE__=" + json_bytes(
        {
            "complete": status["complete"],
            "counts": status["counts"],
            "checks": status["checks"],
            "next": status["next"],
        }
    ).rstrip(b"\n") + b";\n"
    generated = {
        DASHBOARD_HTML: _html_bytes(workspace["project_name"]),
        DASHBOARD_CSS: _css_bytes(),
        DASHBOARD_JS: _js_bytes(),
        DASHBOARD_DATA: data_bytes,
    }
    total = sum(len(payload) for payload in generated.values())
    if total > MAX_DASHBOARD_FILES_BYTES:
        raise WorkspaceError("DASHBOARD_SIZE", "dashboard generated bytes exceed the limit", "dashboard")
    core = {
        "kind": "ai-illustration-workspace-dashboard",
        "schema_version": "1.0",
        "source_workspace": {
            "id": workspace["id"],
            "name": resolved.name,
            "sha256": sha256(workspace_bytes),
        },
        "project_name": workspace["project_name"],
        "complete": status["complete"],
        "counts": status["counts"],
        "status_sha256": sha256(json_bytes(status)),
        "network": False,
        "external_process": False,
    }
    package_id = content_identifier("ai-illustration-workspace-dashboard", core, 20)
    files = [
        {"path": path, "sha256": sha256(payload), "size": len(payload)}
        for path, payload in sorted(generated.items())
    ]
    manifest = {"id": package_id, **core, "files": files}
    generated[DASHBOARD_MANIFEST] = json_bytes(manifest)
    sources = {resolved}
    for check in checks:
        for value in check["resolved_arguments"].values():
            if isinstance(value, Path) and value.exists():
                sources.add(value.resolve(strict=False))
    return manifest, generated, sources


def _output_root(path: Path, sources: set[Path]) -> Path:
    raw = str(path)
    if "\x00" in raw or ".." in path.expanduser().parts:
        raise WorkspaceError("UNSAFE_PATH", "output_root path is unsafe", "output_root")
    expanded = path.expanduser()
    for candidate in (expanded, *expanded.parents):
        if candidate.exists() and candidate.is_symlink():
            raise WorkspaceError("OUTPUT_SYMLINK", "output_root contains a symlink component", "output_root")
    if expanded.exists() and not expanded.is_dir():
        raise WorkspaceError("OUTPUT_TYPE", "output_root must be a directory", "output_root")
    resolved = expanded.resolve(strict=False)
    for source in sources:
        source_resolved = source.resolve(strict=False)
        if source_resolved.is_dir():
            try:
                resolved.relative_to(source_resolved)
            except ValueError:
                pass
            else:
                raise WorkspaceError("OUTPUT_OVERLAP", "dashboard output is inside a declared source directory", "output_root")
            try:
                source_resolved.relative_to(resolved)
            except ValueError:
                pass
            else:
                raise WorkspaceError("OUTPUT_OVERLAP", "dashboard output contains a declared source directory", "output_root")
        else:
            try:
                source_resolved.relative_to(resolved)
            except ValueError:
                pass
            else:
                raise WorkspaceError("OUTPUT_OVERLAP", "dashboard output contains a declared source file", "output_root")
    return resolved


def _write_package(root: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> bool:
    root.mkdir(parents=True, exist_ok=True)
    destination = root / manifest["id"]
    expected = set(files)
    if destination.is_symlink():
        raise WorkspaceError("OUTPUT_SYMLINK", "dashboard destination is a symlink", "output_root")
    if destination.exists():
        if not destination.is_dir():
            raise WorkspaceError("OUTPUT_CONFLICT", "dashboard destination is not a directory", "output_root")
        actual: set[str] = set()
        for path in destination.rglob("*"):
            if path.is_symlink():
                raise WorkspaceError("OUTPUT_SYMLINK", "existing dashboard contains a symlink", str(path))
            if path.is_file():
                actual.add(path.relative_to(destination).as_posix())
        if actual != expected:
            raise WorkspaceError("OUTPUT_CONFLICT", "existing dashboard file set differs", "output_root")
        for relative, payload in files.items():
            if destination.joinpath(*safe_relative_path(relative).parts).read_bytes() != payload:
                raise WorkspaceError("OUTPUT_CONFLICT", f"existing dashboard file differs: {relative}", relative)
        return False
    staging = root / f".{manifest['id']}.tmp"
    if staging.exists() or staging.is_symlink():
        raise WorkspaceError("STAGING_CONFLICT", "dashboard staging path already exists", "output_root")
    try:
        staging.mkdir()
        for relative, payload in files.items():
            target = staging.joinpath(*safe_relative_path(relative).parts)
            target.write_bytes(payload)
        staging.replace(destination)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    return True


def build_workspace_dashboard(workspace_path: Path, output_root: Path, *, write: bool = False) -> dict[str, Any]:
    manifest, files, sources = _dashboard_expected(workspace_path)
    written = False
    if write:
        root = _output_root(output_root, sources)
        written = _write_package(root, manifest, files)
    return {
        "ok": True,
        "dashboard": manifest,
        "package_path": manifest["id"],
        "file_count": len(files),
        "written": written,
    }


def check_workspace_dashboard(manifest_path: Path, workspace_path: Path, output_root: Path) -> dict[str, Any]:
    root = _output_root(output_root, {workspace_path.expanduser().resolve(strict=True)})
    try:
        resolved = manifest_path.expanduser().resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise WorkspaceError("MANIFEST_LOCATION", "dashboard manifest must be beneath output_root", str(manifest_path)) from exc
    manifest, expected_files, _sources = _dashboard_expected(workspace_path)
    destination = root / manifest["id"]
    canonical = destination / DASHBOARD_MANIFEST
    if resolved != canonical.resolve(strict=True):
        raise WorkspaceError("MANIFEST_LOCATION", "dashboard manifest location is not canonical", str(manifest_path))
    expected_names = set(expected_files)
    actual_names: set[str] = set()
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise WorkspaceError("PACKAGE_SYMLINK", "dashboard package contains a symlink", str(path))
        if path.is_file():
            actual_names.add(path.relative_to(destination).as_posix())
    if actual_names != expected_names:
        raise WorkspaceError(
            "FILE_SET",
            f"missing={sorted(expected_names-actual_names)} extra={sorted(actual_names-expected_names)}",
            str(destination),
        )
    for relative, payload in expected_files.items():
        if destination.joinpath(*safe_relative_path(relative).parts).read_bytes() != payload:
            raise WorkspaceError("FILE_MISMATCH", f"dashboard file changed: {relative}", relative)
    return {"ok": True, "dashboard": manifest, "file_count": len(expected_files)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ai_illustration.workspace")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "check"):
        command = commands.add_parser(name)
        command.add_argument("workspace", type=Path)
    build = commands.add_parser("build")
    build.add_argument("workspace", type=Path)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument("--write", action="store_true")
    dashboard_check = commands.add_parser("dashboard-check")
    dashboard_check.add_argument("dashboard_manifest", type=Path)
    dashboard_check.add_argument("workspace", type=Path)
    dashboard_check.add_argument("--output-root", type=Path, required=True)
    return parser


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(json_bytes(value))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"status", "check"}:
            result = workspace_status(args.workspace)
            _emit(result)
            print(
                f"workspace: {result['counts']['complete']}/{result['counts']['total']} complete",
                file=sys.stderr,
            )
            return 0 if args.command == "status" or result["complete"] else 1
        if args.command == "build":
            result = build_workspace_dashboard(args.workspace, args.output_root, write=args.write)
            _emit(result)
            print(
                f"workspace dashboard ready: {result['dashboard']['id']} (written={result['written']})",
                file=sys.stderr,
            )
            return 0
        result = check_workspace_dashboard(
            args.dashboard_manifest,
            args.workspace,
            args.output_root,
        )
        _emit(result)
        print(f"workspace dashboard valid: {result['dashboard']['id']}", file=sys.stderr)
        return 0
    except WorkspaceError as exc:
        _emit({"ok": False, "errors": [exc.to_dict()]})
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
