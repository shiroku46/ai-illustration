"""Deterministic two-character paper-theater scene and timeline planning."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .exporter import ExportError, PACKAGE_MANIFEST, check_export_package
from .naming import SHA256_RE, canonical_json, content_identifier, safe_relative_path

ROLES = ("boke", "tsukkomi")
STAGE_SLOTS = {"left", "right", "center"}
PACKAGE_ID_RE = re.compile(r"variant-export-package-[0-9a-f]{20}")
VARIANT_SET_RE = re.compile(r"variant-set-[0-9a-f]{20}")
SCENE_ID_RE = re.compile(r"paper-theater-scene-plan-[0-9a-f]{20}")
PAPER_KEY_RE = re.compile(r"[a-z0-9.-]+")
CUE_FIELDS = {"kind", "schema_version", "duration_ms", "packages", "stage_slots", "initial", "events"}
EVENT_FIELDS = {"at_ms", "role", "key"}
SCENE_FIELDS = {"id", "kind", "schema_version", "intent", "duration_ms", "roles", "events", "segments"}
ROLE_FIELDS = {
    "package_manifest_path",
    "package_id",
    "package_manifest_sha256",
    "variant_set_ref",
    "character_ref",
    "license_status",
    "stage_slot",
    "initial_key",
}
STATE_FIELDS = {"key", "variant_id", "png_path", "png_sha256"}
SEGMENT_FIELDS = {"start_ms", "end_ms", "stage_slots", "boke", "tsukkomi"}


@dataclass
class PaperTheaterError(ValueError):
    code: str
    message: str
    field: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "field": self.field}


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PaperTheaterError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise PaperTheaterError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _json_bytes(value: Any) -> bytes:
    return canonical_json(value) + b"\n"


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _require_exact(value: dict[str, Any], expected: set[str], code: str, field: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise PaperTheaterError(code, f"missing={missing}; extra={extra}", field)


def _require_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaperTheaterError("INTEGER_REQUIRED", f"{field} must be a positive integer", field)
    return value


def _require_key(value: Any, field: str) -> str:
    if not isinstance(value, str) or not PAPER_KEY_RE.fullmatch(value):
        raise PaperTheaterError("PAPER_THEATER_KEY", f"{field} is invalid", field)
    return value


def _resolved_root(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise PaperTheaterError("SYMLINK_ROOT", "package root must not be a symlink", "package_root")
    if not expanded.is_dir():
        raise PaperTheaterError("ROOT_MISSING", "package root does not exist", "package_root")
    return expanded.resolve()


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_manifest(root: Path, relative: Any, field: str) -> tuple[str, Path]:
    if not isinstance(relative, str):
        raise PaperTheaterError("UNSAFE_PATH", "manifest path must be a string", field)
    try:
        normalized_path = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise PaperTheaterError("UNSAFE_PATH", str(exc), field) from exc
    normalized = normalized_path.as_posix()
    candidate = root.joinpath(*normalized_path.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PaperTheaterError("PACKAGE_MISSING", str(exc), field) from exc
    if not _is_within(root, resolved):
        raise PaperTheaterError("PATH_ESCAPE", "package manifest escapes configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise PaperTheaterError("PACKAGE_PATH", "package manifest must be a regular file", field)
    return normalized, resolved


def _load_verified_package(root: Path, relative: Any, role: str) -> tuple[dict[str, Any], str, str]:
    normalized, manifest = _resolve_manifest(root, relative, f"packages.{role}")
    try:
        result = check_export_package(manifest, root)
    except ExportError as exc:
        raise PaperTheaterError(f"PACKAGE_{exc.code}", exc.message, f"packages.{role}") from exc
    package = result.get("package")
    if not isinstance(package, dict):
        raise PaperTheaterError("PACKAGE_RESULT", "verified package result is malformed", f"packages.{role}")
    package_id = package.get("id")
    if not isinstance(package_id, str) or not PACKAGE_ID_RE.fullmatch(package_id):
        raise PaperTheaterError("PACKAGE_ID", "verified package ID is invalid", f"packages.{role}")
    if normalized != f"{package_id}/{PACKAGE_MANIFEST}":
        raise PaperTheaterError("PACKAGE_LOCATION", "package manifest path is not canonical", f"packages.{role}")
    try:
        manifest_bytes = manifest.read_bytes()
    except OSError as exc:
        raise PaperTheaterError("PACKAGE_READ", str(exc), f"packages.{role}") from exc
    return package, normalized, _sha(manifest_bytes)


def _validate_cue(cue: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    _require_exact(cue, CUE_FIELDS, "CUE_SCHEMA", "cue_sheet")
    if cue.get("kind") != "paper-theater-cue-sheet" or cue.get("schema_version") != "1.0":
        raise PaperTheaterError("CUE_SCHEMA", "invalid cue-sheet kind or schema version", "cue_sheet")
    duration = _require_positive_int(cue.get("duration_ms"), "duration_ms")
    for name in ("packages", "stage_slots", "initial"):
        value = cue.get(name)
        if not isinstance(value, dict) or set(value) != set(ROLES):
            raise PaperTheaterError("CUE_SCHEMA", f"{name} must contain exactly boke and tsukkomi", name)
    slots = cue["stage_slots"]
    if any(slot not in STAGE_SLOTS for slot in slots.values()):
        raise PaperTheaterError("STAGE_SLOT", "stage slots must be left, right, or center", "stage_slots")
    if slots["boke"] == slots["tsukkomi"]:
        raise PaperTheaterError("DUPLICATE_STAGE_SLOT", "active roles must use distinct stage slots", "stage_slots")
    for role in ROLES:
        _require_key(cue["initial"].get(role), f"initial.{role}")
    events = cue.get("events")
    if not isinstance(events, list):
        raise PaperTheaterError("CUE_SCHEMA", "events must be an array", "events")
    normalized_events: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for index, event in enumerate(events):
        field = f"events[{index}]"
        if not isinstance(event, dict):
            raise PaperTheaterError("EVENT_SCHEMA", "event must be an object", field)
        _require_exact(event, EVENT_FIELDS, "EVENT_SCHEMA", field)
        at_ms = event.get("at_ms")
        if isinstance(at_ms, bool) or not isinstance(at_ms, int) or at_ms <= 0 or at_ms >= duration:
            raise PaperTheaterError("EVENT_TIME", "event time must be greater than zero and less than duration", f"{field}.at_ms")
        role = event.get("role")
        if role not in ROLES:
            raise PaperTheaterError("EVENT_ROLE", "event role must be boke or tsukkomi", f"{field}.role")
        key = _require_key(event.get("key"), f"{field}.key")
        identity = (role, at_ms)
        if identity in seen:
            raise PaperTheaterError("DUPLICATE_ROLE_TIME", "duplicate role/time event", field)
        seen.add(identity)
        normalized_events.append({"at_ms": at_ms, "role": role, "key": key})
    normalized_events.sort(key=lambda item: (item["at_ms"], item["role"]))
    return duration, normalized_events


def _package_key_map(package: dict[str, Any], role: str) -> dict[str, dict[str, str]]:
    items = package.get("items")
    if not isinstance(items, list) or not items:
        raise PaperTheaterError("PACKAGE_ITEMS", "verified package has no items", f"packages.{role}")
    mapping: dict[str, dict[str, str]] = {}
    package_id = package["id"]
    for item in items:
        if not isinstance(item, dict):
            raise PaperTheaterError("PACKAGE_ITEMS", "package item is malformed", f"packages.{role}")
        key = item.get("paper_theater_key")
        variant_id = item.get("variant_id")
        png_path = item.get("png_path")
        png_sha = item.get("png_sha256")
        if (
            not isinstance(key, str)
            or not PAPER_KEY_RE.fullmatch(key)
            or not isinstance(variant_id, str)
            or not isinstance(png_path, str)
            or not isinstance(png_sha, str)
            or not SHA256_RE.fullmatch(png_sha)
        ):
            raise PaperTheaterError("PACKAGE_ITEMS", "verified package item binding is malformed", f"packages.{role}")
        if key in mapping:
            raise PaperTheaterError("DUPLICATE_PAPER_THEATER_KEY", "package contains duplicate key", f"packages.{role}")
        mapping[key] = {
            "key": key,
            "variant_id": variant_id,
            "png_path": f"{package_id}/{png_path}",
            "png_sha256": png_sha,
        }
    return mapping


def _plan_from_cue(cue: dict[str, Any], package_root: Path) -> dict[str, Any]:
    duration, events = _validate_cue(cue)
    root = _resolved_root(package_root)
    loaded: dict[str, dict[str, Any]] = {}
    role_metadata: dict[str, dict[str, Any]] = {}
    key_maps: dict[str, dict[str, dict[str, str]]] = {}

    for role in ROLES:
        package, manifest_path, manifest_sha = _load_verified_package(root, cue["packages"][role], role)
        loaded[role] = package
        key_maps[role] = _package_key_map(package, role)
        initial_key = cue["initial"][role]
        if initial_key not in key_maps[role]:
            raise PaperTheaterError("UNKNOWN_INITIAL_KEY", f"{role} initial key is not in its package", f"initial.{role}")
        role_metadata[role] = {
            "package_manifest_path": manifest_path,
            "package_id": package["id"],
            "package_manifest_sha256": manifest_sha,
            "variant_set_ref": package["variant_set_ref"],
            "character_ref": package["character_ref"],
            "license_status": package["license_status"],
            "stage_slot": cue["stage_slots"][role],
            "initial_key": initial_key,
        }

    if loaded["boke"]["id"] == loaded["tsukkomi"]["id"]:
        raise PaperTheaterError("PACKAGE_REUSE", "the same package cannot be assigned to both roles", "packages")
    intents = {loaded[role].get("intent") for role in ROLES}
    if len(intents) != 1 or not intents.issubset({"evaluation", "production"}):
        raise PaperTheaterError("INTENT_MISMATCH", "both packages must have the same valid intent", "packages")
    intent = next(iter(intents))
    if intent == "production":
        for role in ROLES:
            if loaded[role].get("license_status") != "approved":
                raise PaperTheaterError("PRODUCTION_LICENSE_NOT_APPROVED", f"{role} package license is not approved", f"packages.{role}")

    for index, event in enumerate(events):
        if event["key"] not in key_maps[event["role"]]:
            raise PaperTheaterError("UNKNOWN_EVENT_KEY", "event key is not in the assigned role package", f"events[{index}].key")

    states = {role: cue["initial"][role] for role in ROLES}
    events_by_time: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        events_by_time.setdefault(event["at_ms"], []).append(event)
    boundaries = [0, *sorted(events_by_time), duration]
    segments: list[dict[str, Any]] = []
    for boundary_index in range(len(boundaries) - 1):
        start = boundaries[boundary_index]
        if start in events_by_time:
            for event in sorted(events_by_time[start], key=lambda item: item["role"]):
                states[event["role"]] = event["key"]
        end = boundaries[boundary_index + 1]
        if end <= start:
            continue
        segments.append({
            "start_ms": start,
            "end_ms": end,
            "stage_slots": {role: cue["stage_slots"][role] for role in ROLES},
            "boke": dict(key_maps["boke"][states["boke"]]),
            "tsukkomi": dict(key_maps["tsukkomi"][states["tsukkomi"]]),
        })

    core = {
        "kind": "paper-theater-scene-plan",
        "schema_version": "1.0",
        "intent": intent,
        "duration_ms": duration,
        "roles": {role: role_metadata[role] for role in ROLES},
        "events": events,
        "segments": segments,
    }
    return {"id": content_identifier("paper-theater-scene-plan", core, 20), **core}


def _write_scene(scene: dict[str, Any], destination: Path) -> bool:
    expanded = destination.expanduser()
    if expanded.is_symlink():
        raise PaperTheaterError("WRITE_SYMLINK", "scene output must not be a symlink", "write")
    parent = expanded.parent
    if parent.exists() and parent.is_symlink():
        raise PaperTheaterError("WRITE_SYMLINK", "scene output parent must not be a symlink", "write")
    parent.mkdir(parents=True, exist_ok=True)
    payload = _json_bytes(scene)
    if expanded.exists():
        if not expanded.is_file():
            raise PaperTheaterError("WRITE_CONFLICT", "scene output exists and is not a file", "write")
        if expanded.read_bytes() != payload:
            raise PaperTheaterError("WRITE_CONFLICT", "scene output differs from deterministic plan", "write")
        return False
    temporary = expanded.with_name(f".{expanded.name}.tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        temporary.write_bytes(payload)
        temporary.replace(expanded)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def plan_scene(cue_sheet_path: Path, package_root: Path, *, write_path: Path | None = None) -> dict[str, Any]:
    scene = _plan_from_cue(_load_object(cue_sheet_path), package_root)
    written = _write_scene(scene, write_path) if write_path is not None else False
    return {"ok": True, "scene_plan": scene, "written": written}


def _validate_scene_shape(scene: dict[str, Any]) -> None:
    _require_exact(scene, SCENE_FIELDS, "SCENE_SCHEMA", "scene_plan")
    if scene.get("kind") != "paper-theater-scene-plan" or scene.get("schema_version") != "1.0":
        raise PaperTheaterError("SCENE_SCHEMA", "invalid scene-plan kind or schema version", "scene_plan")
    identifier = scene.get("id")
    if not isinstance(identifier, str) or not SCENE_ID_RE.fullmatch(identifier):
        raise PaperTheaterError("SCENE_SCHEMA", "invalid scene-plan ID", "id")
    _require_positive_int(scene.get("duration_ms"), "duration_ms")
    roles = scene.get("roles")
    if not isinstance(roles, dict) or set(roles) != set(ROLES):
        raise PaperTheaterError("SCENE_SCHEMA", "roles must contain boke and tsukkomi", "roles")
    for role in ROLES:
        metadata = roles[role]
        if not isinstance(metadata, dict):
            raise PaperTheaterError("SCENE_SCHEMA", "role metadata must be an object", f"roles.{role}")
        _require_exact(metadata, ROLE_FIELDS, "SCENE_SCHEMA", f"roles.{role}")
        if not isinstance(metadata.get("package_id"), str) or not PACKAGE_ID_RE.fullmatch(metadata["package_id"]):
            raise PaperTheaterError("SCENE_SCHEMA", "invalid package ID", f"roles.{role}.package_id")
        if not isinstance(metadata.get("variant_set_ref"), str) or not VARIANT_SET_RE.fullmatch(metadata["variant_set_ref"]):
            raise PaperTheaterError("SCENE_SCHEMA", "invalid variant-set reference", f"roles.{role}.variant_set_ref")
        if not isinstance(metadata.get("package_manifest_sha256"), str) or not SHA256_RE.fullmatch(metadata["package_manifest_sha256"]):
            raise PaperTheaterError("SCENE_SCHEMA", "invalid package manifest checksum", f"roles.{role}.package_manifest_sha256")
        if metadata.get("stage_slot") not in STAGE_SLOTS:
            raise PaperTheaterError("SCENE_SCHEMA", "invalid stage slot", f"roles.{role}.stage_slot")
        _require_key(metadata.get("initial_key"), f"roles.{role}.initial_key")
    if roles["boke"]["stage_slot"] == roles["tsukkomi"]["stage_slot"]:
        raise PaperTheaterError("DUPLICATE_STAGE_SLOT", "active roles must use distinct stage slots", "roles")
    events = scene.get("events")
    segments = scene.get("segments")
    if not isinstance(events, list) or not isinstance(segments, list) or not segments:
        raise PaperTheaterError("SCENE_SCHEMA", "events and non-empty segments are required", "scene_plan")
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            raise PaperTheaterError("SCENE_SCHEMA", "segment must be an object", f"segments[{index}]")
        _require_exact(segment, SEGMENT_FIELDS, "SCENE_SCHEMA", f"segments[{index}]")
        for role in ROLES:
            state = segment.get(role)
            if not isinstance(state, dict):
                raise PaperTheaterError("SCENE_SCHEMA", "segment state must be an object", f"segments[{index}].{role}")
            _require_exact(state, STATE_FIELDS, "SCENE_SCHEMA", f"segments[{index}].{role}")
            _require_key(state.get("key"), f"segments[{index}].{role}.key")
            if not isinstance(state.get("png_sha256"), str) or not SHA256_RE.fullmatch(state["png_sha256"]):
                raise PaperTheaterError("SCENE_SCHEMA", "invalid PNG checksum", f"segments[{index}].{role}.png_sha256")


def check_scene_plan(scene_plan_path: Path, package_root: Path) -> dict[str, Any]:
    try:
        payload = scene_plan_path.read_bytes()
    except OSError as exc:
        raise PaperTheaterError("LOAD_ERROR", str(exc), str(scene_plan_path)) from exc
    scene = _load_object(scene_plan_path)
    if payload != _json_bytes(scene):
        raise PaperTheaterError("SCENE_CANONICAL", "scene-plan JSON is not canonical", str(scene_plan_path))
    _validate_scene_shape(scene)
    core = {key: value for key, value in scene.items() if key != "id"}
    expected_id = content_identifier("paper-theater-scene-plan", core, 20)
    if scene["id"] != expected_id:
        raise PaperTheaterError("SCENE_ID_MISMATCH", f"expected canonical scene ID {expected_id}", "id")

    cue = {
        "kind": "paper-theater-cue-sheet",
        "schema_version": "1.0",
        "duration_ms": scene["duration_ms"],
        "packages": {role: scene["roles"][role]["package_manifest_path"] for role in ROLES},
        "stage_slots": {role: scene["roles"][role]["stage_slot"] for role in ROLES},
        "initial": {role: scene["roles"][role]["initial_key"] for role in ROLES},
        "events": scene["events"],
    }
    expected = _plan_from_cue(cue, package_root)
    if scene != expected:
        raise PaperTheaterError("SCENE_BINDING_MISMATCH", "scene plan is stale or differs from verified package bindings", str(scene_plan_path))
    return {"ok": True, "scene_plan": scene, "segment_count": len(scene["segments"])}
