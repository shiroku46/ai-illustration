"""Deterministic composition profiles and renderer-job packages."""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from .naming import SHA256_RE, canonical_json, content_identifier, safe_relative_path
from .render_plan import RenderPlanError, check_render_plan_package

COMPOSITION_PROFILE = "composition-profile.json"
SPAN_TRANSFORMS = "span-transforms.json"
SOURCE_BINDINGS = "source-bindings.json"
RENDERER_JOB_MANIFEST = "renderer-job-manifest.json"

MAX_CANVAS = 8192
MAX_COORDINATE = 1_000_000
MAX_SCALE_COMPONENT = 1_000_000
MAX_Z_ORDER = 1_000_000


@dataclass
class CompositionError(ValueError):
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
        raise CompositionError("INTEGER_RANGE", f"{field} must be from {minimum} to {maximum}", field)
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise CompositionError(
            "SCHEMA_KEYS",
            f"{field} keys must be exactly {sorted(expected)}; got {sorted(actual)}",
            field,
        )


def _reject_lexical_path(value: Path, field: str) -> Path:
    raw = str(value)
    if "\x00" in raw or "\\" in raw:
        raise CompositionError("UNSAFE_PATH", f"{field} contains a forbidden path character", field)
    expanded = value.expanduser()
    if ".." in expanded.parts:
        raise CompositionError("UNSAFE_PATH", f"{field} must not contain parent traversal", field)
    return expanded


def _reject_symlink_components(path: Path, field: str) -> None:
    lexical = path if path.is_absolute() else Path.cwd() / path
    for candidate in (lexical, *lexical.parents):
        try:
            if candidate.exists() and candidate.is_symlink():
                raise CompositionError("PATH_SYMLINK", f"{field} contains a symlink component", field)
        except OSError as exc:
            raise CompositionError("PATH_ERROR", str(exc), field) from exc


def _root(path: Path, *, must_exist: bool, field: str) -> Path:
    expanded = _reject_lexical_path(path, field)
    _reject_symlink_components(expanded, field)
    if must_exist and not expanded.is_dir():
        raise CompositionError("ROOT_MISSING", f"{field} does not exist", field)
    if expanded.exists() and not expanded.is_dir():
        raise CompositionError("ROOT_TYPE", f"{field} must be a directory", field)
    try:
        return expanded.resolve()
    except OSError as exc:
        raise CompositionError("PATH_ERROR", str(exc), field) from exc


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
        raise CompositionError("UNSAFE_PATH", str(exc), field) from exc
    candidate = root.joinpath(*safe.parts)
    current = root
    for part in safe.parts:
        current = current / part
        if current.is_symlink():
            raise CompositionError("PATH_SYMLINK", f"{field} contains a symlink component", field)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise CompositionError("FILE_MISSING", str(exc), field) from exc
    if not _within(root, resolved):
        raise CompositionError("PATH_ESCAPE", f"{field} escapes configured root", field)
    if candidate.is_symlink() or not resolved.is_file():
        raise CompositionError("FILE_TYPE", f"{field} must be a regular file", field)
    return safe.as_posix(), resolved


def _lexical_file_under_root(path: Path, root: Path, field: str) -> tuple[str, Path]:
    expanded = _reject_lexical_path(path, field)
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        relative = lexical.relative_to(root).as_posix()
    except ValueError as exc:
        raise CompositionError("PATH_ESCAPE", f"{field} must be beneath its configured root", field) from exc
    return _safe_existing_file(root, relative, field)


def _standalone_file(path: Path, field: str) -> Path:
    expanded = _reject_lexical_path(path, field)
    _reject_symlink_components(expanded, field)
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise CompositionError("FILE_MISSING", str(exc), field) from exc
    if expanded.is_symlink() or not resolved.is_file():
        raise CompositionError("FILE_TYPE", f"{field} must be a regular file", field)
    return resolved


def _load_object(path: Path) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise CompositionError("DUPLICATE_KEY", f"duplicate JSON key: {key}", str(path))
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except CompositionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CompositionError("LOAD_ERROR", str(exc), str(path)) from exc
    if not isinstance(value, dict):
        raise CompositionError("ROOT_TYPE", "JSON root must be an object", str(path))
    return value


def _package_dir_for_relative(root: Path, relative: Any, field: str) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    try:
        safe = safe_relative_path(relative)
    except (TypeError, ValueError) as exc:
        raise CompositionError("UNSAFE_PATH", str(exc), field) from exc
    candidate = root.joinpath(*safe.parts)
    _reject_symlink_components(candidate, field)
    return candidate.parent.resolve(strict=False)


def _package_dir_for_id(root: Path, identifier: Any, field: str) -> Path | None:
    if not isinstance(identifier, str) or not identifier:
        return None
    try:
        safe = safe_relative_path(identifier)
    except (TypeError, ValueError) as exc:
        raise CompositionError("UNSAFE_PATH", str(exc), field) from exc
    if len(safe.parts) != 1:
        raise CompositionError("UNSAFE_PATH", f"{field} must be one package identifier", field)
    candidate = root / safe.parts[0]
    _reject_symlink_components(candidate, field)
    return candidate.resolve(strict=False)


def _source_package_directories(
    plan_path: Path,
    render_plan: dict[str, Any],
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
) -> set[Path]:
    directories: set[Path] = {plan_path.parent.resolve()}
    audio_preview_base = _root(audio_preview_root, must_exist=True, field="audio_preview_root")
    preview_base = _root(preview_root, must_exist=True, field="preview_root")
    package_base = _root(package_root, must_exist=True, field="package_root")
    audio_base = _root(audio_root, must_exist=True, field="audio_root")
    bindings = render_plan.get("source_bindings")
    if not isinstance(bindings, dict):
        return directories

    audio_preview = bindings.get("audio_preview")
    if isinstance(audio_preview, dict):
        package = _package_dir_for_relative(
            audio_preview_base,
            audio_preview.get("path"),
            "source_bindings.audio_preview.path",
        )
        if package is not None:
            directories.add(package)

    source_preview = bindings.get("source_preview")
    if isinstance(source_preview, dict):
        package = _package_dir_for_relative(
            preview_base,
            source_preview.get("path"),
            "source_bindings.source_preview.path",
        )
        if package is not None:
            directories.add(package)

    scene_plan_ref = bindings.get("scene_plan_ref")
    package = _package_dir_for_id(preview_base, scene_plan_ref, "source_bindings.scene_plan_ref")
    if package is not None:
        directories.add(package)

    roles = bindings.get("roles")
    if isinstance(roles, dict):
        for role in ("boke", "tsukkomi"):
            item = roles.get(role)
            if isinstance(item, dict):
                package = _package_dir_for_id(
                    package_base,
                    item.get("package_id"),
                    f"source_bindings.roles.{role}.package_id",
                )
                if package is not None:
                    directories.add(package)

    audio = bindings.get("audio")
    if isinstance(audio, dict):
        for key in ("package_path", "source_path"):
            package = _package_dir_for_relative(audio_base, audio.get(key), f"source_bindings.audio.{key}")
            if package is not None:
                directories.add(package)
    return directories


def _output_candidate(output_root: Path) -> Path:
    expanded = _reject_lexical_path(output_root, "output_root")
    _reject_symlink_components(expanded, "output_root")
    if expanded.exists() and not expanded.is_dir():
        raise CompositionError("ROOT_TYPE", "output_root must be a directory", "output_root")
    try:
        return expanded.resolve(strict=False)
    except OSError as exc:
        raise CompositionError("PATH_ERROR", str(exc), "output_root") from exc


def _reject_output_overlap(output_root: Path, source_packages: set[Path]) -> None:
    candidate = _output_candidate(output_root)
    for source in sorted(source_packages, key=str):
        canonical_source = source.resolve(strict=False)
        if candidate == canonical_source or _within(canonical_source, candidate):
            raise CompositionError(
                "OUTPUT_OVERLAPS_SOURCE",
                f"output_root must not equal or be nested beneath source package {canonical_source}",
                "output_root",
            )


def _anchor(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise CompositionError("PROFILE_SCHEMA", f"{field} must be an object", field)
    _exact_keys(value, {"x", "y"}, field)
    return {
        "x": _bounded_integer(value.get("x"), f"{field}.x", -MAX_COORDINATE, MAX_COORDINATE),
        "y": _bounded_integer(value.get("y"), f"{field}.y", -MAX_COORDINATE, MAX_COORDINATE),
    }


def _scale(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise CompositionError("PROFILE_SCHEMA", f"{field} must be an object", field)
    _exact_keys(value, {"numerator", "denominator"}, field)
    numerator = _bounded_integer(value.get("numerator"), f"{field}.numerator", 1, MAX_SCALE_COMPONENT)
    denominator = _bounded_integer(value.get("denominator"), f"{field}.denominator", 1, MAX_SCALE_COMPONENT)
    if gcd(numerator, denominator) != 1:
        raise CompositionError("SCALE_NOT_REDUCED", f"{field} must be in reduced form", field)
    return {"numerator": numerator, "denominator": denominator}


def _translation(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise CompositionError("PROFILE_SCHEMA", f"{field} must be an object", field)
    _exact_keys(value, {"x", "y"}, field)
    return {
        "x": _bounded_integer(value.get("x"), f"{field}.x", -MAX_COORDINATE, MAX_COORDINATE),
        "y": _bounded_integer(value.get("y"), f"{field}.y", -MAX_COORDINATE, MAX_COORDINATE),
    }


def validate_composition_profile(path: Path) -> tuple[dict[str, Any], bytes]:
    resolved = _standalone_file(path, "composition_profile")
    profile = _load_object(resolved)
    payload = resolved.read_bytes()
    if payload != _json_bytes(profile):
        raise CompositionError("PROFILE_CANONICAL", "composition profile JSON is not canonical", str(path))
    _exact_keys(profile, {"id", "kind", "schema_version", "canvas", "slots"}, "profile")
    if profile.get("kind") != "paper-theater-composition-profile" or profile.get("schema_version") != "1.0":
        raise CompositionError("PROFILE_SCHEMA", "composition profile kind or schema version is invalid", "profile")
    canvas = profile.get("canvas")
    if not isinstance(canvas, dict):
        raise CompositionError("PROFILE_SCHEMA", "canvas must be an object", "canvas")
    _exact_keys(canvas, {"width", "height", "background_rgba"}, "canvas")
    width = _bounded_integer(canvas.get("width"), "canvas.width", 1, MAX_CANVAS)
    height = _bounded_integer(canvas.get("height"), "canvas.height", 1, MAX_CANVAS)
    rgba = canvas.get("background_rgba")
    if not isinstance(rgba, list) or len(rgba) != 4:
        raise CompositionError("PROFILE_SCHEMA", "background_rgba must contain four integer channels", "canvas.background_rgba")
    background = [
        _bounded_integer(channel, f"canvas.background_rgba[{index}]", 0, 255)
        for index, channel in enumerate(rgba)
    ]
    raw_slots = profile.get("slots")
    if not isinstance(raw_slots, list) or len(raw_slots) != 2:
        raise CompositionError("PROFILE_SLOTS", "profile requires exactly two slots", "slots")
    slots: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_slots):
        field = f"slots[{index}]"
        if not isinstance(raw, dict):
            raise CompositionError("PROFILE_SCHEMA", f"{field} must be an object", field)
        _exact_keys(raw, {"name", "source_anchor", "target_anchor", "scale", "translation", "z_order"}, field)
        name = raw.get("name")
        if name not in {"left", "right"} or name in names:
            raise CompositionError("PROFILE_SLOTS", "slot names must be unique and exactly left/right", f"{field}.name")
        names.add(name)
        slots.append(
            {
                "name": name,
                "source_anchor": _anchor(raw.get("source_anchor"), f"{field}.source_anchor"),
                "target_anchor": _anchor(raw.get("target_anchor"), f"{field}.target_anchor"),
                "scale": _scale(raw.get("scale"), f"{field}.scale"),
                "translation": _translation(raw.get("translation"), f"{field}.translation"),
                "z_order": _bounded_integer(raw.get("z_order"), f"{field}.z_order", -MAX_Z_ORDER, MAX_Z_ORDER),
            }
        )
    if names != {"left", "right"}:
        raise CompositionError("PROFILE_SLOTS", "slot set must be exactly left and right", "slots")
    normalized_core = {
        "kind": "paper-theater-composition-profile",
        "schema_version": "1.0",
        "canvas": {"width": width, "height": height, "background_rgba": background},
        "slots": sorted(slots, key=lambda item: item["name"]),
    }
    expected_id = content_identifier("paper-theater-composition-profile", normalized_core, 20)
    if profile.get("id") != expected_id:
        raise CompositionError("PROFILE_ID", "composition profile ID does not match canonical content", "id")
    normalized = {"id": expected_id, **normalized_core}
    if profile != normalized:
        raise CompositionError("PROFILE_CANONICAL", "composition profile field ordering/content is not canonical", str(path))
    return normalized, payload


def _canonical_asset(value: Any, role: str, span_index: int) -> dict[str, str]:
    field = f"spans[{span_index}].{role}"
    if not isinstance(value, dict):
        raise CompositionError("SPAN_SCHEMA", f"{field} must be an object", field)
    required = ("key", "variant_id", "asset_path", "png_sha256", "stage_slot")
    if not all(isinstance(value.get(key), str) and value.get(key) for key in required):
        raise CompositionError("SPAN_SCHEMA", f"{field} binding is incomplete", field)
    try:
        asset_path = safe_relative_path(value["asset_path"]).as_posix()
    except ValueError as exc:
        raise CompositionError("UNSAFE_PATH", str(exc), f"{field}.asset_path") from exc
    if not SHA256_RE.fullmatch(value["png_sha256"]):
        raise CompositionError("SPAN_SCHEMA", f"{field} PNG checksum is invalid", f"{field}.png_sha256")
    if value["stage_slot"] not in {"left", "right"}:
        raise CompositionError("SPAN_SLOT", f"{field} stage slot is invalid", f"{field}.stage_slot")
    return {
        "key": value["key"],
        "variant_id": value["variant_id"],
        "asset_path": asset_path,
        "png_sha256": value["png_sha256"],
        "stage_slot": value["stage_slot"],
    }


def _build_span_transforms(render_plan: dict[str, Any], profile: dict[str, Any]) -> list[dict[str, Any]]:
    raw_spans = render_plan.get("spans")
    if not isinstance(raw_spans, list) or not raw_spans:
        raise CompositionError("SPAN_SCHEMA", "render plan requires at least one span", "spans")
    slot_map = {item["name"]: item for item in profile["slots"]}
    frame_count = _bounded_integer(render_plan.get("frame_count"), "frame_count", 1, 2_000_000)
    result: list[dict[str, Any]] = []
    cursor = 0
    for index, raw in enumerate(raw_spans):
        field = f"spans[{index}]"
        if not isinstance(raw, dict):
            raise CompositionError("SPAN_SCHEMA", f"{field} must be an object", field)
        start_frame = _bounded_integer(raw.get("start_frame"), f"{field}.start_frame", 0, frame_count - 1)
        end_frame = _bounded_integer(raw.get("end_frame"), f"{field}.end_frame", 1, frame_count)
        if start_frame != cursor or end_frame <= start_frame:
            raise CompositionError("SPAN_COVERAGE", "spans must be contiguous, ordered, and non-empty", field)
        start_num = _bounded_integer(raw.get("start_time_num"), f"{field}.start_time_num", 0, 10**15)
        end_num = _bounded_integer(raw.get("end_time_num"), f"{field}.end_time_num", 1, 10**15)
        time_den = _bounded_integer(raw.get("time_den"), f"{field}.time_den", 1, 1_000_000)
        if end_num <= start_num:
            raise CompositionError("SPAN_TIME", "span time bounds must be increasing", field)
        placements: list[dict[str, Any]] = []
        for role in ("boke", "tsukkomi"):
            asset = _canonical_asset(raw.get(role), role, index)
            slot = slot_map[asset["stage_slot"]]
            placements.append(
                {
                    "op": "place-source-asset",
                    "role": role,
                    "slot": slot["name"],
                    "asset": asset,
                    "source_anchor": slot["source_anchor"],
                    "target_anchor": slot["target_anchor"],
                    "scale": slot["scale"],
                    "translation": slot["translation"],
                    "z_order": slot["z_order"],
                    "alpha_mode": "straight-preserve",
                }
            )
        placements.sort(key=lambda item: (item["z_order"], item["role"]))
        result.append(
            {
                "index": index,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "start_time_num": start_num,
                "end_time_num": end_num,
                "time_den": time_den,
                "clear": {"op": "clear-canvas", "background_rgba": profile["canvas"]["background_rgba"]},
                "placements": placements,
            }
        )
        cursor = end_frame
    if cursor != frame_count:
        raise CompositionError("SPAN_COVERAGE", "spans must cover the complete frame range", "spans")
    return result


def _build_expected(
    render_plan_manifest: Path,
    composition_profile: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
) -> tuple[dict[str, Any], dict[str, bytes], set[Path]]:
    plan_root = _root(render_plan_root, must_exist=True, field="render_plan_root")
    plan_relative, plan_path = _lexical_file_under_root(render_plan_manifest, plan_root, "render_plan_manifest")
    try:
        checked = check_render_plan_package(
            plan_path,
            plan_root,
            audio_preview_root,
            preview_root,
            package_root,
            audio_root,
        )
    except RenderPlanError as exc:
        raise CompositionError(f"RENDER_PLAN_{exc.code}", exc.message, exc.field or "render_plan_manifest") from exc
    render_plan = checked.get("render_plan")
    if not isinstance(render_plan, dict):
        raise CompositionError("RENDER_PLAN_RESULT", "render-plan validation result is malformed", "render_plan_manifest")
    plan_bytes = plan_path.read_bytes()
    profile, profile_bytes = validate_composition_profile(composition_profile)
    width = _bounded_integer(render_plan.get("width"), "render_plan.width", 1, MAX_CANVAS)
    height = _bounded_integer(render_plan.get("height"), "render_plan.height", 1, MAX_CANVAS)
    if profile["canvas"]["width"] != width or profile["canvas"]["height"] != height:
        raise CompositionError("CANVAS_MISMATCH", "composition profile canvas must match the render plan", "canvas")
    plan_id = render_plan.get("id")
    if not isinstance(plan_id, str) or not plan_id:
        raise CompositionError("RENDER_PLAN_SCHEMA", "render-plan ID is missing", "render_plan.id")
    intent = render_plan.get("intent")
    if intent not in {"evaluation", "production"}:
        raise CompositionError("INTENT", "render-plan intent is invalid", "render_plan.intent")
    license_status = render_plan.get("audio_license_status")
    if not isinstance(license_status, str) or not license_status:
        raise CompositionError("LICENSE_STATUS", "render-plan audio license status is missing", "render_plan.audio_license_status")
    fps_num = _bounded_integer(render_plan.get("fps_num"), "render_plan.fps_num", 1, 1_000_000)
    fps_den = _bounded_integer(render_plan.get("fps_den"), "render_plan.fps_den", 1, 1_000_000)
    frame_count = _bounded_integer(render_plan.get("frame_count"), "render_plan.frame_count", 1, 2_000_000)
    audio_placement = render_plan.get("audio_placement")
    upstream_bindings = render_plan.get("source_bindings")
    if not isinstance(audio_placement, dict) or not isinstance(upstream_bindings, dict):
        raise CompositionError("RENDER_PLAN_SCHEMA", "render-plan provenance or audio placement is missing", "render_plan")
    source_packages = _source_package_directories(
        plan_path,
        render_plan,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
    )
    spans = _build_span_transforms(render_plan, profile)
    transform_fingerprint = _sha(_json_bytes(spans))
    source_fingerprint = _sha(_json_bytes({"source_bindings": upstream_bindings, "audio_placement": audio_placement}))
    core = {
        "kind": "paper-theater-renderer-job",
        "schema_version": "1.0",
        "source_bindings": {
            "render_plan": {"id": plan_id, "path": plan_relative, "sha256": _sha(plan_bytes)},
            "composition_profile": {"id": profile["id"], "path": COMPOSITION_PROFILE, "sha256": _sha(profile_bytes)},
        },
        "intent": intent,
        "audio_license_status": license_status,
        "canvas": profile["canvas"],
        "fps_num": fps_num,
        "fps_den": fps_den,
        "frame_count": frame_count,
        "span_count": len(spans),
        "audio_placement": audio_placement,
        "operation_vocabulary": [
            "clear-canvas",
            "place-source-asset",
            "preserve-straight-alpha",
            "retain-audio-binding-unchanged",
        ],
        "transform_fingerprint": transform_fingerprint,
        "source_binding_fingerprint": source_fingerprint,
        "media_created": False,
    }
    job_id = content_identifier("paper-theater-renderer-job", core, 20)
    identified = {"id": job_id, **core}
    transform_doc = {
        "kind": "paper-theater-span-transform-inventory",
        "schema_version": "1.0",
        "renderer_job_ref": job_id,
        "render_plan_ref": plan_id,
        "composition_profile_ref": profile["id"],
        "frame_count": frame_count,
        "span_count": len(spans),
        "spans": spans,
    }
    source_doc = {
        "kind": "paper-theater-renderer-source-bindings",
        "schema_version": "1.0",
        "renderer_job_ref": job_id,
        "render_plan": identified["source_bindings"]["render_plan"],
        "composition_profile": identified["source_bindings"]["composition_profile"],
        "upstream": upstream_bindings,
        "audio_placement": audio_placement,
        "intent": intent,
        "audio_license_status": license_status,
    }
    generated = {
        COMPOSITION_PROFILE: profile_bytes,
        SPAN_TRANSFORMS: _json_bytes(transform_doc),
        SOURCE_BINDINGS: _json_bytes(source_doc),
    }
    files = [
        {"path": relative, "sha256": _sha(payload), "size": len(payload)}
        for relative, payload in sorted(generated.items())
    ]
    manifest = {**identified, "files": files}
    generated[RENDERER_JOB_MANIFEST] = _json_bytes(manifest)
    return manifest, generated, source_packages


def _write_package(output_root: Path, manifest: dict[str, Any], files: dict[str, bytes]) -> bool:
    root_path = _reject_lexical_path(output_root, "output_root")
    _reject_symlink_components(root_path, "output_root")
    if root_path.exists() and not root_path.is_dir():
        raise CompositionError("ROOT_TYPE", "output_root must be a directory", "output_root")
    root_path.mkdir(parents=True, exist_ok=True)
    root = root_path.resolve()
    destination = root / manifest["id"]
    if destination.is_symlink():
        raise CompositionError("OUTPUT_SYMLINK", "renderer-job destination must not be a symlink", "output_root")
    expected = set(files)
    if destination.exists():
        if not destination.is_dir():
            raise CompositionError("OUTPUT_CONFLICT", "renderer-job destination is not a directory", "output_root")
        actual: set[str] = set()
        for candidate in destination.rglob("*"):
            if candidate.is_symlink():
                raise CompositionError("OUTPUT_SYMLINK", "existing renderer-job package contains a symlink", str(candidate))
            if candidate.is_file():
                actual.add(candidate.relative_to(destination).as_posix())
        if actual != expected:
            raise CompositionError("OUTPUT_CONFLICT", "existing renderer-job file set differs", "output_root")
        for relative, payload in files.items():
            candidate = destination.joinpath(*safe_relative_path(relative).parts)
            if candidate.read_bytes() != payload:
                raise CompositionError("OUTPUT_CONFLICT", f"existing file differs: {relative}", relative)
        return False
    staging = root / f".{manifest['id']}.tmp"
    if staging.exists():
        if staging.is_symlink():
            raise CompositionError("STAGING_CONFLICT", "staging path is a symlink", "output_root")
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


def build_composition_job_package(
    render_plan_manifest: Path,
    composition_profile: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
    output_root: Path,
    *,
    write: bool = False,
) -> dict[str, Any]:
    manifest, files, source_packages = _build_expected(
        render_plan_manifest,
        composition_profile,
        render_plan_root,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
    )
    if write:
        _reject_output_overlap(output_root, source_packages)
        written = _write_package(output_root, manifest, files)
    else:
        written = False
    return {
        "ok": True,
        "renderer_job": manifest,
        "file_count": len(files),
        "written": written,
        "package_path": manifest["id"],
    }


def _manifest_location(manifest_path: Path, output_root: Path) -> tuple[Path, dict[str, Any], bytes]:
    root = _root(output_root, must_exist=True, field="output_root")
    _relative, resolved = _lexical_file_under_root(manifest_path, root, "renderer_job_manifest")
    return root, _load_object(resolved), resolved.read_bytes()


def check_composition_job_package(
    manifest_path: Path,
    output_root: Path,
    render_plan_root: Path,
    audio_preview_root: Path,
    preview_root: Path,
    package_root: Path,
    audio_root: Path,
) -> dict[str, Any]:
    root, manifest, payload = _manifest_location(manifest_path, output_root)
    if payload != _json_bytes(manifest):
        raise CompositionError("MANIFEST_CANONICAL", "renderer-job manifest JSON is not canonical", str(manifest_path))
    job_id = manifest.get("id")
    if not isinstance(job_id, str):
        raise CompositionError("MANIFEST_SCHEMA", "renderer-job ID is missing", "id")
    canonical = root / job_id / RENDERER_JOB_MANIFEST
    if manifest_path.expanduser().resolve() != canonical.resolve():
        raise CompositionError("MANIFEST_LOCATION", "renderer-job manifest path is not canonical", str(manifest_path))
    bindings = manifest.get("source_bindings")
    if not isinstance(bindings, dict) or not isinstance(bindings.get("render_plan"), dict):
        raise CompositionError("MANIFEST_SCHEMA", "source render-plan binding is missing", "source_bindings")
    source_relative = bindings["render_plan"].get("path")
    if not isinstance(source_relative, str):
        raise CompositionError("MANIFEST_SCHEMA", "source render-plan path is missing", "source_bindings.render_plan.path")
    plan_root = _root(render_plan_root, must_exist=True, field="render_plan_root")
    _normalized, source_manifest = _safe_existing_file(plan_root, source_relative, "source_bindings.render_plan.path")

    raw_plan = _load_object(source_manifest)
    early_sources = _source_package_directories(
        source_manifest,
        raw_plan,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
    )
    _reject_output_overlap(root, early_sources)

    profile_path = root / job_id / COMPOSITION_PROFILE
    expected_manifest, expected_files, source_packages = _build_expected(
        source_manifest,
        profile_path,
        plan_root,
        audio_preview_root,
        preview_root,
        package_root,
        audio_root,
    )
    _reject_output_overlap(root, source_packages)
    if manifest != expected_manifest:
        raise CompositionError("MANIFEST_BINDING_MISMATCH", "renderer-job manifest is stale or not canonical", str(manifest_path))
    destination = root / job_id
    expected_names = set(expected_files)
    actual_names: set[str] = set()
    for candidate in destination.rglob("*"):
        if candidate.is_symlink():
            raise CompositionError("PACKAGE_SYMLINK", "renderer-job package contains a symlink", candidate.relative_to(destination).as_posix())
        if candidate.is_file():
            actual_names.add(candidate.relative_to(destination).as_posix())
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise CompositionError("FILE_SET_MISMATCH", f"missing={missing}; extra={extra}", str(destination))
    for relative, expected_bytes in expected_files.items():
        candidate = destination.joinpath(*safe_relative_path(relative).parts)
        if candidate.read_bytes() != expected_bytes:
            raise CompositionError("FILE_MISMATCH", f"renderer-job file was modified: {relative}", relative)
    return {
        "ok": True,
        "renderer_job": manifest,
        "file_count": len(expected_files),
        "frame_count": manifest["frame_count"],
        "span_count": manifest["span_count"],
    }
