# ADR-0001: First local generation adapter boundary

- Status: accepted for dry-run planning only
- Date: 2026-08-02

## Context

The pipeline needs a replaceable local generation boundary before any model is installed. The boundary must preserve deterministic request metadata, reject remote or credential-bearing endpoints, and allow future execution without coupling domain manifests to a specific user interface or checkpoint.

## Decision

Use a `comfyui-local-api` adapter with a versioned Python interface and ComfyUI API-format workflow JSON.

This phase implements only:

- static workflow validation;
- explicitly allowlisted manifest-to-node input bindings;
- loopback endpoint canonicalization;
- deterministic workflow and bound-payload checksums;
- deterministic output-directory planning;
- redacted execution-plan summaries;
- fail-closed model/license readiness reporting.

The adapter always returns `dry_run: true`. Its `execute` method raises `EXECUTION_DISABLED`. No HTTP client, socket, subprocess, package, model, or image path is present.

## Security and reproducibility boundaries

- Scheme is fixed to `http` for a future local server.
- Host must resolve syntactically to `localhost` or a loopback IP literal.
- Credentials, query strings, fragments, paths, traversal, and remote hosts are rejected.
- Bindings name exact workflow nodes, inputs, and request source fields.
- Unknown bindings and secret-like keys or values fail closed.
- Plans are canonical JSON and independent of wall-clock time.
- The source workflow checksum and separately bound-workflow checksum are recorded.
- An unresolved model ID or non-approved model license state prevents executable readiness, even though dry-run planning remains available.

## Consequences

The next implementation may add an optional localhost HTTP transport behind this interface. That later work must separately authorize network execution, verify a running local server, and retain exact request/response provenance. It must not alter the production manifest vocabulary merely to accommodate ComfyUI.

InvokeAI remains the primary alternative host if the thin ComfyUI boundary later proves unsuitable. Model selection and licensing approval remain separate decisions.
