# Stratified three-model benchmark smoke checkpoint

Before running the full deterministic 144-run matrix, attempt one identical baseline case from each reviewed model family.

The smoke checkpoint reuses the exact existing benchmark package. It does not create new run IDs, workflows, prompts, settings, or result schemas. The three selected runs are the existing package entries with:

```text
seed = 101
prompt_case_id = front-full-body-neutral
```

One matching run is selected for each model family in deterministic family order.

## Read-only smoke status

```powershell
python -m ai_illustration.benchmark_smoke status `
  local/benchmark-run-package `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --results-root local/benchmark-results
```

This command does not contact ComfyUI and does not create the results directory when no results exist.

## Run the three-model smoke checkpoint

Start the reviewed local ComfyUI instance manually first, then run:

```powershell
python -m ai_illustration.benchmark_smoke run `
  local/benchmark-run-package `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --results-root local/benchmark-results `
  --comfyui-root "C:\path\to\ComfyUI" `
  --endpoint http://127.0.0.1:8188 `
  --execute
```

The command validates the package and local runtime before queueing any prompt, then attempts only the missing smoke runs. Successful and failed attempts are journaled through the same interruption-safe result contract used by the full benchmark.

A failed smoke run is not retried automatically. After investigating the failure, an explicit retry is available:

```powershell
python -m ai_illustration.benchmark_smoke run `
  local/benchmark-run-package `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --results-root local/benchmark-results `
  --comfyui-root "C:\path\to\ComfyUI" `
  --endpoint http://127.0.0.1:8188 `
  --execute `
  --retry-failed
```

## Return the checkpoint to the coordinator

After the smoke command returns, produce the sanitized read-only handoff snapshot:

```powershell
python -m ai_illustration.benchmark_handoff `
  local/benchmark-run-package `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --results-root local/benchmark-results `
  --limit 3
```

Paste the complete one-line JSON output back to the development coordinator before continuing the full matrix.

The three smoke results are ordinary members of the 144-run matrix. When the full resumable executor is later started, valid completed smoke runs are verified and skipped rather than generated again.

No smoke command scores aesthetics, ranks models, recommends a winner, selects a production model, downloads models, or starts ComfyUI.
