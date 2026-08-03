"""Deterministic, fully offline WAV-bound paper-theater preview packaging."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import shutil
import struct
from typing import Any

from .naming import SHA256_RE, canonical_json, content_identifier, safe_relative_path
from .preview import PREVIEW_MANIFEST, PreviewError, check_preview_package

AUDIO_PREVIEW_MANIFEST = "audio-preview-manifest.json"
INDEX_HTML = "index.html"
PLAYER_JS = "player.js"
STYLE_CSS = "style.css"
DURATION_POLICIES = ("exact", "audio-at-least-scene", "scene-at-least-audio")
LICENSE_STATUSES = ("unreviewed", "reviewing", "approved", "rejected")
MAX_AUDIO_BYTES = 512 * 1024 * 1024
MAX_CHANNELS = 8
MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 192_000
MAX_DURATION_MS = 24 * 60 * 60 * 1000
MAX_OFFSET_MS = MAX_DURATION_MS
DURATION_TOLERANCE_MS = 5


@dataclass
class AudioPreviewError(ValueError):
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


def _root(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise AudioPreviewError("SYMLINK_ROOT", f"{field} must not be a symlink", field)
    if must_exist and not expanded.is_dir():
        raise AudioPreviewError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise AudioPreviewError("ROOT_TYPE", f"{field} must be a directory", field)
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
        raise AudioPreviewError("UNSAFE_PATH", str(exc), field) from exc
    normalized = safe.as_posix()
    candidate = root.joinpath(*safe.parts)
    current = root
    for part in safe.parts:
        current = current / part
        if current.is_symlink():
            raise AudioPreviewError("PATH_SYMLINK", f"{field} contains a symlink component", field)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AudioPreviewError("FILE_MISSING", str(exc), field) from exc
    if not _within(root, resolved):
        raise AudioPreviewError("PATH_ESCAPE", f"{field} escapes configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise AudioPreviewError("FILE_TYPE", f"{field} must be a regular file", field)
    return normalized, resolved


def _lexical_file_under_root(path: Path, root: Path, field: str) -> tuple[str, Path]:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise AudioPreviewError("UNSAFE_PATH", f"{field} must not contain parent traversal", field)
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        relative = lexical.relative_to(root).as_posix()
    except ValueError as exc:
        raise AudioPreviewError("PATH_ESCAPE", f"{field} must be beneath its configured root", field) from exc
    return _safe_existing_file(root, relative, field)


def _load_object(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise AudioPreviewError("DUPLICATE_KEY", f"duplicate JSON key: {key}", str(path))
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except AudioPreviewError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AudioPreviewError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise AudioPreviewError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _bounded_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise AudioPreviewError("INTEGER_RANGE", f"{field} must be from {minimum} to {maximum}", field)
    return value


def _parse_wav(payload: bytes) -> dict[str, Any]:
    if not payload:
        raise AudioPreviewError("WAV_EMPTY", "WAV input is empty", "audio")
    if len(payload) > MAX_AUDIO_BYTES:
        raise AudioPreviewError("WAV_TOO_LARGE", f"WAV exceeds {MAX_AUDIO_BYTES} bytes", "audio")
    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise AudioPreviewError("WAV_HEADER", "input must be a little-endian RIFF/WAVE file", "audio")
    declared_size = struct.unpack_from("<I", payload, 4)[0]
    if declared_size + 8 != len(payload):
        raise AudioPreviewError("WAV_RIFF_SIZE", "RIFF size does not exactly match file length", "audio")

    position = 12
    fmt: bytes | None = None
    data_size: int | None = None
    data_offset: int | None = None
    chunks: list[str] = []
    while position < len(payload):
        if position + 8 > len(payload):
            raise AudioPreviewError("WAV_TRUNCATED", "truncated WAV chunk header", "audio")
        chunk_id = payload[position : position + 4]
        chunk_size = struct.unpack_from("<I", payload, position + 4)[0]
        start = position + 8
        end = start + chunk_size
        padded_end = end + (chunk_size & 1)
        if end > len(payload) or padded_end > len(payload):
            raise AudioPreviewError("WAV_TRUNCATED", "truncated WAV chunk payload", "audio")
        try:
            chunk_name = chunk_id.decode("ascii")
        except UnicodeDecodeError as exc:
            raise AudioPreviewError("WAV_CHUNK_ID", "WAV chunk ID must be ASCII", "audio") from exc
        chunks.append(chunk_name)
        if chunk_id == b"fmt ":
            if fmt is not None:
                raise AudioPreviewError("WAV_DUPLICATE_FMT", "WAV contains more than one fmt chunk", "audio")
            fmt = payload[start:end]
        elif chunk_id == b"data":
            if data_size is not None:
                raise AudioPreviewError("WAV_DUPLICATE_DATA", "WAV contains more than one data chunk", "audio")
            if fmt is None:
                raise AudioPreviewError("WAV_ORDER", "fmt chunk must precede data chunk", "audio")
            data_size = chunk_size
            data_offset = start
        position = padded_end
    if position != len(payload):
        raise AudioPreviewError("WAV_TRAILING", "unexpected trailing bytes after WAV chunks", "audio")
    if fmt is None or data_size is None or data_offset is None:
        raise AudioPreviewError("WAV_REQUIRED_CHUNK", "WAV requires exactly one fmt and one data chunk", "audio")
    if len(fmt) < 16:
        raise AudioPreviewError("WAV_FMT", "fmt chunk is shorter than 16 bytes", "audio")

    format_tag, channels, sample_rate, byte_rate, block_align, bits_per_sample = struct.unpack_from("<HHIIHH", fmt, 0)
    declared_tag = format_tag
    if format_tag == 0xFFFE:
        if len(fmt) < 40:
            raise AudioPreviewError("WAV_EXTENSIBLE", "extensible fmt chunk is incomplete", "audio")
        extension_size = struct.unpack_from("<H", fmt, 16)[0]
        if extension_size < 22 or len(fmt) < 18 + extension_size:
            raise AudioPreviewError("WAV_EXTENSIBLE", "extensible fmt extension is incomplete", "audio")
        subtype = fmt[24:40]
        expected_suffix = b"\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        if subtype[4:] != expected_suffix:
            raise AudioPreviewError("WAV_FORMAT", "unsupported extensible WAV subtype", "audio")
        format_tag = struct.unpack_from("<I", subtype, 0)[0]
    if format_tag not in {1, 3}:
        raise AudioPreviewError("WAV_COMPRESSED", "only uncompressed PCM or IEEE-float WAV is supported", "audio")
    if channels < 1 or channels > MAX_CHANNELS:
        raise AudioPreviewError("WAV_CHANNELS", f"channels must be from 1 to {MAX_CHANNELS}", "audio")
    if sample_rate < MIN_SAMPLE_RATE or sample_rate > MAX_SAMPLE_RATE:
        raise AudioPreviewError("WAV_SAMPLE_RATE", f"sample rate must be from {MIN_SAMPLE_RATE} to {MAX_SAMPLE_RATE}", "audio")
    allowed_bits = {8, 16, 24, 32} if format_tag == 1 else {32, 64}
    if bits_per_sample not in allowed_bits:
        raise AudioPreviewError("WAV_SAMPLE_WIDTH", "unsupported sample width for WAV encoding", "audio")
    expected_align = channels * (bits_per_sample // 8)
    if block_align != expected_align or byte_rate != sample_rate * block_align:
        raise AudioPreviewError("WAV_RATE_BINDING", "block alignment or byte rate is inconsistent", "audio")
    if data_size <= 0 or data_size % block_align:
        raise AudioPreviewError("WAV_DATA_SIZE", "audio data must contain complete non-empty frames", "audio")
    frame_count = data_size // block_align
    duration_ms = (frame_count * 1000 + sample_rate // 2) // sample_rate
    if duration_ms <= 0 or duration_ms > MAX_DURATION_MS:
        raise AudioPreviewError("WAV_DURATION", f"duration must be from 1 to {MAX_DURATION_MS} ms", "audio")
    encoding = "pcm" if format_tag == 1 else "ieee-float"
    return {
        "container": "wav",
        "riff_format": "RIFF/WAVE",
        "declared_format_tag": declared_tag,
        "format_tag": format_tag,
        "encoding": encoding,
        "channels": channels,
        "sample_rate": sample_rate,
        "bits_per_sample": bits_per_sample,
        "block_align": block_align,
        "byte_rate": byte_rate,
        "frame_count": frame_count,
        "data_offset": data_offset,
        "data_size": data_size,
        "duration_ms": duration_ms,
        "duration_frames": frame_count,
        "duration_rate": sample_rate,
        "chunks": chunks,
    }


def _validate_sync(scene_duration_ms: Any, audio_duration_ms: int, offset_ms: Any, policy: str) -> tuple[int, int]:
    scene_duration = _bounded_integer(scene_duration_ms, "duration_ms", 1, MAX_DURATION_MS)
    offset = _bounded_integer(offset_ms, "offset_ms", -MAX_OFFSET_MS, MAX_OFFSET_MS)
    if policy not in DURATION_POLICIES:
        raise AudioPreviewError("DURATION_POLICY", f"duration_policy must be one of {DURATION_POLICIES}", "duration_policy")
    synchronized_end = offset + audio_duration_ms
    if synchronized_end <= 0 or offset >= scene_duration:
        raise AudioPreviewError("NO_SYNC_OVERLAP", "audio and scene timelines do not overlap", "offset_ms")
    if policy == "exact" and abs(synchronized_end - scene_duration) > DURATION_TOLERANCE_MS:
        raise AudioPreviewError("DURATION_MISMATCH", "synchronized audio end must match scene duration", "duration_policy")
    if policy == "audio-at-least-scene" and synchronized_end + DURATION_TOLERANCE_MS < scene_duration:
        raise AudioPreviewError("AUDIO_TOO_SHORT", "synchronized audio does not reach scene end", "duration_policy")
    if policy == "scene-at-least-audio" and synchronized_end - DURATION_TOLERANCE_MS > scene_duration:
        raise AudioPreviewError("AUDIO_TOO_LONG", "synchronized audio extends beyond scene end", "duration_policy")
    return offset, synchronized_end


def _preview_reference(preview_manifest: Path, preview_root: Path, package_root: Path) -> tuple[str, bytes, dict[str, Any], Path]:
    preview_root_resolved = _root(preview_root, must_exist=True, field="preview_root")
    normalized, resolved = _lexical_file_under_root(preview_manifest, preview_root_resolved, "preview_manifest")
    try:
        checked = check_preview_package(resolved, preview_root_resolved, package_root)
    except PreviewError as exc:
        raise AudioPreviewError(f"PREVIEW_{exc.code}", exc.message, exc.field or "preview_manifest") from exc
    preview = checked.get("preview")
    if not isinstance(preview, dict):
        raise AudioPreviewError("PREVIEW_RESULT", "preview validation result is malformed", "preview_manifest")
    preview_id = preview.get("id")
    if not isinstance(preview_id, str):
        raise AudioPreviewError("PREVIEW_RESULT", "preview ID is missing", "preview_manifest")
    expected = preview_root_resolved / preview_id / PREVIEW_MANIFEST
    if resolved != expected.resolve():
        raise AudioPreviewError("PREVIEW_LOCATION", "preview manifest path is not canonical", "preview_manifest")
    return normalized, resolved.read_bytes(), preview, expected.parent


def _copy_preview_assets(preview: dict[str, Any], source_dir: Path) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    source_assets = preview.get("assets")
    if not isinstance(source_assets, list):
        raise AudioPreviewError("PREVIEW_ASSETS", "preview assets are malformed", "assets")
    assets: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    for index, item in enumerate(source_assets):
        if not isinstance(item, dict):
            raise AudioPreviewError("PREVIEW_ASSETS", "preview asset is malformed", f"assets[{index}]")
        relative = item.get("path")
        expected_sha = item.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_sha, str) or not SHA256_RE.fullmatch(expected_sha):
            raise AudioPreviewError("PREVIEW_ASSETS", "preview asset binding is malformed", f"assets[{index}]")
        normalized, source = _safe_existing_file(source_dir, relative, f"assets[{index}].path")
        payload = source.read_bytes()
        if _sha(payload) != expected_sha:
            raise AudioPreviewError("PREVIEW_ASSET_MISMATCH", "preview asset checksum changed", normalized)
        payloads[normalized] = payload
        assets.append({"path": normalized, "sha256": expected_sha, "size": len(payload)})
    return sorted(assets, key=lambda item: item["path"]), payloads


def _player_data(core: dict[str, Any]) -> dict[str, Any]:
    return {
        "scene_duration_ms": core["scene_duration_ms"],
        "audio_duration_ms": core["audio"]["duration_ms"],
        "offset_ms": core["offset_ms"],
        "audio_path": core["audio"]["path"],
        "width": core["width"],
        "height": core["height"],
        "segments": core["segments"],
    }


def _html_bytes(core: dict[str, Any]) -> bytes:
    data = html.escape(canonical_json(_player_data(core)).decode("ascii"), quote=True)
    csp = (
        "default-src 'none'; img-src 'self'; media-src 'self'; script-src 'self'; style-src 'self'; "
        "connect-src 'none'; object-src 'none'; frame-src 'none'; font-src 'none'; "
        "base-uri 'none'; form-action 'none'"
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline Audio Paper Theater Preview</title>
<link rel="stylesheet" href="{STYLE_CSS}">
</head>
<body>
<main id="preview" data-preview="{data}">
<section id="stage" aria-label="paper theater stage">
<img id="boke" class="character" alt="boke">
<img id="tsukkomi" class="character" alt="tsukkomi">
</section>
<audio id="audio" preload="metadata" src="{core['audio']['path']}"></audio>
<section id="controls" aria-label="preview controls">
<button id="play" type="button">Play</button>
<button id="pause" type="button">Pause</button>
<button id="restart" type="button">Restart</button>
<input id="scrub" type="range" min="0" max="{core['scene_duration_ms']}" step="1" value="0" aria-label="timeline">
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
    return b"(()=>{'use strict';const root=document.getElementById('preview');const data=JSON.parse(root.dataset.preview);const audio=document.getElementById('audio');const stage=document.getElementById('stage');stage.style.setProperty('--canvas-ratio',`${data.width}/${data.height}`);const images={boke:document.getElementById('boke'),tsukkomi:document.getElementById('tsukkomi')};const scrub=document.getElementById('scrub');const current=document.getElementById('current');const audioStart=Math.max(0,data.offset_ms);const audioEnd=Math.min(data.scene_duration_ms,data.offset_ms+data.audio_duration_ms);let frame=0;let mode='paused';let sceneValue=0;let wallStart=0;let wallScene=0;function bounded(ms){return Math.max(0,Math.min(data.scene_duration_ms,Math.floor(ms)))}function segmentAt(ms){let result=data.segments[data.segments.length-1];const query=ms===data.scene_duration_ms?Math.max(0,ms-1):ms;for(const segment of data.segments){if(query>=segment.start_ms&&query<segment.end_ms){result=segment;break}}return result}function render(ms){const value=bounded(ms);const segment=segmentAt(value);for(const role of ['boke','tsukkomi']){const image=images[role];const state=segment[role];image.src=state.asset_path;image.className=`character slot-${segment.stage_slots[role]}`;image.dataset.sha256=state.png_sha256}scrub.value=String(value);current.value=String(value);sceneValue=value;return value}function currentScene(){if(mode==='audio')return bounded(audio.currentTime*1000+data.offset_ms);if(mode==='pre'||mode==='post')return bounded(wallScene+performance.now()-wallStart);return sceneValue}function stopAt(ms){audio.pause();audio.muted=false;cancelAnimationFrame(frame);mode='paused';render(ms)}function tick(){const value=currentScene();render(value);if(value>=data.scene_duration_ms){stopAt(data.scene_duration_ms);return}if(mode==='pre'&&value>=audioStart){startAudio(audioStart);return}if(mode!=='paused')frame=requestAnimationFrame(tick)}function startWall(nextMode,from){audio.pause();audio.muted=false;cancelAnimationFrame(frame);mode=nextMode;wallScene=bounded(from);wallStart=performance.now();frame=requestAnimationFrame(tick)}function audioPosition(scene){return Math.max(0,Math.min(data.audio_duration_ms/1000,(scene-data.offset_ms)/1000))}function startAudio(scene){audio.pause();audio.muted=false;audio.currentTime=audioPosition(scene);cancelAnimationFrame(frame);mode='audio';audio.play().then(()=>{frame=requestAnimationFrame(tick)}).catch(()=>{stopAt(scene)})}function startPre(scene){audio.pause();audio.currentTime=0;audio.muted=true;cancelAnimationFrame(frame);audio.play().then(()=>{mode='pre';wallScene=bounded(scene);wallStart=performance.now();frame=requestAnimationFrame(tick)}).catch(()=>{startWall('pre',scene)})}function playFrom(scene){const target=scene>=data.scene_duration_ms?0:bounded(scene);if(target<audioStart){startPre(target);return}if(target<audioEnd){startAudio(target);return}startWall('post',target)}function seekScene(ms){const target=bounded(ms);audio.pause();audio.muted=false;audio.currentTime=audioPosition(target);cancelAnimationFrame(frame);mode='paused';render(target)}document.getElementById('play').addEventListener('click',()=>playFrom(currentScene()));document.getElementById('pause').addEventListener('click',()=>stopAt(currentScene()));document.getElementById('restart').addEventListener('click',()=>stopAt(0));scrub.addEventListener('input',()=>seekScene(Number(scrub.value)));audio.addEventListener('timeupdate',()=>{if(mode==='audio')render(currentScene())});audio.addEventListener('seeked',()=>{if(mode==='paused')render(sceneValue)});audio.addEventListener('ended',()=>{if(mode!=='audio')return;const end=bounded(data.offset_ms+data.audio_duration_ms);if(end<data.scene_duration_ms)startWall('post',end);else stopAt(data.scene_duration_ms)});seekScene(0)})();\n"


def _build_expected(
    preview_manifest: Path,
    preview_root: Path,
    package_root: Path,
    audio_relative: str,
    audio_root: Path,
    *,
    offset_ms: int,
    duration_policy: str,
    audio_license_status: str,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    preview_relative, preview_bytes, preview, preview_dir = _preview_reference(preview_manifest, preview_root, package_root)
    audio_root_resolved = _root(audio_root, must_exist=True, field="audio_root")
    normalized_audio, audio_source = _safe_existing_file(audio_root_resolved, audio_relative, "audio")
    if Path(normalized_audio).suffix.lower() != ".wav":
        raise AudioPreviewError("AUDIO_EXTENSION", "audio input must use the .wav extension", "audio")
    try:
        declared_audio_size = audio_source.stat().st_size
    except OSError as exc:
        raise AudioPreviewError("AUDIO_STAT", str(exc), "audio") from exc
    if declared_audio_size <= 0:
        raise AudioPreviewError("WAV_EMPTY", "WAV input is empty", "audio")
    if declared_audio_size > MAX_AUDIO_BYTES:
        raise AudioPreviewError("WAV_TOO_LARGE", f"WAV exceeds {MAX_AUDIO_BYTES} bytes", "audio")
    with audio_source.open("rb") as handle:
        audio_bytes = handle.read(MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) != declared_audio_size or len(audio_bytes) > MAX_AUDIO_BYTES:
        raise AudioPreviewError("WAV_SIZE_CHANGED", "WAV size changed or exceeded the bounded read", "audio")
    facts = _parse_wav(audio_bytes)
    offset, synchronized_end = _validate_sync(preview.get("duration_ms"), facts["duration_ms"], offset_ms, duration_policy)
    if audio_license_status not in LICENSE_STATUSES:
        raise AudioPreviewError("AUDIO_LICENSE", f"audio_license_status must be one of {LICENSE_STATUSES}", "audio_license_status")
    intent = preview.get("intent")
    if intent == "production" and audio_license_status != "approved":
        raise AudioPreviewError("PRODUCTION_AUDIO_LICENSE", "production preview requires approved audio licensing", "audio_license_status")
    if intent not in {"evaluation", "production"}:
        raise AudioPreviewError("PREVIEW_INTENT", "preview intent is invalid", "intent")
    assets, asset_payloads = _copy_preview_assets(preview, preview_dir)
    audio_sha = _sha(audio_bytes)
    output_audio_path = f"audio/{audio_sha}.wav"
    audio_manifest = {
        "source_path": normalized_audio,
        "path": output_audio_path,
        "sha256": audio_sha,
        "size": len(audio_bytes),
        "license_status": audio_license_status,
        **facts,
    }
    core = {
        "kind": "paper-theater-audio-preview",
        "schema_version": "1.0",
        "source_preview_ref": preview.get("id"),
        "source_preview_path": preview_relative,
        "source_preview_sha256": _sha(preview_bytes),
        "scene_plan_ref": preview.get("scene_plan_ref"),
        "intent": intent,
        "width": preview.get("width"),
        "height": preview.get("height"),
        "scene_duration_ms": preview.get("duration_ms"),
        "offset_ms": offset,
        "duration_policy": duration_policy,
        "duration_tolerance_ms": DURATION_TOLERANCE_MS,
        "synchronized_audio_end_ms": synchronized_end,
        "clock": "audio-current-time",
        "roles": preview.get("roles"),
        "segments": preview.get("segments"),
        "assets": assets,
        "audio": audio_manifest,
    }
    package_id = content_identifier("paper-theater-audio-preview", core, 20)
    identified = {"id": package_id, **core}
    generated = {
        INDEX_HTML: _html_bytes(identified),
        PLAYER_JS: _js_bytes(),
        STYLE_CSS: _css_bytes(),
        output_audio_path: audio_bytes,
        **asset_payloads,
    }
    files = [
        {"path": path, "sha256": _sha(payload), "size": len(payload)}
        for path, payload in sorted(generated.items())
    ]
    manifest = {**identified, "files": files}
    generated[AUDIO_PREVIEW_MANIFEST] = _json_bytes(manifest)
    return manifest, generated


def _write_package(output_root: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> bool:
    root_path = output_root.expanduser()
    if root_path.is_symlink():
        raise AudioPreviewError("SYMLINK_ROOT", "output_root must not be a symlink", "output_root")
    root_path.mkdir(parents=True, exist_ok=True)
    root = root_path.resolve()
    destination = root / manifest["id"]
    if destination.is_symlink():
        raise AudioPreviewError("OUTPUT_SYMLINK", "audio preview destination must not be a symlink", "output_root")
    expected = set(files)
    if destination.exists():
        if not destination.is_dir():
            raise AudioPreviewError("OUTPUT_CONFLICT", "audio preview destination is not a directory", "output_root")
        actual = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
        if actual != expected:
            raise AudioPreviewError("OUTPUT_CONFLICT", "existing audio preview file set differs", "output_root")
        for relative, payload in files.items():
            candidate = destination.joinpath(*safe_relative_path(relative).parts)
            if candidate.is_symlink() or candidate.read_bytes() != payload:
                raise AudioPreviewError("OUTPUT_CONFLICT", f"existing file differs: {relative}", relative)
        return False

    staging = root / f".{manifest['id']}.tmp"
    if staging.exists():
        if staging.is_symlink():
            raise AudioPreviewError("STAGING_CONFLICT", "staging path is a symlink", "output_root")
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


def build_audio_preview_package(
    preview_manifest: Path,
    preview_root: Path,
    package_root: Path,
    audio_relative: str,
    audio_root: Path,
    output_root: Path,
    *,
    offset_ms: int,
    duration_policy: str,
    audio_license_status: str,
    write: bool = False,
) -> dict[str, Any]:
    manifest, files = _build_expected(
        preview_manifest,
        preview_root,
        package_root,
        audio_relative,
        audio_root,
        offset_ms=offset_ms,
        duration_policy=duration_policy,
        audio_license_status=audio_license_status,
    )
    written = _write_package(output_root, manifest, files) if write else False
    return {
        "ok": True,
        "audio_preview": manifest,
        "file_count": len(files),
        "written": written,
        "package_path": manifest["id"],
    }


def _manifest_location(manifest_path: Path, output_root: Path) -> tuple[Path, dict[str, Any], bytes]:
    root = _root(output_root, must_exist=True, field="output_root")
    _normalized, resolved = _lexical_file_under_root(manifest_path, root, "audio_preview_manifest")
    return root, _load_object(resolved), resolved.read_bytes()


def check_audio_preview_package(
    manifest_path: Path,
    output_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
) -> dict[str, Any]:
    root, manifest, payload = _manifest_location(manifest_path, output_root)
    if payload != _json_bytes(manifest):
        raise AudioPreviewError("MANIFEST_CANONICAL", "audio preview manifest JSON is not canonical", str(manifest_path))
    package_id = manifest.get("id")
    if not isinstance(package_id, str):
        raise AudioPreviewError("MANIFEST_SCHEMA", "audio preview ID is missing", "id")
    canonical = root / package_id / AUDIO_PREVIEW_MANIFEST
    if manifest_path.expanduser().resolve() != canonical.resolve():
        raise AudioPreviewError("MANIFEST_LOCATION", "audio preview manifest path is not canonical", str(manifest_path))
    source_preview_path = manifest.get("source_preview_path")
    audio = manifest.get("audio")
    if not isinstance(source_preview_path, str) or not isinstance(audio, dict) or not isinstance(audio.get("source_path"), str):
        raise AudioPreviewError("MANIFEST_SCHEMA", "source bindings are missing", str(manifest_path))
    preview_root_resolved = _root(preview_root, must_exist=True, field="preview_root")
    _normalized, source_preview = _safe_existing_file(preview_root_resolved, source_preview_path, "source_preview_path")
    expected_manifest, expected_files = _build_expected(
        source_preview,
        preview_root_resolved,
        package_root,
        audio["source_path"],
        audio_root,
        offset_ms=manifest.get("offset_ms"),
        duration_policy=manifest.get("duration_policy"),
        audio_license_status=audio.get("license_status"),
    )
    if manifest != expected_manifest:
        raise AudioPreviewError("MANIFEST_BINDING_MISMATCH", "audio preview manifest is stale or not canonical", str(manifest_path))
    destination = root / package_id
    expected_names = set(expected_files)
    actual_names: set[str] = set()
    for candidate in destination.rglob("*"):
        if candidate.is_symlink():
            raise AudioPreviewError("PACKAGE_SYMLINK", "audio preview package contains a symlink", candidate.relative_to(destination).as_posix())
        if candidate.is_file():
            actual_names.add(candidate.relative_to(destination).as_posix())
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise AudioPreviewError("FILE_SET_MISMATCH", f"missing={missing}; extra={extra}", str(destination))
    for relative, expected_bytes in expected_files.items():
        candidate = destination.joinpath(*safe_relative_path(relative).parts)
        if candidate.read_bytes() != expected_bytes:
            raise AudioPreviewError("FILE_MISMATCH", f"audio preview file was modified: {relative}", relative)
    return {
        "ok": True,
        "audio_preview": manifest,
        "file_count": len(expected_files),
        "segment_count": len(manifest["segments"]),
    }
