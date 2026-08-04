# Software MVP 1.0 completion

The repository is complete as a **software MVP** when the canonical release contract at `release/mvp-v1.json` passes the read-only release audit and the repository CI and full unit-test suite pass on the same immutable revision.

## Included software capabilities

Version 1.0 provides a verified local-first path for:

1. versioned character, style, generation, candidate, review, and export manifests;
2. local tool/model evidence, compatibility, installation, licensing, and commercial-review profiles;
3. deterministic ComfyUI workflow binding and dry-run planning;
4. explicit approved fixed-seed loopback-only ComfyUI execution;
5. checksum-bound candidate PNG packages that remain unreviewed;
6. loopback-only candidate review UI and structured review decisions;
7. accepted-identity-bound expression and pose variant planning;
8. verified caller-supplied PNG export packages;
9. deterministic two-character paper-theater scene planning;
10. fully offline image and WAV previews;
11. rational frame planning, explicit composition profiles, and deterministic RGBA frame rendering;
12. offline rendered-frame playback;
13. explicit-profile, checksum-pinned local FFmpeg video export;
14. multi-track owner workspace status and a static offline progress dashboard;
15. integrity checkers for every declared workspace stage.

## Completion proof

Run from the repository root:

```bash
PYTHONPATH=src python -m ai_illustration.release_audit release/mvp-v1.json
python scripts/public_export_guard.py .
python scripts/validate_repository.py
PYTHONPATH=src python -m unittest discover -s tests
```

The release audit is non-mutating. It starts no external process and contacts no network endpoint. It verifies:

- package version and installed console entry points;
- all main and dedicated CLI command surfaces;
- all 12 workspace checker kinds;
- required modules, schemas, documents, and GitHub validation workflows;
- the disabled legacy ComfyUI execution boundary;
- protected source files for forbidden shell and external-URL patterns;
- zero runtime Python dependencies;
- a sorted SHA-256 inventory of critical release files.

## Owner prerequisites for actual production

Software completion does not invent or satisfy these owner-controlled inputs:

- a locally installed and running ComfyUI instance;
- an explicitly reviewed model, workflow, and any custom nodes;
- licensing and commercial-use evidence appropriate to the owner's context;
- final boke and tsukkomi identity selection;
- human candidate and variant acceptance decisions;
- caller-recorded or otherwise approved WAV audio;
- an exact local FFmpeg executable;
- the owner's composition, export, and publication decisions.

The workspace dashboard reports these as explicit next actions when their artifacts are absent.

## Deliberately excluded automatic effects

The software never automatically:

- installs or downloads ComfyUI, models, custom nodes, or FFmpeg;
- selects a final character or approves an aesthetic result;
- approves licensing or commercial use;
- accesses credentials or remote hosted services;
- publishes generated media;
- converts a technically valid candidate into an accepted production asset.

Those exclusions are release guarantees rather than missing implementation work.
