# Benchmark readiness preflight

This preflight is the final non-generating gate between local model installation and any benchmark prompt submission.

It verifies two separate boundaries:

1. the exact local model files installed under one explicit ComfyUI directory;
2. the running loopback ComfyUI instance’s required node classes and exact loader choices.

It never starts ComfyUI, downloads a model, writes a file, queues a prompt, retrieves an image or history, changes a queue, or selects a model.

## Prerequisites

The repository-side manifest and workflow contract must pass:

```powershell
python -m ai_illustration.model_install_manifest check `
  benchmark/model-install-manifest.v001.json `
  --workspace-root .
```

Install the exact reviewed artifacts with the dry-run-first helper documented in `docs/MODEL_INSTALL_MANIFEST.md`.

## Offline local-file check

Replace the path with the actual ComfyUI directory:

```powershell
python -m ai_illustration.benchmark_readiness offline-check `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --comfyui-root "C:\path\to\ComfyUI"
```

The check rejects:

- a missing model file;
- an unexpected byte size;
- a SHA-256 mismatch;
- a symlinked file or path;
- a file outside the exact manifest destination;
- a stale or changed repository profile, workflow, or manifest.

The large files are read only to calculate SHA-256. No timestamp, directory, model, or repository file is changed.

## Runtime check

Start the existing ComfyUI installation in its normal local-only mode, bound to loopback. Then run:

```powershell
python -m ai_illustration.benchmark_readiness runtime-check `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --comfyui-root "C:\path\to\ComfyUI" `
  --endpoint "http://127.0.0.1:8188"
```

The runtime check first repeats the complete offline gate. Network contact occurs only after all exact local files pass.

Allowed requests are limited to:

```text
GET /system_stats
GET /object_info/{exact-required-node-class}
```

Proxies and redirects are disabled by the shared preflight HTTP client. Credentials, arbitrary URLs, `/prompt`, history, image retrieval, queue management, model management, and custom-node management are not accepted.

## What readiness proves

A `ready: true` result proves only that:

- the exact reviewed local model artifacts are present;
- the committed API workflow templates still match their checksums and settings;
- the running ComfyUI exposes every required workflow node class;
- the exact checkpoint, diffusion-model, text-encoder, and VAE filenames appear in the matching loader choices.

It does not prove visual quality, benchmark success, production suitability, artistic approval, or model selection. Anima Aesthetic remains evaluation-only even when the preflight passes.

## Stable side-effect fields

Every result explicitly includes:

```json
{
  "network_contacted": false,
  "filesystem_mutated": false,
  "external_process_started": false,
  "prompt_queued": false
}
```

`network_contacted` becomes true only during the loopback runtime check after the offline gate succeeds. The other three values remain false.

## Failure handling

Do not rename or replace a failing artifact to bypass this check. A size or SHA-256 mismatch requires deleting the incorrect local file and repeating the exact reviewed installation procedure. A missing node class or loader choice requires correcting the local ComfyUI installation before any benchmark execution is allowed.
