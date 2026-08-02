# AI Illustration Pipeline

Local-first development for a fixed two-woman manzai character illustration workflow.

## Current capability

The repository defines product, architecture, style, and asset contracts and includes:

- a fixture-only manifest validation core;
- a deterministic local tool/hardware catalog;
- a non-executing ComfyUI API-workflow adapter that validates workflows and creates dry-run execution plans.

It does **not** install a model, start ComfyUI, contact a server, or generate production images.

Six production manifest types are supported: character specification, style profile, generation request, candidate asset, review decision, and export manifest.

The tool-evaluation catalog can validate synthetic tool/model profiles, list them deterministically, and compare declared hardware requirements with a local hardware profile. Compatibility never implies license or commercial-use approval.

The ComfyUI adapter accepts only loopback HTTP endpoints, rejects credentials and secret-like values, binds only explicitly allowlisted workflow inputs, records deterministic workflow checksums, and always remains in dry-run mode.

## Run without installation

```bash
PYTHONPATH=src python -m ai_illustration.cli validate tests/fixtures/valid
PYTHONPATH=src python -m ai_illustration.cli catalog-validate tests/fixtures/catalog/tool-profile.json
PYTHONPATH=src python -m ai_illustration.cli catalog-list tests/fixtures/catalog/tool-profile.json
PYTHONPATH=src python -m ai_illustration.cli catalog-compat tests/fixtures/catalog/tool-profile.json tests/fixtures/catalog/hardware-profile.json
PYTHONPATH=src python -m ai_illustration.cli adapter-check tests/fixtures/comfyui/workflow-api.json
PYTHONPATH=src python -m ai_illustration.cli adapter-plan tests/fixtures/valid/generation-request.json tests/fixtures/comfyui/workflow-api.json --bindings tests/fixtures/comfyui/bindings.json
PYTHONPATH=src python -m unittest discover -s tests
```

Commands write deterministic machine-readable JSON to stdout, a short summary to stderr, and return nonzero for invalid data.

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

No compatibility or adapter result grants a license or approves commercial use.
