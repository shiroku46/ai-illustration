# AI Development Automation Foundation

A public, reusable, history-free foundation for guarded AI-assisted development on GitHub.

## What this repository provides

- read-only CI that is safe for forked pull requests;
- an owner-authorized Claude issue queue;
- bounded reconciliation for missing trusted checks;
- a deterministic recovery and merge decision engine;
- a default-branch-controlled supervisor that never executes proposed-branch code with write permissions;
- a Bootstrap generator for installing the same controls in another repository;
- security, export, workflow, and policy regression tests.

## Safety model

Untrusted pull-request code runs only in jobs with `contents: read` and without Secrets, OIDC, or write permissions. Jobs that can comment, relabel, dispatch, close, mark ready, or merge operate only from the default branch, inspect immutable current SHAs, require same-repository provenance, use fixed workflow names and refs, bound their candidate set, and use an expected-head-SHA merge guard.

The queue accepts issue creation or an exact standalone `/claude-run` comment only when `github.actor` is the configured owner. A separate default-branch-only trusted dispatch path is available to the supervisor.

## Validation

```bash
python scripts/public_export_guard.py .
python scripts/validate_repository.py
python -m unittest discover -s tests
```

## Bootstrap

```bash
python bootstrap/generator.py --target ../example-repository --owner YOUR_GITHUB_LOGIN
```

Review the generated install checklist before enabling write-capable workflows. Repository Secrets are never generated, copied, or printed.

## Project status

This public repository is the implementation source of truth. Earlier private sandboxes remain archives and are not imported into this history.
