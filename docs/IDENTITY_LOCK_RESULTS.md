# Identity-lock results and consistency sheets

This stage records evidence from the deterministic identity-lock matrix. It does not decide whether a character is consistent enough for production.

## Result contract

An `identity-lock-results` document binds the exact identity-lock plan by ID, version, and canonical SHA-256. There must be exactly one result entry for every matrix run and no extra run.

Each result repeats the matrix identity fields so stale or substituted evidence fails closed:

- exact selected model family/profile/workflow hashes;
- role;
- canonical candidate and generation-request IDs;
- canonical identity PNG SHA-256;
- strategy ID and type;
- pose and expression;
- structural-control SHA-256 when applicable.

Only two execution states are accepted:

- `succeeded`: exact local PNG path/SHA-256/dimensions plus elapsed time and optional peak VRAM;
- `failed`: stable error code/message plus elapsed time, with no image fields.

Succeeded images are re-read beneath an explicit local result root. Symlinks, path escape, missing/non-regular files, oversize files, malformed or animated PNGs, checksum/dimension mismatch, and trailing data are rejected.

Runtime and VRAM are evidence only. Identity scores, aesthetic scores, ranks, winners, recommendations, approvals, similarity thresholds, selected strategies, and automatic downstream promotion fields are forbidden.

## Consistency sheets

The renderer creates one standalone SVG for every `(role, strategy)` pair. Every sheet uses the same sorted pose rows and expression columns. Each cell identifies the exact run and shows either:

- the exact PNG bytes embedded as a `data:image/png;base64` image; or
- a visible failure tile with error code/message and elapsed time.

The package manifest binds:

- exact plan and result-set hashes;
- common pose/expression ordering;
- every SVG path, SHA-256, byte size, run ID set, and success/failure count;
- `decision_policy=owner-only`.

The SVGs contain no script, external CSS/font/link, remote asset, or active content. Rendering uses a fresh output directory, stages files before atomic publication, and never modifies the source result tree.

## Commands

```text
python -m ai_illustration.identity_lock_results results-check RESULTS.json PLAN.json --result-root RESULTS_ROOT
python -m ai_illustration.identity_lock_results render-sheets RESULTS.json PLAN.json --result-root RESULTS_ROOT --output-dir NEW_OUTPUT_DIR
```

`results-check` is read-only. `render-sheets` is the only command that writes and it writes only a new explicit output package directory.

## Owner boundary

A complete result set and clean consistency sheets are not approval. The owner must later review both roles across at least the required three poses and three expressions, compare controlled strategies, reject structural or identity failures, and explicitly authorize the identity lock before variant production can proceed.
