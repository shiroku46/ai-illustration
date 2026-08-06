# Art-direction contract

The quality reset requires an explicit art direction before model benchmarking. A prompt, a technically valid PNG, or a single attractive sample is not an art-direction contract.

## Documents

### `art-direction-profile`

One profile defines both members of the fixed female comedy duo. It must contain exactly one `boke` role and one `tsukkomi` role. Each role records the intended silhouette, body ratio, head/hand/foot exaggeration, costume construction, palette, line behavior, eye design, shading ceiling, front full-body neutral target, background/isolation target, identity anchors, and prohibited AI-like traits.

The profile also carries the complete global anti-goal vocabulary:

- `uniform_polished_linework`
- `generic_mobile_game_face`
- `over_rendered_lighting`
- `accidental_architecture_or_background_objects`
- `anatomical_collapse`
- `fused_or_missing_hands_or_limbs`
- `incoherent_clothing`
- `unintended_2_5d_rendering`

These are minimum exclusions, not an aesthetic score. Additional profile-specific anti-goals may be added as stable snake-case tokens.

A profile must bind at least one local visual reference for each role. Every reference records a safe POSIX relative path, role, declared purpose, supported image media type, and SHA-256. The checker reads only beneath the explicit reference root and rejects missing files, path escape, symlinks, non-regular files, zero or oversized files, media-signature mismatches, and checksum mismatches.

The profile status is only `draft` or `reviewing`. There is deliberately no self-declared `approved` profile status.

### `art-direction-review`

The owner review binds the exact canonical profile bytes through:

- profile ID;
- profile version;
- SHA-256 of canonical JSON plus one trailing LF;
- reviewer;
- UTC timestamp;
- decision;
- observations.

The review ID is derived from those semantic fields, excluding its own ID and optional free-form notes. Observations are sorted and deduplicated for identity calculation. Notes do not change the decision identity.

Only `decision=approve` authorizes the later benchmark-manifest stage, and only when the profile, both role contracts, every required anti-goal, and every live reference still validate. `reject` and `needs_revision` are valid records but never authorize benchmarking. Any profile edit or reference-byte change makes the prior approval stale.

## Read-only checks

```text
python -m ai_illustration.art_direction profile-check PROFILE.json --reference-root REFERENCES
python -m ai_illustration.art_direction approval-check PROFILE.json REVIEW.json --reference-root REFERENCES
```

Both commands print one deterministic compact JSON result and return exit code `0` only when the requested gate passes. They do not write, rename, copy, download, upload, generate, rank, select, or persist anything. They do not use a network, subprocess, shell, browser, model, ComfyUI instance, provider, credential, or external service.

## Boundary

This contract does not invent or approve the characters. Real reference boards or rough designs must still be supplied and approved by the owner. Model license review, benchmark manifests, fixed-seed execution, contact sheets, model selection, identity locking, expression/pose variants, and paper-theater production remain later gated stages.
