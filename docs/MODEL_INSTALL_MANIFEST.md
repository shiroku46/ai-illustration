# Exact model installation manifest

The benchmark model files are owner-controlled local inputs. They are not committed, downloaded, installed, or executed by this repository.

The canonical machine-readable record is:

```text
benchmark/model-install-manifest.v001.json
```

Validate the repository-side evidence without mutation:

```text
python -m ai_illustration.model_install_manifest check \
  benchmark/model-install-manifest.v001.json \
  --workspace-root .
```

A successful result verifies the exact model profiles, profile hashes, license scopes, model artifact metadata, benchmark settings, and required local API-workflow paths. It does not claim that the large model files are present.

## Exact local files

Relative to the owner’s ComfyUI directory:

| Model | Destination | Exact filename | Bytes | SHA-256 | Scope |
|---|---|---:|---:|---|---|
| Animagine XL 4.0 Opt | `models/checkpoints` | `animagine-xl-4.0-opt.safetensors` | 6,940,833,562 | `6327eca98bfb6538dd7a4edce22484a1bbc57a8cff6b11d075d40da1afb847ac` | production candidate |
| Illustrious XL v2.0 Stable | `models/checkpoints` | `Illustrious-XL-v2.0.safetensors` | 6,938,043,394 | `c2a1a3eaa13d4c107dc7e00c3fe830cab427aa026362740ea094745b3422a331` | production candidate |
| Anima Aesthetic v1.1 | `models/diffusion_models` | `anima-aesthetic-v1.1.safetensors` | 4,181,385,600 | `3c1868387a3a1ff504bbb87c33678321965ead381fcf87afbd0264daa600c082` | evaluation only |
| Anima text encoder | `models/text_encoders` | `qwen_3_06b_base.safetensors` | 1,192,135,096 | `cd2a512003e2f9f3cd3c32a9c3573f820bb28c940f73c57b1ddaa983d9223eba` | required by Anima |
| Anima VAE | `models/vae` | `qwen_image_vae.safetensors` | 253,806,246 | `a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f` | required by Anima |

Do not rename the files. The later workflow and preflight gates bind the exact filenames.

## Windows checksum verification

Run this for each downloaded file, replacing the path:

```powershell
Get-FileHash -Algorithm SHA256 "C:\path\to\ComfyUI\models\checkpoints\animagine-xl-4.0-opt.safetensors"
```

The reported hash must exactly match the manifest. A mismatched file must not be used or renamed into place.

Optional size check:

```powershell
(Get-Item "C:\path\to\file.safetensors").Length
```

## Benchmark settings

The fixed initial settings are recorded in the manifest.

- Animagine: 1024×1024, 28 steps, CFG 5, `euler_ancestral`, tag prompts. These settings follow the official model-card guidance.
- Illustrious: 1024×1024, 28 steps, CFG 5, `euler_ancestral`, hybrid prompts. These are a project comparison baseline, not a claim of official recommended settings.
- Anima Aesthetic: 1024×1024, 40 steps, CFG 4.5, `er_sde`, natural-language prompts. These are selected within the official model-card ranges.

All later benchmark cases still use the shared fixed seed set and owner-approved art-direction contract.

## Required API workflow exports

After the files are installed and ComfyUI can load them, export one API-format workflow per model to these repository-ignored local paths:

```text
local/benchmark-workflows/animagine-xl.api.json
local/benchmark-workflows/illustrious-xl.api.json
local/benchmark-workflows/anima-aesthetic.api.json
```

The workflow must encode the exact model filename and manifest settings. UI-format workflow JSON is not accepted in place of API-format JSON.

## License boundary

Animagine and Illustrious are production candidates only after their exact profiles and later generated evidence continue to pass all gates. Anima Aesthetic is admitted solely as a benchmark comparison under the recorded non-production evaluation scope. The owner model-selection gate refuses to issue a production lock for it.

No successful download, checksum, workflow load, benchmark result, timing result, or contact-sheet position constitutes aesthetic approval or model selection.
