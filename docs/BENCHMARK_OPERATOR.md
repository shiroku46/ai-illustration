# Windows benchmark operator

`tools/benchmark-operator.ps1` is the reviewed owner-side entry point for the exact three-model bake-off. It coordinates existing repository helpers; it does not implement a downloader, start ComfyUI, rank images, or select a model.

The script is intentionally conservative:

- no effect switches: read-only status/planning only;
- `-Prepare`: verifies and copies the two exact owner reference PNGs, then creates or verifies the deterministic 144-run package;
- `-InstallModels`: invokes the reviewed exact-artifact installer and requires both license/scope acknowledgements;
- `-ExecuteBenchmark`: runs runtime readiness, then invokes the bounded resumable executor;
- `-Finalize`: renders contact sheets only after no benchmark runs remain pending.

## 1. Read-only status

From the repository root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\benchmark-operator.ps1
```

No `local/` directory is created by this mode.

If the ComfyUI directory already exists, it can be supplied to report exact local model state and offline readiness without downloading anything:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\benchmark-operator.ps1 `
  -ComfyUIRoot "C:\path\to\ComfyUI"
```

## 2. Prepare owner references and the 144-run package

Use the exact two source PNGs whose bytes were approved. Their original filenames do not matter; the helper stores them under the exact art-direction-bound names `boke-rakuko.png` and `tsukkomi-sakura.png`.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\benchmark-operator.ps1 `
  -BokePath "C:\path\to\boke-source.png" `
  -TsukkomiPath "C:\path\to\tsukkomi-source.png" `
  -Prepare
```

This stage does not contact ComfyUI or download a model.

## 3. Install the reviewed model artifacts

This is the large-download step and is never implicit. Supply the actual local ComfyUI root and both acknowledgements:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\benchmark-operator.ps1 `
  -ComfyUIRoot "C:\path\to\ComfyUI" `
  -InstallModels `
  -AcknowledgeExactArtifacts `
  -AcknowledgeAnimaEvaluationOnly
```

The underlying installer verifies exact size and SHA-256 for every artifact and preserves Anima Aesthetic as evaluation-only.

## 4. Start ComfyUI manually

The coordinator never starts or discovers ComfyUI. Start the existing installation in local-only mode yourself. The expected default endpoint is:

```text
http://127.0.0.1:8188
```

Only explicit HTTP loopback endpoints are accepted.

## 5. First small benchmark batch

Run a small batch first:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\benchmark-operator.ps1 `
  -ComfyUIRoot "C:\path\to\ComfyUI" `
  -ExecuteBenchmark `
  -MaxRuns 3
```

The coordinator first requires runtime readiness. The executor then queues one exact workflow at a time, journals completed/failed runs atomically, and can be interrupted and resumed.

If the first batch is healthy, continue with a larger bound, for example:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\benchmark-operator.ps1 `
  -ComfyUIRoot "C:\path\to\ComfyUI" `
  -ExecuteBenchmark `
  -MaxRuns 48
```

Previously completed runs are verified and skipped. Failed runs are not repeated unless `-RetryFailed` is explicitly supplied.

## 6. Render contact sheets

After status reports zero pending runs:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\benchmark-operator.ps1 `
  -Finalize
```

Contact sheets are written under:

```text
local/benchmark-contact-sheets/
```

They are owner-review material only. No aesthetic score, recommendation, approval, or model selection is produced by the coordinator.

## Local outputs

All mutable owner-machine state remains under the ignored `local/` directory:

```text
local/art-references/
local/benchmark-run-package/
local/benchmark-results/
local/benchmark-contact-sheets/
```

The repository continues to hold only deterministic contracts, exact hashes, scripts, tests, and review logic.
