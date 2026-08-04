# Bounded Local Video Export

Phase 14 converts one fully verified Phase 13 frame-preview package into one local MP4 only when the caller supplies both an explicit canonical export profile and an exact checksum-pinned FFmpeg executable.

## Boundary

- The repository never installs, downloads, discovers, updates, or selects FFmpeg.
- The caller supplies the executable path and records its exact SHA-256 in the profile.
- Only the `mp4-h264-aac-v1` family is accepted: MP4, `libx264`, AAC, `yuv420p`, and `alpha_policy=require-opaque-source`.
- Preset, CRF, AAC bitrate, maximum output bytes, and the FFmpeg checksum are caller-authored profile values.
- Every verified source frame is decoded with the existing bounded PNG decoder. Non-opaque pixels and odd canvas dimensions fail; no resize, crop, pad, flatten, or alpha discard occurs.
- The signed audio offset is preserved. Positive offsets add leading silence, negative offsets trim the source head, and all cases pad/trim audio to the exact scene duration.
- Planning never executes FFmpeg or mutates output.
- Execution uses an argument list with `shell=False`, no stdin, a sanitized environment, a fixed staging directory, a caller-bounded timeout, and a plan-addressed atomic package.
- CI tests use a mocked executable and do not publish encoded media.

## Canonical profile

A profile contains exactly:

- `family=mp4-h264-aac-v1`
- `container=mp4`
- `video_codec=libx264`
- `audio_codec=aac`
- `pixel_format=yuv420p`
- `alpha_policy=require-opaque-source`
- one supported x264 preset
- an integer CRF from 0 through 51
- an AAC bitrate from 32 through 512 kbps
- `movflags=+faststart`
- the exact lowercase FFmpeg SHA-256
- a bounded maximum output size

The profile ID is derived from the canonical profile body.

## Plan

```bash
PYTHONPATH=src python -m ai_illustration.video_export plan \
  path/to/frame-previews/PACKAGE/frame-preview-manifest.json \
  path/to/profiles/video-export-profile.json \
  --ffmpeg path/to/reviewed/ffmpeg \
  --frame-preview-root path/to/frame-previews \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/variant-packages \
  --audio-root path/to/original-audio \
  --profile-root path/to/profiles \
  --output-root path/to/video-exports
```

The returned plan contains placeholders rather than absolute machine paths. It binds the complete source package, canonical profile, executable checksum and size, exact frame rate/count, duration, signed audio placement, filter graph, output format, and safety limits.

## Run

```bash
PYTHONPATH=src python -m ai_illustration.video_export run \
  path/to/frame-previews/PACKAGE/frame-preview-manifest.json \
  path/to/profiles/video-export-profile.json \
  --ffmpeg path/to/reviewed/ffmpeg \
  --timeout-seconds 1800 \
  --frame-preview-root path/to/frame-previews \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/variant-packages \
  --audio-root path/to/original-audio \
  --profile-root path/to/profiles \
  --output-root path/to/video-exports
```

Successful execution publishes one plan-addressed directory containing:

- `video-export-plan.json`
- `video-export-manifest.json`
- `video.mp4`

A verified identical package is idempotent and does not execute FFmpeg again. Timeout, process failure, missing output, empty output, oversize output, unexpected staging files, or publication conflict removes the staging directory and publishes nothing.

## Check

```bash
PYTHONPATH=src python -m ai_illustration.video_export check \
  path/to/video-exports/PLAN-ID/video-export-manifest.json \
  path/to/profiles/video-export-profile.json \
  --ffmpeg path/to/reviewed/ffmpeg \
  --output-root path/to/video-exports \
  --frame-preview-root path/to/frame-previews \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/variant-packages \
  --audio-root path/to/original-audio \
  --profile-root path/to/profiles
```

Checking revalidates the full Phase 13 chain, profile, current executable fingerprint, plan, exact package file set, and recorded video SHA-256/size without re-encoding.

## Reproducibility statement

The package does not claim identical MP4 bytes across different FFmpeg binaries, versions, platforms, or builds. Its reproducibility scope is the exact source/profile/executable binding plus the checksum-recorded output bytes. Licensing and compatibility of the caller-supplied FFmpeg binary remain caller-reviewed responsibilities.
