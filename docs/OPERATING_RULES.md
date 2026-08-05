# Operating rules

## Authoritative repositories and policy

The public Foundation repository and its public E2E repository are the implementation and acceptance sources of truth. Private predecessor repositories are archives only.

`docs/MINIMUM_SAFETY_PROFILE.md` and owner-authored Issue #160 are the authoritative day-to-day development policy. When older text assumes that Claude implements, Codex reviews, or an external provider must approve a merge, the GitHub-direct coordinator-review policy takes precedence.

GitHub resources are finite. API requests, Actions usage, artifacts, storage, concurrency, and billing-sensitive features must be bounded; no operating rule may describe GitHub as unlimited.

## Mandatory Phase 0 before ordinary flow

Every newly bootstrapped repository must complete setup steps 1–5 in `docs/PROJECT_STARTUP.md` before the harmless Bootstrap acceptance exercise. Successful acceptance is the final Phase 0 gate. Product Issues and implementation start only after that gate passes.

The coordinator performs all connected inspections first. When no repository-settings API is callable, the owner must complete this one-time GitHub UI action in the exact target repository before acceptance:

`Settings` → `Actions` → `General` → `Workflow permissions`

1. select **Read and write permissions**;
2. enable **Allow GitHub Actions to create and approve pull requests**;
3. save the setting.

Pre-PR Phase 0 guidance is a narrow startup exception, not a runtime GitHub notice. It is delivered directly in the initiating project conversation before GitHub orchestration starts, contains only non-secret navigation or a local command plus the automatic-resumption condition, does not call `human_only_notice()`, does not publish a GitHub comment, does not require an Issue/PR destination, and does not create or relax a runtime reason code. Completion is later recorded in non-secret Bootstrap acceptance evidence.

The mandatory setup prerequisites are connected access to the exact GitHub repository, enabled Actions and Foundation workflows, and both Workflow-permissions settings. Codex environments and Claude credentials are optional and are required only when the owner deliberately enables those provider routes. GitHub-only acceptance and product development must not wait for provider setup or quota.

After the mandatory prerequisites pass, the harmless acceptance candidate proves that branch, Pull Request, exact-head check, coordinator-review, and guarded merge orchestration work and thereby completes Phase 0.

Do not retry a stalled write-capable workflow or ask the owner to repost commands until the Workflow-permissions setting has been checked. After acceptance, do not request the setup again unless connected evidence shows that it was reset or the integration is no longer usable.

## Ordinary flow

1. Phase 0 acceptance for the exact repository is already complete and recorded without Secret values.
2. A trusted owner-authored Issue states the goal, risk tier, acceptance criteria, every allowed changed or renamed path, prohibited effects, required checks, and rollback.
3. Before implementation, the coordinator inspects all open same-repository Pull Requests and active candidate branches. Overlapping current or previous renamed paths block a second candidate unless the Issue declares the exact dependency.
4. The coordinating ChatGPT uses the connected GitHub App/API to create one dedicated same-repository branch from the intended base.
5. The coordinator implements through GitHub-visible file and commit operations, changes only authorized paths, and opens or updates one Pull Request.
6. Public Pull Request checks execute the exact candidate SHA with `contents: read`, no Secrets, no OIDC, and no write permission.
7. Fixed default-branch trusted checks and configured product checks create exact-head evidence for that same GitHub-visible SHA.
8. The coordinator inspects the complete exact diff, changed and renamed paths, source Issue scope, protected authorization, required checks, review threads, and current remote head.
9. Apply the risk-tier coordinator-review rule:
   - low risk: one exact-head coordinator diff review;
   - standard risk: one nonempty structured exact-SHA coordinator review record;
   - protected risk: explicit protected authorization plus separate coordinator scope/security and final correctness/race review passes on the unchanged exact head.
10. Findings are corrected through the same GitHub-centered branch. Any head change invalidates stale check and coordinator-review evidence.
11. Immediately before merge, revalidate provenance, source Issue, scope, protected authorization, complete required checks, exact-SHA coordinator-review record, unresolved threads, hold state, default branch, mergeability, and the current head.
12. Merge through the Merge API with the exact expected head SHA.

A separate human merge click is not required. Codex and Claude are optional helpers or second opinions and are never mandatory implementation, review, or merge stages.

No `workflow_dispatch` or `repository_dispatch` event payload may authorize a candidate, source Issue, changed path, workflow, ref, repository, command, provider, review result, or merge.

## Implementation route

GitHub direct implementation is the default and sufficient route. The connected GitHub route owns branch creation, authorized file writes, commits, Pull Request publication, correction publication, remote-SHA confirmation, and merge orchestration.

Codex or Claude may assist only when the owner explicitly requests that provider or the coordinator deliberately chooses a non-blocking helper. Provider-reported local commits remain incomplete until the expected GitHub-visible remote branch head equals the reported commit.

Provider absence, limits, setup, account, connection, generic output, stale-SHA output, or content-free output cannot block GitHub-direct development and must not create routine human work.

## Coordinator review

The authoritative review route is `github-coordinator`. The Pull Request or machine-readable status records the exact current SHA and `review_state: required | pending | clean | blocked`.

### Low risk

The coordinator reviews the complete exact-head diff, verifies scope and successful required checks, confirms no unresolved thread, and records whether a blocking finding remains.

### Standard risk

The coordinator creates a nonempty structured record bound to the exact 40-character GitHub-visible SHA. The record identifies the Issue, Pull Request, risk tier, current and previous renamed paths, check conclusions, unresolved-thread count, findings summary, and `clean` or `blocked` result.

### Protected risk

The coordinator performs two distinct stages on the unchanged exact head:

1. **Scope/security review** — verify explicit protected authorization, exact changed and renamed paths, permissions, authentication and Secret interfaces, trust boundaries, candidate-execution isolation, OIDC/write separation, prohibited effects, and rollback.
2. **Correctness/race review** — after all evidence queries, re-check implementation correctness, tests, source Issue, exact head, default branch, hold state, unresolved threads, mergeability, and expected-head mutation boundary.

The structured review record describes both stages and states whether any blocking finding remains. A changed head invalidates both stages.

An optional Codex or Claude opinion may supplement this process but never replaces or blocks the coordinator review.

## Issue scope and protected authorization

Every changed and renamed path must match the bounded scope declared by a trusted owner-authored Issue. Only exact repository-relative paths and bounded suffix patterns such as `tests/**` are accepted.

Protected changes include `.github/**`, `bootstrap/**`, supervisor and security-policy code, permission changes, authentication, repository settings, billing, deployment, production, and destructive data operations. Protected work requires a nonempty category, exact authorized paths, operation, prohibited effects, validation, and rollback contract.

```text
## Allowed paths
- .github/workflows/example.yml

<!-- foundation-protected-authorization
category: workflow
paths:
- .github/workflows/example.yml
operation: add one reviewed workflow
prohibited: no secrets, deployment, or repository settings
validation: public CI, tests, coordinator exact-SHA scope/security review, coordinator final correctness/race review
rollback: revert the merge commit
-->
```

The supervisor fails closed when any current or previous renamed path exceeds the trusted scope or protected work lacks the stricter contract.

## Collision prevention

Before branch creation, publication, correction, or merge, inspect all open same-repository Pull Requests and active Issue-bound candidates. Treat both rename sources and destinations as occupied paths.

Do not create, rebase, overwrite, force-update, or merge a second candidate whose authorized path set intersects a live candidate unless the new Issue explicitly declares a dependency on that exact PR and head. A collision is automation-owned, records `human_action_required: false`, and resumes only after the blocker merges, closes, or is replaced through a separately authorized recovery path.

## Native exact-head evidence

Before readiness or merge, the supervisor resolves fixed active default-branch workflow identities for:

- `.github/workflows/ci.yml` / `CI`;
- `.github/workflows/unit-tests.yml` / `Unit Tests`;
- `.github/workflows/e2e.yml` / `E2E Acceptance`, when that fixed workflow exists;
- configured product lint, test, build, and type-check workflows.

Every required candidate workflow file blob is compared with the corresponding stable default-branch blob. The supervisor then requires successful completed runs belonging to the exact Pull Request, same repository, fixed workflow identity, and current head SHA. Missing, pending, cancelled, failed, stale-SHA, cross-Pull-Request, wrong-workflow, wrong-repository, candidate-modified-workflow, or candidate-authored evidence cannot authorize progress.

When GitHub records automation-authored Pull Request runs as `action_required` before any job starts, connected automation may create one metadata-only commit on the same authorized branch. The new exact head invalidates all prior evidence and must receive fresh checks and the risk-tier-required coordinator review; no person is asked to approve the run.

## Status and evidence

The Pull Request body or machine-readable status records:

- `implementation_route: github-direct | codex-optional | claude-optional`;
- exact GitHub-visible head SHA;
- risk tier;
- `review_route: github-coordinator`;
- `review_state: required | pending | clean | blocked`;
- required checks and observed conclusions;
- unresolved review-thread count;
- next automatic action;
- `human_action_required: true | false`.

Legacy provider fields may remain during migration, but `selected_auditor: none` and provider `route-unavailable` do not block completion. Provider output that is stale, untrusted, edited without provenance, content-free, or tied to another SHA is ignored.

During bounded runtime migration, an existing immutable trusted exact-SHA request comment authored by `github-actions[bot]` may still be emitted by older default-branch code. It is retained only as immutable state/provenance evidence; it does not select an external provider, prove an external review, or block the authoritative GitHub coordinator-review route.

## Internal stops are durable and non-notifying

Retry exhaustion, provider quota, no progress, stale or incomplete evidence, blocking review, merge state, collision, ambiguous technical conditions, all-path denial, and protected-path denial are internal automation states. They must never become routine requests for a person to implement, merge, approve, retry, close, resolve review state, change permissions or settings, alter billing, or deploy.

Before persisting an internal stop, the runtime performs the mandatory self-resolution audit against the live exact SHA. It rechecks repository metadata; current Pull Request head and mergeability; complete changed and renamed paths; source Issue trust and scope; protected authorization; fixed workflow identities; immutable trusted and native evidence; coordinator-review evidence and unresolved threads; collaborator permission; idempotency; collision state; and alternative connected recovery paths. It fetches the live Pull Request again immediately before any record or disposable close. A failed query or moved head produces no effect.

Internal stop records are sanitized canonical JSON on the fixed non-default branch `automation-internal-stops` at:

```text
automation-stops/pr-<number>/<exact-sha>/<REASON_CODE>.json
```

The record contains `notification: false`, `human_action_required: false`, the reason, Issue, Pull Request, exact SHA, bounded detail, coordinator-review state, and connected evidence. Routine internal stops are never posted as Issue or Pull Request comments and never create or edit routine stop labels.

## Human-only notice boundary

Only these reason codes may notify a person:

- `HUMAN_ONLY_ACCOUNT_LEVEL_REPOSITORY_CREATION_UI_UNAVAILABLE`
- `HUMAN_ONLY_CREDENTIAL_PROVIDER_UI_REQUIRED`
- `HUMAN_ONLY_DISCONNECTED_INTEGRATION_RECONNECTION_UI_REQUIRED`

A notice requires a trusted source Issue, live open same-repository Pull Request, lowercase 40-character current head SHA, concrete connected paths already attempted, independently observed impossibility evidence, exact targets or provider, one canonical reason-compatible UI action, an automatic-resumption condition, and the same mandatory connected self-resolution audit.

Provider quota or provider absence by itself is never a human-only reason. For account-level repository creation, the runtime independently queries the exact target repositories through the connected GitHub API. Credential and integration-reconnection reasons fail closed until a reason-specific connected evidence adapter proves the UI-only condition; generic caller assertions are never sufficient.

Before publication, the runtime persists a sanitized deterministic notice record at:

```text
automation-stops/pr-<number>/<exact-sha>/<HUMAN_ONLY_REASON>.notice.json
```

The live destination is revalidated before persistence and before publication. Routine technical failures, provider limits, missing evidence, merge state, path denial, untrusted evidence, unsupported assertions, or unresolved ambiguity cannot use the human-only formatter. Automation resumes automatically when the audited UI condition changes; a new owner message is not required.

## Trusted exact-base recovery for an existing Pull Request

An owner-authored Issue may contain exactly one `foundation-queue-existing-pr-base` HTML comment with exactly the keys `pull_request`, `base_ref`, and `base_sha`. The Queue rejects duplicate or malformed blocks and requires a lowercase 40-character SHA. In its fixed default-branch prepare job it freshly verifies that the referenced Pull Request is open, its head and base repositories are this repository (so a fork or cross-repository head is rejected), and its live head ref and SHA exactly match the request. The ref must independently resolve to that SHA.

The implementation job revalidates that identity immediately before work, checks out the immutable SHA with persisted credentials disabled, and creates only a separate generated fix branch identity. It has read-only repository permissions and cannot publish with Git transport. Instead it hands off a canonical SHA-256-bound artifact containing the exact base, path manifest, modes, deletions, per-file digests, and complete base64 file bytes. A separate read-only job verifies and materializes those bytes before running validation.

Only after successful validation does a fixed default-branch publication job receive `contents: write`. It never checks out or executes candidate code. It revalidates the source Pull Request and unmoved base, verifies the artifact and every file digest, and uses the Git Data API to create blobs, a tree, commit, and separate generated ref. It creates or reuses one Draft integration Pull Request whose base is the verified existing branch and records the source Issue and Pull Request, exact base and candidate SHAs, and changed paths. Any movement or identity mismatch stops before publication. Without a valid owner-authored block, the Queue retains its ordinary immutable default-branch base and target behavior.

## Active runtime and retired recovery entry points

- Mandatory Phase 0 consists of the GitHub repository connection, GitHub Actions permissions, and a validated Bootstrap installation pinned to an accepted Foundation source SHA.
- Codex and Claude setup is optional. Provider absence or exhausted provider capacity is nonblocking and must not be converted into a human-action requirement.
- Supported active runtime modules are `scripts/github_coordinator_supervisor.py`, `scripts/supervisor_policy.py`, `scripts/queue_event_guard.py`, the Queue classifier/hydration/retry-identity modules, `scripts/github_api_governor.py`, and `scripts/foundation_drift.py`.
- The former `ai_recovery_supervisor`, `supervisor_final_guard`, `supervisor_runtime`, and `supervisor_queue_recovery` v1/v2/v3 entry points are retired and are not distributed by Bootstrap.
- Queue event admission receives bounded scalar GitHub context only. It never reads `github.event_path` or an event payload file; connected source-Issue, trigger-identity, base-SHA, retry-record, exact-head review, collision, and expected-head merge checks remain fail closed.

## Target-owned product check contract

Generated targets keep `.github/foundation-product-checks.json` outside the Foundation lock. The captured default-branch version names bounded product workflows that must succeed on the exact candidate SHA and be explicitly associated with the same Pull Request. A candidate configuration is parsed for future validity but never judges itself. A configuration-changing Pull Request is therefore judged by the previous default configuration and the new configuration becomes effective only after merge. Currently configured product workflow definitions must remain byte-identical to the captured default branch while they judge a candidate.
