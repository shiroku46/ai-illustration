# Local video export

Phase 14 adds an explicit local FFmpeg boundary after the verified Phase 13 frame-preview package.

## What it does

The module:

- revalidates the complete frame-preview and upstream package chain;
- validates one canonical caller-authored video export profile;
- fingerprints one caller-supplied local FFmpeg executable by SHA-256;
- decodes every verified PNG with the existing bounded decoder and requires every alpha byte to be fully opaque;
- rejects odd canvas dimensions instead of silently padding, resizing, cropping, or flattening;
- produces a deterministic placeholder-based FFmpeg argument vector without storing absolute machine paths;
- invokes the executable only through an explicit `run` command with `shell=False`;
- stages the output, records its SHA-256 and size, and publishes only after a successful bounded run;
- verifies an existing package without re-encoding it.

It does not install FFmpeg, search `PATH`, download codecs, accept arbitrary command fragments, call a network service, or claim output bytes are identical across different FFmpeg binaries.

## Explicit profile

The first reviewed family is `mp4-h264-aac-v1`. It requires these explicit choices:

- MP4 container;
- `libx264` video;
- `yuv420p` pixel format;
- one allowlisted x264 preset and integer CRF from 0 through 51;
- AAC audio and an integer bitrate from 32 through 512 kbps;
- `require-opaque-source` alpha policy;
- exact numbered frames with no resize;
- stripped input metadata and a fixed creation-time field.

The implementation does not select this profile automatically. The caller supplies a canonical profile JSON whose ID is derived from all fields except `id`.

## Timing

The source frame rate is passed to the image2 input as the exact `fps_num/fps_den` ratio. Input starts at frame number zero and output is limited to the exact verified frame count and scene duration.

The Phase 13 signed audio offset is preserved:

- a positive offset uses `adelay` to add leading silence;
- a negative offset uses `atrim` and resets audio timestamps;
- zero offset starts audio at scene time zero;
- all cases use `apad` and a final `atrim` to match the exact scene duration.

The argument template uses `@FFMPEG@` and `@OUTPUT@` placeholders. Absolute executable and staging paths are substituted only at execution time and never affect the plan ID.

## Commands

Dry-run planning performs no filesystem mutation and does not invoke FFmpeg:

```bash
PYTHONPATH=src python -m ai_illustration.video_export plan \
  path/to/frame-preview-manifest.json \
  path/to/video-export-profile.json \
  --ffmpeg path/to/local/ffmpeg \
  --frame-preview-root path/to/frame-previews \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/packages \
  --audio-root path/to/audio \
  --profile-root path/to/profiles \
  --output-root path/to/video-exports
```

Execute the exact plan locally:

```bash
PYTHONPATH=src python -m ai_illustration.video_export run \
  path/to/frame-preview-manifest.json \
  path/to/video-export-profile.json \
  --ffmpeg path/to/local/ffmpeg \
  --frame-preview-root path/to/frame-previews \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/packages \
  --audio-root path/to/audio \
  --profile-root path/to/profiles \
  --output-root path/to/video-exports \
  --timeout-seconds 1800
```

Check an existing package without running FFmpeg:

```bash
PYTHONPATH=src python -m ai_illustration.video_export check \
  path/to/video-export-manifest.json \
  path/to/video-export-profile.json \
  --ffmpeg path/to/local/ffmpeg \
  --output-root path/to/video-exports \
  --frame-preview-root path/to/frame-previews \
  --frame-render-root path/to/frame-renders \
  --renderer-job-root path/to/renderer-jobs \
  --render-plan-root path/to/render-plans \
  --audio-preview-root path/to/audio-previews \
  --preview-root path/to/previews \
  --package-root path/to/packages \
  --audio-root path/to/audio \
  --profile-root path/to/profiles
```

## Execution safety

- the FFmpeg file must be regular, executable, non-symlinked, local, and no larger than 512 MiB;
- no shell is used and stdin is disabled;
- only a small environment allowlist is passed to the child process;
- stdout and stderr capture are bounded;
- execution timeout is bounded to 24 hours;
- output is bounded to 2 GiB and is written only to a temporary package directory;
- timeout, nonzero exit, diagnostic overflow, missing output, oversize output, conflict, and publication failure remove the staging directory;
- an already valid plan-addressed package is accepted without re-running FFmpeg.

## Reproducibility boundary

The plan is deterministic for identical source, profile, and executable bytes. The encoded file is recorded by exact SHA-256 and size. The project does not assert that a different FFmpeg binary, platform, linked codec build, or encoder implementation will produce identical video bytes.
