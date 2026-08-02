# AI Illustration Pipeline

Local-first development for a fixed two-woman manzai character illustration workflow.

## Current capability

The repository defines product, architecture, style, and asset contracts and includes a fixture-only manifest validation core. It does **not** install a model or generate production images.

Six production manifest types are supported: character specification, style profile, generation request, candidate asset, review decision, and export manifest.

The tool-evaluation catalog can validate synthetic tool/model profiles, list them deterministically, and compare declared hardware requirements with a local hardware profile. Compatibility never implies license or commercial-use approval.

## Run without installation

```bash
PYTHONPATH=src python -m ai_illustration.cli validate tests/fixtures/valid
PYTHONPATH=src python -m ai_illustration.cli catalog-validate tests/fixtures/catalog/tool-profile.json
PYTHONPATH=src python -m ai_illustration.cli catalog-list tests/fixtures/catalog/tool-profile.json
PYTHONPATH=src python -m ai_illustration.cli catalog-compat tests/fixtures/catalog/tool-profile.json tests/fixtures/catalog/hardware-profile.json
PYTHONPATH=src python -m unittest discover -s tests
```

Commands write deterministic machine-readable JSON to stdout, a short summary to stderr, and return nonzero for invalid data.

## Validation boundaries

The manifest core checks deterministic identifiers and export paths, safe relative paths, SHA-256 format, dimensions, sRGB and alpha declarations, provenance, licensing state, review readiness, and cross-document references.

The catalog distinguishes:

- `hard-incompatible`: declared operating system, RAM, VRAM, runtime, adapter, or offline requirements fail;
- `missing-evidence`: hardware declarations pass but evidence or licensing review is incomplete;
- `compatible-by-declaration`: declared hardware requirements pass and required evidence fields are present.

No compatibility result grants a license or approves commercial use. No network request, image generation, model download, database, server, or hosted service is used.
