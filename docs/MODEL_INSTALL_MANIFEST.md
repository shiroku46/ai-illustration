# Exact benchmark model installation

The model files remain owner-controlled local inputs. This repository commits only reviewed profiles, exact artifact metadata, API-format workflow templates, validation code, and a fail-closed Windows installer.

Canonical records:

```text
benchmark/model-install-manifest.v001.json
benchmark/model-profiles/*.json
benchmark/workflows/*.api.json
```

## Repository-side verification

Run this before any local download:

```powershell
python -m ai_illustration.model_install_manifest check `
  benchmark/model-install-manifest.v001.json `
  --workspace-root .
```

A successful result verifies:

- exact profile bytes and canonical profile hashes;
- benchmark, production-model, and commercial-output license scopes;
- artifact URLs, filenames, destinations, byte sizes, and SHA-256 values;
- exact committed workflow bytes and workflow hashes;
- model-loader topology, fixed seed, sampler, scheduler, steps, CFG, resolution, prompts, and output node.

It does not claim that the large model files are installed.

## Exact local files

Relative to the owner’s ComfyUI directory:

| Model | Destination | Exact filename | Bytes | SHA-256 | Scope |
|---|---|---:|---:|---|---|
| Animagine XL 4.0 Opt | `models/checkpoints` | `animagine-xl-4.0-opt.safetensors` | 6,940,833,562 | `6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac` | production candidate |
| Illustrious XL v2.0 Stable | `models/checkpoints` | `Illustrious-XL-v2.0.safetensors` | 6,938,043,394 | `c2a1a3eaa13d4c107dc7e00c3fe830cab427aa026362740ea094745b3422a331` | production candidate |
| Anima Aesthetic v1.1 | `models/diffusion_models` | `anima-aesthetic-v1.1.safetensors` | 4,181,385,600 | `3c1868387a3a1ff504bbb87c33678321965ead381fcf87afbd0264daa600c082` | evaluation only |
| Anima text encoder | `models/text_encoders` | `qwen_3_06b_base.safetensors` | 1,192,135,096 | `cd2a512003e2f9f3cd3c32a9c3573f820bb28c940f73c57b1ddaa983d9223eba` | required by Anima |
| Anima VAE | `models/vae` | `qwen_image_vae.safetensors` | 253,806,246 | `a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f` | required by Anima |

Total download size is 19,506,203,898 bytes, approximately 18.17 GiB. Do not rename the files.

## Dry-run installation plan

The helper is dry-run by default. Replace the path with the actual ComfyUI directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install-benchmark-models.ps1 `
  -ComfyUIRoot "C:\path\to\ComfyUI"
```

Dry-run behavior:

- verifies the exact reviewed manifest bytes;
- verifies any already-present artifact by byte size and SHA-256;
- reports missing files as `planned-download`;
- creates no directories;
- performs no network request;
- launches no process and does not start ComfyUI.

Any existing file with the expected name but different bytes causes an immediate failure and is never overwritten.

## Explicit installation

After reviewing the dry-run output, execute:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install-benchmark-models.ps1 `
  -ComfyUIRoot "C:\path\to\ComfyUI" `
  -Execute `
  -AcknowledgeExactArtifacts `
  -AcknowledgeAnimaEvaluationOnly
```

The helper downloads only the exact HTTPS URLs in the reviewed manifest. Each file is downloaded to a unique partial path, checked for exact byte size and SHA-256, and moved into place only after verification. A mismatched or racing target is never replaced.

## Fixed workflow templates

No manual workflow construction or export is required. The committed API-format templates are:

```text
benchmark/workflows/animagine-xl.api.json
benchmark/workflows/illustrious-xl.api.json
benchmark/workflows/anima-aesthetic.api.json
```

Initial settings:

- Animagine: 1024×1024, seed 101, 28 steps, CFG 5, `euler_ancestral`, `normal`, tag prompts.
- Illustrious: 1024×1024, seed 101, 28 steps, CFG 5, `euler_ancestral`, `normal`, hybrid prompts. These are a project comparison baseline rather than an official-settings claim.
- Anima Aesthetic: 1024×1024, seed 101, 40 steps, CFG 4.5, `er_sde`, `simple`, natural-language prompts, split diffusion/text-encoder/VAE loaders, and sampling shift 3.0.

Later benchmark preparation replaces the template seed, prompt case, and output prefix deterministically while preserving the exact model and settings contract.

## License and quality boundary

Animagine and Illustrious are production candidates only if all later evidence and owner gates continue to pass. Anima Aesthetic is included solely for non-production benchmark comparison under its recorded scope; the owner-selection gate cannot issue a production lock for it.

A successful download, checksum, workflow load, generation, timing result, or contact-sheet position is not aesthetic approval and does not select a model.
