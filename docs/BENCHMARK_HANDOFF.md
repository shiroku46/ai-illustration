# Benchmark first-batch handoff

After the first real benchmark batch has been attempted, use the read-only handoff command to produce a compact sanitized JSON snapshot for the development coordinator.

The command validates the deterministic package, journals, stored images, and aggregate results before reporting anything. It does not contact ComfyUI, queue prompts, modify benchmark state, score images, rank models, or select a winner.

From the repository root:

```powershell
python -m ai_illustration.benchmark_handoff `
  local/benchmark-run-package `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --results-root local/benchmark-results `
  --limit 3
```

For the initial checkpoint, paste the complete one-line JSON output back to the development coordinator.

The handoff output contains only aggregate progress and a bounded run summary. Successful run summaries contain run/model/seed/case identity, elapsed time, image checksum, and image dimensions. Failed run summaries contain the same run identity plus a bounded error code/message. Local filesystem paths and secret-like token values are redacted.

The default limit is 3 and the maximum is 144. The reported runs follow deterministic benchmark package order; this makes the initial three-run checkpoint stable and reproducible.

If validation detects a changed package, journal, image, or aggregate result, the command fails closed and reports diagnostics instead of emitting unverified run summaries.
