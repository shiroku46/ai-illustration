# Asset specification

## Status notation

- **Confirmed:** required contract.
- **Recommended:** default that may be revised through a versioned decision.
- **Unresolved:** intentionally open.

## Purpose

Define stable identifiers, file requirements, metadata, versioning, and the minimum expression/pose matrix for boke and tsukkomi assets intended for later audio-synchronized paper-theater rendering.

This specification authorizes no model download, generated production image, proprietary source asset, or commercial-use claim.

## Canonical character identifiers

Use stable internal role IDs until final public names are approved:

- `boke`
- `tsukkomi`

Public display names may change without changing these IDs.

## Delivery format

### Raster delivery

- **Confirmed:** PNG with alpha transparency.
- **Confirmed:** sRGB color space.
- **Confirmed:** straight-alpha semantics in metadata; consumers must not assume premultiplied storage.
- **Confirmed:** transparent pixels must not contain visible background fragments or matte halos.
- **Recommended:** 8-bit RGBA for the MVP.

### Editable source

- **Recommended:** retain a structured or layer-friendly source where the chosen creation tool permits it.
- **Unresolved:** canonical editable format. Candidates include SVG, layered raster, or another documented open/interoperable format.
- Editable source files are not required for fixture-only MVP validation.

## Canvas and framing

### Baseline profile `stage-full-v1`

- **Recommended canvas:** 2048 × 2048 pixels.
- **Recommended visible character height:** 1700–1900 pixels for full-body variants.
- **Recommended anchor:** stage-floor point centered at `(1024, 1900)`.
- **Confirmed:** all variants within one profile use the same canvas, scale convention, and anchor semantics.
- **Confirmed:** transparent padding is preserved to avoid composition jumps during swaps.

### Additional crops

- `full`: complete body and required transparent padding.
- `half`: waist-up crop with a profile-specific anchor.
- **MVP requirement:** `full` only.
- **Unresolved:** whether `half` becomes mandatory for close-up scenes.

Dimensions are versioned by export profile. A later profile may change resolution without mutating `stage-full-v1`.

## Facing and stage semantics

Facing identifiers:

- `front`
- `inward`
- `outward`
- `left`
- `right`

The MVP uses `front` and may use `inward` after the canonical stage-side assignment is approved.

**Unresolved:** which character occupies the left and right stage positions. Side assignment belongs in a separate composition profile, not in the character ID.

## Vocabulary identifiers

Identifiers use lowercase ASCII and hyphens. Display labels may be localized separately.

### Expression IDs

Minimum vocabulary:

- `neutral`
- `speaking-neutral`
- `smile`
- `laugh`
- `surprise`
- `embarrassed`
- `concerned`
- `irritated`
- `retort`

### Pose IDs

Minimum vocabulary:

- `standing-neutral`
- `speaking-open`
- `listening`
- `small-reaction`
- `large-reaction`
- `pointing`
- `arms-folded`

Not every expression/pose combination is mandatory. The matrix below defines the MVP set.

## Minimum variant matrix

Each character requires these seven approved full-body variants before a complete `stage-full-v1` set can be marked ready:

| Variant ID | Pose | Expression | Primary use |
|---|---|---|---|
| `base-neutral` | `standing-neutral` | `neutral` | Idle/default |
| `speaking` | `speaking-open` | `speaking-neutral` | Ordinary dialogue |
| `listening` | `listening` | `neutral` | Other character speaking |
| `smile` | `speaking-open` | `smile` | Light positive beat |
| `laugh` | `large-reaction` | `laugh` | Strong laughter |
| `surprise` | `large-reaction` | `surprise` | Unexpected turn |
| `role-reaction` | role-specific | role-specific | Boke/tsukkomi distinction |

Role-specific requirements:

- `boke`: `small-reaction` + `embarrassed` or another human-approved comic reaction.
- `tsukkomi`: `pointing` or `arms-folded` + `retort` or `irritated`.

The exact aesthetic interpretation remains human-approved. Metadata identifiers must remain stable once an exported set is released.

## Optional mouth-state layer

**Unresolved.** Later audio synchronization may require separate mouth states:

- `closed`
- `open-small`
- `open-wide`

The MVP shall not assume these are separate files. If added, the export profile must declare whether mouth states are full variants or overlays and must define compositing anchors.

## Directory layout

Recommended tracked metadata layout:

```text
characters/specifications/<character-id>/<version>.json
styles/profiles/<style-id>/<version>.json
requests/manifests/<request-id>.json
candidates/metadata/<request-id>/<candidate-id>.json
reviews/decisions/<candidate-id>/<review-id>.json
approved/registry/<character-id>/<asset-id>.json
exports/manifests/<export-set-id>.json
```

Recommended delivery layout:

```text
exports/<profile-version>/<character-id>/<crop>/<facing>/<variant-id>/
  <character-id>__<crop>__<facing>__<variant-id>__v<version>.png
  <character-id>__<crop>__<facing>__<variant-id>__v<version>.json
```

Example:

```text
exports/stage-full-v1/boke/full/front/surprise/
  boke__full__front__surprise__v001.png
  boke__full__front__surprise__v001.json
```

## File naming

Pattern:

```text
<character>__<crop>__<facing>__<variant>__v<NNN>.<extension>
```

Rules:

- lowercase ASCII only;
- fields separated by double underscores;
- vocabulary words use single hyphens;
- version is three digits starting at `001`;
- filename fields must match sidecar fields exactly;
- never overwrite bytes under an existing filename with a different checksum.

## Asset sidecar

Required fields:

```json
{
  "schema_version": "1.0",
  "asset_id": "boke__full__front__surprise__v001",
  "character_id": "boke",
  "character_spec_version": "1.0.0",
  "style_profile_version": "1.0.0",
  "export_profile": "stage-full-v1",
  "variant_id": "surprise",
  "pose_id": "large-reaction",
  "expression_id": "surprise",
  "crop": "full",
  "facing": "front",
  "width": 2048,
  "height": 2048,
  "color_space": "sRGB",
  "alpha": true,
  "sha256": "<64 lowercase hex characters>",
  "source_request_id": "<request-id>",
  "source_candidate_id": "<candidate-id>",
  "approval_review_id": "<review-id>",
  "license_review_status": "approved",
  "created_at": "<UTC RFC 3339 timestamp>",
  "supersedes": null
}
```

Additional adapter, tool, model, seed, and configuration provenance belongs in the request/candidate records and may be summarized in the asset sidecar.

## Metadata requirements

- Schema versions use semantic or explicitly documented versioning.
- Timestamps use UTC RFC 3339.
- SHA-256 is calculated from the referenced file bytes.
- Relative paths reject `..`, absolute paths, null bytes, and platform-dependent separators in stored canonical form.
- Unknown provenance or missing approval blocks `production_ready` status.
- License-review state must be explicit and never inferred.

## Versioning

### Asset version

Increment when approved visible bytes change.

- Patch-like corrections still create a new asset version.
- An existing released filename is immutable.
- Superseded assets remain addressable in provenance records.

### Specification version

Increment character/style specification versions when identity or style constraints change. New assets reference the exact versions used.

### Export profile version

Create a new profile ID/version when canvas, anchor, framing, alpha semantics, or required matrix changes incompatibly.

## Set manifest

A set manifest lists all production-ready assets for one character and export profile. Required fields include:

- set ID and schema version;
- character and profile identifiers;
- exact asset IDs and checksums;
- required-matrix result;
- approval status;
- creation timestamp;
- superseded set reference when applicable.

A duo manifest references one approved set for each character plus a composition-profile version.

## Paper-theater compatibility

The downstream renderer must be able to:

- load assets by stable ID without knowing generator internals;
- swap variants without changing canvas size or anchor;
- place the characters independently at stable stage anchors;
- select a fallback such as `base-neutral` when a requested optional variant is unavailable;
- verify checksums and manifest versions;
- distinguish production-ready assets from candidates;
- map future timing events to character and variant IDs.

No renderer should read files directly from candidate or review directories.

## Validation rules

### Hard failures

- filename/sidecar mismatch;
- missing or invalid checksum;
- checksum mismatch;
- incorrect dimensions for profile;
- missing alpha channel for raster delivery;
- unknown character, pose, expression, crop, facing, or profile ID;
- missing source request/candidate/approval references;
- production-ready asset with license status other than `approved`;
- duplicate immutable path with different bytes;
- path traversal or absolute path;
- incomplete required matrix for a set declared complete.

### Warnings requiring review

- unusual visible bounding box or floor-anchor distance;
- excessive transparent margin outside profile tolerance;
- palette deviation from character specification;
- near-duplicate variant checksum/perceptual similarity;
- missing editable source where one is recommended;
- optional mouth-state inconsistency.

Warnings do not automatically approve or reject aesthetics.

## Fixture policy

Automated tests may use:

- synthetic one-color or geometric PNG fixtures;
- tiny bounded placeholder images created in tests;
- metadata-only examples;
- deliberately invalid fixtures for failure tests.

Tests must not include generated character art, model weights, copyrighted proprietary references, or production assets unless a later trusted issue explicitly authorizes them.

## Unresolved decisions

- Final canonical canvas profile after renderer testing.
- Final stage-side assignment.
- Editable source format.
- Separate overlay strategy for mouths, arms, or effects.
- Required close-up crop profiles.
- Final public names and localized display labels.
