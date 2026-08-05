"""Transport-only classification for prepared ComfyUI smoke bundles."""

from __future__ import annotations

import json
from typing import Any

from . import comfyui_smoke_bundle as _base
from .comfyui_smoke_common import MANIFEST_FILE, REQUEST_FILE, _json_bytes, _sha
from .naming import content_identifier
from .quality import TRANSPORT_SMOKE_INTENT

_ORIGINAL_BUNDLE_OBJECTS = _base._bundle_objects


def _quality_bundle_objects(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    manifest, generated = _ORIGINAL_BUNDLE_OBJECTS(*args, **kwargs)
    request = json.loads(generated[REQUEST_FILE].decode("utf-8"))
    request["output_intent"] = TRANSPORT_SMOKE_INTENT
    request_bytes = _json_bytes(request)
    generated[REQUEST_FILE] = request_bytes

    for entry in manifest["files"]:
        if entry["path"] == REQUEST_FILE:
            entry["sha256"] = _sha(request_bytes)
            entry["size"] = len(request_bytes)
            break
    else:
        raise RuntimeError("smoke bundle request inventory entry is missing")

    core = {key: value for key, value in manifest.items() if key != "id"}
    manifest = {"id": content_identifier("comfyui-smoke-bundle", core, 20), **core}
    generated[MANIFEST_FILE] = _json_bytes(manifest)
    return manifest, generated


if not getattr(_base, "_quality_stage_patched", False):
    _base._bundle_objects = _quality_bundle_objects
    _base._quality_stage_patched = True
