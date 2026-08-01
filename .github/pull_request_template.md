## Source Issue

Closes #

## Exact scope

- 

## Validation

- [ ] `python scripts/public_export_guard.py .`
- [ ] `python scripts/validate_repository.py`
- [ ] `python -m unittest discover -s tests`
- [ ] exact current-head Codex review

## Security

- [ ] no proposed-branch code executes in a write-capable job
- [ ] no Secrets, billing, repository settings, deployment, production, or destructive data changes
- [ ] protected paths are covered by trusted Issue authorization
