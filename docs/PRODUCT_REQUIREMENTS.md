# Product requirements

## Status and decision labels

This document separates decisions into three classes:

- **Confirmed**: required by the trusted project direction and safe to build against.
- **Recommended**: a preferred implementation direction that still requires validation.
- **Unresolved**: a decision that must remain open until evidence or human review is available.

## Purpose

**Confirmed.** Build a local-first illustration production system for a fixed two-woman manzai act. The system will help create, compare, review, organize, and export character artwork that can later be displayed as paper-theater-style visuals synchronized to dialogue audio.

The product is not an autonomous publishing system. Human review remains the final authority for character identity, style quality, licensing suitability, and commercial use.

## Primary users

- **Confirmed:** the project owner acting as director, reviewer, and asset approver.
- **Recommended:** future collaborators who may review candidates or prepare approved assets without changing the canonical character identity.
- **Non-goal:** a public multi-tenant image-generation service.

## Core characters

- **Confirmed:** exactly two primary female characters, one boke and one tsukkomi.
- **Confirmed:** the pair must be visually readable as a duo while remaining individually identifiable.
- **Confirmed:** the final visual identities are not defined in this phase and must not be invented by implementation agents.
- **Recommended:** differentiate the pair through silhouette, posture, rhythm of shapes, facial construction, and controlled palette contrast rather than relying only on hair color or costume color.

## Scope

### In scope

1. Character reference and specification management.
2. Reproducible candidate-generation requests using locally available tools selected in later research.
3. Side-by-side candidate comparison with recorded metadata.
4. Human accept, reject, and shortlist decisions.
5. Controlled creation of expression and pose variants from approved character identities.
6. Export of transparent, consistently named assets for later paper-theater compositing.
7. Provenance, tool version, model version, prompt/configuration, seed, license-review status, and reviewer-decision tracking.
8. Validation that assets conform to required dimensions, naming, transparency, and metadata rules.

### Out of scope for the MVP

- Audio processing, lip sync, subtitle generation, or video rendering.
- Automatic publication, deployment, or monetization.
- Hosted paid generation as a required dependency.
- Training or downloading model weights.
- Final character-design selection without human approval.
- Claims that a model or tool is commercially usable without explicit license evidence.
- Fully automated aesthetic judgment.

## Functional requirements

### Character specification

- The system shall store one canonical specification per character.
- Each specification shall include stable character ID, role, approved visual references, design constraints, prohibited drift, palette guidance, silhouette guidance, and review status.
- Character specifications shall be versioned and shall not be overwritten silently.

### Candidate requests

- A request shall identify character ID, requested pose, expression, framing, style-profile version, tool/model identifier, configuration, seed when supported, and output intent.
- A request shall be reproducible from recorded metadata when the selected tool supports deterministic execution.
- The system shall preserve failed requests and failure reasons without treating them as approved output.

### Candidate review

- The system shall present comparable candidates with identical review fields.
- Review decisions shall support at least `shortlist`, `accept`, `reject`, and `needs_revision`.
- Rejection reasons shall use structured categories plus optional notes.
- Approval of a candidate shall record reviewer identity, timestamp, source request, and immutable asset checksum.

### Variant production

- Variants shall derive from an approved character identity and reference its version.
- The system shall prevent unreviewed variants from being marked production-ready.
- Each variant shall declare character, expression, pose, facing direction, crop, and version.

### Export

- Production exports shall use transparent PNG for raster delivery.
- The system shall emit a metadata sidecar for each export.
- Export names and paths shall be deterministic and collision-safe.
- Export validation shall fail closed when required metadata, alpha channel, dimensions, or identifiers are missing.

## Manzai-specific requirements

- **Confirmed:** default presentation places the boke and tsukkomi in stable left/right stage positions, with the final side assignment unresolved.
- **Confirmed:** expression and pose swaps must be possible without moving the whole composition.
- **Confirmed:** the asset system must support quick reaction changes suitable for dialogue timing.
- **Recommended:** each character should have a neutral base, speaking pose, listening pose, surprise reaction, laughter reaction, irritation/retort reaction, and embarrassment reaction.
- **Recommended:** silhouette and gesture should remain readable at reduced video size.
- **Unresolved:** canonical left/right assignment.
- **Unresolved:** whether mouth-open variants are required independently of expression variants.
- **Unresolved:** whether hand/arm overlays will be separate layers or baked into full-body variants.

## Quality requirements

### Visual consistency

- Approved variants must preserve identity-defining facial proportions, silhouette, costume anchors, and palette anchors.
- Controlled asymmetry and deliberate deformation are allowed; accidental anatomy corruption and identity drift are not.
- The output must follow the versioned image style guide.

### Anti-AI appearance

- Review must explicitly check line uniformity, contour over-cleanliness, excessive symmetry, generic eye construction, hand defects, repeated facial templates, polished gradient overuse, and mechanically balanced anatomy.
- A candidate may not pass solely because it is technically clean.
- The review system must retain human notes for subjective concerns that automated checks cannot prove.

### Reproducibility and provenance

- Every produced asset shall be traceable to a request and tool configuration.
- Checksums shall be recorded for approved source and exported files.
- Unknown provenance shall block production-ready status.

### Licensing and safety

- Tool and model licensing status shall be `unreviewed`, `reviewing`, `approved`, or `rejected`.
- `unreviewed` or `reviewing` tools may be used only for isolated evaluation and shall not authorize commercial release.
- No credential, token, private source, or paid-service action shall be stored in asset metadata.

## MVP

The MVP is complete when the repository can:

1. Represent two versioned character specifications without selecting their final designs.
2. Represent a versioned style profile and anti-AI review checklist.
3. Create and validate generation-request manifests without executing a model.
4. Register candidate asset metadata and structured review decisions.
5. Validate deterministic paths, identifiers, checksums, dimensions, transparency declarations, and required sidecars.
6. Export or simulate an export manifest for a minimum expression/pose matrix defined in `ASSET_SPECIFICATION.md`.
7. Pass automated tests using fixture metadata and synthetic placeholder files only.

The MVP deliberately excludes model installation and actual image generation.

## Later phases

1. **Tool research:** verify local tools, hardware requirements, output quality, licenses, and commercial-use conditions.
2. **Pipeline adapter:** implement one reviewed local tool adapter behind a stable interface.
3. **Candidate review UI:** add local comparison and structured approval workflow.
4. **Identity control:** add approved-reference and drift-evaluation support.
5. **Variant workflow:** generate and review expression/pose sets.
6. **Paper-theater integration:** expose approved assets and timing-friendly identifiers to the audio/video system.

## Acceptance criteria for implementation issues

Each implementation issue derived from this document must:

- define one bounded outcome;
- list exact allowed paths;
- state prohibited effects;
- use fixtures rather than downloaded models or generated production assets unless separately authorized;
- include deterministic tests;
- preserve local-first operation;
- identify unresolved product decisions instead of guessing;
- require exact-head CI, Unit Tests, and exact-SHA review before merge.

## Unresolved decisions

- Final visual identity of both characters.
- Final left/right stage assignment.
- Canonical canvas size beyond the baseline recommendation in the asset specification.
- Whether the source-of-truth editable format will be SVG, layered raster, another open format, or a mixture.
- Which local generation model and application will be adopted.
- Whether pose components will be layered separately.
- Required mouth-shape granularity for later audio synchronization.
- Hardware performance targets and batch-size expectations.
