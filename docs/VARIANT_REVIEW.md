# Owner review for generated variants

A generated pose/expression PNG is not approved merely because it belongs to a deterministic variant plan. This contract binds one owner decision to one exact identity-aware variant set and one exact live PNG.

## Exact bindings

A `variant-review-decision` records:

- exact canonical variant-set ID and SHA-256;
- exact variant ID and live PNG SHA-256;
- source candidate ID, generation-request ID, and source PNG SHA-256;
- all identity-gate fields copied exactly from the variant set;
- decision and resulting state;
- reviewer and UTC timestamp;
- stable hard-fail categories;
- owner observations.

The review ID is content-derived from all semantic fields above. Observation, hard-fail, and identity-evidence run lists are normalized for identity. Optional notes do not affect the ID.

## Live PNG gate

The checker revalidates the complete variant set first. A production set therefore requires the same five exact identity-evidence inputs used by production `variant-check`.

The reviewed PNG is then read from the variant's exact planned relative path beneath an explicit result root. The checker rejects path escape, symlinks, missing/non-regular files, oversized data, malformed PNG structure, trailing data, missing alpha or sRGB declaration, wrong dimensions, and checksum mismatch.

## Decisions and result states

Allowed decisions:

- `accept`;
- `reject`;
- `needs_revision`.

Result states are deterministic:

| Variant intent | Decision | Result state |
| --- | --- | --- |
| evaluation | accept | `evaluation-accepted` |
| production | accept | `production-variant-approved` |
| either | reject | `rejected` |
| either | needs_revision | `needs-revision` |

An evaluation acceptance is explicitly non-production. It cannot be promoted by changing only the review document.

A production acceptance requires `identity_gate=owner-approved` and an empty hard-fail list. Production identity review reference/checksum, selected strategy, accepted identity-evidence run IDs, and selected model family/profile/workflow hashes must exactly match the variant set.

## Hard-fail categories

The stable quality vocabulary is reused:

- `malformed_or_missing_limb`
- `broken_joint_or_torso`
- `incoherent_clothing`
- `face_asymmetry`
- `unintended_background`
- `generic_ai_style`
- `identity_drift`
- `isolation_failure`

Reject and needs-revision decisions may record these failures. A production accept may record none.

## Read-only command

```text
python -m ai_illustration.variant_review review-check REVIEW.json VARIANT_SET.json \
  --manifest-root MANIFESTS --result-root RESULTS
```

For a production variant set, also supply the same identity evidence used for production planning/checking:

```text
  --identity-review IDENTITY_REVIEW.json \
  --identity-plan IDENTITY_PLAN.json \
  --identity-results IDENTITY_RESULTS.json \
  --identity-result-root IDENTITY_RESULTS_ROOT \
  --identity-package-root IDENTITY_SHEETS_ROOT
```

All five options are all-or-none. Evaluation review rejects production identity evidence rather than ignoring it.

The review checker performs no image generation or repair, no filesystem mutation, no model/control/LoRA installation or training, no ComfyUI/provider/network call, and no automatic aesthetic or identity scoring/ranking/selection.

## Next boundary

This document format is the canonical owner-review evidence for future variant export integration. The existing exporter must not treat an older, less-bound review as equivalent to this contract once that integration unit is completed.
