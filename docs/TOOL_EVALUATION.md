# Local generation host evaluation

Retrieval date: 2026-08-02.

This record supports an adapter-boundary decision only. It is not legal advice, model approval, installation authorization, or a commercial-use determination.

## Selected first host: ComfyUI Local Server API

Official ComfyUI sources describe a local open-source node/workflow inference engine, an API-format workflow representation, a Local Server API, offline operation, and Windows/Linux/macOS support. The core repository declares GPL-3.0.

Sources:

- https://github.com/Comfy-Org/ComfyUI
- https://docs.comfy.org/
- https://docs.comfy.org/development/overview
- https://github.com/Comfy-Org/ComfyUI/blob/master/script_examples/basic_api_example.py

The project selects the documented HTTP/JSON boundary, not ComfyUI code. Nothing in this repository vendors, installs, launches, or contacts ComfyUI.

## Primary alternative: InvokeAI

InvokeAI's official repository describes a locally hosted application with workflow nodes and gallery-oriented features and declares an Apache-2.0 core license. It remains a credible alternative, but its broader integrated application surface is unnecessary for the first thin adapter.

Source: https://github.com/invoke-ai/InvokeAI

## Model-family license notes

No checkpoint is selected in this phase.

- Stability AI's official license page states that listed Core Models, including the Stable Diffusion 3.5 Suite, are available under its Community License for individuals or organizations below USD 1M annual revenue. Exact model terms and the owner's applicable entity/revenue context remain `unreviewed`; this is a candidate family, not an approval. Source: https://stability.ai/license
- Black Forest Labs' official FLUX `[dev]` terms describe non-commercial/non-production use absent a separate commercial license. FLUX `[dev]` is therefore not eligible as the default commercial-production model under the currently reviewed evidence. Sources: https://bfl.ai/legal/non-commercial-license-terms and https://github.com/black-forest-labs/flux

## Decision summary

- Host adapter: ComfyUI Local Server API.
- Transport boundary: documented localhost HTTP/JSON concepts.
- Model checkpoint: unresolved.
- InvokeAI: retained alternative.
- Stable Diffusion 3.5: license-review candidate only.
- FLUX `[dev]`: commercial-production blocked absent separate licensing evidence.
- Human review remains required before installation, model download, generation, or commercial use.
