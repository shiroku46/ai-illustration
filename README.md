# AI Illustration Pipeline 1.0

Local-first, verified software for creating and reviewing two fixed female manzai characters and assembling their approved assets into synchronized paper-theater previews and local video exports.

## Software MVP status

The repository implements the complete software path from approved local generation inputs through:

- deterministic manifest and provenance validation;
- local tool/model evidence, compatibility, licensing, and installation profiles;
- fixed-seed ComfyUI workflow planning;
- deterministic inspection and preparation of owner-exported ComfyUI API workflows;
- strict non-generating ComfyUI node/checkpoint readiness preflight;
- explicit strict-loopback ComfyUI execution with deterministic candidate packaging;
- local candidate comparison and structured human review;
- reviewed expression/pose variant planning;
- checksum-verified transparent PNG export packages;
- two-character scene and timeline planning;
- fully offline image and WAV playback previews;
- rational frame planning and explicit composition profiles;
- deterministic RGBA frame rendering;
- offline rendered-frame playback;
- explicit-profile local FFmpeg video export;
- multi-track owner workspace status and an offline progress dashboard;
- stage-specific integrity checking throughout the pipeline.

Version `1.0.0` is certified by the canonical release contract at `release/mvp-v1.json` and the read-only release audit.

```bash
PYTHONPATH=src python -m ai_illustration.release_audit release/mvp-v1.json
```

The software has no runtime Python dependencies and does not install, download, or select ComfyUI, models, custom nodes, or FFmpeg.

## Owner workspace

A workspace can represent boke and tsukkomi as parallel tracks and join them at the shared paper-theater stages.

```bash
PYTHONPATH=src python -m ai_illustration.workspace status path/to/workspace.json
PYTHONPATH=src python -m ai_illustration.workspace build \
  path/to/workspace.json \
  --output-root path/to/dashboards \
  --write
```

Each declared stage is reported as `complete`, `not-started`, `blocked`, or `invalid`. A stage is complete only when its existing full repository checker succeeds. Caller-authored next actions are displayed but never executed.

## Generation boundary

Dry-run workflow planning remains available:

```bash
PYTHONPATH=src python -m ai_illustration.cli adapter-plan \
  path/to/generation-request.json \
  path/to/workflow-api.json \
  --bindings path/to/bindings.json
```

### ComfyUI smoke-test bundle preparation

An owner-exported API-format workflow can be inspected and converted into a deterministic bundle for the existing executor:

```bash
PYTHONPATH=src python -m ai_illustration.comfyui_smoke inspect \
  path/to/workflow-api.json

PYTHONPATH=src python -m ai_illustration.comfyui_smoke prepare \
  path/to/workflow-api.json \
  --output-root path/to/smoke-bundles \
  --review-date 2026-08-05 \
  --write

PYTHONPATH=src python -m ai_illustration.comfyui_smoke check \
  path/to/smoke-bundles/BUNDLE-ID/smoke-bundle-manifest.json \
  --output-root path/to/smoke-bundles
```

Preparation is offline and defaults to `reviewing`. It never infers tool or model license approval. An execution-ready bundle requires real owner-reviewed evidence URLs, a review date, and separate explicit acknowledgements for the installed tool, exact model, and commercial-use review. See `docs/COMFYUI_SMOKE_TEST.md` for the Windows PowerShell flow from Comfy Desktop API export through generated-package checking.

### Non-generating ComfyUI preflight

After an approved bundle passes its offline checker and ComfyUI is running, verify that the exact node classes and checkpoint exist before queueing a prompt:

```bash
PYTHONPATH=src python -m ai_illustration.comfyui_preflight run \
  path/to/smoke-bundles/BUNDLE-ID/smoke-bundle-manifest.json \
  --bundle-root path/to/smoke-bundles
```

The preflight allows bounded GET requests only to local system stats, the checkpoint list, and individual object-info routes for exact workflow classes. It never calls `/prompt`, retrieves images, starts a process, changes a queue, or writes files. See `docs/COMFYUI_PREFLIGHT.md`.

Actual generation requires explicit approved request/tool/model/execution profiles, an already-running local ComfyUI instance, and `--execute`:

```bash
PYTHONPATH=src python -m ai_illustration.cli adapter-run \
  path/to/generation-request.json \
  path/to/workflow-api.json \
  --bindings path/to/bindings.json \
  --tool-profile path/to/tool-profile.json \
  --model-profile path/to/model-profile.json \
  --execution-profile path/to/execution-profile.json \
  --endpoint http://127.0.0.1:8188 \
  --output-root path/to/candidate-packages \
  --execute
```

Only strict loopback HTTP routes are available. Proxies, redirects, credentials, cookies, LAN/cloud hosts, returned URLs, websocket access, and arbitrary endpoints are disabled. Generated candidates remain technically valid but unreviewed.

## Review and paper-theater path

The main CLI provides commands for:

- `review-ui`;
- `variant-plan` and `variant-check`;
- `variant-export` and `export-check`;
- `paper-plan` and `paper-check`;
- `preview-plan` and `preview-check`;
- `audio-preview-plan` and `audio-preview-check`;
- `render-plan` and `render-plan-check`;
- `composition-job` and `composition-job-check`;
- `frame-render` and `frame-render-check`.

All writes are explicit, content-addressed where applicable, and fail closed on stale bindings, path escape, symlinks, missing or extra files, and byte modification.

## Rendered preview and video export

```bash
PYTHONPATH=src python -m ai_illustration.frame_preview build ... --write
PYTHONPATH=src python -m ai_illustration.video_export plan ...
PYTHONPATH=src python -m ai_illustration.video_export run ... --timeout-seconds 1800
PYTHONPATH=src python -m ai_illustration.video_export check ...
```

Video export requires one caller-authored profile and one exact checksum-pinned local FFmpeg executable. It uses no shell and publishes only after bounded isolated execution and complete result verification.

## Validation

```bash
python scripts/public_export_guard.py .
python scripts/validate_repository.py
PYTHONPATH=src python -m unittest discover -s tests
```

## Completion boundary

Software completion does not mean final character art, model licensing, commercial approval, recorded audio, or publication decisions were supplied automatically. Those remain explicit owner and human-review prerequisites. See `docs/MVP_COMPLETION.md` for the exact distinction.
