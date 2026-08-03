"""Deterministic, renderer-neutral final render-plan compilation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from .audio_preview import AudioPreviewError, check_audio_preview_package
from .naming import SHA256_RE, canonical_json, content_identifier, safe_relative_path

RENDER_PLAN_MANIFEST = "render-plan-manifest.json"
FRAME_INVENTORY = "frame-inventory.json"
SPAN_INVENTORY = "render-spans.json"
SOURCE_INVENTORY = "source-bindings.json"
MAX_FPS_COMPONENT = 1_000_000
MAX_FRAME_COUNT = 2_000_000
MAX_DURATION_MS = 24 * 60 * 60 * 1000


@dataclass
class RenderPlanError(ValueError):
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


def _bounded_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise RenderPlanError("INTEGER_RANGE", f"{field} must be from {minimum} to {maximum}", field)
    return value


def _root(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise RenderPlanError("UNSAFE_PATH", f"{field} must not contain parent traversal", field)
    if expanded.is_symlink():
        raise RenderPlanError("SYMLINK_ROOT", f"{field} must not be a symlink", field)
    if must_exist and not expanded.is_dir():
        raise RenderPlanError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise RenderPlanError("ROOT_TYPE", f"{field} must be a directory", field)
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
        raise RenderPlanError("UNSAFE_PATH", str(exc), field) from exc
    candidate = root.joinpath(*safe.parts)
    current = root
    for part in safe.parts:
        current = current / part
        if current.is_symlink():
            raise RenderPlanError("PATH_SYMLINK", f"{field} contains a symlink component", field)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RenderPlanError("FILE_MISSING", str(exc), field) from exc
    if not _within(root, resolved):
        raise RenderPlanError("PATH_ESCAPE", f"{field} escapes configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise RenderPlanError("FILE_TYPE", f"{field} must be a regular file", field)
    return safe.as_posix(), resolved


def _lexical_file_under_root(path: Path, root: Path, field: str) -> tuple[str, Path]:
    expanded = path.expanduser()
    if ".." in expanded.parts:
        raise RenderPlanError("UNSAFE_PATH", f"{field} must not contain parent traversal", field)
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        relative = lexical.relative_to(root).as_posix()
    except ValueError as exc:
        raise RenderPlanError("PATH_ESCAPE", f"{field} must be beneath its configured root", field) from exc
    return _safe_existing_file(root, relative, field)


def _load_object(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise RenderPlanError("DUPLICATE_KEY", f"duplicate JSON key: {key}", str(path))
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except RenderPlanError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RenderPlanError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise RenderPlanError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _canonical_state(segment: dict[str, Any], role: str, index: int) -> dict[str, str]:
    state = segment.get(role)
    slots = segment.get("stage_slots")
    if not isinstance(state, dict) or not isinstance(slots, dict):
        raise RenderPlanError("SEGMENT_SCHEMA", "segment state or stage slots are missing", f"segments[{index}]")
    asset_path = state.get("asset_path")
    png_sha = state.get("png_sha256")
    variant_id = state.get("variant_id")
    key = state.get("key")
    slot = slots.get(role)
    if not all(isinstance(item, str) and item for item in (asset_path, variant_id, key, slot)):
        raise RenderPlanError("SEGMENT_SCHEMA", f"{role} segment binding is malformed", f"segments[{index}].{role}")
    try:
        normalized = safe_relative_path(asset_path).as_posix()
    except ValueError as exc:
        raise RenderPlanError("UNSAFE_PATH", str(exc), f"segments[{index}].{role}.asset_path") from exc
    if not isinstance(png_sha, str) or not SHA256_RE.fullmatch(png_sha):
        raise RenderPlanError("SEGMENT_SCHEMA", f"{role} PNG checksum is malformed", f"segments[{index}].{role}.png_sha256")
    return {"key": key, "variant_id": variant_id, "asset_path": normalized, "png_sha256": png_sha, "stage_slot": slot}


def _segments(audio_preview: dict[str, Any], duration_ms: int) -> list[dict[str, Any]]:
    raw = audio_preview.get("segments")
    if not isinstance(raw, list) or not raw:
        raise RenderPlanError("SEGMENT_SCHEMA", "audio preview requires at least one segment", "segments")
    result: list[dict[str, Any]] = []
    cursor = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise RenderPlanError("SEGMENT_SCHEMA", "segment must be an object", f"segments[{index}]")
        start = _bounded_integer(item.get("start_ms"), f"segments[{index}].start_ms", 0, duration_ms)
        end = _bounded_integer(item.get("end_ms"), f"segments[{index}].end_ms", 1, duration_ms)
        if start != cursor or end <= start:
            raise RenderPlanError("SEGMENT_COVERAGE", "segments must be contiguous, ordered, and non-empty", f"segments[{index}]")
        result.append({"start_ms": start, "end_ms": end, "boke": _canonical_state(item, "boke", index), "tsukkomi": _canonical_state(item, "tsukkomi", index)})
        cursor = end
    if cursor != duration_ms:
        raise RenderPlanError("SEGMENT_COVERAGE", "segments must cover the complete scene duration", "segments")
    return result


def _frame_state(segments: list[dict[str, Any]], start_num: int, time_den: int) -> dict[str, Any]:
    for segment in segments:
        if start_num < segment["end_ms"] * time_den:
            return segment
    return segments[-1]


def _build_frames(segments: list[dict[str, Any]], duration_ms: int, fps_num: int, fps_den: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    frame_den = fps_num
    frame_step_num = 1000 * fps_den
    total_num = duration_ms * frame_den
    frame_count = (total_num + frame_step_num - 1) // frame_step_num
    if frame_count < 1 or frame_count > MAX_FRAME_COUNT:
        raise RenderPlanError("FRAME_COUNT", f"frame count must be from 1 to {MAX_FRAME_COUNT}", "frame_count")
    frames: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for index in range(frame_count):
        start_num = index * frame_step_num
        end_num = min((index + 1) * frame_step_num, total_num)
        segment = _frame_state(segments, start_num, frame_den)
        frame = {"index": index, "start_time_num": start_num, "end_time_num": end_num, "time_den": frame_den, "boke": segment["boke"], "tsukkomi": segment["tsukkomi"]}
        frames.append(frame)
        state_key = canonical_json({"boke": frame["boke"], "tsukkomi": frame["tsukkomi"]})
        if spans and spans[-1]["_state_key"] == state_key:
            spans[-1]["end_frame"] = index + 1
            spans[-1]["end_time_num"] = end_num
        else:
            spans.append({"_state_key": state_key, "start_frame": index, "end_frame": index + 1, "start_time_num": start_num, "end_time_num": end_num, "time_den": frame_den, "boke": frame["boke"], "tsukkomi": frame["tsukkomi"]})
    for span in spans:
        span.pop("_state_key")
    return frames, spans


def _source_bindings(audio_preview: dict[str, Any], source_relative: str, source_bytes: bytes) -> dict[str, Any]:
    audio = audio_preview.get("audio")
    assets = audio_preview.get("assets")
    roles = audio_preview.get("roles")
    if not isinstance(audio, dict) or not isinstance(assets, list) or not isinstance(roles, dict):
        raise RenderPlanError("SOURCE_SCHEMA", "audio preview source bindings are malformed", "audio_preview")
    audio_sha = audio.get("sha256")
    audio_path = audio.get("path")
    source_audio_path = audio.get("source_path")
    sample_rate = audio.get("sample_rate")
    audio_frames = audio.get("frame_count")
    license_status = audio.get("license_status")
    if not isinstance(audio_sha, str) or not SHA256_RE.fullmatch(audio_sha) or not isinstance(audio_path, str) or not isinstance(source_audio_path, str) or not isinstance(sample_rate, int) or not isinstance(audio_frames, int) or not isinstance(license_status, str):
        raise RenderPlanError("SOURCE_SCHEMA", "audio binding is malformed", "audio")
    try:
        audio_path = safe_relative_path(audio_path).as_posix()
        source_audio_path = safe_relative_path(source_audio_path).as_posix()
    except ValueError as exc:
        raise RenderPlanError("UNSAFE_PATH", str(exc), "audio") from exc
    normalized_assets: list[dict[str, Any]] = []
    for index, item in enumerate(assets):
        if not isinstance(item, dict):
            raise RenderPlanError("SOURCE_SCHEMA", "asset binding is malformed", f"assets[{index}]")
        path = item.get("path")
        sha = item.get("sha256")
        size = item.get("size")
        if not isinstance(path, str) or not isinstance(sha, str) or not SHA256_RE.fullmatch(sha) or not isinstance(size, int):
            raise RenderPlanError("SOURCE_SCHEMA", "asset binding is malformed", f"assets[{index}]")
        try:
            path = safe_relative_path(path).as_posix()
        except ValueError as exc:
            raise RenderPlanError("UNSAFE_PATH", str(exc), f"assets[{index}].path") from exc
        normalized_assets.append({"path": path, "sha256": sha, "size": size})
    normalized_roles: dict[str, Any] = {}
    for role in ("boke", "tsukkomi"):
        item = roles.get(role)
        if not isinstance(item, dict):
            raise RenderPlanError("SOURCE_SCHEMA", f"{role} role binding is missing", f"roles.{role}")
        normalized_roles[role] = {key: item.get(key) for key in ("package_id", "package_manifest_sha256", "variant_set_ref", "character_ref", "license_status", "stage_slot")}
    return {
        "audio_preview": {"id": audio_preview.get("id"), "path": source_relative, "sha256": _sha(source_bytes)},
        "source_preview": {"id": audio_preview.get("source_preview_ref"), "path": audio_preview.get("source_preview_path"), "sha256": audio_preview.get("source_preview_sha256")},
        "scene_plan_ref": audio_preview.get("scene_plan_ref"),
        "roles": normalized_roles,
        "assets": sorted(normalized_assets, key=lambda item: item["path"]),
        "audio": {"package_path": audio_path, "source_path": source_audio_path, "sha256": audio_sha, "sample_rate": sample_rate, "frame_count": audio_frames, "duration_ms": audio.get("duration_ms"), "license_status": license_status},
    }


def _build_expected(audio_preview_manifest: Path, audio_preview_root: Path, preview_root: Path, package_root: Path, audio_root: Path, *, fps_num: int, fps_den: int) -> tuple[dict[str, Any], dict[str, bytes]]:
    numerator = _bounded_integer(fps_num, "fps_num", 1, MAX_FPS_COMPONENT)
    denominator = _bounded_integer(fps_den, "fps_den", 1, MAX_FPS_COMPONENT)
    source_root = _root(audio_preview_root, must_exist=True, field="audio_preview_root")
    source_relative, source_path = _lexical_file_under_root(audio_preview_manifest, source_root, "audio_preview_manifest")
    try:
        checked = check_audio_preview_package(source_path, source_root, preview_root, package_root, audio_root)
    except AudioPreviewError as exc:
        raise RenderPlanError(f"AUDIO_PREVIEW_{exc.code}", exc.message, exc.field or "audio_preview_manifest") from exc
    audio_preview = checked.get("audio_preview")
    if not isinstance(audio_preview, dict):
        raise RenderPlanError("AUDIO_PREVIEW_RESULT", "audio preview validation result is malformed", "audio_preview_manifest")
    source_bytes = source_path.read_bytes()
    duration_ms = _bounded_integer(audio_preview.get("scene_duration_ms"), "scene_duration_ms", 1, MAX_DURATION_MS)
    width = _bounded_integer(audio_preview.get("width"), "width", 1, 8192)
    height = _bounded_integer(audio_preview.get("height"), "height", 1, 8192)
    intent = audio_preview.get("intent")
    if intent not in {"evaluation", "production"}:
        raise RenderPlanError("INTENT", "source intent is invalid", "intent")
    segments = _segments(audio_preview, duration_ms)
    frames, spans = _build_frames(segments, duration_ms, numerator, denominator)
    bindings = _source_bindings(audio_preview, source_relative, source_bytes)
    audio = bindings["audio"]
    sample_rate = audio["sample_rate"]
    offset_ms = _bounded_integer(audio_preview.get("offset_ms"), "offset_ms", -MAX_DURATION_MS, MAX_DURATION_MS)
    audio_placement = {"policy": "signed-rational-sample-offset-no-resampling", "offset_ms": offset_ms, "start_sample_num": offset_ms * sample_rate, "start_sample_den": 1000, "source_sample_rate": sample_rate, "source_frame_count": audio["frame_count"], "duration_policy": audio_preview.get("duration_policy"), "synchronized_audio_end_ms": audio_preview.get("synchronized_audio_end_ms")}
    core = {
        "kind": "paper-theater-render-plan",
        "schema_version": "1.0",
        "source_bindings": bindings,
        "intent": intent,
        "audio_license_status": audio["license_status"],
        "width": width,
        "height": height,
        "scene_duration_ms": duration_ms,
        "fps_num": numerator,
        "fps_den": denominator,
        "frame_time_unit": "milliseconds",
        "frame_boundary_policy": "state-at-frame-start",
        "frame_count": len(frames),
        "frames": frames,
        "spans": spans,
        "audio_placement": audio_placement,
        "output_target": {"kind": "renderer-neutral-media-target", "video_container": "unspecified", "video_codec": "unspecified", "audio_codec": "copy-or-renderer-defined", "pixel_width": width, "pixel_height": height, "frame_count": len(frames), "media_created": False},
    }
    plan_id = content_identifier("paper-theater-render-plan", core, 20)
    identified = {"id": plan_id, **core}
    frame_doc = {"kind": "paper-theater-frame-inventory", "schema_version": "1.0", "render_plan_ref": plan_id, "frame_count": len(frames), "frames": frames}
    span_doc = {"kind": "paper-theater-render-span-inventory", "schema_version": "1.0", "render_plan_ref": plan_id, "frame_count": len(frames), "span_count": len(spans), "spans": spans}
    source_doc = {"kind": "paper-theater-render-source-bindings", "schema_version": "1.0", "render_plan_ref": plan_id, "bindings": bindings, "audio_placement": audio_placement}
    generated = {FRAME_INVENTORY: _json_bytes(frame_doc), SPAN_INVENTORY: _json_bytes(span_doc), SOURCE_INVENTORY: _json_bytes(source_doc)}
    files = [{"path": path, "sha256": _sha(payload), "size": len(payload)} for path, payload in sorted(generated.items())]
    manifest = {**identified, "files": files}
    generated[RENDER_PLAN_MANIFEST] = _json_bytes(manifest)
    return manifest, generated


def _write_package(output_root: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> bool:
    root_path = output_root.expanduser()
    if ".." in root_path.parts:
        raise RenderPlanError("UNSAFE_PATH", "output_root must not contain parent traversal", "output_root")
    if root_path.is_symlink():
        raise RenderPlanError("SYMLINK_ROOT", "output_root must not be a symlink", "output_root")
    root_path.mkdir(parents=True, exist_ok=True)
    root = root_path.resolve()
    destination = root / manifest["id"]
    if destination.is_symlink():
        raise RenderPlanError("OUTPUT_SYMLINK", "render-plan destination must not be a symlink", "output_root")
    expected = set(files)
    if destination.exists():
        if not destination.is_dir():
            raise RenderPlanError("OUTPUT_CONFLICT", "render-plan destination is not a directory", "output_root")
        actual: set[str] = set()
        for candidate in destination.rglob("*"):
            if candidate.is_symlink():
                raise RenderPlanError("OUTPUT_SYMLINK", "existing render-plan package contains a symlink", str(candidate))
            if candidate.is_file():
                actual.add(candidate.relative_to(destination).as_posix())
        if actual != expected:
            raise RenderPlanError("OUTPUT_CONFLICT", "existing render-plan file set differs", "output_root")
        for relative, payload in files.items():
            candidate = destination.joinpath(*safe_relative_path(relative).parts)
            if candidate.read_bytes() != payload:
                raise RenderPlanError("OUTPUT_CONFLICT", f"existing file differs: {relative}", relative)
        return False
    staging = root / f".{manifest['id']}.tmp"
    if staging.exists():
        if staging.is_symlink():
            raise RenderPlanError("STAGING_CONFLICT", "staging path is a symlink", "output_root")
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


def build_render_plan_package(audio_preview_manifest: Path, audio_preview_root: Path, preview_root: Path, package_root: Path, audio_root: Path, output_root: Path, *, fps_num: int, fps_den: int, write: bool = False) -> dict[str, Any]:
    manifest, files = _build_expected(audio_preview_manifest, audio_preview_root, preview_root, package_root, audio_root, fps_num=fps_num, fps_den=fps_den)
    written = _write_package(output_root, manifest, files) if write else False
    return {"ok": True, "render_plan": manifest, "file_count": len(files), "written": written, "package_path": manifest["id"]}


def _manifest_location(manifest_path: Path, output_root: Path) -> tuple[Path, dict[str, Any], bytes]:
    root = _root(output_root, must_exist=True, field="output_root")
    _relative, resolved = _lexical_file_under_root(manifest_path, root, "render_plan_manifest")
    return root, _load_object(resolved), resolved.read_bytes()


def check_render_plan_package(manifest_path: Path, output_root: Path, audio_preview_root: Path, preview_root: Path, package_root: Path, audio_root: Path) -> dict[str, Any]:
    root, manifest, payload = _manifest_location(manifest_path, output_root)
    if payload != _json_bytes(manifest):
        raise RenderPlanError("MANIFEST_CANONICAL", "render-plan manifest JSON is not canonical", str(manifest_path))
    plan_id = manifest.get("id")
    if not isinstance(plan_id, str):
        raise RenderPlanError("MANIFEST_SCHEMA", "render-plan ID is missing", "id")
    canonical = root / plan_id / RENDER_PLAN_MANIFEST
    if manifest_path.expanduser().resolve() != canonical.resolve():
        raise RenderPlanError("MANIFEST_LOCATION", "render-plan manifest path is not canonical", str(manifest_path))
    bindings = manifest.get("source_bindings")
    if not isinstance(bindings, dict) or not isinstance(bindings.get("audio_preview"), dict):
        raise RenderPlanError("MANIFEST_SCHEMA", "source audio-preview binding is missing", "source_bindings")
    source_relative = bindings["audio_preview"].get("path")
    if not isinstance(source_relative, str):
        raise RenderPlanError("MANIFEST_SCHEMA", "source audio-preview path is missing", "source_bindings.audio_preview.path")
    source_root = _root(audio_preview_root, must_exist=True, field="audio_preview_root")
    _normalized, source_manifest = _safe_existing_file(source_root, source_relative, "source_bindings.audio_preview.path")
    expected_manifest, expected_files = _build_expected(source_manifest, source_root, preview_root, package_root, audio_root, fps_num=manifest.get("fps_num"), fps_den=manifest.get("fps_den"))
    if manifest != expected_manifest:
        raise RenderPlanError("MANIFEST_BINDING_MISMATCH", "render-plan manifest is stale or not canonical", str(manifest_path))
    destination = root / plan_id
    expected_names = set(expected_files)
    actual_names: set[str] = set()
    for candidate in destination.rglob("*"):
        if candidate.is_symlink():
            raise RenderPlanError("PACKAGE_SYMLINK", "render-plan package contains a symlink", candidate.relative_to(destination).as_posix())
        if candidate.is_file():
            actual_names.add(candidate.relative_to(destination).as_posix())
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise RenderPlanError("FILE_SET_MISMATCH", f"missing={missing}; extra={extra}", str(destination))
    for relative, expected_bytes in expected_files.items():
        candidate = destination.joinpath(*safe_relative_path(relative).parts)
        if candidate.read_bytes() != expected_bytes:
            raise RenderPlanError("FILE_MISMATCH", f"render-plan file was modified: {relative}", relative)
    return {"ok": True, "render_plan": manifest, "file_count": len(expected_files), "frame_count": manifest["frame_count"], "span_count": len(manifest["spans"])}
