# Owner benchmark review and selected-model lock

Contact sheets, success counts, timing, and VRAM usage are evidence. They do not select a production model. The benchmark-review contract keeps that decision explicitly human and binds it to exact local bytes.

## Exact evidence binding

A `model-benchmark-review` binds the canonical SHA-256 of:

- the validated fixed-seed benchmark plan;
- the complete validated benchmark result set;
- the deterministic contact-sheet package manifest.

The checker also rebuilds the expected contact-sheet package from the exact result set and compares the supplied package directory with it. The canonical manifest and every SVG must be present with identical bytes, size, and SHA-256. Missing, extra, changed, stale, or symlinked package entries close the gate.

## Decisions

Only three decisions exist:

- `select_model`: explicitly locks one exact model family, model-profile reference/SHA-256, and workflow SHA-256 from the benchmark plan;
- `reject_all`: records that none of the benchmarked model families advances;
- `needs_revision`: records that new or corrected benchmark evidence is required.

`reject_all` and `needs_revision` may not contain a selected model or accepted runs. They never authorize identity-lock work.

## Multiple-seed evidence requirement

A `select_model` decision is valid only when the owner lists at least four verified successful runs from the selected family. The accepted set must span:

- at least three distinct seeds;
- at least three distinct prompt cases;
- `front-full-body-neutral`;
- `three-quarter-readable-hands`.

This prevents one lucky seed or a face-only result from advancing a model. Accepted runs must be exact successful result IDs; failed, unknown, duplicate, stale, or other-family runs are rejected. Accepted and rejected run sets cannot overlap.

Rejected evidence may be accompanied by the stable hard-fail vocabulary already used by the creative gate:

- `malformed_or_missing_limb`
- `broken_joint_or_torso`
- `incoherent_clothing`
- `face_asymmetry`
- `unintended_background`
- `generic_ai_style`
- `identity_drift`
- `isolation_failure`

The hard-fail list never automatically chooses a model. It records the owner's observations about rejected evidence.

## Deterministic identity

The review ID is derived from the exact plan, result-set, and package hashes plus reviewer, UTC timestamp, decision, selected-model fields, sorted accepted and rejected run IDs, sorted hard-fail categories, and sorted observations. Optional notes do not affect the decision identity.

Fields for scores, ranks, winners, recommendations, confidence, automatic approval, or derived selection are not accepted.

## Read-only command

```text
python -m ai_illustration.benchmark_review review-check REVIEW.json RESULTS.json PLAN.json \
  --workspace-root WORKSPACE \
  --reference-root ART_REFERENCES \
  --result-root RESULTS_ROOT \
  --package-root CONTACT_SHEET_PACKAGE
```

The command prints deterministic compact JSON and returns exit code `0` only when the complete evidence and review gate passes. A valid `select_model` response includes the exact selected-model lock. No lock is returned for `reject_all` or `needs_revision`.

The checker performs no writes and does not launch a browser or server, access a network or provider, invoke ComfyUI, install or download a model or workflow, execute a benchmark, generate or repair an image, or infer a model from timing, success counts, or contact-sheet order.

## Boundary

This gate selects a model/workflow foundation only after real owner-reviewed benchmark evidence exists. It does not select the final boke or tsukkomi character design, create canonical character sheets, prove identity consistency, approve a LoRA dataset, or authorize uncontrolled text-to-image variants. Those remain later gated stages.
