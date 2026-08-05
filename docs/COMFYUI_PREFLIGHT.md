# Strict ComfyUI readiness preflight

This preflight checks an already-running local ComfyUI instance before any image-generation prompt is queued.

It consumes one **approved Phase 18 smoke bundle** and proves that:

- the bundle still passes its complete offline integrity check;
- the bundle is explicitly approved and execution-ready;
- the exact loopback endpoint recorded in the bundle is reachable;
- ComfyUI returns a bounded, valid system summary;
- every exact `class_type` used by the workflow is installed;
- the exact checkpoint filename bound in `generation-request.json` is installed.

A successful preflight does **not** generate an image, approve artwork, or publish media.

## Network boundary

The command permits GET requests only to:

- `/system_stats`;
- `/models/checkpoints`;
- `/object_info/{node_class}` for each exact bounded workflow class.

The implementation does not permit POST, `/prompt`, `/history`, `/view`, uploads, queue management, interrupts, model installation, custom-node management, websocket, proxy, redirect, credentials, cookies, LAN hosts, cloud hosts, or arbitrary returned URLs.

Responses are bounded by both `Content-Length` and actual bytes read. JSON is duplicate-key checked. The output includes only selected versions and device/VRAM facts; server argv, paths, environment values, and unrelated fields are discarded.

## Prerequisites

Complete Phase 18 first:

1. export one ComfyUI API-format workflow;
2. inspect it;
3. prepare an explicitly approved smoke bundle with owner-reviewed evidence;
4. run the offline bundle checker.

A default `reviewing` bundle is intentionally rejected before any network contact.

## Windows PowerShell flow

From the repository root:

```powershell
Set-Location "$HOME\ai-illustration"
git fetch origin
git reset --hard origin/main
$env:PYTHONPATH = Join-Path (Get-Location) "src"
```

Assume the approved bundle is under:

```text
local\bundles\comfyui-smoke-bundle-<id>\
```

Set reusable paths:

```powershell
$BundleRoot = ".\local\bundles"
$BundleManifest = Get-ChildItem `
    "$BundleRoot\comfyui-smoke-bundle-*\smoke-bundle-manifest.json" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName

$BundleManifest
```

First run the complete offline checker. ComfyUI may remain closed for this step:

```powershell
python -m ai_illustration.comfyui_smoke check `
    "$BundleManifest" `
    --output-root "$BundleRoot"
```

The result must contain:

```text
"execution_ready": true
```

Start Comfy Desktop and leave it running. Then run the non-generating preflight:

```powershell
python -m ai_illustration.comfyui_preflight run `
    "$BundleManifest" `
    --bundle-root "$BundleRoot" `
    --timeout-seconds 10
```

A successful result contains:

```text
"ok": true
"ready": true
"prompt_queued": false
```

It also reports the exact required checkpoint, the installed checkpoint count, all required workflow node classes, and a sanitized local system/device summary.

## Failure meanings

### `BUNDLE_NOT_APPROVED`

The bundle is still `reviewing`. Do not bypass this gate. Prepare a separate approved bundle only after reviewing the installed ComfyUI tool, exact checkpoint licensing/evidence, and commercial-use status.

### `HTTP_ERROR` or `HTTP_TIMEOUT`

ComfyUI is not running at the exact loopback endpoint recorded in the bundle, or startup is not complete. Start Comfy Desktop and wait for its workspace to become available.

### `CHECKPOINT_MISSING`

The exact checkpoint filename in the approved request is absent from ComfyUI's `checkpoints` model list. Do not substitute another checkpoint silently. Either install/review the intended checkpoint or export and approve a new workflow/bundle.

### `NODE_CLASSES_MISSING`

One or more workflow node classes are unavailable. The workflow may require a custom node package not installed in this ComfyUI instance. Do not remove nodes silently; install/review the required node implementation or create a new approved workflow.

### `HTTP_REDIRECT`

The endpoint redirected. Redirects are prohibited because preflight is bound to one exact loopback origin.

### `HTTP_RESPONSE_TOO_LARGE`

A server response exceeded the fixed preflight limit. No prompt was queued.

## Generation after readiness

Only after preflight reports `ready: true`, run the existing strict executor using the six files inside the same approved bundle:

```powershell
$BundleDirectory = Split-Path "$BundleManifest" -Parent

python -m ai_illustration.cli adapter-run `
    "$BundleDirectory\generation-request.json" `
    "$BundleDirectory\workflow-api.json" `
    --bindings "$BundleDirectory\bindings.json" `
    --tool-profile "$BundleDirectory\tool-profile.json" `
    --model-profile "$BundleDirectory\model-profile.json" `
    --execution-profile "$BundleDirectory\execution-profile.json" `
    --endpoint "http://127.0.0.1:8188" `
    --output-root ".\local\execution" `
    --execute
```

The endpoint must match the endpoint recorded in the bundle. Generated candidates remain `technically_valid` and `unreviewed`; visual human review is still required.

## Non-effects

The preflight performs no:

- filesystem write;
- ComfyUI launch;
- prompt queueing or image generation;
- history or image retrieval;
- model or custom-node installation;
- approval or visual decision;
- media publication.
