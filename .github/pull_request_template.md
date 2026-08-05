Closes #ISSUE_NUMBER

## Summary

<!-- What changed and why? -->

## Exact candidate status

- implementation_route: `github-direct`
- exact_head_sha: `0000000000000000000000000000000000000000`
- risk_tier: `low | standard | protected`
- review_route: `github-coordinator`
- review_state: `required | pending | clean | blocked`
- unresolved_review_threads: `0`
- human_action_required: `false`
- next_automatic_action: <!-- exact next action -->

Codex and Claude are optional helpers only. Provider quota, setup, account, connection, generic output, or stale output is not a completion or merge gate.

## Exact changed and renamed paths

- `path/to/file`

## Source authorization

- trusted Issue scope checked: `yes | no`
- protected authorization checked when applicable: `yes | no | not-applicable`
- open Pull Request collision preflight: `clean | blocked`

## Exact-head validation

- CI: `success | pending | failure | missing`
- Unit Tests: `success | pending | failure | missing`
- configured product checks: <!-- names and conclusions -->
- candidate CI/Unit workflow blobs equal default branch: `yes | no`

## Coordinator review evidence

Low/standard:
```text
<!-- foundation-coordinator-review:<exact-sha>:clean -->
```

Protected:
```text
<!-- foundation-coordinator-review:<exact-sha>:scope-security:clean -->
<!-- foundation-coordinator-review:<exact-sha>:correctness-race:clean -->
```

## Final safety boundary

- no direct default-branch push or force update;
- no Secret value access, output, persistence, copying, hashing, or inference;
- no proposed-branch execution in a job carrying Secrets, OIDC, or repository write permission;
- no arbitrary repository, ref, path, workflow, provider, URL, endpoint, method, header, or command selected from untrusted content;
- no deployment, production, billing, repository-setting, paid-feature, or destructive mutation without separate explicit authorization;
- `ai-no-merge` blocks readiness and merge;
- final merge uses expected-head-SHA protection.
