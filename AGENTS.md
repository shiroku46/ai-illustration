# Agent operating contract

1. Work from one trusted owner-authored Issue.
2. Use a dedicated branch and Draft Pull Request; never push directly to `main`.
3. Require every changed and renamed path to match the Issue's ordinary allowlist. Accept only exact repository-relative paths and bounded suffix patterns such as `tests/**`.
4. Require each protected path to appear independently in both the ordinary allowlist and the protected-change authorization block.
5. Do not access another repository, deploy, alter Secrets, change billing, mutate production, or expand permissions.
6. Bind validation, review, recovery, readiness, and merge to the current exact head SHA.
7. Treat stale, candidate-authored, incomplete, missing, pending, failed, wrong-workflow, wrong-repository, cross-Pull-Request, and candidate-modified-workflow evidence as absent.
8. Before readiness or merge, compare each required candidate workflow file blob with the stable default-branch blob, then require a successful same-repository native Pull Request run for the fixed `CI`, `Unit Tests`, and fixed `E2E Acceptance` identity when present.
9. Keep contributor checks read-only with no Secrets, OIDC, or write permission. Keep write-capable execution default-branch-controlled and never execute proposed-branch code there.
10. Do not ask a person merely to press Merge, Approve, Retry, Close, resolve routine review state, or change routine workflow state.
11. Before any stop, query repository metadata, the live Pull Request twice, every changed and renamed path, source Issue allowlist and protected authorization, fixed workflow identities and stable blobs, immutable trusted run/job and native evidence, Codex and threads, permissions, idempotency, and alternative connected paths.
12. Persist routine stops only as deterministic sanitized JSON on `automation-internal-stops`; never post a routine stop comment or mutate a routine stop label. A failed audit or moved head writes nothing.
13. Order combined Codex comments and reviews by immutable event time. Measure no-progress only from immutable exact-SHA request, workflow, and review evidence.
14. Human notification is allowed only for the three canonical account/provider UI reason codes after the same connected audit and live destination revalidation.
15. Account-level repository absence must be independently derived from connected GitHub API queries of the exact targets, and caller assertions must match. Credential and reconnection reasons fail closed without a reason-specific connected provider adapter.
16. Persist the exact deterministic human-only audit record before publication. Deduplication requires both that record and an immutable `github-actions[bot]` notice comment; untrusted or edited comments do not count.
17. `ai-no-merge` always stops merge execution, and final merge uses expected-head-SHA protection.
