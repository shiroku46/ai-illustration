# Strict loopback ComfyUI execution

Phase 15 adds an explicit opt-in execution boundary for an already-running, caller-managed ComfyUI instance.

## Boundary

The repository does not install, download, discover, select, update, or launch ComfyUI, checkpoints, custom nodes, or workflows. Execution is allowed only against `http://localhost`, `http://127.0.0.1`, or `http://[::1]` with an optional port.

Only these requests are possible:

- `POST /prompt` with the exact bound API-format workflow;
- `GET /history/{prompt_id}` for the returned prompt only;
- `GET /view` for exact authorized output descriptors.

Proxies, redirects, credentials, cookies, environment-derived authorization, websocket access, arbitrary returned URLs, LAN hosts, and cloud hosts are disabled.

## Required approvals

Execution requires all of the following:

- a canonical fixed-seed `generation-request` whose license status is `approved`;
- a canonical approved and installed offline tool profile using `comfyui-local-api`;
- a canonical approved and installed offline model-configuration profile using the same adapter;
- an exact workflow and bindings file with no secret-like data;
- a canonical execution profile binding the exact workflow SHA-256, profile IDs, authorized output node IDs, expected PNG dimensions, and all byte/time/count limits;
- the explicit `adapter-run` command with `--execute`.

The execution profile ID is content-derived. Output node IDs must be sorted and unique.

## Run

```bash
PYTHONPATH=src python -m ai_illustration.cli adapter-run \
  path/to/generation-request.json \
  path/to/workflow-api.json \
  --bindings path/to/bindings.json \
  --tool-profile path/to/tool-profile.json \
  --model-profile path/to/model-profile.json \
  --execution-profile path/to/execution-profile.json \
  --endpoint http://127.0.0.1:8188 \
  --output-root path/to/comfyui-executions \
  --execute
```

The package directory is the deterministic execution-plan ID. It contains:

- `execution-plan.json`;
- `execution-manifest.json`;
- one verified PNG and one canonical candidate sidecar per accepted result.

Returned server filenames are provenance only. Local filenames are content-addressed and deterministic.

## Idempotency

Before contacting ComfyUI, the command checks for an existing package under the plan ID. If that package passes complete offline verification against the current request, workflow, bindings, profiles, and PNG bytes, it is reused and no second prompt is queued.

A conflicting, missing, extra, traversing, symlinked, stale, or tampered package fails closed.

## Offline check

```bash
PYTHONPATH=src python -m ai_illustration.cli adapter-run-check \
  path/to/comfyui-executions/PLAN-ID/execution-manifest.json \
  path/to/generation-request.json \
  path/to/workflow-api.json \
  --bindings path/to/bindings.json \
  --tool-profile path/to/tool-profile.json \
  --model-profile path/to/model-profile.json \
  --execution-profile path/to/execution-profile.json \
  --endpoint http://127.0.0.1:8188 \
  --output-root path/to/comfyui-executions
```

The checker performs no HTTP request. It reconstructs the execution plan, verifies every source checksum, candidate PNG, sidecar, manifest field, and exact file set.

## Review and licensing

Generated candidates are always `technically_valid` and `unreviewed`. Execution never marks a candidate accepted, production-ready, commercially approved, or suitable for publication. Human review remains mandatory.
