# Identity-lock experiment planning

Identity consistency is a gated stage after model selection and before expression/pose variants. This contract exists to prevent a return to independent text-to-image rerolls after a character design has been selected.

## Preconditions

An `identity-lock-plan` is only a plan. It does not select a model, approve a character, install a control model, train a LoRA, or generate an image.

The plan binds one exact production-eligible selected-model lock:

- model family;
- model profile reference and SHA-256;
- workflow SHA-256;
- source benchmark-review reference and SHA-256;
- explicit `production_eligible=true`.

The later real plan must be created from the exact owner-selected benchmark evidence. An evaluation-only model cannot be represented as production eligible.

## Canonical identities

The plan contains exactly one `boke` and one `tsukkomi` identity. Each role binds:

- candidate ID;
- generation-request ID;
- exact PNG SHA-256;
- safe local reference path.

This is the identity anchor for all runs in the matrix. Prompt-only identities and per-run model overrides are forbidden.

## Shared consistency matrix

Both roles use the same shared target sets:

- at least three unique pose targets;
- at least three unique expression targets.

Every strategy is expanded over the complete Cartesian product. The minimum required configuration therefore contains at least 36 runs:

`2 roles × 2 required strategies × 3 poses × 3 expressions`.

Run IDs and output paths are content-derived and deterministic. Every run carries the exact selected-model hashes and canonical role image SHA-256. Structural-control hashes are also carried when the strategy uses them.

## Required strategies

### `reference-only`

Uses the selected canonical identity reference as the appearance anchor. It provides a baseline for measuring how much pose control is actually needed.

### `reference-plus-pose`

Uses the same identity reference plus exactly one declared structural-control family:

- `openpose`;
- `lineart`;
- `depth`;
- `t2i-adapter`.

The strategy must bind one safe local control asset path and SHA-256 for every shared pose target. Missing, duplicated, or extra pose bindings fail closed.

### Optional `character-lora`

A character LoRA is not a shortcut around reference quality. It is allowed in the plan only when all of these exact hashes exist:

- curated dataset manifest SHA-256;
- training artifact SHA-256;
- training configuration SHA-256;

and both license and provenance states are explicitly `approved`.

## Read-only commands

```text
python -m ai_illustration.identity_lock plan-check PLAN.json
python -m ai_illustration.identity_lock matrix PLAN.json
```

`plan-check` validates only the contract. `matrix` expands the deterministic run matrix. Both commands are read-only and emit compact deterministic JSON to stdout.

The module performs no model/control/LoRA installation or download, no ComfyUI/provider/network call, no LoRA training, no image generation, and no filesystem write.

## Owner decision boundary

The matrix does not score or approve identity consistency. Fields for identity/aesthetic scores, ranks, winners, recommendations, automatic approval, automatic promotion, model overrides, or prompt-only identity are rejected.

After real runs exist, owner review must still decide whether both characters preserve face geometry, hairstyle/accessories, costume topology/palette, body proportions, line/shading behavior, and clean isolation across the required poses and expressions. Only that later explicit review may authorize production variants.
