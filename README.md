# Otto Airflow Upgrade

> **Status: internal preview.** This action is the proactive counterpart to
> [`otto-review-action`](https://github.com/astronomer/otto-review-action). The
> code-migration step depends on an Otto build that ships the `upgrader`
> persona; until that lands, run with `run-otto: false` for pure, dependable
> version-bump PRs (still Runtime-aware, compatibility-clamped, and verified).

A scheduled, **dependabot-style** upgrader for Astro/Airflow projects. On a cron
it detects what your project is running, works out the safe target, bumps the
Astro Runtime tag and pinned providers, optionally applies the breaking-change
code migration with Otto, verifies the result, and opens (or updates) a single
pull request.

`otto-review-action` is **reactive** — a human opens a PR and Otto reviews the
diff. This action is the **proactive** mirror image: nobody opens anything;
the bot notices you're behind and authors the upgrade for you.

## Why not just point dependabot at `requirements.txt`?

Dependabot bumps version strings. It doesn't know:

1. **Runtime ↔ Airflow mapping** — your base image is `FROM .../runtime:3.1-12`,
   not `apache-airflow==3.1.7`. This action bumps the Runtime tag.
2. **Compatibility clamping** — it won't propose a provider major that your
   pinned Airflow can't take. The `max-upgrade-scope` clamp holds bumps back to
   the largest jump you've allowed.
3. **Breaking-change code migration** — the Otto step rewrites moved imports and
   renamed call sites, not just the pin.
4. **Verification at the target** — the PR carries a "all N DAGs still import at
   the target Airflow" check, so the bump is trustworthy, not just plausible.

## Quickstart

```yaml
# .github/workflows/airflow-upgrade.yml
name: Airflow upgrade
on:
  schedule:
    - cron: "0 6 * * 1"   # Mondays 06:00 UTC
  workflow_dispatch:

permissions:
  contents: write          # push the upgrade branch
  pull-requests: write     # open/update the PR

jobs:
  upgrade:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astronomer/otto-upgrade-action@v0
        with:
          astro-api-token: ${{ secrets.ASTRO_API_TOKEN }}
          astro-organization: ${{ secrets.ASTRO_ORGANIZATION }}
          target: latest-minor
          max-upgrade-scope: minor
```

The consuming workflow must `actions/checkout` the repo first — the action
operates on the working tree and pushes a branch from it.

## The tiering model

Airflow upgrades aren't all the same risk, so the action tiers every jump and
behaves differently per tier:

| Tier | Example | What the action does |
| --- | --- | --- |
| 🟢 **patch** | Runtime `3.1-14` → `3.1-17` (same Airflow minor) | Bump pins, verify, open PR. Usually no code migration needed. |
| 🟡 **minor** | Runtime `3.1` → `3.2` | Bump pins, run the Otto code migration, verify, open PR with a breaking-change summary. |
| 🔴 **major** | Airflow `2.x` → `3.x` | **Advisory only.** A scheduled bot never auto-authors a major migration; it surfaces it for the guided upgrade (`astro otto`) instead. |

Tiering is computed from the **Airflow version** behind each Runtime tag, so it
is correct regardless of the tag scheme (AF2-era `12.12.0` vs AF3-era `3.2-5`).

`target` chooses how far to reach (`patch` / `latest-minor` / `latest`);
`max-upgrade-scope` is the safety clamp. A `latest` target that would be a major
jump is clamped down to the newest in-scope version, and the PR notes that a
larger upgrade was held back.

## Verification

The `verify-level` input controls the post-upgrade check:

- `syntax` — byte-compile every DAG (fast, no network).
- `import` (default) — additionally import every DAG inside an ephemeral env
  pinned to the **target** Airflow + providers. This catches the failure mode
  upgrades actually cause: a moved/removed import or renamed call site.
- `none` — skip.

Verification only ever reports **failed** on a genuine code error. If the target
environment can't be provisioned (no network, resolution error), it reports
**skipped** — infra flakiness never masquerades as a broken upgrade.

## The rolling PR

The action maintains **one** branch (`otto/airflow-upgrade` by default). Re-runs
force-push to it and edit the same PR in place rather than stacking a new PR
every run. When a newer target ships next week, this PR updates to it.

## Inputs

| Name | Default | Description |
| --- | --- | --- |
| `astro-api-token` | env `ASTRO_API_TOKEN` | Token for the Otto migration step. Without it, the action still opens the version-bump PR and flags breaking changes for manual review. |
| `astro-organization` | env `ASTRO_ORGANIZATION` | Org ID for gateway routing. |
| `astro-domain` | `astronomer.io` | Override for non-prod. |
| `github-token` | `${{ github.token }}` | Pushes the branch and opens the PR. Needs `contents:write` + `pull-requests:write`. Use a PAT/App token if you want the PR to trigger your other CI. |
| `project-path` | `.` | Astro project root (holds the Dockerfile + requirements.txt). |
| `target` | `latest-minor` | `patch`, `latest-minor`, or `latest`. |
| `max-upgrade-scope` | `minor` | `patch`, `minor`, or `major`. Majors stay advisory-only regardless. |
| `include-providers` | `true` | Also bump pinned providers. Unpinned are reported, never changed. |
| `run-otto` | `true` | Run the Otto code migration for minor jumps. `false` = pure version bump. |
| `verify-level` | `import` | `syntax`, `import`, or `none`. |
| `base-branch` | _(checked-out branch)_ | PR base. |
| `branch` | `otto/airflow-upgrade` | Rolling head branch. |
| `labels` | `airflow-upgrade,dependencies` | Comma-separated PR labels (best-effort). |
| `model` | _(persona default)_ | `--model` passed to Otto. |
| `astro-cli-version` | _(latest)_ | Astro CLI version for the Otto step. |
| `dry-run` | `false` | Compute + apply locally, write the would-be PR body to the job summary, open nothing. |

## Outputs

| Name | Description |
| --- | --- |
| `current-runtime` / `target-runtime` | Detected and resolved Runtime tags. |
| `overall-scope` | Largest tier across the plan: `patch`/`minor`/`major`/`none`. |
| `no-update` | `true` when nothing is behind. |
| `verify-status` | `passed`/`failed`/`skipped`. |
| `pr-number` / `pr-url` / `branch` | The opened/updated PR (empty in dry-run / no-op). |

## How it works

1. **Detect** — parse the Dockerfile `FROM` Runtime tag and `apache-airflow-providers-*` pins.
2. **Resolve** — query the public Runtime feed and PyPI (no credentials needed), pick the target per `target`/`max-upgrade-scope`, tier the jump.
3. **Apply** — rewrite the Runtime tag and provider pins in place (extras, markers, and comments preserved).
4. **Migrate** (minor jumps, Otto) — `astro otto --persona upgrader` rewrites moved imports / renamed call sites over the bumped project. Degrades gracefully: if Otto is unavailable or errors, the version-bump PR still opens with a manual-review note.
5. **Verify** — syntax + (default) import-at-target check.
6. **Open/update PR** — single rolling branch, body with the version table, migration summary, and verification result.

## Security notes

- The Astro token is masked and exported only to the Otto step. The GitHub token
  is never exposed to Otto.
- Run this on a schedule (or `workflow_dispatch`) against your own default
  branch — not on `pull_request_target` from forks.
- The resolve step is intentionally unauthenticated so it works in CI / `act`
  without secrets.

## Development

```bash
uv run --with pytest python -m pytest tests/ -q   # unit tests (no network — fixtures)
act -j dry-run                                    # e2e dry-run locally (needs Docker)
```

See [`e2e/`](./e2e) for the sample Astro project the e2e workflow upgrades.
