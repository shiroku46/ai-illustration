# AI Illustration Pipeline

Local-first development for a fixed two-woman manzai character illustration workflow.

## Current capability

The repository defines product, architecture, style, and asset contracts and includes:

- a fixture-only manifest validation core;
- a deterministic local tool/hardware catalog;
- a non-executing ComfyUI API-workflow adapter that validates workflows and creates dry-run execution plans;
- a read-only local candidate comparison UI that exports structured review-decision JSON in the browser;
- deterministic expression/pose variant-set planning bound to one accepted candidate identity;
- deterministic packaging of caller-supplied local variant PNGs by verified byte copy, with sidecars and a paper-theater lookup index.

It does **not** install a model, start ComfyUI, contact a server, generate, edit, resize, or re-encode images. Export packaging reads local PNGs, verifies them, and copies their bytes unchanged only when `--write` is explicitly supplied.

Six production manifest types are supported: character specification, style profile, generation request, candidate asset, review decision, and export manifest. Variant-set plans and variant export packages are additional deterministic documents for downstream production stages.

The tool-evaluation catalog can validate synthetic tool/model profiles, list them deterministically, and compare declared hardware requirements with a local hardware profile. Compatibility never implies license or commercial-use approval.

The ComfyUI adapter accepts only loopback HTTP endpoints, rejects credentials and secret-like values, binds only explicitly allowlisted workflow inputs, records deterministic workflow checksums, and always remains in dry-run mode.

The review UI binds only to `127.0.0.1`, serves local static files and validated candidate PNGs only, never writes server-side files, and creates review JSON as a browser download.

Variant planning requires one `technically_valid` candidate, verified source PNG bytes, and the latest exact candidate/request/checksum-bound review to be `accept`. It emits stable variant IDs, planned PNG and sidecar paths, dimensions, identity bindings, unresolved design decisions, and paper-theater lookup keys without writing image bytes.

Variant export requires exactly one supplied local `<variant-id>.png` for every planned variant. It verifies PNG structure, dimensions, sRGB, alpha, and SHA-256; copies bytes unchanged into an atomic package; writes deterministic sidecars, a checksum inventory, and a paper-theater index; and rejects extra, missing, conflicting, escaped, or modified files.

## Run without installation

```bash
PYTHONPATH=src python -m ai_illustration.cli validate tests/fixtures/valid
PYTHONPATH=src python -m ai_illustration.cli catalog-validate tests/fixtures/catalog/tool-profile.json
PYTHONPATH=src python -m ai_illustration.cli catalog-list tests/fixtures/catalog/tool-profile.json
PYTHONPATH=src python -m ai_illustration.cli catalog-compat tests/fixtures/catalog/tool-profile.json tests/fixtures/catalog/hardware-profile.json
PYTHONPATH=src python -m ai_illustration.cli adapter-check tests/fixtures/comfyui/workflow-api.json
PYTHONPATH=src python -m ai_illustration.cli adapter-plan tests/fixtures/valid/generation-request.json tests/fixtures/comfyui/workflow-api.json --bindings tests/fixtures/comfyui/bindings.json
PYTHONPATH=src python -m ai_illustration.cli review-ui path/to/manifest-directory --asset-root path/to/asset-directory --port 8765
PYTHONPATH=src python -m ai_illustration.cli variant-plan path/to/manifest-directory --source-candidate candidate-id --matrix tests/fixtures/variants/matrix.json --intent evaluation
PYTHONPATH=src python -m ai_illustration.cli variant-check path/to/variant-set.json --manifest-root path/to/manifest-directory
PYTHONPATH=src python -m ai_illustration.cli variant-export path/to/variant-set.json --manifest-root path/to/manifest-directory --source-root path/to/supplied-variant-pngs --output-root path/to/packages
PYTHONPATH=src python -m ai_illustration.cli variant-export path/to/variant-set.json --manifest-root path/to/manifest-directory --source-root path/to/supplied-variant-pngs --output-root path/to/packages --write
PYTHONPATH=src python -m ai_illustration.cli variant-export path/to/production-variant-set.json --manifest-root path/to/manifest-directory --source-root path/to/supplied-variant-pngs --approval-root path/to/variant-reviews --output-root path/to/packages --write
PYTHONPATH=src python -m ai_illustration.cli export-check path/to/packages/variant-export-package-ID/package-manifest.json --output-root path/to/packages
PYTHONPATH=src python -m unittest discover -s tests
```

Commands write deterministic machine-readable JSON to stdout, a short summary to stderr, and return nonzero for invalid data. The long-running `review-ui` command prints its local URL and stops with `Ctrl+C`.

## Local candidate review

The manifest directory may contain character specifications, generation requests, candidate assets, and prior review decisions. Candidate images are optional: a missing, invalid, checksum-mismatched, non-sRGB, or non-alpha PNG is represented by a metadata placeholder rather than being served.

The browser UI supports deterministic candidate ordering, role/character/expression/pose/review filters, comparison of up to four candidates, provenance and checksum inspection, and structured decisions using the anti-AI and identity-drift categories. Downloaded review JSON includes the immutable candidate checksum and source request ID and can be validated with the existing `validate` command after it is placed with the referenced manifests.

Security boundaries:

- the listener is fixed to `127.0.0.1`;
- only `GET` and `HEAD` are accepted;
- static routes and candidate-image routes are explicit;
- manifest and asset paths must remain beneath their configured roots, including after symlink resolution;
- no remote scripts, fonts, analytics, cookies, storage dependency, uploads, or server-side mutation are used.

## Reviewed variant planning

A matrix explicitly supplies each requested expression, pose, facing, crop, and optional mouth state. Input order does not affect output. Duplicate combinations, unsafe tokens, stale reviews, mismatched checksums or references, unavailable or tampered candidates, unknown provenance, and unapproved production licensing fail closed.

Unresolved stage side, canvas override, editable source format, layer strategy, and mouth-shape granularity remain explicit nullable fields rather than inferred defaults. Evaluation plans remain non-commercial; production plans require approved source request, character, and style licensing states.

## Verified local export packages

The source directory is intentionally strict: it contains only one flat `<variant-id>.png` per canonical variant. Fuzzy lookup, nested source paths, symlinks, and extra files are rejected. Dry run is the default and leaves the output root untouched.

With `--write`, all outputs are built in a temporary directory beneath the output root and atomically published as one content-addressed package directory. Existing identical packages are accepted idempotently; differing packages are never overwritten. `export-check` verifies canonical package JSON and the SHA-256 inventory for every PNG, sidecar, and paper-theater index file.

Evaluation packages remain explicitly non-production and do not accept an approval root. Production packaging additionally requires one canonical `<variant-id>.json` review in `--approval-root` for every supplied PNG. Each review must be `accept` and bind the exact variant-set ID, variant ID, and supplied PNG SHA-256. A reviewed source identity and approved licensing alone do not approve newly supplied variant artwork.

A production variant review has exactly these fields:

```json
{
  "id": "variant-review-<content hash>",
  "kind": "variant-review-decision",
  "schema_version": "1.0",
  "variant_set_ref": "variant-set-...",
  "variant_id": "variant-...",
  "png_sha256": "<64 lowercase hex>",
  "decision": "accept",
  "reviewer": "owner"
}
```

The ID is the normal deterministic `variant-review` content identifier calculated from all fields except `id`.

## Validation boundaries

The manifest core checks deterministic identifiers and export paths, safe relative paths, SHA-256 format, dimensions, sRGB and alpha declarations, provenance, licensing state, review readiness, and cross-document references.

The catalog distinguishes:

- `hard-incompatible`: declared operating system, RAM, VRAM, runtime, adapter, or offline requirements fail;
- `missing-evidence`: hardware declarations pass but evidence or licensing review is incomplete;
- `compatible-by-declaration`: declared hardware requirements pass and required evidence fields are present.

The adapter distinguishes planning from authorization:

- every plan has `dry_run: true`;
- unresolved model identifiers or non-approved model license state set `executable_ready: false`;
- `execute()` is deliberately disabled;
- no socket, subprocess, external request, model loading, or image output is performed.

No compatibility, adapter, review, variant-planning, or export-packaging result grants a license or approves commercial use.
