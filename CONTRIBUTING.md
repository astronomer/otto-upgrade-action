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

## Releasing

Consumers pin `@v0`, and `v0` only moves when a release is cut — merged-but-
untagged fixes are invisible to every user (this bit us: `v0` sat on the
initial commit while fixes accumulated on `main` for weeks). On every merge to
`main` that changes behavior:

1. Tag a semver release on the merge commit: `git tag vX.Y.Z <sha>`.
2. Move the floating major tag: `git tag -f v0 <sha>`.
3. Push both: `git push origin vX.Y.Z && git push -f origin v0`.
4. `gh release create vX.Y.Z` with notes covering everything since the last
   tag (check `git log <last-tag>..main` — enumerate earlier unreleased
   merges too, since `@v0` consumers jump straight between releases).
5. Validate before tagging: the release must point at a commit that has passed
   a real end-to-end run (not just CI unit tests) — `@v0` consumers receive it
   unreviewed on their next scheduled run.
