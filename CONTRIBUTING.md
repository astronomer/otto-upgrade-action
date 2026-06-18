# Contributing

## Local development

```bash
uv run --with pytest python -m pytest tests/ -q          # unit tests (no network — fixtures)
uv run --with ruff ruff check scripts/ tests/            # lint
shellcheck --severity=warning scripts/*.sh               # shell lint
act -j dry-run                                           # e2e dry-run locally (needs Docker)
```

## Layout

- `action.yaml` — the composite action (inputs, outputs, step orchestration).
- `scripts/` — the work: `detect_versions.py`, `resolve_target.py`, `apply_bump.py`,
  `build_pr_body.py`, `import_check.py` (Python, unit-tested) and `build-prompt.sh`,
  `run-otto.sh`, `verify.sh`, `open-pr.sh` (bash, exercised by the e2e).
- `tests/` — pytest over the Python scripts (HTTP is stubbed; no network).
- `e2e/` — a sample Astro project the e2e workflow upgrades.

## Pull requests

- Keep CI green: tests, `ruff`, `shellcheck`, `yamllint`.
- Pin any new `uses:` action to a commit SHA with a `# vX.Y.Z` comment.
- Add a test for behavior changes — the resolver/tiering and detection logic are
  fully unit-testable.
- Don't commit secrets. The version-resolution path is intentionally
  unauthenticated.
