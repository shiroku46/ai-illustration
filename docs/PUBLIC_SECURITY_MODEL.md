# Public workflow security model

| Context | Code source | Permissions | Secrets |
|---|---|---:|---:|
| Fork or same-repository Pull Request checks | exact Pull Request head | `contents: read` | none |
| Queue implementation | default-branch workflow plus bounded Claude write step | bounded repository write | one configured Claude credential |
| Trusted exact-SHA validation | fixed current default-branch workflow; candidate executes only in isolated jobs | `contents: read` in candidate jobs | none |
| Reconciliation | current default branch | fixed Actions dispatch write | none |
| Supervisor | current default branch | bounded Issue/Pull Request/Actions writes | none |

Write-capable jobs never check out or execute a proposed branch. They inspect metadata through fixed GitHub APIs and dispatch only the allowlisted `trusted-checks.yml` workflow on the current default branch for an immutable candidate SHA.

## Immutable attestation contract

The trusted evidence is GitHub-owned workflow-run and workflow-job metadata. The supervisor accepts an attempt only when all of the following remain true:

- the workflow ID and path identify `.github/workflows/trusted-checks.yml`;
- the workflow run used `workflow_dispatch` on the current default branch and its workflow SHA is the current default-branch SHA;
- the actor is the repository owner, configured owner, or `github-actions[bot]`;
- the display title contains the exact full candidate SHA and the fixed authorization job proves that SHA is still an eligible same-repository Pull Request head;
- candidate execution checks out that immutable input SHA with persisted credentials disabled and verifies `git rev-parse HEAD` before validation;
- exactly one job named `CI / validate` and exactly one job named `Unit Tests / test` belong to the recognized run ID and carry that run's trusted default-branch `head_sha`;
- the run and both required jobs are completed successfully.

Foreign-workflow, wrong-actor, wrong-title, wrong-path, or stale-default-branch runs are rejected before attempt classification and can never authorize progress. Once a run passes those identity gates, missing, duplicate, cancelled, failed, incomplete, wrong-run, or wrong-workflow-SHA job evidence fails closed and consumes the bounded recognized-attempt budget. Candidate-authored commit statuses and custom Check Runs are not merge-authorizing evidence.

Actions are pinned to immutable commit SHAs. Candidate scans and recognized retry attempts are bounded. Same-repository provenance and explicit protected-path authorization are mandatory. Idempotency markers prevent duplicate comments and repeated actions. Merge requires clean exact-SHA Codex evidence, mergeability, and `expected_head_sha` protection.
