"""Deterministic offline playback previews for verified frame and audio packages."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
import shutil
import sys
from typing import Any, Sequence

from .audio_preview import AudioPreviewError, check_audio_preview_package
from .frame_renderer import (
    FRAME_INVENTORY,
    FRAME_RENDER_MANIFEST,
    FrameRenderError,
    check_frame_render_package,
)
from .naming import SHA256_RE, canonical_json, content_identifier, safe_relative_path

FRAME_PREVIEW_MANIFEST = "frame-preview-manifest.json"
INDEX_HTML = "index.html"
PREVIEW_DATA_JS = "preview-data.js"
PLAYER_JS = "player.js"
STYLE_CSS = "style.css"

MAX_PACKAGE_BYTES = 768 * 1024 * 1024
MAX_FRAME_COUNT = 100_000


@dataclass
class FramePreviewError(ValueError):
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
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise FramePreviewError("DUPLICATE_KEY", f"duplicate JSON key: {key}", str(path))
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except FramePreviewError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FramePreviewError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise FramePreviewError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _reject_lexical(path: Path, field: str) -> Path:
    raw = str(path)
    if "\x00" in raw or "\\" in raw:
        raise FramePreviewError("UNSAFE_PATH", f"{field} contains a forbidden path character", field)
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise FramePreviewError("UNSAFE_PATH", f"{field} must not contain parent traversal", field)
    return expanded


def _reject_symlink_components(path: Path, field: str) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    for candidate in (lexical, *lexical.parents):
        try:
            if candidate.exists() and candidate.is_symlink():
                raise FramePreviewError("PATH_SYMLINK", f"{field} contains a symlink component", field)
        except OSError as exc:
            raise FramePreviewError("PATH_ERROR", str(exc), field) from exc


def _root(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = _reject_lexical(path, field)
    _reject_symlink_components(expanded, field)
    if must_exist and not expanded.is_dir():
        raise FramePreviewError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise FramePreviewError("ROOT_TYPE", f"{field} must be a directory", field)
    try:
        return expanded.resolve(strict=must_exist)
    except OSError as exc:
        raise FramePreviewError("PATH_ERROR", str(exc), field) from exc


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_file(root: Path, relative: str, field: str) -> tuple[str, Path]:
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise FramePreviewError("UNSAFE_PATH", str(exc), field) from exc
    candidate = root.joinpath(*safe.parts)
    current = root
    for part in safe.parts:
        current /= part
        if current.is_symlink():
            raise FramePreviewError("PATH_SYMLINK", f"{field} contains a symlink component", field)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise FramePreviewError("FILE_MISSING", str(exc), field) from exc
    if not _within(root, resolved):
        raise FramePreviewError("PATH_ESCAPE", f"{field} escapes configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise FramePreviewError("FILE_TYPE", f"{field} must be a regular file", field)
    return safe.as_posix(), resolved


def _relative_file(path: Path, root: Path, field: str) -> tuple[str, Path]:
    expanded = _reject_lexical(path, field)
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        relative = lexical.relative_to(root).as_posix()
    except ValueError as exc:
        raise FramePreviewError("PATH_ESCAPE", f"{field} must be beneath its configured root", field) from exc
    return _safe_file(root, relative, field)


def _canonical_object(path: Path, field: str) -> tuple[dict[str, Any], bytes]:
    value = _load_object(path)
    payload = path.read_bytes()
    if payload != _json_bytes(value):
        raise FramePreviewError("NONCANONICAL_JSON", f"{field} is not canonical JSON", field)
    return value, payload


def _binding(value: Any, field: str) -> tuple[str, str, str]:
    if not isinstance(value, dict):
        raise FramePreviewError("BINDING_SCHEMA", f"{field} is missing", field)
    identifier, path, sha256 = value.get("id"), value.get("path"), value.get("sha256")
    if not isinstance(identifier, str) or not identifier:
        raise FramePreviewError("BINDING_SCHEMA", f"{field}.id is invalid", f"{field}.id")
    if not isinstance(path, str):
        raise FramePreviewError("BINDING_SCHEMA", f"{field}.path is invalid", f"{field}.path")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise FramePreviewError("BINDING_SCHEMA", f"{field}.sha256 is invalid", f"{field}.sha256")
    return identifier, path, sha256


def _verify_bound_file(root: Path, binding: Any, field: str) -> tuple[dict[str, Any], bytes, str, Path]:
    identifier, relative, expected_sha = _binding(binding, field)
    normalized, path = _safe_file(root, relative, f"{field}.path")
    value, payload = _canonical_object(path, field)
    if value.get("id") != identifier:
        raise FramePreviewError("BINDING_ID_MISMATCH", f"{field} ID does not match bound file", field)
    if _sha(payload) != expected_sha:
        raise FramePreviewError("BINDING_SHA_MISMATCH", f"{field} checksum does not match bound file", field)
    return value, payload, normalized, path


def _validated_sources(
    frame_render_manifest: Path,
    audio_preview_manifest: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
    str,
    Path,
    Path,
    set[Path],
]:
    frame_root = _root(frame_render_root, must_exist=True, field="frame_render_root")
    renderer_root = _root(renderer_job_root, must_exist=True, field="renderer_job_root")
    plan_root = _root(render_plan_root, must_exist=True, field="render_plan_root")
    audio_preview_base = _root(audio_preview_root, must_exist=True, field="audio_preview_root")
    preview_base = _root(preview_root, must_exist=True, field="preview_root")
    package_base = _root(package_root, must_exist=True, field="package_root")
    audio_base = _root(audio_root, must_exist=True, field="audio_root")

    frame_relative, frame_path = _relative_file(frame_render_manifest, frame_root, "frame_render_manifest")
    audio_relative, audio_path = _relative_file(audio_preview_manifest, audio_preview_base, "audio_preview_manifest")

    try:
        checked_frame = check_frame_render_package(
            frame_path,
            frame_root,
            renderer_root,
            plan_root,
            audio_preview_base,
            preview_base,
            package_base,
            audio_base,
        )
    except FrameRenderError as exc:
        raise FramePreviewError(f"FRAME_RENDER_{exc.code}", exc.message, exc.field or "frame_render_manifest") from exc
    frame_manifest = checked_frame.get("frame_render")
    if not isinstance(frame_manifest, dict):
        raise FramePreviewError("FRAME_RENDER_RESULT", "frame-render checker result is malformed", "frame_render_manifest")
    canonical_frame, _frame_bytes = _canonical_object(frame_path, "frame_render_manifest")
    if canonical_frame != frame_manifest:
        raise FramePreviewError("FRAME_RENDER_RESULT", "frame-render checker returned different manifest data", "frame_render_manifest")

    renderer_manifest, _renderer_bytes, _renderer_relative, renderer_path = _verify_bound_file(
        renderer_root,
        frame_manifest.get("source_renderer_job"),
        "source_renderer_job",
    )
    source_bindings = renderer_manifest.get("source_bindings")
    if not isinstance(source_bindings, dict):
        raise FramePreviewError("RENDERER_BINDINGS", "renderer-job source bindings are missing", "source_renderer_job")
    render_plan, _plan_bytes, _plan_relative, plan_path = _verify_bound_file(
        plan_root,
        source_bindings.get("render_plan"),
        "source_render_plan",
    )
    plan_bindings = render_plan.get("source_bindings")
    if not isinstance(plan_bindings, dict):
        raise FramePreviewError("RENDER_PLAN_BINDINGS", "render-plan source bindings are missing", "source_render_plan")
    bound_audio, bound_audio_bytes, bound_audio_relative, bound_audio_path = _verify_bound_file(
        audio_preview_base,
        plan_bindings.get("audio_preview"),
        "source_audio_preview",
    )
    if audio_path != bound_audio_path or audio_relative != bound_audio_relative:
        raise FramePreviewError(
            "AUDIO_PREVIEW_BINDING_MISMATCH",
            "caller-supplied audio-preview manifest is not the one bound by the render plan",
            "audio_preview_manifest",
        )

    try:
        checked_audio = check_audio_preview_package(
            audio_path,
            audio_preview_base,
            preview_base,
            package_base,
            audio_base,
        )
    except AudioPreviewError as exc:
        raise FramePreviewError(f"AUDIO_PREVIEW_{exc.code}", exc.message, exc.field or "audio_preview_manifest") from exc
    audio_manifest = checked_audio.get("audio_preview")
    if not isinstance(audio_manifest, dict):
        raise FramePreviewError("AUDIO_PREVIEW_RESULT", "audio-preview checker result is malformed", "audio_preview_manifest")
    canonical_audio, audio_bytes = _canonical_object(audio_path, "audio_preview_manifest")
    if canonical_audio != audio_manifest or audio_bytes != bound_audio_bytes or canonical_audio != bound_audio:
        raise FramePreviewError(
            "AUDIO_PREVIEW_RESULT",
            "audio-preview checker or source chain returned different manifest data",
            "audio_preview_manifest",
        )

    frame_package = frame_path.parent.resolve()
    audio_package = audio_path.parent.resolve()
    expected_frame_location = frame_root / str(frame_manifest.get("id")) / FRAME_RENDER_MANIFEST
    expected_audio_location = audio_preview_base / str(audio_manifest.get("id")) / "audio-preview-manifest.json"
    if frame_path != expected_frame_location.resolve():
        raise FramePreviewError("FRAME_RENDER_LOCATION", "frame-render manifest path is not canonical", "frame_render_manifest")
    if audio_path != expected_audio_location.resolve():
        raise FramePreviewError("AUDIO_PREVIEW_LOCATION", "audio-preview manifest path is not canonical", "audio_preview_manifest")

    sources = {
        frame_package,
        audio_package,
        renderer_path.parent.resolve(),
        plan_path.parent.resolve(),
        frame_root,
        renderer_root,
        plan_root,
        audio_preview_base,
        preview_base,
        package_base,
        audio_base,
    }
    return (
        frame_manifest,
        audio_manifest,
        render_plan,
        frame_relative,
        audio_relative,
        frame_package,
        audio_package,
        sources,
    )


def _positive_int(value: Any, field: str, maximum: int = 10**15) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise FramePreviewError("INTEGER_RANGE", f"{field} must be a positive bounded integer", field)
    return value


def _nonnegative_int(value: Any, field: str, maximum: int = 10**15) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise FramePreviewError("INTEGER_RANGE", f"{field} must be a nonnegative bounded integer", field)
    return value


def _frame_payloads(
    frame_manifest: dict[str, Any],
    frame_package: Path,
    scene_duration_ms: int,
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]], dict[str, bytes]]:
    inventory_binding = frame_manifest.get("frame_inventory")
    inventory_id, inventory_relative, inventory_sha = _binding(inventory_binding, "frame_inventory")
    normalized_inventory, inventory_path = _safe_file(frame_package, inventory_relative, "frame_inventory.path")
    if normalized_inventory != FRAME_INVENTORY:
        raise FramePreviewError("FRAME_INVENTORY_PATH", "frame inventory must use the canonical path", "frame_inventory.path")
    inventory, inventory_bytes = _canonical_object(inventory_path, "frame_inventory")
    if inventory.get("id") != inventory_id or _sha(inventory_bytes) != inventory_sha:
        raise FramePreviewError("FRAME_INVENTORY_BINDING", "frame inventory binding is stale", "frame_inventory")

    frame_count = _positive_int(frame_manifest.get("frame_count"), "frame_count", MAX_FRAME_COUNT)
    fps_num = _positive_int(frame_manifest.get("fps_num"), "fps_num", 1_000_000)
    fps_den = _positive_int(frame_manifest.get("fps_den"), "fps_den", 1_000_000)
    if (
        inventory.get("frame_count") != frame_count
        or inventory.get("fps_num") != fps_num
        or inventory.get("fps_den") != fps_den
        or inventory.get("time_unit") != "milliseconds"
        or inventory.get("renderer_job_ref") != frame_manifest.get("source_renderer_job", {}).get("id")
    ):
        raise FramePreviewError("FRAME_INVENTORY_BINDING", "frame inventory metadata differs from frame manifest", "frame_inventory")

    raw_frames = inventory.get("frames")
    if not isinstance(raw_frames, list) or len(raw_frames) != frame_count:
        raise FramePreviewError("FRAME_INVENTORY_SCHEMA", "frame inventory count is invalid", "frame_inventory.frames")
    payloads: dict[str, bytes] = {FRAME_INVENTORY: inventory_bytes}
    player_frames: list[dict[str, Any]] = []
    previous_end: tuple[int, int] | None = None
    total = len(inventory_bytes)

    for index, raw in enumerate(raw_frames):
        if not isinstance(raw, dict) or raw.get("index") != index:
            raise FramePreviewError("FRAME_ORDER", "frame inventory indices must be contiguous", f"frames[{index}]")
        relative = raw.get("path")
        sha256 = raw.get("sha256")
        size = raw.get("size")
        if not isinstance(relative, str) or not relative.startswith("frames/"):
            raise FramePreviewError("FRAME_PATH", "frame path must be beneath frames/", f"frames[{index}].path")
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise FramePreviewError("FRAME_SHA256", "frame checksum is invalid", f"frames[{index}].sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise FramePreviewError("FRAME_SIZE", "frame size is invalid", f"frames[{index}].size")
        normalized, path = _safe_file(frame_package, relative, f"frames[{index}].path")
        payload = path.read_bytes()
        if len(payload) != size or _sha(payload) != sha256:
            raise FramePreviewError("FRAME_FILE_MISMATCH", "frame bytes do not match inventory", normalized)

        start_num = _nonnegative_int(raw.get("start_time_num"), f"frames[{index}].start_time_num")
        end_num = _positive_int(raw.get("end_time_num"), f"frames[{index}].end_time_num")
        time_den = _positive_int(raw.get("time_den"), f"frames[{index}].time_den", 1_000_000)
        if end_num <= start_num:
            raise FramePreviewError("FRAME_TIME", "frame interval must be non-empty", f"frames[{index}]")
        if previous_end is not None:
            previous_num, previous_den = previous_end
            if start_num * previous_den != previous_num * time_den:
                raise FramePreviewError("FRAME_TIME", "frame intervals must be contiguous", f"frames[{index}]")
        previous_end = (end_num, time_den)
        player_frames.append(
            {
                "index": index,
                "path": normalized,
                "sha256": sha256,
                "start_time_num": start_num,
                "end_time_num": end_num,
                "time_den": time_den,
            }
        )
        payloads[normalized] = payload
        total += len(payload)
        if total > MAX_PACKAGE_BYTES:
            raise FramePreviewError("PACKAGE_SIZE_LIMIT", "copied frame bytes exceed package limit", "frames")

    assert previous_end is not None
    final_num, final_den = previous_end
    if final_num != scene_duration_ms * final_den:
        raise FramePreviewError(
            "FRAME_DURATION_MISMATCH",
            "final rational frame boundary does not equal the audio-preview scene duration",
            "frame_inventory.frames",
        )
    return inventory, inventory_bytes, player_frames, payloads


def _audio_payload(audio_manifest: dict[str, Any], audio_package: Path) -> tuple[dict[str, Any], bytes, str]:
    audio = audio_manifest.get("audio")
    if not isinstance(audio, dict):
        raise FramePreviewError("AUDIO_SCHEMA", "audio-preview audio binding is missing", "audio")
    relative, sha256, size = audio.get("path"), audio.get("sha256"), audio.get("size")
    if not isinstance(relative, str):
        raise FramePreviewError("AUDIO_SCHEMA", "audio.path is invalid", "audio.path")
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
        raise FramePreviewError("AUDIO_SCHEMA", "audio.sha256 is invalid", "audio.sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise FramePreviewError("AUDIO_SCHEMA", "audio.size is invalid", "audio.size")
    normalized, path = _safe_file(audio_package, relative, "audio.path")
    payload = path.read_bytes()
    if len(payload) != size or _sha(payload) != sha256:
        raise FramePreviewError("AUDIO_FILE_MISMATCH", "WAV bytes do not match audio-preview manifest", normalized)
    return audio, payload, normalized


def _player_data(
    frame_manifest: dict[str, Any],
    audio_manifest: dict[str, Any],
    frames: list[dict[str, Any]],
    audio_path: str,
) -> dict[str, Any]:
    audio = audio_manifest["audio"]
    canvas = frame_manifest["canvas"]
    return {
        "width": canvas["width"],
        "height": canvas["height"],
        "scene_duration_ms": audio_manifest["scene_duration_ms"],
        "offset_ms": audio_manifest["offset_ms"],
        "audio_duration_ms": audio["duration_ms"],
        "audio_path": audio_path,
        "fps_num": frame_manifest["fps_num"],
        "fps_den": frame_manifest["fps_den"],
        "frames": frames,
    }


def _preview_data_bytes(data: dict[str, Any]) -> bytes:
    return b"window.__FRAME_PREVIEW__=" + canonical_json(data) + b";\n"


def _html_bytes() -> bytes:
    csp = (
        "default-src 'none'; img-src 'self'; media-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'none'; object-src 'none'; frame-src 'none'; font-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{html.escape(csp, quote=True)}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline Rendered Frame Preview</title>
<link rel="stylesheet" href="{STYLE_CSS}">
</head>
<body>
<main id="preview">
<section id="stage" aria-label="rendered paper theater stage">
<img id="frame" alt="rendered paper theater frame">
</section>
<audio id="audio" preload="metadata"></audio>
<section id="controls" aria-label="preview controls">
<button id="play" type="button">Play</button>
<button id="pause" type="button">Pause</button>
<button id="restart" type="button">Restart</button>
<input id="scrub" type="range" min="0" step="1" value="0" aria-label="timeline">
<output id="current">0</output>
</section>
</main>
<script src="{PREVIEW_DATA_JS}"></script>
<script src="{PLAYER_JS}"></script>
</body>
</html>
"""
    return document.encode("utf-8")


def _css_bytes() -> bytes:
    return (
        "html,body{margin:0;min-height:100%;background:#f4f4f4;color:#111;font-family:sans-serif}"
        "body{display:grid;place-items:center}#preview{width:min(96vw,1200px)}"
        "#stage{position:relative;width:100%;aspect-ratio:var(--canvas-ratio,16/9);overflow:hidden;"
        "background:transparent;border:1px solid #bbb}#frame{display:block;width:100%;height:100%;"
        "object-fit:contain;opacity:1;transform:scale(1);transition:opacity 90ms linear,transform 90ms ease-out}"
        "#frame.frame-change{opacity:.94;transform:scale(1.002)}"
        "#controls{display:grid;grid-template-columns:auto auto auto 1fr auto;gap:.5rem;align-items:center;padding:.75rem 0}"
        "button,input{font:inherit}output{min-width:6ch;text-align:right}\n"
    ).encode("utf-8")


def _js_bytes() -> bytes:
    script = r"""(()=>{'use strict';
const data=window.__FRAME_PREVIEW__;
if(!data||!Array.isArray(data.frames)||data.frames.length===0){throw new Error('preview data missing');}
const stage=document.getElementById('stage');
const image=document.getElementById('frame');
const audio=document.getElementById('audio');
const scrub=document.getElementById('scrub');
const current=document.getElementById('current');
stage.style.setProperty('--canvas-ratio',`${data.width}/${data.height}`);
audio.src=data.audio_path;
scrub.max=String(data.scene_duration_ms);
const audioStart=Math.max(0,data.offset_ms);
const audioEnd=Math.min(data.scene_duration_ms,data.offset_ms+data.audio_duration_ms);
let animation=0;
let mode='paused';
let sceneValue=0;
let wallStart=0;
let wallScene=0;
let shownSha='';
function bounded(ms){return Math.max(0,Math.min(data.scene_duration_ms,ms));}
function frameAt(ms){
  if(ms>=data.scene_duration_ms){return data.frames[data.frames.length-1];}
  let low=0,high=data.frames.length-1;
  while(low<=high){
    const middle=(low+high)>>1;
    const frame=data.frames[middle];
    const scaled=ms*frame.time_den;
    if(scaled<frame.start_time_num){high=middle-1;}
    else if(scaled>=frame.end_time_num){low=middle+1;}
    else{return frame;}
  }
  return data.frames[Math.max(0,Math.min(data.frames.length-1,low))];
}
function render(ms){
  const value=bounded(ms);
  const frame=frameAt(value);
  if(frame.sha256!==shownSha){
    image.classList.remove('frame-change');
    void image.offsetWidth;
    image.src=frame.path;
    image.dataset.sha256=frame.sha256;
    image.classList.add('frame-change');
    shownSha=frame.sha256;
  }
  const whole=Math.floor(value);
  scrub.value=String(whole);
  current.value=String(whole);
  sceneValue=value;
  return value;
}
function currentScene(){
  if(mode==='audio'){return bounded(audio.currentTime*1000+data.offset_ms);}
  if(mode==='pre'||mode==='post'){return bounded(wallScene+performance.now()-wallStart);}
  return sceneValue;
}
function stopAt(ms){
  audio.pause();audio.muted=false;cancelAnimationFrame(animation);mode='paused';render(ms);
}
function tick(){
  const value=currentScene();render(value);
  if(value>=data.scene_duration_ms){stopAt(data.scene_duration_ms);return;}
  if(mode==='pre'&&value>=audioStart){startAudio(audioStart);return;}
  if(mode!=='paused'){animation=requestAnimationFrame(tick);}
}
function startWall(nextMode,from){
  audio.pause();audio.muted=false;cancelAnimationFrame(animation);
  mode=nextMode;wallScene=bounded(from);wallStart=performance.now();animation=requestAnimationFrame(tick);
}
function audioPosition(scene){
  return Math.max(0,Math.min(data.audio_duration_ms/1000,(scene-data.offset_ms)/1000));
}
function startAudio(scene){
  audio.pause();audio.muted=false;audio.currentTime=audioPosition(scene);cancelAnimationFrame(animation);mode='audio';
  audio.play().then(()=>{animation=requestAnimationFrame(tick);}).catch(()=>{stopAt(scene);});
}
function startPre(scene){
  audio.pause();audio.currentTime=0;audio.muted=true;cancelAnimationFrame(animation);
  audio.play().then(()=>{mode='pre';wallScene=bounded(scene);wallStart=performance.now();animation=requestAnimationFrame(tick);})
    .catch(()=>{startWall('pre',scene);});
}
function playFrom(scene){
  const target=scene>=data.scene_duration_ms?0:bounded(scene);
  if(target<audioStart){startPre(target);return;}
  if(target<audioEnd){startAudio(target);return;}
  startWall('post',target);
}
function seekScene(ms){
  const target=bounded(ms);audio.pause();audio.muted=false;audio.currentTime=audioPosition(target);
  cancelAnimationFrame(animation);mode='paused';render(target);
}
document.getElementById('play').addEventListener('click',()=>playFrom(currentScene()));
document.getElementById('pause').addEventListener('click',()=>stopAt(currentScene()));
document.getElementById('restart').addEventListener('click',()=>stopAt(0));
scrub.addEventListener('input',()=>seekScene(Number(scrub.value)));
audio.addEventListener('timeupdate',()=>{if(mode==='audio'){render(currentScene());}});
audio.addEventListener('ended',()=>{
  if(mode!=='audio'){return;}
  const end=bounded(data.offset_ms+data.audio_duration_ms);
  if(end<data.scene_duration_ms){startWall('post',end);}else{stopAt(data.scene_duration_ms);}
});
seekScene(0);
})();"""
    return (script + "\n").encode("utf-8")


def _cross_validate(
    frame_manifest: dict[str, Any],
    audio_manifest: dict[str, Any],
    render_plan: dict[str, Any],
) -> int:
    canvas = frame_manifest.get("canvas")
    if not isinstance(canvas, dict):
        raise FramePreviewError("CANVAS_SCHEMA", "frame-render canvas is missing", "canvas")
    width = _positive_int(canvas.get("width"), "canvas.width", 8192)
    height = _positive_int(canvas.get("height"), "canvas.height", 8192)
    if audio_manifest.get("width") != width or audio_manifest.get("height") != height:
        raise FramePreviewError("CANVAS_MISMATCH", "audio-preview dimensions differ from rendered frames", "canvas")
    if render_plan.get("width") != width or render_plan.get("height") != height:
        raise FramePreviewError("RENDER_PLAN_CANVAS_MISMATCH", "render-plan dimensions differ from rendered frames", "canvas")
    if frame_manifest.get("intent") != audio_manifest.get("intent") or render_plan.get("intent") != frame_manifest.get("intent"):
        raise FramePreviewError("INTENT_MISMATCH", "source package intents differ", "intent")
    audio_license = audio_manifest.get("audio", {}).get("license_status") if isinstance(audio_manifest.get("audio"), dict) else None
    if (
        frame_manifest.get("audio_license_status") != audio_license
        or render_plan.get("audio_license_status") != audio_license
    ):
        raise FramePreviewError("AUDIO_LICENSE_MISMATCH", "source audio license states differ", "audio_license_status")
    scene_duration = _positive_int(audio_manifest.get("scene_duration_ms"), "scene_duration_ms")
    if render_plan.get("scene_duration_ms") != scene_duration:
        raise FramePreviewError("DURATION_MISMATCH", "render-plan and audio-preview scene durations differ", "scene_duration_ms")
    frame_audio = frame_manifest.get("audio_placement")
    plan_audio = render_plan.get("audio_placement")
    if not isinstance(frame_audio, dict) or not isinstance(plan_audio, dict) or frame_audio != plan_audio:
        raise FramePreviewError("AUDIO_PLACEMENT_MISMATCH", "frame-render and render-plan audio placement differ", "audio_placement")
    if frame_audio.get("offset_ms") != audio_manifest.get("offset_ms"):
        raise FramePreviewError("AUDIO_OFFSET_MISMATCH", "audio offset differs across source packages", "offset_ms")
    return scene_duration


def _build_expected(
    frame_render_manifest: Path,
    audio_preview_manifest: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
) -> tuple[dict[str, Any], dict[str, bytes], set[Path]]:
    (
        frame_manifest,
        audio_manifest,
        render_plan,
        frame_relative,
        audio_relative,
        frame_package,
        audio_package,
        sources,
    ) = _validated_sources(
        frame_render_manifest,
        audio_preview_manifest,
        frame_render_root,
        renderer_job_root,
        render_plan_root,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
    )
    scene_duration = _cross_validate(frame_manifest, audio_manifest, render_plan)
    inventory, inventory_bytes, frames, frame_payloads = _frame_payloads(frame_manifest, frame_package, scene_duration)
    audio, wav_bytes, audio_relative_path = _audio_payload(audio_manifest, audio_package)
    if sum(len(payload) for payload in frame_payloads.values()) + len(wav_bytes) > MAX_PACKAGE_BYTES:
        raise FramePreviewError("PACKAGE_SIZE_LIMIT", "copied media exceeds package limit", "files")

    player_data = _player_data(frame_manifest, audio_manifest, frames, audio_relative_path)
    generated: dict[str, bytes] = {
        **frame_payloads,
        audio_relative_path: wav_bytes,
        INDEX_HTML: _html_bytes(),
        PREVIEW_DATA_JS: _preview_data_bytes(player_data),
        PLAYER_JS: _js_bytes(),
        STYLE_CSS: _css_bytes(),
    }
    frame_manifest_path = frame_package / FRAME_RENDER_MANIFEST
    audio_manifest_path = audio_package / "audio-preview-manifest.json"
    core = {
        "kind": "paper-theater-frame-preview-package",
        "schema_version": "1.0",
        "source_frame_render": {
            "id": frame_manifest["id"],
            "path": frame_relative,
            "sha256": _sha(frame_manifest_path.read_bytes()),
        },
        "source_audio_preview": {
            "id": audio_manifest["id"],
            "path": audio_relative,
            "sha256": _sha(audio_manifest_path.read_bytes()),
        },
        "intent": frame_manifest["intent"],
        "audio_license_status": audio["license_status"],
        "canvas": frame_manifest["canvas"],
        "scene_duration_ms": scene_duration,
        "fps_num": frame_manifest["fps_num"],
        "fps_den": frame_manifest["fps_den"],
        "frame_count": frame_manifest["frame_count"],
        "audio_placement": frame_manifest["audio_placement"],
        "frame_inventory": {
            "id": inventory["id"],
            "path": FRAME_INVENTORY,
            "sha256": _sha(inventory_bytes),
        },
        "audio": {
            "path": audio_relative_path,
            "sha256": audio["sha256"],
            "size": audio["size"],
            "duration_ms": audio["duration_ms"],
            "channels": audio.get("channels"),
            "sample_rate": audio.get("sample_rate"),
            "bits_per_sample": audio.get("bits_per_sample"),
        },
        "playback_policy": {
            "clock": "audio-current-time-with-wall-clock-before-and-after-audio",
            "frame_selection": "rational-boundary-search-state-at-frame-start",
            "final_partial_frame": "preserved-from-source-inventory",
            "transition": "css-only-on-frame-checksum-change",
            "autoplay": False,
            "network": False,
        },
        "media_copied_unchanged": True,
        "video_created": False,
    }
    package_id = content_identifier("paper-theater-frame-preview-package", core, 20)
    files = [
        {"path": relative, "sha256": _sha(payload), "size": len(payload)}
        for relative, payload in sorted(generated.items())
    ]
    manifest = {"id": package_id, **core, "files": files}
    generated[FRAME_PREVIEW_MANIFEST] = _json_bytes(manifest)
    return manifest, generated, sources


def _output_candidate(output_root: Path) -> Path:
    expanded = _reject_lexical(output_root, "output_root")
    _reject_symlink_components(expanded, "output_root")
    if expanded.exists() and not expanded.is_dir():
        raise FramePreviewError("ROOT_TYPE", "output_root must be a directory", "output_root")
    return expanded.resolve(strict=False)


def _reject_output_overlap(output_root: Path, sources: set[Path]) -> None:
    candidate = _output_candidate(output_root)
    for source in sorted(sources, key=str):
        resolved = source.resolve(strict=False)
        if candidate == resolved or _within(resolved, candidate) or _within(candidate, resolved):
            raise FramePreviewError(
                "OUTPUT_OVERLAPS_SOURCE",
                f"output_root overlaps source package {resolved}",
                "output_root",
            )


def _write_package(output_root: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> bool:
    root_path = _reject_lexical(output_root, "output_root")
    _reject_symlink_components(root_path, "output_root")
    if root_path.exists() and not root_path.is_dir():
        raise FramePreviewError("ROOT_TYPE", "output_root must be a directory", "output_root")
    root_path.mkdir(parents=True, exist_ok=True)
    root = root_path.resolve()
    destination = root / manifest["id"]
    expected = set(files)
    if destination.is_symlink():
        raise FramePreviewError("OUTPUT_SYMLINK", "frame preview destination is a symlink", "output_root")
    if destination.exists():
        if not destination.is_dir():
            raise FramePreviewError("OUTPUT_CONFLICT", "frame preview destination is not a directory", "output_root")
        actual: set[str] = set()
        for candidate in destination.rglob("*"):
            if candidate.is_symlink():
                raise FramePreviewError("OUTPUT_SYMLINK", "existing package contains a symlink", str(candidate))
            if candidate.is_file():
                actual.add(candidate.relative_to(destination).as_posix())
        if actual != expected:
            raise FramePreviewError("OUTPUT_CONFLICT", "existing package file set differs", "output_root")
        for relative, payload in files.items():
            if destination.joinpath(*safe_relative_path(relative).parts).read_bytes() != payload:
                raise FramePreviewError("OUTPUT_CONFLICT", f"existing file differs: {relative}", relative)
        return False

    staging = root / f".{manifest['id']}.tmp"
    if staging.exists():
        if staging.is_symlink():
            raise FramePreviewError("STAGING_CONFLICT", "staging path is a symlink", "output_root")
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


def build_frame_preview_package(
    frame_render_manifest: Path,
    audio_preview_manifest: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
    output_root: Path,
    *,
    write: bool = False,
) -> dict[str, Any]:
    manifest, files, sources = _build_expected(
        frame_render_manifest,
        audio_preview_manifest,
        frame_render_root,
        renderer_job_root,
        render_plan_root,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
    )
    written = False
    if write:
        _reject_output_overlap(output_root, sources)
        written = _write_package(output_root, manifest, files)
    return {
        "ok": True,
        "frame_preview": manifest,
        "file_count": len(files),
        "written": written,
        "package_path": manifest["id"],
    }


def check_frame_preview_package(
    manifest_path: Path,
    output_root: Path,
    frame_render_root: Path,
    renderer_job_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
) -> dict[str, Any]:
    root = _root(output_root, must_exist=True, field="output_root")
    _, resolved = _relative_file(manifest_path, root, "frame_preview_manifest")
    manifest, payload = _canonical_object(resolved, "frame_preview_manifest")
    package_id = manifest.get("id")
    if not isinstance(package_id, str):
        raise FramePreviewError("MANIFEST_SCHEMA", "frame-preview package ID is missing", "id")
    canonical = root / package_id / FRAME_PREVIEW_MANIFEST
    if resolved != canonical.resolve():
        raise FramePreviewError("MANIFEST_LOCATION", "frame-preview manifest path is not canonical", str(manifest_path))
    frame_binding = manifest.get("source_frame_render")
    audio_binding = manifest.get("source_audio_preview")
    _frame_id, frame_relative, _frame_sha = _binding(frame_binding, "source_frame_render")
    _audio_id, audio_relative, _audio_sha = _binding(audio_binding, "source_audio_preview")
    frame_root = _root(frame_render_root, must_exist=True, field="frame_render_root")
    audio_preview_base = _root(audio_preview_root, must_exist=True, field="audio_preview_root")
    _, frame_manifest = _safe_file(frame_root, frame_relative, "source_frame_render.path")
    _, audio_manifest = _safe_file(audio_preview_base, audio_relative, "source_audio_preview.path")
    expected_manifest, expected_files, sources = _build_expected(
        frame_manifest,
        audio_manifest,
        frame_root,
        renderer_job_root,
        render_plan_root,
        audio_preview_base,
        preview_root,
        package_root,
        audio_root,
    )
    _reject_output_overlap(root, sources)
    if manifest != expected_manifest or payload != expected_files[FRAME_PREVIEW_MANIFEST]:
        raise FramePreviewError("MANIFEST_BINDING_MISMATCH", "frame-preview manifest is stale or not canonical", str(manifest_path))
    destination = root / package_id
    expected_names, actual_names = set(expected_files), set()
    for candidate in destination.rglob("*"):
        if candidate.is_symlink():
            raise FramePreviewError("PACKAGE_SYMLINK", "frame-preview package contains a symlink", str(candidate))
        if candidate.is_file():
            actual_names.add(candidate.relative_to(destination).as_posix())
    if actual_names != expected_names:
        raise FramePreviewError(
            "FILE_SET_MISMATCH",
            f"missing={sorted(expected_names - actual_names)}; extra={sorted(actual_names - expected_names)}",
            str(destination),
        )
    for relative, expected in expected_files.items():
        if destination.joinpath(*safe_relative_path(relative).parts).read_bytes() != expected:
            raise FramePreviewError("FILE_MISMATCH", f"frame-preview file was modified: {relative}", relative)
    return {
        "ok": True,
        "frame_preview": manifest,
        "file_count": len(expected_files),
        "frame_count": manifest["frame_count"],
    }


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--frame-render-root", type=Path, required=True)
    parser.add_argument("--renderer-job-root", type=Path, required=True)
    parser.add_argument("--render-plan-root", type=Path, required=True)
    parser.add_argument("--audio-preview-root", type=Path, required=True)
    parser.add_argument("--preview-root", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ai_illustration.frame_preview")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("frame_render_manifest", type=Path)
    build.add_argument("audio_preview_manifest", type=Path)
    _common_arguments(build)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument("--write", action="store_true")

    check = subparsers.add_parser("check")
    check.add_argument("frame_preview_manifest", type=Path)
    check.add_argument("--output-root", type=Path, required=True)
    _common_arguments(check)
    return parser


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.buffer.write(_json_bytes(value))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_frame_preview_package(
                args.frame_render_manifest,
                args.audio_preview_manifest,
                args.frame_render_root,
                args.renderer_job_root,
                args.render_plan_root,
                args.audio_preview_root,
                args.preview_root,
                args.package_root,
                args.audio_root,
                args.output_root,
                write=args.write,
            )
            _emit(result)
            print(
                f"frame preview ready: {result['frame_preview']['id']} "
                f"({result['frame_preview']['frame_count']} frames, written={result['written']})",
                file=sys.stderr,
            )
            return 0
        result = check_frame_preview_package(
            args.frame_preview_manifest,
            args.output_root,
            args.frame_render_root,
            args.renderer_job_root,
            args.render_plan_root,
            args.audio_preview_root,
            args.preview_root,
            args.package_root,
            args.audio_root,
        )
        _emit(result)
        print(
            f"frame preview valid: {result['frame_preview']['id']} ({result['frame_count']} frames)",
            file=sys.stderr,
        )
        return 0
    except FramePreviewError as exc:
        _emit({"ok": False, "error": exc.to_dict()})
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
