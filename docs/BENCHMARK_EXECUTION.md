# Resumable local benchmark execution

This command is the bounded execution layer for the exact 144-run package prepared by `benchmark_run_package`.

It does not download models, start ComfyUI, change queues, install custom nodes, rank images, recommend a winner, or select a production model. It only contacts the explicitly configured loopback ComfyUI instance after the exact local model/runtime readiness gate passes.

## Prerequisites

Before execution:

1. place the two approved owner reference PNGs with `tools/prepare-art-references.ps1`;
2. prepare `local/benchmark-run-package` with `benchmark_run_package`;
3. install the exact reviewed model files with `tools/install-benchmark-models.ps1`;
4. start the existing ComfyUI installation bound to loopback.

The executor repeats the exact runtime readiness preflight itself before the first pending prompt, so a missing/tampered model, unavailable loader choice, or missing required node stops execution before `/prompt` is used.

## Read-only progress

This command never contacts ComfyUI:

```powershell
python -m ai_illustration.benchmark_execute status `
  local/benchmark-run-package `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --results-root local/benchmark-results
```

It reports succeeded, failed, pending, completion state, and the next run ID. Existing journals and PNGs are verified before they are counted.

## Execute or resume

Replace the ComfyUI path with the actual installation directory:

```powershell
python -m ai_illustration.benchmark_execute run `
  local/benchmark-run-package `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --results-root local/benchmark-results `
  --comfyui-root "C:\path\to\ComfyUI" `
  --endpoint "http://127.0.0.1:8188" `
  --execute
```

The default upper bound is 144 attempts in one invocation. For a small first batch:

```powershell
--max-runs 3
```

A failed run is recorded once and is not automatically repeated. To explicitly retry failed runs:

```powershell
--retry-failed
```

Each workflow is queued one at a time. The executor polls only that prompt ID, retrieves only its declared output PNG, validates the expected dimensions, and atomically stores the local result before moving to the next run.

## Interruption and resume

A completed run has two local artifacts:

```text
local/benchmark-results/journal/<run-id>.json
local/benchmark-results/images/<run-id>.png
```

The aggregate document is:

```text
local/benchmark-results/model-benchmark-results.v001.json
```

If the process is interrupted, a run without a valid completed journal remains pending. On the next invocation, every existing journal and PNG is revalidated against the exact benchmark package before it is reused. Different bytes are never silently overwritten.

## Network boundary

Execution is restricted to the existing `ComfyUIHttpClient` boundary:

```text
POST /prompt
GET /history/{prompt_id}
GET /view
```

Only `http://localhost`, `http://127.0.0.1`, or `http://[::1]` origins are accepted. Proxies and redirects are disabled by the shared client. Credentials, cookies, arbitrary URLs, queue clearing, model management, and custom-node management are not supported.

Before execution, readiness uses the read-only routes documented in `BENCHMARK_READINESS.md`.

## Results boundary

The aggregate file conforms to the existing `model-benchmark-results` contract. It records execution facts only: run identity, model/profile, seed, prompt case, settings, elapsed time, image checksum/dimensions, or a bounded failure code/message.

It contains no aesthetic score, ranking, recommendation, creative approval, or automatic model selection. Anima Aesthetic remains evaluation-only regardless of execution success.
