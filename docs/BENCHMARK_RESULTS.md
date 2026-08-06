# Benchmark results and contact sheets

Benchmark results are execution evidence, not aesthetic judgments. The result-set contract records every expected fixed-seed matrix run exactly once and preserves the owner-only model-selection boundary.

## Exact result coverage

A `model-benchmark-results` document binds one exact benchmark-plan ID, version, and canonical SHA-256. The checker regenerates the deterministic benchmark matrix and requires an exact run-ID match:

- no missing runs;
- no extra runs;
- no duplicate runs;
- no changed model family, profile hash, workflow hash, seed, prompt case, role scope, or native settings.

Every run is either `succeeded` or `failed`.

A successful run records its deterministic matrix image path, raw PNG SHA-256, width, height, elapsed milliseconds, and optional peak VRAM MiB. A failed run records a stable error code, message, and elapsed milliseconds, and has no image fields. Failed runs remain part of the evidence set rather than disappearing from the comparison.

Fields for aesthetic scores, ranks, winners, approvals, recommendations, or automatic selection are rejected. Timing and VRAM evidence do not imply creative quality.

## Local PNG validation

Successful result images must remain beneath the explicit result root and must be regular non-symlink files. The validator checks:

- safe relative path and deterministic matrix-path binding;
- file-size and dimension bounds;
- raw SHA-256;
- PNG signature, chunk boundaries, chunk CRCs, and terminal IEND;
- no bytes after IEND;
- 8-bit non-interlaced RGB, grayscale-alpha, or RGBA structure;
- bounded decompression length and valid scanline filters;
- no animated-PNG chunks.

The source images are never rewritten, repaired, converted, moved, or deleted.

## Contact-sheet package

The renderer creates one standalone SVG for each `(model family, prompt case)` group. Seeds appear in ascending order under a shared four-column layout. Successful tiles embed the exact local PNG bytes as `data:image/png;base64,...`; failed tiles visibly show the seed, failure code, message, elapsed time, and run ID.

The SVG contains only generated shapes, text, and embedded PNG data. It has no script, external stylesheet, remote image, downloadable font, hyperlink, iframe, network URL, or browser-side logic.

`contact-sheet-manifest.json` canonically binds:

- the exact result-set ID, version, and SHA-256;
- the exact benchmark-plan binding;
- `selection_policy=owner-only`;
- every SVG path, SHA-256, byte size, model family, prompt case, run-ID list, and success/failure count.

Identical inputs produce byte-identical SVG and manifest files. The output directory must not already exist. Files are staged in a fresh sibling directory and published by one directory rename. Source files remain untouched.

## Commands

Read-only validation:

```text
python -m ai_illustration.benchmark_results results-check RESULTS.json PLAN.json \
  --workspace-root WORKSPACE \
  --reference-root ART_REFERENCES \
  --result-root RESULTS_ROOT
```

Deterministic package creation:

```text
python -m ai_illustration.benchmark_results render-contact-sheets RESULTS.json PLAN.json \
  --workspace-root WORKSPACE \
  --reference-root ART_REFERENCES \
  --result-root RESULTS_ROOT \
  --output-dir NEW_OUTPUT_DIRECTORY
```

The first command performs no mutation. The second writes only the explicitly requested new package directory after every dependency, result, and image has passed validation. Neither command launches a server or browser, accesses a network or provider, invokes ComfyUI, installs or downloads a model or workflow, generates or repairs benchmark art, or chooses a winner.

## Boundary

Contact sheets are owner-review evidence only. They do not approve an art direction, license, character, image, model, identity-lock method, variant workflow, or production asset. Actual benchmark execution and the owner's visual decision remain separately gated work.
