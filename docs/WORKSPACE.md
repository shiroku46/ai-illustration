# Owner workspace and offline progress dashboard

Phase 16 adds one read-only owner workspace for understanding the complete AI-illustration pipeline without executing any external tool.

## Purpose

The repository already contains strict validators and integrity checkers for generation inputs, ComfyUI candidate packages, reviewed variants, export packages, paper-theater plans, previews, audio binding, frame rendering, and local video export. A workspace references those existing artifacts in dependency order and reports their current state from the existing checker for each stage.

The workspace does not generate images, launch ComfyUI, encode video, open a browser, or make a human review or licensing decision.

## States

Each declared check has exactly one state:

- `complete`: the current stage-specific repository checker succeeded;
- `not-started`: the primary artifact does not exist;
- `blocked`: one or more declared earlier dependencies are not complete;
- `invalid`: the primary artifact exists but its current checker failed.

Missing external tools, source media, or human review decisions are represented as caller-authored next actions. They are not silently treated as software failures.

## Workspace document

A workspace is canonical JSON with a content-derived `ai-illustration-workspace-...` ID. Paths are POSIX paths relative to the workspace file. Absolute paths, traversal, backslashes, null bytes, symlink components, duplicate IDs, forward dependencies, unsupported arguments, and secret-like values fail closed.

Checks are processed in their declared order. The same kind may appear more than once, which allows separate boke and tsukkomi tracks before their shared paper-theater dependency.

An action is display-only:

```json
{
  "type": "human",
  "label": "Review the generated boke candidates",
  "argv": []
}
```

or:

```json
{
  "type": "command",
  "label": "Run the existing offline checker",
  "argv": ["python", "-m", "ai_illustration.cli", "adapter-run-check", "..."]
}
```

No action is executed by the workspace module.

## Status

```bash
PYTHONPATH=src python -m ai_illustration.workspace status path/to/workspace.json
```

The command returns canonical JSON containing counts, every check, sanitized diagnostics, overall completion, and the first actionable check. It performs no network request and starts no external process.

To require all declared checks to be complete:

```bash
PYTHONPATH=src python -m ai_illustration.workspace check path/to/workspace.json
```

`check` returns nonzero while any declared stage is not complete.

## Offline dashboard

Dry-run dashboard planning:

```bash
PYTHONPATH=src python -m ai_illustration.workspace build \
  path/to/workspace.json \
  --output-root path/to/workspace-dashboards
```

Atomic publication:

```bash
PYTHONPATH=src python -m ai_illustration.workspace build \
  path/to/workspace.json \
  --output-root path/to/workspace-dashboards \
  --write
```

The content-addressed package contains:

- `index.html`;
- `style.css`;
- `app.js`;
- `workspace-data.js`;
- `workspace-dashboard-manifest.json`.

The generated dashboard is a static snapshot. It uses no remote resource, request API, storage, telemetry, form submission, dynamic evaluation, server, or browser automation.

## Dashboard verification

```bash
PYTHONPATH=src python -m ai_illustration.workspace dashboard-check \
  path/to/workspace-dashboards/DASHBOARD-ID/workspace-dashboard-manifest.json \
  path/to/workspace.json \
  --output-root path/to/workspace-dashboards
```

The checker re-evaluates the workspace through all current stage checkers, reconstructs every dashboard byte, and rejects stale status, modified files, missing or extra files, conflicts, traversal, and symlinks.

## Completion boundary

A fully complete workspace means every artifact declared by its owner passes its existing integrity checker. It does not mean that final character identity, aesthetic acceptance, model licensing, commercial use, or publication was approved automatically. Those decisions remain explicit human inputs.
