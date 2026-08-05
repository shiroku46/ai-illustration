# Minimum safety profile

This document is the authoritative default for day-to-day development performed with this Foundation.

## Default roles

- **Coordinating ChatGPT and the connected GitHub App/API** are the ordinary implementation, publication, exact-SHA review, correction, and merge-orchestration route.
- **GitHub Actions** performs exact-head Foundation and product validation in read-only jobs.
- **Codex and Claude** are optional helpers or second-opinion reviewers. Neither provider is required for implementation, review, or merge.
- **The repository owner** performs only genuinely unavoidable account, credential, approval, or provider-UI operations that connected capabilities cannot complete.

GitHub resources are finite. API requests, Actions usage, artifacts, storage, concurrency, and billing-sensitive features must be bounded and governed; this Foundation never treats GitHub as unlimited.

## Ordinary GitHub-centered flow

1. Start from one trusted owner-authored Issue containing a bounded path scope, risk tier, acceptance criteria, prohibited effects, required checks, and rollback.
2. Inspect all open same-repository Pull Requests and active candidate branches. Do not start overlapping live work unless the Issue declares the exact dependency or integration relationship.
3. Create one same-repository branch from the current intended base. Never push automation changes directly to the default branch.
4. Implement through the connected GitHub write path. Every commit must become visible on the expected remote branch before it counts as progress.
5. Open or update one Pull Request linked to the source Issue.
6. Run required Foundation and product checks on the exact GitHub-visible head SHA.
7. The coordinator inspects the complete exact-head diff, changed and renamed paths, source Issue scope, protected authorization, check conclusions, threads, and mergeability.
8. Apply the risk-tier coordinator-review rule below and persist a nonempty exact-SHA review record.
9. Correct findings through the same GitHub-centered branch. A changed head invalidates all stale check and review evidence.
10. Immediately before merge, re-fetch the Pull Request, exact head SHA, source Issue, scope, protected authorization, required checks, coordinator-review record, unresolved threads, hold state, default branch, and mergeability.
11. Merge only with expected-head-SHA protection.

## Risk tiers and coordinator review

### Low risk

Examples: documentation, text-only guidance, formatting, tests-only changes, and generated metadata with no executable, workflow, permission, authentication, deployment, production, billing, repository-setting, or destructive effect.

Required:

- bounded trusted Issue scope;
- exact-head required checks;
- one coordinator review of the complete GitHub-visible diff;
- no unresolved review thread;
- final live recheck and expected-head merge protection.

### Standard risk

Examples: ordinary application or Foundation code that does not touch a protected category.

Required:

- all low-risk safeguards;
- one structured coordinator review record bound to the exact 40-character GitHub-visible head SHA;
- a nonempty summary of scope, correctness, tests, risks, and whether a blocking finding remains;
- every finding fixed on the same branch and reviewed again after any head change;
- no unresolved review thread.

### Protected risk

Protected categories include workflows, permissions, authentication or Secret interfaces, supervisor and security policy, repository settings, billing, deployment or production, and destructive data operations.

Required:

- explicit protected authorization in the trusted Issue;
- all exact-head Foundation and product checks;
- a first coordinator scope/security review of authorization, changed and renamed paths, permissions, trust boundaries, Secret/OIDC isolation, candidate-execution boundaries, and prohibited effects;
- a second coordinator correctness/race review on the unchanged exact head after all evidence queries;
- one nonempty structured exact-SHA coordinator review record describing both passes and stating whether a blocking finding remains;
- no unresolved review thread;
- final live recheck and expected-head merge protection.

The two protected review passes are separate review stages by the coordinating ChatGPT. They are not external-provider audits and do not require Codex or Claude.

## Optional provider policy

- `github-direct` is the ordinary implementation route.
- Codex and Claude may be selected only when the owner explicitly requests a provider or the coordinator deliberately uses one as a non-blocking helper or second opinion.
- Provider quota, setup, account, connection, generic assistant, stale-SHA, or content-free output is not authoritative review evidence.
- Provider absence or failure must record `human_action_required: false` unless a separately proven canonical UI-only condition exists.
- Do not repeatedly post identical provider requests.
- Provider output never replaces the risk-tier-required coordinator review.

## GitHub-visible completion rule

Local agent output is advisory. A branch write, commit, correction, review target, or completion exists only when connected GitHub evidence confirms the expected repository, branch, and exact remote SHA. Provider-reported commits that were not pushed never satisfy implementation, validation, review, or merge gates.

## Required coordinator-review record

The exact-head review record must include:

- source Issue number;
- Pull Request number;
- lowercase 40-character GitHub-visible head SHA;
- risk tier;
- reviewed current and previous renamed paths;
- required checks and observed conclusions;
- unresolved review-thread count;
- scope/security result when protected;
- correctness/race result;
- nonempty findings summary;
- final result: `clean` or `blocked`.

A changed head invalidates the record. An edited, stale, content-free, wrong-repository, wrong-Pull-Request, or untrusted record is absent.

## Required status fields

The Pull Request body or machine-readable status must record:

- `implementation_route: github-direct | codex-optional | claude-optional`;
- exact GitHub-visible head SHA;
- risk tier: `low | standard | protected`;
- `review_route: github-coordinator`;
- `review_state: required | pending | clean | blocked`;
- required checks and current conclusions;
- unresolved review-thread count;
- next automatic action;
- `human_action_required: true | false`.

Legacy provider fields may remain during migration, but `selected_auditor: none` and provider `route-unavailable` never block completion under this policy.

## Invariants that are never relaxed

- no automation direct push to the default branch;
- no force update of a shared branch;
- one trusted owner-authored bounded scope;
- same-repository provenance;
- complete collision preflight before new implementation or publication;
- exact GitHub-visible remote SHA for checks, review, and merge;
- no Secret value access, output, persistence, copying, hashing, or inference;
- no proposed-branch execution in a job carrying Secrets, OIDC, or repository write permission;
- no arbitrary repository, ref, workflow, path, URL, endpoint, provider, or command selected from untrusted content;
- explicit authorization for protected operations;
- required Foundation and product checks;
- no unresolved review thread;
- one final live recheck;
- expected-head-SHA merge protection;
- no deployment, production, billing, repository-setting, paid-feature, or destructive mutation without separate explicit authorization.

## Human-action boundary

Provider quota or absence is not a request for the owner to perform routine implementation, review, retry, or merge work. The coordinator continues through GitHub-direct work and records a non-notifying automation-owned state when another technical gate is genuinely blocked.

Human notice remains limited to genuine account, credential, MFA, CAPTCHA, hardware-key, trusted-device, environment approval, billing, or disconnected-integration UI operations that cannot be completed through connected capabilities.
