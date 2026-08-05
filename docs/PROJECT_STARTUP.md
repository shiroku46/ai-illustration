# Repository startup: mandatory Phase 0

Every newly bootstrapped repository must complete setup steps 1–5 before the harmless Bootstrap acceptance exercise. Successful completion of the acceptance exercise in step 6 is the final Phase 0 gate. The first product Issue or product implementation request may start only after that final gate passes.

The coordinating ChatGPT/agent performs every check available through connected tools first. It asks the owner only for settings that require an authenticated provider UI, local authenticated CLI, MFA, CAPTCHA, hardware key, or another operation for which no callable connector/API exists.

Pre-PR Phase 0 guidance is a narrowly scoped exception to the runtime GitHub human-notice mechanism: it is delivered directly in the project-start conversation for the exact repository before GitHub orchestration starts. It does not call `human_only_notice()`. It does not publish an automated GitHub notice. It does not create a new runtime reason code. It does not require an Issue/PR destination. It may contain only the exact non-secret UI navigation or local command, the reason it is necessary, and the automatic-resumption condition. The coordinator records completion later in the non-secret Bootstrap acceptance evidence and does not repeat the guidance unless connected evidence shows the prerequisite became unusable.

## 1. Connect the exact GitHub repository

- Connect the GitHub account to ChatGPT and authorize the exact target repository in the GitHub app or connector configuration.
- Confirm the repository is visible and writable through the connected GitHub route after any indexing delay.
- Confirm branch, file, Issue, Pull Request, check, and expected-head merge operations are available within the repository-scoped permissions.

GitHub repository access is mandatory. A provider-specific repository connection is not required for the GitHub-only route.

## 2. Confirm provider independence and optionally enable providers

Codex and Claude are optional helpers. GitHub-only acceptance, implementation, coordinator review, correction, and merge must continue when either or both providers are absent, disconnected, or at their usage limits.

Only when the owner deliberately enables an optional provider:

- for Codex, connect the account and create an environment for the exact repository;
- for Claude, complete the optional credential setup described in step 3.

Provider replies such as `To use Codex here, create an environment for this repo` or `To use Codex here, create a Codex account and connect to github` apply only to the optional Codex route. They are not review evidence, do not block GitHub-direct development, and must not trigger repeated implementation or review requests.

## 3. Install the Claude credential only when Claude is deliberately enabled

For the GitHub-only route, no Claude credential is required.

When the owner deliberately enables the optional Claude route and OAuth is used:

- generate the token locally with `claude setup-token`;
- store the value only as the repository Actions Secret named exactly `CLAUDE_CODE_OAUTH_TOKEN`;
- never paste, print, commit, log, hash, copy, or include the token value in an Issue, Pull Request, workflow, document, or chat transcript.

Provider absence, credential absence, or quota exhaustion must record `human_action_required: false` unless the owner has explicitly enabled that provider and a separately proven credential UI action is genuinely necessary.

## 4. Configure GitHub Actions and Workflow permissions

In the exact target repository, open:

`Settings` → `Actions` → `General` → `Workflow permissions`

Set and save both required options:

1. select **Read and write permissions**;
2. enable **Allow GitHub Actions to create and approve pull requests**.

Also confirm:

- GitHub Actions are enabled for the repository;
- the Foundation workflows exist on the default branch;
- `AUTOMATION_OWNER` is set only when the repository owner is not the intended trusted actor.

These Workflow permissions are mandatory because the Foundation must be able to create or update branches and Pull Requests, post authorized status or review records, update review/readiness state, and complete bounded expected-head merge orchestration. If those operations repeatedly fail, inspect this setting before retrying workflows or asking the owner to repost commands.

The coordinator must verify the repository setting through connected repository-settings/API tools when a callable endpoint exists. When no such endpoint is available, use the narrowly scoped pre-PR guidance defined above: give the exact navigation once, never request a value or screenshot containing credentials, and resume automatically after the owner confirms the setting is saved.

## 5. Validate the installed Foundation

Run the Foundation checks or the generated-target equivalent:

```bash
python scripts/public_export_guard.py .
python scripts/validate_repository.py
python -m unittest discover -s tests
```

Validation must not depend on Codex or Claude availability. Optional provider setup is tested only when that route is deliberately enabled.

## 6. Complete Bootstrap acceptance and finish Phase 0

After steps 1–5 pass, prove that the exact repository can complete one harmless bounded candidate entirely through GitHub:

- create a dedicated same-repository branch and Pull Request;
- receive native CI and Unit Tests on the exact remote head SHA;
- record a nonempty exact-SHA coordinator review for the candidate;
- confirm automation can perform the required bounded write operations without a Workflow-permissions failure;
- confirm no Codex or Claude response is required for completion;
- confirm expected-head protection is used for the merge decision.

Successful acceptance completes Phase 0 and unlocks product work.

## Phase 0 evidence

Record only non-secret evidence:

- exact repository name;
- date of acceptance;
- connected GitHub repository access confirmed;
- GitHub Actions and Foundation workflows enabled;
- Workflow permissions set to **Read and write permissions**;
- **Allow GitHub Actions to create and approve pull requests** enabled;
- Bootstrap acceptance Issue/PR, exact head SHA, successful checks, and coordinator-review record;
- optional Codex environment confirmation only when Codex was deliberately enabled;
- optional Claude Secret **name** confirmation only when Claude was deliberately enabled, never its value.

After the exact repository passes Phase 0, **Do not request these steps again** unless connected evidence shows that repository authorization, Actions availability, Workflow permissions, or a deliberately enabled optional provider route is no longer usable. Resume orchestration automatically after the missing UI-only prerequisite is completed; the owner must not be asked to repost a provider command, copy a prompt, create a Pull Request, press Retry, or repeat routine instructions.

## Non-destructive Bootstrap publication

Choose the installation mode before rendering:

- `new-repository` requires an empty target except for `.git`;
- `existing-product` preserves an existing `README.md`, `LICENSE`, `AGENTS.md`, `CLAUDE.md`, and `SECURITY.md`, adds absent Foundation-specific paths, and stops before mutation when a different managed destination already exists.

The renderer computes the complete plan before writing. Review every `add`, `upgrade`, `preserved`, `overwrite-authorized`, and `collision` entry. An exact trusted Issue may authorize overwrite of one named managed path, but no blanket overwrite is allowed.

Every successful render creates `FOUNDATION.lock.json` with the exact source repository/SHA, generator version, installation mode, timestamp, and sorted managed path hashes. Run:

```bash
python scripts/foundation_drift.py --root .
```

For an upgrade, render the new Foundation into a separate candidate directory and compare its lock:

```bash
python scripts/foundation_drift.py --root . --expected-lock /path/to/candidate/FOUNDATION.lock.json
```

Publish rendered bytes through the connected GitHub App/API on one dedicated same-repository branch and Draft Pull Request. Verify the exact observed default SHA before branch creation and the exact candidate SHA after publication. Never install by committing a temporary workflow to the default branch, never require `BOOTSTRAP_WORKFLOW_TOKEN` or a PAT, and never force-update the Bootstrap branch.

Rollback requires no installer cleanup commit: close the unmerged Draft Pull Request, or revert the single protected Bootstrap merge.

## Configure product-native validation

After Bootstrap, define required application lint, test, build or type-check workflows in `.github/foundation-product-checks.json`. The file is target-owned and excluded from `FOUNDATION.lock.json`. Use only fixed same-repository Pull Request workflows. The GitHub Coordinator requires successful exact-head runs explicitly associated with the candidate PR and refuses candidate-modified workflow definitions. Provider availability is unrelated to product validation.
