# Owner identity-lock review

Identity-lock generation and comparison sheets are evidence, not approval. Production variants may advance only after the owner explicitly approves both role identities through this gate.

## Exact evidence binding

An `identity-lock-review` binds:

- the exact identity-lock plan ID/version/SHA-256;
- the exact complete result-set ID/version/SHA-256;
- the exact deterministic consistency-sheet package ID/manifest SHA-256;
- reviewer identity and UTC timestamp.

The checker rebuilds the expected consistency-sheet package from the verified plan and results, then compares every file beneath the supplied package root byte-for-byte. Missing, extra, changed, or symlinked package content fails closed.

## Approval decision

Only `decision=approve_identity_lock` can emit identity locks. Approval requires exactly one `boke` selection and one `tsukkomi` selection.

Each role selection binds:

- the canonical candidate ID, request ID, and identity PNG SHA-256;
- the exact selected production model family/profile/workflow hashes;
- one exact strategy from the identity-lock plan;
- every run in that role/strategy's complete shared pose × expression grid.

With the minimum plan, each role therefore accepts exactly nine successful checksum-verified runs covering three poses and three expressions. A partial subset, one lucky image, a failed run, another role, another strategy, or stale evidence cannot approve the identity.

The two accepted run sets cannot overlap.

## Rejected evidence

The owner may record failed visual examples separately as a run ID plus one or more stable hard-fail categories:

- `malformed_or_missing_limb`
- `broken_joint_or_torso`
- `incoherent_clothing`
- `face_asymmetry`
- `unintended_background`
- `generic_ai_style`
- `identity_drift`
- `isolation_failure`

An accepted run cannot also be rejected or carry hard-fail evidence.

## Other decisions

`reject` and `needs_revision` are valid owner records, but they must contain no role selections and emit no identity locks.

## Deterministic review identity

The review ID is derived from all decision semantics: exact evidence hashes, reviewer, timestamp, decision, normalized role selections and accepted run IDs, normalized rejected evidence/hard-fail categories, and sorted observations. Optional notes do not affect the review ID.

Scores, rankings, winners, recommendations, similarity confidence/thresholds, inferred strategies, automatic approval, and automatic downstream promotion are forbidden fields.

## Read-only command

```text
python -m ai_illustration.identity_lock_review review-check REVIEW.json PLAN.json RESULTS.json --result-root RESULTS_ROOT --package-root SHEET_PACKAGE_ROOT
```

The command performs no generation, network access, ComfyUI/provider call, model/control/LoRA installation or training, browser/server launch, or filesystem mutation.

A successful explicit approval returns exact `boke` and `tsukkomi` identity locks. Those locks are the authorization boundary for controlled production variants.

## Production variant boundary

A clean creative-candidate review remains necessary for every variant plan, but it is no longer sufficient for `intent=production`.

Production variant planning, canonical revalidation, and export must be supplied the exact same five identity-evidence inputs:

- owner identity-lock review;
- identity-lock plan;
- identity-lock results;
- live identity result root;
- deterministic consistency-sheet package root.

The gate is re-run at each production boundary. The role lock returned by the owner review must bind the exact source candidate ID, generation-request ID, and candidate PNG SHA-256. Its approved model profile ID must also match the source generation request's model ID. Stale evidence, changed sheets, a different candidate, a different model, or a non-approval decision closes the gate.

A production variant set records `identity_gate=owner-approved`, the exact review reference/checksum, selected strategy, accepted identity-evidence runs, and selected model family/profile/workflow hashes. These fields participate in deterministic variant identity so an unlocked evaluation plan cannot collide with a production plan.

Evaluation variant planning remains available after clean creative approval for non-production experimentation. It records `identity_gate=evaluation-unlocked` and carries no review, strategy, run, or production-model lock. Supplying production identity evidence to an evaluation command is rejected rather than silently ignored.
