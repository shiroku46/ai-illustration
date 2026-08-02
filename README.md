# AI Illustration Pipeline

Local-first development for a fixed two-woman manzai character illustration workflow.

## Current capability

The repository defines product, architecture, style, and asset contracts and includes a fixture-only manifest validation core. It does **not** install a model or generate production images.

Six manifest types are supported: character specification, style profile, generation request, candidate asset, review decision, and export manifest.

## Run without installation

```bash
PYTHONPATH=src python -m ai_illustration.cli validate tests/fixtures/valid
PYTHONPATH=src python -m unittest discover -s tests
```

The validator writes machine-readable JSON diagnostics to stdout, a short summary to stderr, and returns nonzero for invalid data.

## Validation rules

The MVP checks deterministic identifiers and export paths, safe relative paths, SHA-256 format, dimensions, sRGB and alpha declarations, provenance, licensing state, review readiness, and cross-document references.

No network request, image generation, model download, database, server, or hosted service is used.
