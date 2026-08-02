# AI Illustration Pipeline

Local-first development for a fixed two-woman manzai character illustration workflow.

## Current capability

The repository defines product, architecture, style, and asset contracts and includes:

- a fixture-only manifest validation core;
- a deterministic local tool/hardware catalog;
- a non-executing ComfyUI API-workflow adapter that validates workflows and creates dry-run execution plans;
- a read-only local candidate comparison UI that exports structured review-decision JSON in the browser;
- deterministic expression/pose variant-set planning bound to one accepted candidate identity.

It does **not** install a model, start ComfyUI, contact a server, generate or transform images, or write production PNG files.

Six production manifest types are supported: character specification, style profile, generation request, candidate asset, review decision, and export manifest. Variant-set plans are a separate non-executing planning document for the next production stage.

The tool-evaluation catalog can validate synthetic tool/model profiles, list them deterministically, and compare declared hardware requirements with a local hardware profile. Compatibility never implies license or commercial-use approval.

The ComfyUI adapter accepts only loopback HTTP endpoints, rejects credentials and secret-like values, binds only explicitly allowlisted workflow inputs, records deterministic workflow checksums, and always remains in dry-run mode.

The review UI binds only to `127.0.0.1`, serves local static files and validated candidate PNGs only, never writes server-side files, and creates review JSON as a browser download.

Variant planning requires one `technically_valid` candidate and the latest exact candidate/request/checksum-bound review to be `accept`. It emits stable variant IDs, planned PNG and sidecar paths, dimensions, identity bindings, unresolved design decisions, and paper-theater lookup keys without writing image bytes.

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

A matrix explicitly supplies each requested expression, pose, facing, crop, and optional mouth state. Input order does not affect output. Duplicate combinations, unsafe tokens, stale reviews, mismatched checksums or references, unavailable candidates, unknown provenance, and unapproved production licensing fail closed.

Unresolved stage side, canvas override, editable source format, layer strategy, and mouth-shape granularity remain explicit nullable fields rather than inferred defaults. Evaluation plans remain non-commercial; production plans require approved source request, character, and style licensing states.

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

No compatibility, adapter, review, or variant-planning result grants a license or approves commercial use.
