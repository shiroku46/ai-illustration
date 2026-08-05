# Local ComfyUI smoke test

This guide prepares one owner-exported ComfyUI API workflow for the existing strict-loopback execution boundary. Preparation and checking are offline. They do not start ComfyUI, queue a prompt, generate media, download a model, or approve licensing.

## Boundary

There are two bundle states:

- `reviewing`: deterministic files can be inspected and checked, but `adapter-run` will not execute them;
- `approved`: created only after the owner supplies reviewed evidence URLs, a review date, and three explicit acknowledgements. It is eligible for the existing strict-loopback executor but still produces evaluation-only, unreviewed candidates.

The software never turns compatibility or installation into license approval. The checkpoint filename remains a workflow input such as `sd_xl_turbo_1.0_fp16.safetensors`; it is not used as the stable model-profile ID.

## 1. Export an API-format workflow

1. Open Comfy Desktop and load the local image workflow.
2. Queue one image in ComfyUI first and confirm the workflow itself succeeds.
3. Open Settings with the gear icon or `Ctrl+,`.
4. Under Comfy settings, enable **Dev Mode / Enable dev mode options (API save, etc.)**.
5. Use **Save (API Format)** and save the file outside the future bundle output directory.

The required API format has node IDs as top-level keys and each node contains `class_type` and `inputs`. A normal editor workflow JSON with fields such as `version`, `nodes`, and `links` is not the same format.

Suggested local location:

```powershell
Set-Location "$HOME\ai-illustration"
New-Item -ItemType Directory -Force ".\local\e2e\comfyui-smoke-input" | Out-Null
# Save from ComfyUI as:
# .\local\e2e\comfyui-smoke-input\workflow-api.json
$env:PYTHONPATH = Join-Path (Get-Location) "src"
```

## 2. Inspect without writing

```powershell
python -m ai_illustration.comfyui_smoke inspect `
  ".\local\e2e\comfyui-smoke-input\workflow-api.json"
```

Inspection traces the selected sampler graph and reports:

- raw and canonical workflow SHA-256;
- node and class inventory;
- sampler, checkpoint, latent-size, prompt, and `SaveImage` node IDs;
- checkpoint filename, seed, steps, dimensions, and prompt text;
- proposed scalar request bindings;
- exact diagnostics when a required value is missing or ambiguous.

For an ambiguous workflow, pass explicit node IDs shown in the JSON:

```powershell
python -m ai_illustration.comfyui_smoke inspect `
  ".\local\e2e\comfyui-smoke-input\workflow-api.json" `
  --sampler-node "5" `
  --checkpoint-node "1" `
  --size-node "4" `
  --positive-node "2" `
  --negative-node "3" `
  --output-node "9"
```

Explicit scalar overrides are available through `--seed`, `--steps`, `--width`, `--height`, `--positive-prompt`, and `--negative-prompt`.

## 3. Prepare a reviewing bundle

A reviewing bundle records that installation is present but owner licensing review is incomplete. Dry run is the default.

```powershell
python -m ai_illustration.comfyui_smoke prepare `
  ".\local\e2e\comfyui-smoke-input\workflow-api.json" `
  --output-root ".\local\e2e\comfyui-smoke-bundles" `
  --review-date "2026-08-05"
```

Add `--write` to publish the content-addressed bundle:

```powershell
python -m ai_illustration.comfyui_smoke prepare `
  ".\local\e2e\comfyui-smoke-input\workflow-api.json" `
  --output-root ".\local\e2e\comfyui-smoke-bundles" `
  --review-date "2026-08-05" `
  --write
```

The bundle contains exactly:

```text
workflow-api.json
generation-request.json
bindings.json
tool-profile.json
model-profile.json
execution-profile.json
smoke-bundle-manifest.json
```

## 4. Complete owner review before execution

Actual `adapter-run --execute` requires an approved bundle. Review the installed ComfyUI license/evidence and the exact checkpoint/model license and commercial-use terms appropriate to the intended use. Then prepare a new approved bundle with real HTTPS evidence URLs and all three explicit acknowledgements:

```powershell
python -m ai_illustration.comfyui_smoke prepare `
  ".\local\e2e\comfyui-smoke-input\workflow-api.json" `
  --output-root ".\local\e2e\comfyui-smoke-bundles" `
  --profile-state approved `
  --review-date "2026-08-05" `
  --tool-evidence-url "https://OWNER-REVIEWED-COMFYUI-SOURCE" `
  --model-evidence-url "https://OWNER-REVIEWED-MODEL-SOURCE" `
  --confirm-tool-license `
  --confirm-model-license `
  --confirm-commercial-use `
  --write
```

The command refuses placeholder evidence URLs and refuses partial acknowledgement. Approved preparation invokes the existing offline execution-plan validator before publication; it still does not contact ComfyUI.

## 5. Check a saved bundle offline

Replace `BUNDLE-ID` with the `bundle_path` returned by `prepare`:

```powershell
python -m ai_illustration.comfyui_smoke check `
  ".\local\e2e\comfyui-smoke-bundles\BUNDLE-ID\smoke-bundle-manifest.json" `
  --output-root ".\local\e2e\comfyui-smoke-bundles"
```

The checker verifies every file, checksum, content ID, canonical JSON document, workflow selection, request binding, profile reference, dimensions, and reconstructed byte. It performs no HTTP request or external process execution.

## 6. Start ComfyUI and execute one image

Open Comfy Desktop and keep it running. Confirm local availability:

```powershell
Test-NetConnection 127.0.0.1 -Port 8188
Invoke-WebRequest "http://127.0.0.1:8188/" -UseBasicParsing -TimeoutSec 10 |
  Select-Object StatusCode
```

Set the approved bundle directory and run the existing executor:

```powershell
$bundle = ".\local\e2e\comfyui-smoke-bundles\BUNDLE-ID"

python -m ai_illustration.cli adapter-run `
  "$bundle\generation-request.json" `
  "$bundle\workflow-api.json" `
  --bindings "$bundle\bindings.json" `
  --tool-profile "$bundle\tool-profile.json" `
  --model-profile "$bundle\model-profile.json" `
  --execution-profile "$bundle\execution-profile.json" `
  --endpoint "http://127.0.0.1:8188" `
  --output-root ".\local\e2e\comfyui-executions" `
  --execute
```

The executor accepts only strict loopback HTTP, queues the exact bound workflow, retrieves only authorized `SaveImage` results, verifies PNG dimensions and checksums, and publishes a content-addressed candidate package. Generated candidates remain `technically_valid` and `unreviewed`.

## 7. Verify the generated package offline

Use the `package_path` returned by `adapter-run`:

```powershell
$execution = ".\local\e2e\comfyui-executions\EXECUTION-PLAN-ID"

python -m ai_illustration.cli adapter-run-check `
  "$execution\execution-manifest.json" `
  "$bundle\generation-request.json" `
  "$bundle\workflow-api.json" `
  --bindings "$bundle\bindings.json" `
  --tool-profile "$bundle\tool-profile.json" `
  --model-profile "$bundle\model-profile.json" `
  --execution-profile "$bundle\execution-profile.json" `
  --endpoint "http://127.0.0.1:8188" `
  --output-root ".\local\e2e\comfyui-executions"
```

This final check performs no HTTP request. Visual acceptance and later variant production remain separate human-review steps.
