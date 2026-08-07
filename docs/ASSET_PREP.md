# Deterministic asset preparation

Phase F prepares already owner-approved production variants for stable paper-theater placement. It is a technical normalization stage, not an image-generation or aesthetic-repair stage.

## Input gate

Asset preparation accepts only an export package that passes the existing full `check_export_package` verifier and remains:

- `intent=production`;
- `license_status=approved`;
- `identity_gate=owner-approved`;
- bound to exact identity-review, strategy, evidence-run, and selected-model hashes;
- populated with the formal per-variant owner reviews embedded by the production exporter.

Evaluation packages, unlocked identity projections, missing review bindings, or changed package bytes fail closed.

## Profile

An `asset-prep-profile` has one exact configuration for `boke` and one for `tsukkomi`. Each role declares a target canvas, integer target anchor, `bottom-center-visible-bounds` anchor policy, transparent-border inspection width, maximum permitted border alpha, minimum visible pixel count, and an exact rational ceiling for semitransparent pixels.

Profile IDs are content-derived. The manifest records the profile's canonical SHA-256 and version so later preparation cannot silently substitute different geometry or isolation thresholds.

## Isolation metrics

For every reviewed source PNG the preparation stage records:

- the exact `alpha > 0` visible bounding box;
- visible-pixel count;
- semitransparent (`0 < alpha < 255`) count;
- the reduced exact rational semitransparent fraction;
- nonzero-alpha pixel count within the configured outer border band.

No aesthetic score or confidence is calculated. These values are hard technical/isolation evidence only.

## Pixel-preserving normalization

The only permitted visible-image operation is:

1. identify the smallest bounding box containing every `alpha > 0` source pixel;
2. remove only fully transparent outer margins;
3. copy the cropped RGBA pixels unchanged onto a transparent caller-authored target canvas;
4. align the integer bottom-center anchor of that visible crop to the configured target anchor.

For an even crop width, the integer source anchor uses the left of the two center pixels: `(crop_width - 1) // 2`. No fractional translation is introduced.

The stage never scales, rotates, interpolates, recolors, sharpens, blurs, inpaints, vectorizes, synthesizes a background, or regenerates anatomy. After deterministic PNG encoding, the output is decoded again and every visible source RGBA pixel is compared at its translated destination. Any changed, dropped, or newly visible pixel fails closed.

## PNG boundary

The standard-library decoder accepts bounded non-interlaced 8-bit RGBA and grayscale-alpha PNGs with an explicit sRGB chunk. It validates PNG signature, chunk bounds, CRCs, filter reconstruction, zlib stream completion, dimensions, alpha, animation exclusion, and exact end-of-file after IEND. Output is deterministic 8-bit RGBA+sRGB PNG with fixed filter and compression settings.

## Manifest and publication

`asset-prep-manifest.json` is content-addressed and binds the exact source export-package bytes, variant-set SHA, profile SHA/version, role, identity projection, formal variant review refs/hashes, source/output PNG hashes and dimensions, isolation metrics, crop boxes, translations, and output paths.

Builds use a fresh explicit output directory only. Files are staged under the output parent, verified against the planned byte inventory, and atomically published. Existing/symlinked destinations and output paths inside the source package root are rejected. Source packages are never modified.

## CLI

Read-only validation and in-memory preparation planning:

```text
python -m ai_illustration.asset_prep check PROFILE.json PACKAGE_MANIFEST.json \
  --package-root PACKAGE_ROOT
```

Explicit fresh-directory build:

```text
python -m ai_illustration.asset_prep build PROFILE.json PACKAGE_MANIFEST.json \
  --package-root PACKAGE_ROOT \
  --output-dir PREPARED_OUTPUT
```

The commands perform no model/control/LoRA installation or training, ComfyUI/provider/network call, browser/server launch, automatic approval, or aesthetic ranking.
