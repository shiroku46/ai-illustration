# Security policy

## Reporting

Report suspected vulnerabilities privately through GitHub's security-advisory interface. Do not include credentials, token values, private logs, or private repository links in public issues.

## Trust boundaries

- Fork pull requests are untrusted.
- Proposed-branch code must never execute in a write-capable job.
- `pull_request_target` is intentionally prohibited.
- Write-capable jobs must use default-branch-controlled code.
- Every merge decision is bound to the pull request's current exact head SHA.
- A moved head invalidates checks, review evidence, and prior authorization decisions.
- Protected paths require explicit trusted Issue authorization.
- `ai-no-merge` is a hard hold.
- Secrets, authentication, billing, repository settings, deployment, production mutation, and destructive data operations remain outside ordinary automation.

## Supported versions

Only the current default branch is supported.
