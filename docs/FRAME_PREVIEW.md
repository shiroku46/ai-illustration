# Offline Rendered-Frame Preview

Phase 13 combines one verified Phase 12 frame-render package with the exact verified WAV-bound audio-preview package already referenced by its render plan.

The result is a content-addressed directory that can be opened locally through `index.html`. It contains copied frame PNGs, one copied WAV, deterministic player files, the copied frame inventory, and `frame-preview-manifest.json`.

## Safety and media boundaries

- Phase 12 and Phase 9 integrity checkers run before planning, writing, or checking.
- The render-plan binding must identify the exact caller-supplied audio-preview manifest by ID, canonical path, and SHA-256.
- PNG and WAV bytes are copied unchanged.
- Frame selection uses the exact rational intervals from `frame-inventory.json`.
- The audio clock is used while the WAV is active; the existing signed offset is preserved.
- Pre-audio and post-audio intervals use only the browser wall clock.
- The final partial frame remains clamped to the exact source inventory boundary.
- Playback never starts automatically.
- The package contains no remote URL, request API, telemetry, storage dependency, upload, recording, or executable renderer command.
- No image or audio transformation, muxing, or video encoding occurs.

A small CSS-only transition is applied only when the displayed frame checksum changes. It does not alter source media.

## Build

Dry run is the default:

```bash
PYTHONPATH=src python -m ai_illustration.frame_preview build \
  path/to/frame-renders/paper-theater-frame-render-package-ID/frame-render-manifest.json \
  path/to/audio-previews/paper-theater-audio-preview-ID/audio-preview-manifest.json \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/variant-packages \
  --audio-root path/to/original-audio \
  --output-root path/to/frame-previews
```

Add `--write` to publish the complete package atomically:

```bash
PYTHONPATH=src python -m ai_illustration.frame_preview build \
  path/to/frame-renders/paper-theater-frame-render-package-ID/frame-render-manifest.json \
  path/to/audio-previews/paper-theater-audio-preview-ID/audio-preview-manifest.json \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/variant-packages \
  --audio-root path/to/original-audio \
  --output-root path/to/frame-previews \
  --write
```

Identical existing output is accepted without rewriting it. Differing, missing, extra, traversing, overlapping, or symlinked files fail closed.

## Check

```bash
PYTHONPATH=src python -m ai_illustration.frame_preview check \
  path/to/frame-previews/paper-theater-frame-preview-package-ID/frame-preview-manifest.json \
  --output-root path/to/frame-previews \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/variant-packages \
  --audio-root path/to/original-audio
```

The checker reconstructs all generated text and verifies the exact copied inventory, PNG, and WAV bytes. Any stale source binding or changed output fails.

## Open locally

After a successful write, open the package's `index.html` manually in a browser. The controls provide play, pause, restart, and millisecond scrubbing. The package does not launch a browser or local server itself.

This preview is not a video file and is not a publication approval. Production intent remains dependent on every upstream licensing and human-review gate.
