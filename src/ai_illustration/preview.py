"""Deterministic, fully offline paper-theater preview packaging."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
import json
from pathlib import Path
import shutil
from typing import Any

from .naming import SHA256_RE, canonical_json, content_identifier, safe_relative_path
from .paper_theater import PaperTheaterError, check_scene_plan

PREVIEW_MANIFEST = "preview-manifest.json"
INDEX_HTML = "index.html"
PLAYER_JS = "player.js"
STYLE_CSS = "style.css"
MAX_DIMENSION = 8192
ROLES = ("boke", "tsukkomi")


@dataclass
class PreviewError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreviewError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise PreviewError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _dimension(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > MAX_DIMENSION:
        raise PreviewError("DIMENSION", f"{field} must be an integer from 1 to {MAX_DIMENSION}", field)
    return value


def _root(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise PreviewError("SYMLINK_ROOT", f"{field} must not be a symlink", field)
    if must_exist and not expanded.is_dir():
        raise PreviewError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise PreviewError("ROOT_TYPE", f"{field} must be a directory", field)
    return expanded.resolve()


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_existing_file(root: Path, relative: str, field: str) -> tuple[str, Path]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise PreviewError("UNSAFE_PATH", str(exc), field) from exc
    normalized = safe.as_posix()
    candidate = root.joinpath(*safe.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PreviewError("FILE_MISSING", str(exc), field) from exc
    if not _within(root, resolved):
        raise PreviewError("PATH_ESCAPE", f"{field} escapes configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise PreviewError("FILE_TYPE", f"{field} must be a regular file", field)
    return normalized, resolved


def _scene_reference(scene_plan: Path, package_root: Path) -> tuple[str, bytes, dict[str, Any]]:
    root = _root(package_root, must_exist=True, field="package_root")
    expanded = scene_plan.expanduser()
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise PreviewError("SCENE_MISSING", str(exc), "scene_plan") from exc
    if expanded.is_symlink() or not resolved.is_file():
        raise PreviewError("SCENE_TYPE", "scene plan must be a regular file", "scene_plan")
    if not _within(root, resolved):
        raise PreviewError("SCENE_LOCATION", "scene plan must be beneath package_root", "scene_plan")
    relative = resolved.relative_to(root).as_posix()
    try:
        checked = check_scene_plan(resolved, root)
    except PaperTheaterError as exc:
        raise PreviewError(f"SCENE_{exc.code}", exc.message, exc.field or "scene_plan") from exc
    scene = checked.get("scene_plan")
    if not isinstance(scene, dict):
        raise PreviewError("SCENE_RESULT", "scene validation result is malformed", "scene_plan")
    return relative, resolved.read_bytes(), scene


def _collect_assets(scene: dict[str, Any], package_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, bytes]]:
    root = _root(package_root, must_exist=True, field="package_root")
    assets_by_sha: dict[str, dict[str, Any]] = {}
    asset_bytes: dict[str, bytes] = {}
    segments: list[dict[str, Any]] = []
    source_segments = scene.get("segments")
    if not isinstance(source_segments, list) or not source_segments:
        raise PreviewError("SCENE_SEGMENTS", "scene plan has no segments", "segments")

    for index, segment in enumerate(source_segments):
        if not isinstance(segment, dict):
            raise PreviewError("SCENE_SEGMENTS", "scene segment is malformed", f"segments[{index}]")
        output_segment: dict[str, Any] = {
            "start_ms": segment.get("start_ms"),
            "end_ms": segment.get("end_ms"),
            "stage_slots": segment.get("stage_slots"),
        }
        if (
            isinstance(output_segment["start_ms"], bool)
            or not isinstance(output_segment["start_ms"], int)
            or isinstance(output_segment["end_ms"], bool)
            or not isinstance(output_segment["end_ms"], int)
            or output_segment["start_ms"] < 0
            or output_segment["end_ms"] <= output_segment["start_ms"]
        ):
            raise PreviewError("SCENE_SEGMENTS", "scene boundaries are invalid", f"segments[{index}]")
        slots = output_segment["stage_slots"]
        if not isinstance(slots, dict) or set(slots) != set(ROLES):
            raise PreviewError("SCENE_SEGMENTS", "scene stage slots are malformed", f"segments[{index}].stage_slots")
        for role in ROLES:
            state = segment.get(role)
            if not isinstance(state, dict):
                raise PreviewError("SCENE_SEGMENTS", "scene role state is malformed", f"segments[{index}].{role}")
            png_path = state.get("png_path")
            expected_sha = state.get("png_sha256")
            if not isinstance(png_path, str) or not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
                raise PreviewError("SCENE_ASSET", "scene PNG binding is malformed", f"segments[{index}].{role}")
            normalized, source = _safe_existing_file(root, png_path, f"segments[{index}].{role}.png_path")
            payload = source.read_bytes()
            actual_sha = _sha(payload)
            if actual_sha != expected_sha:
                raise PreviewError("SOURCE_ASSET_MISMATCH", "source PNG checksum differs from scene binding", normalized)
            asset_path = f"assets/{actual_sha}.png"
            if actual_sha not in assets_by_sha:
                assets_by_sha[actual_sha] = {
                    "path": asset_path,
                    "sha256": actual_sha,
                    "size": len(payload),
                    "source_path": normalized,
                }
                asset_bytes[asset_path] = payload
            output_segment[role] = {
                "key": state.get("key"),
                "variant_id": state.get("variant_id"),
                "asset_path": asset_path,
                "png_sha256": actual_sha,
            }
        segments.append(output_segment)
    return [assets_by_sha[key] for key in sorted(assets_by_sha)], segments, asset_bytes


def _player_data(core: dict[str, Any]) -> dict[str, Any]:
    return {
        "duration_ms": core["duration_ms"],
        "width": core["width"],
        "height": core["height"],
        "segments": core["segments"],
    }


def _html_bytes(core: dict[str, Any]) -> bytes:
    data = html.escape(canonical_json(_player_data(core)).decode("ascii"), quote=True)
    csp = (
        "default-src 'none'; img-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; "
        "font-src 'none'; base-uri 'none'; form-action 'none'"
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline Paper Theater Preview</title>
<link rel="stylesheet" href="{STYLE_CSS}">
</head>
<body>
<main id="preview" data-preview="{data}">
<section id="stage" aria-label="paper theater stage">
<img id="boke" class="character" alt="boke">
<img id="tsukkomi" class="character" alt="tsukkomi">
</section>
<section id="controls" aria-label="preview controls">
<button id="play" type="button">Play</button>
<button id="pause" type="button">Pause</button>
<button id="restart" type="button">Restart</button>
<input id="scrub" type="range" min="0" max="{core['duration_ms']}" step="1" value="0" aria-label="timeline">
<output id="current">0</output>
</section>
</main>
<script src="{PLAYER_JS}"></script>
</body>
</html>
"""
    return document.encode("utf-8")


def _css_bytes() -> bytes:
    return b"html,body{margin:0;min-height:100%;background:#f5f5f5;color:#111;font-family:sans-serif}body{display:grid;place-items:center}#preview{width:min(96vw,1200px)}#stage{position:relative;width:100%;aspect-ratio:var(--canvas-ratio,16/9);overflow:hidden;background:white;border:1px solid #bbb}.character{position:absolute;inset-block:0;width:48%;height:100%;object-fit:contain;display:none}.character.slot-left{display:block;left:0}.character.slot-right{display:block;right:0}.character.slot-center{display:block;left:26%}#controls{display:grid;grid-template-columns:auto auto auto 1fr auto;gap:.5rem;align-items:center;padding:.75rem 0}button,input{font:inherit}output{min-width:5ch;text-align:right}\n"


def _js_bytes() -> bytes:
    return b"(()=>{'use strict';const root=document.getElementById('preview');const data=JSON.parse(root.dataset.preview);const stage=document.getElementById('stage');stage.style.setProperty('--canvas-ratio',`${data.width}/${data.height}`);const images={boke:document.getElementById('boke'),tsukkomi:document.getElementById('tsukkomi')};const scrub=document.getElementById('scrub');const current=document.getElementById('current');let playing=false;let started=0;let position=0;let frame=0;function segmentAt(ms){let result=data.segments[data.segments.length-1];for(const segment of data.segments){if(ms>=segment.start_ms&&ms<segment.end_ms){result=segment;break}}return result}function render(ms){const bounded=Math.max(0,Math.min(data.duration_ms,Math.floor(ms)));const segment=segmentAt(bounded===data.duration_ms?Math.max(0,bounded-1):bounded);for(const role of ['boke','tsukkomi']){const image=images[role];const state=segment[role];image.src=state.asset_path;image.className=`character slot-${segment.stage_slots[role]}`;image.dataset.sha256=state.png_sha256}scrub.value=String(bounded);current.value=String(bounded);return bounded}function tick(now){if(!playing)return;const bounded=render(position+(now-started));if(bounded>=data.duration_ms){position=data.duration_ms;playing=false;return}frame=requestAnimationFrame(tick)}document.getElementById('play').addEventListener('click',()=>{if(playing)return;if(position>=data.duration_ms)position=0;playing=true;started=performance.now();frame=requestAnimationFrame(tick)});document.getElementById('pause').addEventListener('click',()=>{if(playing)position=render(position+(performance.now()-started));playing=false;cancelAnimationFrame(frame)});document.getElementById('restart').addEventListener('click',()=>{playing=false;cancelAnimationFrame(frame);position=0;render(position)});scrub.addEventListener('input',()=>{playing=false;cancelAnimationFrame(frame);position=Number(scrub.value);render(position)});render(position)})();\n"


def _build_expected(scene_plan: Path, package_root: Path, width: int, height: int) -> tuple[dict[str, Any], dict[str, bytes]]:
    width = _dimension(width, "width")
    height = _dimension(height, "height")
    scene_relative, scene_bytes, scene = _scene_reference(scene_plan, package_root)
    assets, segments, asset_bytes = _collect_assets(scene, package_root)
    roles = scene.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        raise PreviewError("SCENE_ROLES", "scene roles are malformed", "roles")
    role_provenance = {
        role: {
            "package_id": roles[role].get("package_id"),
            "package_manifest_sha256": roles[role].get("package_manifest_sha256"),
            "variant_set_ref": roles[role].get("variant_set_ref"),
            "character_ref": roles[role].get("character_ref"),
            "license_status": roles[role].get("license_status"),
            "stage_slot": roles[role].get("stage_slot"),
        }
        for role in ROLES
    }
    core = {
        "kind": "paper-theater-preview",
        "schema_version": "1.0",
        "scene_plan_ref": scene.get("id"),
        "scene_plan_path": scene_relative,
        "scene_plan_sha256": _sha(scene_bytes),
        "width": width,
        "height": height,
        "intent": scene.get("intent"),
        "duration_ms": scene.get("duration_ms"),
        "roles": role_provenance,
        "segments": segments,
        "assets": assets,
    }
    preview_id = content_identifier("paper-theater-preview", core, 20)
    identified = {"id": preview_id, **core}
    generated = {
        INDEX_HTML: _html_bytes(identified),
        PLAYER_JS: _js_bytes(),
        STYLE_CSS: _css_bytes(),
        **asset_bytes,
    }
    files = [
        {"path": path, "sha256": _sha(payload), "size": len(payload)}
        for path, payload in sorted(generated.items())
    ]
    manifest = {**identified, "files": files}
    generated[PREVIEW_MANIFEST] = _json_bytes(manifest)
    return manifest, generated


def _write_package(output_root: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> bool:
    root_path = output_root.expanduser()
    if root_path.is_symlink():
        raise PreviewError("SYMLINK_ROOT", "output_root must not be a symlink", "output_root")
    root_path.mkdir(parents=True, exist_ok=True)
    root = root_path.resolve()
    destination = root / manifest["id"]
    if destination.is_symlink():
        raise PreviewError("OUTPUT_SYMLINK", "preview destination must not be a symlink", "output_root")
    expected = set(files)
    if destination.exists():
        if not destination.is_dir():
            raise PreviewError("OUTPUT_CONFLICT", "preview destination is not a directory", "output_root")
        actual = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
        if actual != expected:
            raise PreviewError("OUTPUT_CONFLICT", "existing preview file set differs", "output_root")
        for relative, payload in files.items():
            candidate = destination / relative
            if candidate.is_symlink() or candidate.read_bytes() != payload:
                raise PreviewError("OUTPUT_CONFLICT", f"existing file differs: {relative}", relative)
        return False

    staging = root / f".{manifest['id']}.tmp"
    if staging.exists():
        if staging.is_symlink():
            raise PreviewError("STAGING_CONFLICT", "staging path is a symlink", "output_root")
        shutil.rmtree(staging)
    try:
        staging.mkdir()
        for relative, payload in files.items():
            target = staging.joinpath(*safe_relative_path(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        staging.replace(destination)
    except Exception:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        raise
    return True


def build_preview_package(scene_plan: Path, package_root: Path, output_root: Path, *, width: int, height: int, write: bool = False) -> dict[str, Any]:
    manifest, files = _build_expected(scene_plan, package_root, width, height)
    written = _write_package(output_root, manifest, files) if write else False
    return {"ok": True, "preview": manifest, "file_count": len(files), "written": written, "package_path": manifest["id"]}


def _manifest_location(manifest_path: Path, output_root: Path) -> tuple[Path, dict[str, Any], bytes]:
    root = _root(output_root, must_exist=True, field="output_root")
    expanded = manifest_path.expanduser()
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise PreviewError("MANIFEST_MISSING", str(exc), str(manifest_path)) from exc
    if expanded.is_symlink() or not resolved.is_file() or not _within(root, resolved):
        raise PreviewError("MANIFEST_PATH", "preview manifest is not a safe file beneath output_root", str(manifest_path))
    return root, _load_object(resolved), resolved.read_bytes()


def check_preview_package(manifest_path: Path, output_root: Path, package_root: Path) -> dict[str, Any]:
    root, manifest, payload = _manifest_location(manifest_path, output_root)
    if payload != _json_bytes(manifest):
        raise PreviewError("MANIFEST_CANONICAL", "preview manifest JSON is not canonical", str(manifest_path))
    preview_id = manifest.get("id")
    if not isinstance(preview_id, str):
        raise PreviewError("MANIFEST_SCHEMA", "preview ID is missing", "id")
    canonical = root / preview_id / PREVIEW_MANIFEST
    if manifest_path.expanduser().resolve() != canonical.resolve():
        raise PreviewError("MANIFEST_LOCATION", "preview manifest path is not canonical", str(manifest_path))
    scene_relative = manifest.get("scene_plan_path")
    if not isinstance(scene_relative, str):
        raise PreviewError("MANIFEST_SCHEMA", "scene_plan_path is missing", "scene_plan_path")
    package_root_resolved = _root(package_root, must_exist=True, field="package_root")
    _normalized, scene_path = _safe_existing_file(package_root_resolved, scene_relative, "scene_plan_path")
    expected_manifest, expected_files = _build_expected(scene_path, package_root_resolved, manifest.get("width"), manifest.get("height"))
    if manifest != expected_manifest:
        raise PreviewError("MANIFEST_BINDING_MISMATCH", "preview manifest is stale or not canonical for the current scene", str(manifest_path))

    destination = root / preview_id
    expected_names = set(expected_files)
    actual_names: set[str] = set()
    for candidate in destination.rglob("*"):
        if candidate.is_symlink():
            raise PreviewError("PACKAGE_SYMLINK", "preview package contains a symlink", candidate.relative_to(destination).as_posix())
        if candidate.is_file():
            actual_names.add(candidate.relative_to(destination).as_posix())
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise PreviewError("FILE_SET_MISMATCH", f"missing={missing}; extra={extra}", str(destination))
    for relative, expected_bytes in expected_files.items():
        candidate = destination.joinpath(*safe_relative_path(relative).parts)
        if candidate.read_bytes() != expected_bytes:
            raise PreviewError("FILE_MISMATCH", f"preview file was modified: {relative}", relative)
    return {"ok": True, "preview": manifest, "file_count": len(expected_files), "segment_count": len(manifest["segments"])}
