# Architecture

## Status notation

- **Confirmed:** required project boundary.
- **Recommended:** preferred design pending implementation evidence.
- **Unresolved:** intentionally undecided.

## Architectural goal

**Confirmed.** Provide a local-first, reproducible, review-gated pipeline that turns versioned character and style specifications into traceable candidate records and, after human approval, production-ready character assets for a two-person paper-theater presentation.

This phase defines boundaries and data flow only. It does not select, install, download, or execute an image-generation model.

## Guiding principles

1. Human approval is the final authority for character identity and visual quality.
2. Every artifact is traceable to an immutable request and configuration.
3. Unreviewed model or tool licensing never implies commercial approval.
4. Generation adapters are replaceable and isolated from domain data.
5. Candidate files are untrusted inputs until validated.
6. Production-ready status is a state transition, not a folder-copy convention.
7. File names and metadata paths are deterministic.
8. Failures are recorded and resumable; partial output is never silently promoted.
9. The core workflow must not require a paid hosted service.

## System context

```text
Character specification + style profile
                |
                v
       Request manifest builder
                |
                v
     Local generation adapter boundary
                |
                v
 Candidate intake and technical validation
                |
                v
 Comparison + structured human review
                |
                v
 Approved identity / approved variant registry
                |
                v
 Export validation + deterministic packaging
                |
                v
 Paper-theater asset consumer (later project phase)
```

## Component boundaries

### 1. Specification registry

Stores versioned, reviewable definitions for:

- characters;
- style profiles;
- pose and expression vocabularies;
- export profiles;
- tool and model license-review records.

Specifications contain no credentials and do not embed model weights or proprietary source assets.

### 2. Request manifest builder

Builds a normalized manifest containing:

- request ID and schema version;
- character and specification versions;
- style-profile version;
- pose, expression, crop, facing, and intended use;
- adapter and model identifiers without assuming availability;
- configuration and seed when supported;
- expected output constraints;
- creation timestamp and provenance.

The builder validates references before a request can enter an executable state.

### 3. Generation adapter boundary

A future adapter translates the normalized request into a specific local tool invocation.

Required adapter properties:

- no domain decision is hidden inside adapter code;
- inputs and outputs are declared;
- command construction is testable without execution;
- execution may be disabled for fixture-only tests;
- environment and hardware checks are explicit;
- timeout, cancellation, and failure classification are supported;
- stdout and logs are sanitized before persistence;
- credentials are not accepted by the core interface.

**Unresolved:** the first supported local tool and model.

### 4. Candidate intake

The intake service treats all generated or imported files as untrusted and records:

- source request ID;
- original filename and checksum;
- media type and byte size;
- declared and observed dimensions;
- alpha-channel status;
- color-space declaration;
- adapter/tool version;
- creation status and failure state.

Technical validation must complete before the candidate is shown as reviewable.

### 5. Review workspace

A local review layer presents candidates using consistent framing and fields. It records structured decisions and notes while preserving the original files.

Minimum decision states:

- `shortlist`;
- `accept`;
- `reject`;
- `needs_revision`.

The review layer shall not overwrite prior decisions. A later decision creates a new event linked to the same candidate.

### 6. Approved asset registry

An approved record binds:

- character identity version;
- source candidate checksum;
- approval event;
- expression and pose identifiers;
- production-readiness status;
- supersession and deprecation links.

Only approved records can become export sources.

### 7. Export packager

The packager creates deterministic delivery paths and metadata sidecars. It validates:

- canonical IDs and versions;
- expected dimensions;
- transparent PNG requirement for raster delivery;
- checksum and provenance;
- expression/pose matrix completeness for the requested profile;
- absence of unapproved files.

### 8. Paper-theater consumer boundary

The later consumer reads approved export manifests and addresses assets by stable identifiers. It must not depend on generator-specific prompts or internal candidate directories.

## Recommended repository structure

```text
config/
  schemas/
  vocabularies/
characters/
  specifications/
styles/
  profiles/
requests/
  manifests/
candidates/
  metadata/
reviews/
  decisions/
approved/
  registry/
exports/
  manifests/
src/
  domain/
  adapters/
  validation/
  review/
  export/
tests/
  fixtures/
```

Binary and generated directories should be ignored or stored outside Git history unless a later issue explicitly authorizes bounded fixtures. The repository should primarily track schemas, metadata fixtures, documentation, and code.

## Data flow and state model

### Request states

```text
draft -> validated -> executable -> running -> succeeded
                           |             |-> failed
                           |             |-> cancelled
                           |-> blocked
```

`executable` is unavailable until the chosen adapter and license-review policy permit it.

### Candidate states

```text
received -> technically_valid -> reviewable -> shortlisted -> approved
                    |               |             |-> rejected
                    |               |             |-> needs_revision
                    |-> invalid
```

An approved candidate may later be `superseded` but is not deleted from provenance records.

### Export states

```text
planned -> validated -> packaged -> verified
              |             |-> failed
              |-> blocked
```

## Deterministic identifiers and naming

Recommended identifiers:

- character: `boke` and `tsukkomi` as stable role IDs until final public names exist;
- request: sortable timestamp plus content-derived suffix or UUID;
- candidate: request ID plus candidate index and checksum prefix;
- variant: character, pose, expression, facing, crop, and version;
- export: schema-defined relative path from the same identifiers.

Example:

```text
exports/v1/boke/full/front/speaking-smile/boke__full__front__speaking-smile__v001.png
exports/v1/boke/full/front/speaking-smile/boke__full__front__speaking-smile__v001.json
```

Names shall use lowercase ASCII, hyphens inside vocabulary values, and double underscores between major fields.

## Metadata and provenance

Every sidecar should include:

- schema version;
- asset and character identifiers;
- source request and candidate identifiers;
- relevant specification versions;
- checksums;
- dimensions, format, and alpha status;
- adapter, tool, and model identifiers;
- seed/configuration when available;
- license-review status at creation and approval;
- review event references;
- timestamps in UTC;
- supersession status.

Prompts or configuration may be stored only when they contain no secrets or private material.

## Licensing boundary

A tool/model record has one of these states:

- `unreviewed`;
- `reviewing`;
- `approved`;
- `rejected`.

Research evidence must record source, license identifier/text location, version, commercial-use conditions, redistribution conditions, and unresolved ambiguities. The architecture does not infer permission from popularity or availability.

## Safety and trust boundaries

- Candidate media is untrusted and must not be executed.
- Metadata parsing must use bounded sizes and strict schemas.
- File paths must be repository-relative or workspace-relative and reject traversal.
- Checksums are calculated from bytes, not accepted from candidate metadata.
- Logs must not store credentials or private source paths.
- Model execution, downloads, network access, and paid APIs require separately authorized issues.
- Production exports require an approved human review event.

## Recovery and idempotency

- Re-running validation with identical input must produce the same result.
- Re-running packaging must not overwrite a different checksum under the same version.
- Incomplete work shall remain in a temporary or staging state.
- A failed adapter run preserves the request and sanitized error classification.
- Resume operations must check the immutable request ID and current output checksums.
- Manual deletion of candidate files must not silently remove provenance records; records become `missing` or `archived`.

## Observability

Recommended local records:

- structured operation events;
- validation reports;
- adapter execution summaries;
- review decision events;
- export manifests.

Reports should be machine-readable JSON plus concise human-readable summaries. No telemetry service is required.

## Phased implementation plan

### Phase 1: schemas and validators

- Define JSON schemas for character, style, request, candidate, review, and export records.
- Implement deterministic IDs and paths.
- Add fixture-only validation tests.
- Gate: no model or image execution.

### Phase 2: local catalog CLI

- Create commands to initialize specifications, validate manifests, register fixture candidates, record review decisions, and plan exports.
- Gate: all commands operate on local files and synthetic fixtures.

### Phase 3: tool and license research

- Compare candidate local tools using verifiable documentation and bounded experiments authorized separately.
- Gate: no commercial-use approval without evidence.

### Phase 4: one adapter

- Implement one reviewed adapter behind the stable boundary.
- Gate: explicit environment checks, dry-run mode, timeouts, and no hidden downloads.

### Phase 5: review UI

- Add a local interface for comparison and structured decisions.
- Gate: UI cannot mark production-ready without required metadata and review event.

### Phase 6: variant and export workflow

- Produce reviewed expression/pose sets and package them for paper-theater use.
- Gate: completeness, identity consistency, checksum, and sidecar validation.

## Validation gates for every phase

- exact issue scope and allowed paths;
- deterministic tests;
- no secrets or unapproved network/service use;
- schemas remain backward-compatible or include migrations;
- current exact-head CI and Unit Tests;
- exact-SHA independent review;
- human approval for aesthetic or commercial-use decisions.

## Unresolved decisions

- Implementation language and CLI framework.
- Storage database versus file-only catalog; file-only is recommended for the first phase.
- First local generation adapter and model.
- Editable source format.
- Hardware baseline and performance targets.
- Whether comparison UI is web-based or desktop-native.
- Whether mouth states are separate layers or full variants.
