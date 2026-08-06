# Exact three-model benchmark plan and run package

The canonical benchmark plan is:

```text
benchmark/model-benchmark-plan.v001.json
```

It binds the approved test art direction, owner approval, Windows RTX 4060 8GB / 32GB RAM hardware profile, three exact model profiles, three exact ComfyUI API workflow templates, eight shared seeds, and all six required prompt cases.

The matrix contains exactly:

```text
3 models × 8 seeds × 6 prompt cases = 144 runs
```

Anima Aesthetic remains evaluation-only. It may appear in the comparison package but cannot receive a production selected-model lock.

## Prepare the exact local art references

The approved image bytes are not committed. Use the exact original PNG files supplied by the owner.

Dry-run:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\prepare-art-references.ps1 `
  -BokePath "C:\path\to\楽子.png" `
  -TsukkomiPath "C:\path\to\櫻.png"
```

Copy after the dry-run succeeds:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\prepare-art-references.ps1 `
  -BokePath "C:\path\to\楽子.png" `
  -TsukkomiPath "C:\path\to\櫻.png" `
  -Execute
```

Default destination:

```text
local/art-references/楽子.png
local/art-references/櫻.png
```

The helper verifies the exact approved SHA-256 values before any copy, uses a temporary file, and never overwrites different bytes.

## Prepare the deterministic run package

After the references are present:

```powershell
python -m ai_illustration.benchmark_run_package prepare `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --reference-root local/art-references `
  --package-root local/benchmark-run-package
```

The command revalidates:

- the exact art-direction profile and owner approval against both local reference images;
- the hardware profile;
- every model profile and license scope;
- every committed template workflow and SHA-256;
- all plan-to-installation settings and file bindings.

It then writes one API-format workflow per run under:

```text
local/benchmark-run-package/runs/
```

and a deterministic package manifest:

```text
local/benchmark-run-package/benchmark-run-package.json
```

No network request or ComfyUI call occurs. The command does not queue a prompt, generate an image, score an output, rank a model, or select a winner.

## Verify the package later

```powershell
python -m ai_illustration.benchmark_run_package check `
  benchmark/model-benchmark-plan.v001.json `
  benchmark/model-install-manifest.v001.json `
  --workspace-root . `
  --reference-root local/art-references `
  --package-root local/benchmark-run-package
```

Missing, extra, changed, stale, or symlinked package files fail closed.

## Exact setting names

The existing benchmark-plan schema uses stable lowercase hyphenated setting aliases, such as `euler-ancestral` and `er-sde`. The exact ComfyUI spellings, `euler_ancestral` and `er_sde`, are independently checksum-bound in the installation manifest and committed workflows. The run-package generator compares both contracts and writes the exact ComfyUI spellings into every generated workflow.

## Remaining execution boundary

After the run package is prepared, the local model installation and non-generating readiness checks in `docs/MODEL_INSTALL_MANIFEST.md` and `docs/BENCHMARK_READINESS.md` must pass. Actual 144-run execution is a separate effectful stage and may not begin from a failed or stale readiness result.
