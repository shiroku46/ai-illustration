# Quality stages and the creative-candidate firewall

Technical integrity and creative suitability are independent facts. A valid PNG, a successful ComfyUI request, or an approved model license does **not** make an image a usable character design.

## Stages

| Stage | Meaning | Allowed next action |
| --- | --- | --- |
| `transport_smoke_output` | The local API, workflow binding, download, PNG parser, checksum, and package path worked. It is not a character candidate. | Technical diagnostics only. Never variant, export, paper-theater, or video planning. |
| `technical_candidate` | A real non-smoke candidate package passed file, provenance, request, and checksum validation. No aesthetic approval is implied. | Owner creative review against the art-direction and quality gate. |
| `creative_candidate` | A derived review result, never a generator/package status. The owner explicitly accepted the exact `technical_candidate` request and SHA-256 with creative scope and no hard-fail category. | Variant planning and later downstream stages. |

Generation and packaging may emit only `transport_smoke_output` or `technical_candidate`. No model, prompt, scorer, validator, or packaging function may emit `creative_candidate` automatically.

## Review fields

A new quality-aware review records these fields together:

- `review_scope`: `technical` or `creative`;
- `resulting_quality_stage`: the stage after the review;
- `hard_fail_categories`: a sorted unique list.

A technical `accept` confirms only technical observations and cannot cross the creative gate. Only a creative `accept` of an exact `technical_candidate` may yield `creative_candidate`, and its hard-fail list must be empty.

Legacy candidate/review documents without these fields remain readable where possible, but they fail closed at the creative and downstream gates. Repackage or re-review them rather than inferring approval.

## Enforced review and variant boundary

The backend review contract and variant planner enforce the following sequence:

1. The candidate must be a live checksum-verified `technical_candidate` before a creative review can be authored.
2. The review must bind the exact candidate ID, generation-request ID, and candidate SHA-256.
3. The reviewer identity, `review_scope=creative`, `decision=accept`, `resulting_quality_stage=creative_candidate`, and an empty hard-fail list must all be present.
4. The latest review for that candidate must still satisfy the complete gate when a variant set is planned.
5. A later reject, `needs_revision`, technical-only accept, stale checksum, missing quality stage, or transport smoke stage closes the downstream gate again.

Technical status and creative status remain separate. `technically_valid` means that the bytes and declared metadata passed technical checks; it does not mean that the image is approved as a character identity.

## Browser review workspace

The loopback review workspace remains read-only and local-only. Candidate cards and comparison cards display these facts separately:

- technical status;
- packaged quality stage;
- latest review scope;
- latest resulting quality stage;
- latest hard-fail categories.

The review form defaults explicitly to `technical`. Creative scope is enabled only for a live, SHA-256-verifiable `technical_candidate`; smoke output, a missing or unknown packaged stage, an unavailable image, an invalid technical status, unavailable Web Crypto, or a checksum failure closes the creative control. Accept and shortlist also require a freshly fetched matching PNG.

Every downloaded review contains the complete quality field group. Its ID is generated with Web Crypto SHA-256 over the same semantic identity as the backend: candidate ID, request ID, candidate checksum, reviewer, decision, scope, resulting stage, timestamp, sorted ordinary categories, and sorted hard-fail categories. The preview contains the exact JSON bytes used for the download. No browser storage, telemetry, upload, server mutation, remote asset, or external dependency is used.

## Hard-fail categories

- `malformed_or_missing_limb`
- `broken_joint_or_torso`
- `incoherent_clothing`
- `face_asymmetry`
- `unintended_background`
- `generic_ai_style`
- `identity_drift`
- `isolation_failure`

Reject or `needs_revision` reviews may record one or more of these categories. A creative `accept` may record none.

## Required order after the reset

1. Define the art-direction profile and its identity/non-goals.
2. Define a model benchmark manifest with fixed comparable inputs.
3. Generate model-separated contact sheets for owner review.
4. Introduce one reviewed local model at a time.
5. Promote only exact, clean owner-approved assets to downstream planning.

The SDXL Turbo smoke output remains evidence of transport execution only and must not be reused as a character identity, variant anchor, export source, repair input, or prompt-only shortcut.
