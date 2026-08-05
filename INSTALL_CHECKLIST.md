<!-- ai-dev-automation-foundation:generated-target -->
# Installation checklist

## Phase 0 — Mandatory GitHub setup

- [ ] Connect ChatGPT to GitHub and authorize this exact repository.
- [ ] Confirm GitHub Actions and Foundation workflows exist on the default branch.
- [ ] Select **Read and write permissions** under `Settings` → `Actions` → `General` → `Workflow permissions`.
- [ ] Enable **Allow GitHub Actions to create and approve pull requests**.
- [ ] Optionally set `AUTOMATION_OWNER` to `shiroku46` when the repository owner is not the trusted coordinator.
- [ ] Run `python scripts/public_export_guard.py .`, `python scripts/validate_repository.py`, and `python scripts/foundation_drift.py --root .`.
- [ ] Complete one harmless branch/PR candidate with exact-head checks, GitHub coordinator review, zero unresolved threads, and expected-head merge.

Codex and Claude setup is optional. Provider environment, credential, quota, account, setup or connection is not required for GitHub-only acceptance or product development.

## Installation identity

- installation mode: `existing-product`
- version file: `FOUNDATION.lock.json`
- [ ] Confirm the lock records the exact Foundation source SHA and sorted managed-file hashes.
- [ ] Keep target-owned files outside the managed lock.
- [ ] Configure required product workflows in `.github/foundation-product-checks.json`; the previous default-branch config judges each configuration-changing PR.
- [ ] For upgrades, render a candidate in a separate directory and compare locks before publication.

## Non-destructive publication

- [ ] Compute and review the complete Bootstrap plan before mutation.
- [ ] Publish the rendered bytes on one dedicated same-repository branch through the connected GitHub App/API route.
- [ ] Open one Draft Pull Request against the exact observed default-branch SHA.
- [ ] Verify the GitHub-visible candidate head and exact changed paths.
- [ ] Do not create a temporary installer workflow on the default branch.
- [ ] Do not request `BOOTSTRAP_WORKFLOW_TOKEN`, a PAT, or another long-lived credential.
- [ ] Do not force-update the Bootstrap branch.
- [ ] Roll back by closing the unmerged Draft PR, or revert one protected merge.

## Operating boundary

- [ ] Use one trusted owner-authored Issue with risk tier, bounded paths, checks, prohibited effects, and rollback.
- [ ] Inspect current and renamed-path collisions before implementation and merge.
- [ ] Never push automation changes directly to the default branch.
- [ ] Require exact-head `CI` and `Unit Tests`.
- [ ] Require `review_route: github-coordinator` and zero unresolved review threads.
- [ ] Protected work requires explicit authorization and clean scope/security plus correctness/race markers.
- [ ] `ai-no-merge` blocks readiness and merge.
- [ ] Merge only with expected-head-SHA protection.
- [ ] Optional provider failure remains non-blocking with `human_action_required: false`.
- [ ] Persist routine automation stops only on `automation-internal-stops`; never publish routine stop comments.
- [ ] Never output, persist, copy, hash, or infer Secret values.
- [ ] Never execute proposed-branch code in a job carrying Secrets, OIDC, or repository write permission.
