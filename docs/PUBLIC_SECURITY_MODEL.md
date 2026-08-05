# Public workflow security model

| Context | Code source | Permissions | Secrets / OIDC |
|---|---|---:|---:|
| Pull Request checks | exact Pull Request head | `contents: read` | none |
| GitHub coordinator Supervisor | current default branch only | `actions: read`, `issues: read`, bounded `contents: write` / `pull-requests: write` | none |
| Optional Claude implementation | immutable authorized base | repository/Issue/PR read, OIDC | one optional Claude credential |
| Optional candidate verification | immutable checkpoint artifact | `contents: read` | none |
| Optional candidate handoff | verified artifact metadata | `contents: read` | none |
| CI reconciliation observation | current default branch | `actions: read`, `contents: read`, `pull-requests: read` | none |
| Bounded Queue recovery | current default branch only | bounded `actions: write`, `contents: write`, `issues: read`, `pull-requests: write` | none |

## GitHub coordinator boundary

GitHub-direct is the ordinary and sufficient route. The Supervisor never checks out or executes Pull Request code. It fails closed unless one exact head has a trusted source Issue, bounded changed and previous paths, explicit protected authorization, no live path collision, unchanged CI/Unit workflow blobs, successful exact-head native runs, risk-tier-required immutable GitHub coordinator review, zero unresolved threads, no `ai-no-merge`, stable source/default/check/review/path evidence, and current mergeability.

Draft candidates are marked Ready only after those gates pass. A complete fresh evaluation follows, then merge uses the exact expected head SHA. Codex and Claude output is never implementation-completion, review, readiness, or merge evidence.

## Optional provider boundary

The Claude Queue starts only after an owner-authored standalone `/claude-run` or explicit owner workflow dispatch. Ordinary Issue creation, CI completion, review, schedule, and Supervisor flow do not select a provider.

A default-branch preflight checks any declared command requirements against the edit-only provider tool contract. Contradictions skip model invocation with public-safe, non-notifying evidence. The provider job uses an immutable credential-free checkout, repository read permissions, OIDC only for the provider action, `track_progress: false`, and a final-five-turn checkpoint reserve.

Complete or WIP checkpoints contain exact base SHA, bounded retry identity, authorized changed paths, and content/diff digests. They are retained for one day. A separate read-only job verifies successful complete checkpoint artifacts and runs Foundation validation. The optional workflow itself has no repository-write job.

Provider failure remains non-blocking. `auth_secret` is human-only only when the optional route was explicitly enabled and a separate connected adapter proved a canonical credential UI action is unavoidable. Without both proofs, `human_action_required` remains false.

## Bounded Queue recovery boundary

The fixed default-branch reconciliation workflow is the only write-capable Queue recovery path. It reacts to completed Queue runs and performs a scheduled silent-stop scan. It does not carry a provider credential, Secret, or OIDC permission.

For a complete checkpoint, recovery requires the exact run-bound `queue-complete-<run-id>` artifact and one successful read-only `verify` job. A WIP checkpoint may be resumed from the exact `queue-wip-<run-id>` artifact or from one same-Issue remote checkpoint branch. The recovery job validates the trusted open owner Issue, immutable trigger identity, exact base SHA, artifact member set, patch and metadata digests, changed paths, protected authorization, retry identity, default-branch stability, live candidate collisions, and deterministic branch identity.

The write-capable job may apply candidate bytes to the Git index solely to compute and publish one commit. It never imports or executes candidate code, never exposes artifact bytes as commands, never force-pushes, and creates at most one deterministic branch and Draft Pull Request per recovery identity. Public read-only CI and Unit Tests, followed by exact-head coordinator review and expected-head merge, remain mandatory.

When no usable checkpoint exists, the recovery path classifies immutable completed-run evidence and persists one deterministic retry intent on `automation-internal-stops` before dispatch. Only classifier-approved retryable classes may dispatch, and the total is bounded to three attempts per Issue/trigger/base fingerprint. Permission, test, and unproven authentication failures do not loop. Exhaustion and routine stops are sanitized, non-notifying records. Scheduled scans detect an open trusted `/claude-run` request with no active Queue run, candidate, branch, or artifact and may create only the next unrecorded bounded attempt.

## Native evidence and Bootstrap parity

`CI` and `Unit Tests` run on the exact candidate with no Secrets, OIDC, or write permission. Candidate-authored status evidence is not merge-authorizing. Actions are pinned to immutable commit SHAs.

Bootstrap copies every managed Foundation file byte-for-byte, including Supervisor, optional Queue, bounded reconciliation, validator, classifier, templates, policy, and startup guidance. Existing consumers must update all managed files from one Foundation revision rather than copying only the recovery workflow. GitHub-only Phase 0 requires connected repository access, enabled Actions/Foundation workflows, and Workflow permissions; no provider environment or credential is required.

## Default-branch product validation

The coordinator reads `.github/foundation-product-checks.json` from one captured default-branch SHA. Candidate configuration bytes are validated but do not select checks for that candidate. Every configured workflow definition must match the captured default blob, and its successful Pull Request run must be bound to the exact remote head and PR. Configuration, workflow, run, association or default-branch races fail closed before expected-head merge.

A newly declared product workflow must already exist as a candidate blob, but it becomes merge-authorizing only after the configuration reaches the default branch. Foundation-owned workflow paths are reserved and cannot be reintroduced under product-check aliases.