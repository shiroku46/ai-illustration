# Software MVP completion boundary

The repository has a deterministic, read-only release audit for the **software** surface. Passing that audit means the declared code, schemas, browser review assets, quality gates, local packaging/rendering tools, and operator-facing contracts are present and internally consistent. It does **not** mean that final character art, real benchmark evidence, identity locks, production variants, audio, or final encoded media exist.

## Quality-reset software gates

The release audit now treats the following as critical software paths. Removing their source, schema, or governing documentation makes the software release incomplete:

1. quality-stage separation between transport smoke output, technical candidates, and owner-created creative candidates;
2. local browser creative-review controls and hard-fail vocabulary;
3. checksum-bound art-direction profile and owner-review contracts;
4. fixed-seed model benchmark plan, result/contact-sheet validation, and owner model-selection review;
5. deterministic identity-lock plan, result/consistency-sheet validation, and owner identity approval;
6. identity-gated production variant planning/checking;
7. formal checksum-bound owner review of each generated variant;
8. production export that preserves the exact variant-set, identity/model, review, and PNG provenance;
9. non-resynthesizing Phase F asset preparation that only trims transparent margins and copies visible RGBA pixels unchanged onto caller-authored canvases.

These are software capabilities and fail-closed boundaries. Their presence must never be interpreted as automatic aesthetic approval or as evidence that the real runtime steps have occurred.

## Software completion

Software MVP completion requires the release audit, repository validation, and complete automated regression suite to succeed across the declared quality-stage, art-direction, benchmark, identity-lock, variant-review, asset-preparation, generation, packaging, paper-theater, rendering, video-export, and workspace software surface.

The audit remains read-only. It does not contact the network, start a subprocess, generate an image, write a review, install a model, or mutate the repository.

## Content/runtime completion remains external

Real project content is still incomplete until the owner/runtime evidence exists. At minimum this includes:

- the canonical owner art-reference files;
- the approved local ComfyUI installation and exact reviewed model/workflow artifacts;
- the real three-family benchmark on the owner's target hardware;
- owner selection of the model/base design from benchmark evidence;
- real identity-lock runs and explicit owner identity approval;
- generated pose/expression variants and explicit owner review of each production variant;
- caller-authored asset-preparation geometry/isolation settings for final delivery;
- source audio and any final timing/composition decisions;
- checksum-pinned FFmpeg only when final video encoding is requested;
- final publication/distribution decisions.

The software audit therefore answers **“is the guarded software pipeline ready?”**, not **“is the artwork finished?”**.

## Canonical audit command

```text
python -m ai_illustration.release_audit release/mvp-v1.json
```

A successful result reports `complete=true` only for the software contract. Remaining human/runtime prerequisites stay explicit in `release/mvp-v1.json` and are not silently converted into completed content.
