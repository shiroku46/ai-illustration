# Reproducible model benchmark planning

The benchmark plan is a read-only contract. It does not install a model, call ComfyUI, generate an image, rank aesthetics, or select a winner.

## Required gates

A valid `model-benchmark-plan` binds exact canonical JSON bytes for:

- one `art-direction-profile`;
- its exact approving `art-direction-review`;
- one `hardware-profile`;
- at least three unique approved `model-configuration` tool profiles.

It also binds each model family to an exact local workflow JSON by raw-file SHA-256. The checker revalidates the art-direction approval, hardware declaration, model profile licensing and commercial-use review, offline capability, deterministic seed support, and declared hardware compatibility. A stale hash or changed dependency closes the gate.

Model profiles do not need to be installed for planning. Installation and execution remain later actions. They do need approved evidence and an approved model-configuration decision before they can enter the benchmark plan.

## Shared comparison contract

Every model receives the same shared seed list with at least eight unique non-negative integers and the same six prompt cases:

- `front-full-body-neutral`
- `three-quarter-readable-hands`
- `expressive-face-close-up`
- `seated-or-asymmetric-pose`
- `clothing-detail-stress`
- `two-character-secondary-stress`

The first five are single-role tests. The final case is explicitly a secondary two-character stress test. Each case records positive art-contract text, negative/anti-goal text, and stable crop, pose, and expression tokens.

Per-model settings preserve documented native behavior: width, height, sampler, scheduler, steps, CFG, prompt format, and an evidence note. Per-model seed overrides and automatic ranking/selection fields are not accepted.

## Workflow safety

Workflow paths must remain beneath the authorized workspace root and resolve to local, non-symlink, size-bounded regular JSON files. The checker validates raw SHA-256, requires an object root, and rejects obvious credential-like keys or values. It never submits the workflow.

## Deterministic matrix

The matrix expands in stable model-family, seed, and prompt-case order. Every row receives a content-derived `bench-...` run ID plus deterministic image and metadata paths. The matrix contains exactly:

```text
number of models × number of seeds × six prompt cases
```

No generated output is claimed to exist merely because a path appears in the matrix.

## Read-only commands

```text
python -m ai_illustration.model_benchmark plan-check PLAN.json \
  --workspace-root WORKSPACE --reference-root ART_REFERENCES

python -m ai_illustration.model_benchmark matrix PLAN.json \
  --workspace-root WORKSPACE --reference-root ART_REFERENCES
```

Both commands print deterministic compact JSON. Exit code `0` means the requested gate passed. Neither command writes files, accesses credentials, invokes a network or subprocess, downloads assets, executes a workflow, generates an image, measures performance, or chooses a model.

## Boundary

Real model files, actual licensed workflow files, benchmark execution, elapsed-time and peak-VRAM capture, result manifests, contact sheets, and owner visual review remain later gated work. A valid plan is permission to prepare reproducible local execution, not proof that any model is creatively suitable.
