# Otto Airflow Upgrade

> **Status: internal preview.** This action is the proactive counterpart to
> [`otto-review-action`](https://github.com/astronomer/otto-review-action). The
> code-migration step depends on an Otto build that ships the `upgrader`
> persona; until that lands, run with `run-otto: false` for pure, dependable
> version-bump PRs (still Runtime-aware, compatibility-clamped, and verified).

A scheduled, **dependabot-style** upgrader for Astro/Airflow projects. On a cron
it detects what your project runs, works out the safe target, bumps the Astro
Runtime tag and pinned providers, optionally applies the breaking-change code
migration with Otto, verifies the result, and opens (or updates) one pull
request.

`otto-review-action` is **reactive** — a human opens a PR and Otto reviews the
diff. This action is the **proactive** mirror image: nobody opens anything; the
bot notices you're behind and authors the upgrade for you.

| | Trigger | Otto's job | Output |
| --- | --- | --- | --- |
| `otto-review-action` | a PR opens | review the human's diff | inline comments + verdict |
| **`otto-upgrade-action`** | a schedule fires | author the upgrade diff | a version-bump + migration PR |

## Why not just point dependabot at `requirements.txt`?

Dependabot bumps version strings. It doesn't know:

1. **Runtime ↔ Airflow mapping** — your base image is `FROM .../runtime:3.1-12`,
   not `apache-airflow==3.1.7`. This action bumps the Runtime tag.
2. **Compatibility clamping** — it won't propose a provider major your pinned
   Airflow can't take. `max-upgrade-scope` holds bumps to the largest jump you allow.
3. **Breaking-change code migration** — the Otto step rewrites moved imports and
   renamed call sites, not just the pin.
4. **Verification at the target** — the PR carries an "all N DAGs still import at
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

concurrency:               # one rolling-PR run at a time
  group: otto-airflow-upgrade
  cancel-in-progress: false

jobs:
  upgrade:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false   # don't leave the write token in the workspace
      - uses: astronomer/otto-upgrade-action@v0
        with:
          astro-api-token: ${{ secrets.ASTRO_API_TOKEN }}
          astro-organization: ${{ secrets.ASTRO_ORGANIZATION }}
          target: latest-minor
          max-upgrade-scope: minor
```

The consuming workflow must `actions/checkout` the repo first — the action
operates on the working tree and pushes a branch from it.

## Scenarios

Worked examples of what the action does for the common starting states. Assume
the quickstart config (`target: latest-minor`, `max-upgrade-scope: minor`) unless
noted.

### 1. Patch — a newer Runtime build on the same Airflow

Your `Dockerfile` says `FROM .../runtime:3.1-16` and Astronomer ships `3.1-17`
(same Airflow 3.1.x — a base-image CVE or provider-bundle fix).

→ Tier **🟢 patch**. The action bumps the tag, runs verification, and opens a PR.
No code migration (imports don't move within a patch). This is the boring,
high-value cadence teams skip — safe to merge once CI is green.

> Note: patch-level Runtime bumps occasionally carry a breaking change in a
> bundled provider. Verification (import-at-target) is your backstop here.

### 2. Minor — `3.1` → `3.2` with breaking changes

`FROM .../runtime:3.1-12` and the newest Runtime on your Airflow major is `3.2-5`
(Airflow 3.2.x).

→ Tier **🟡 minor**. The action bumps the tag, runs the Otto migration (rewrites
moved imports, renames changed params), verifies every DAG imports at 3.2, and
opens a PR whose body has the version table, a "breaking changes handled" list,
a "manual follow-ups" checklist, and the verification result. Always
human-reviewed.

### 3. Provider-only — Runtime current, providers behind

Runtime is already newest, but `apache-airflow-providers-amazon==9.0.0` is pinned
and `9.30.0` is out.

→ The action bumps the provider pin, runs the Otto migration framed around the
provider transition, and verifies DAGs import against your **current** Airflow
plus the new provider. PR title: "Upgrade 1 Airflow provider(s)".

### 4. Major — Airflow 2 → 3 available

You're on a 2.x Runtime and Airflow 3 is available.

→ Tier **🔴 major**. The action **does not author a PR**. It emits a job-summary
advisory pointing you at the guided upgrade (`astro otto`, the Airflow upgrade
workflow), because a 2→3 migration is not something a scheduled bot should ship
unattended. (Provider majors *are* authored — only Airflow majors are advisory.)

### 5. Clamped — a bigger jump held back by `max-upgrade-scope`

You set `target: latest` but `max-upgrade-scope: minor`, and the newest Runtime
is a major jump.

→ The action clamps to the newest in-scope (minor) target and opens that PR, with
a note: "A larger upgrade was available but held back by `max-upgrade-scope`.
Raise the input to go further."

### 6. Nothing to do

Everything is already at the newest in-scope version.

→ No-op. `no-update` output is `true`, no branch, no PR, no noise.

### 7. Pure version bump (no token / no migration)

`run-otto: false` (or no `ASTRO_API_TOKEN`).

→ The action still bumps pins, verifies, and opens the PR — it just skips the
code migration and the PR body says "review breaking changes manually". Use this
for the patch cadence or to keep the action credential-free.

### 8. Verification fails at the target

A DAG won't import at the bumped version (a provider dropped a class you use).

→ The PR **still opens** (so you see the proposed upgrade and the failure
together), its body leads with a ⚠️ "verification failed" banner, and the
scheduled run goes **red** so you notice. Merge-gating stays with your repo's CI.

## Cadence — what a run actually does day to day

Most scheduled runs do nothing, by design. A run is cheap and quiet unless
something actually shipped:

- **It runs tomorrow and nothing changed** (no new Airflow, provider, or Runtime
  release) → **no-op**. No branch, no commit, no PR, a clean green run,
  `no-update: true`. You won't even get a notification.
- **The rolling PR already proposes the current newest target** (it opened a
  `3.2-5` PR last week, you haven't merged it, and nothing newer shipped) → the
  action recomputes the same target, sees the rolling branch already has an
  identical tree, and **skips the push entirely** — no new commit, no
  "force-pushed" event, no re-triggered CI on the PR. The open PR is left exactly
  as it was.
- **A newer patch/minor shipped since the last run** → it updates the existing
  rolling PR in place (or opens one if none is open).
- **A new Airflow major shipped** → a job-summary advisory, no PR.

So on a repo that's already current, the bot is effectively invisible. You hear
from it only when there's a real upgrade to look at — which is the whole point of
running it on a schedule instead of remembering to check.

## When this helps (and when it doesn't)

**Helps when:**

- You run one or more Astro projects and staying on the latest patch/minor is
  busywork nobody gets to.
- You want the CVE/patch cadence handled automatically, as a verified PR you just
  review and merge.
- You'd rather have a standing "here's your next minor, pre-migrated and
  import-checked" PR than discover at audit time that you're six minors behind.

**Doesn't help / hold off when:**

- **Brand-new or unpinned projects** — there's nothing pinned to bump. Pin your
  Runtime tag and providers first.
- **Airflow major migrations (2→3)** — advisory-only here; use the guided upgrade
  (`astro otto`) and review breaking changes interactively.
- **Repos with untrusted DAG authors** — `verify-level: import` runs repo code to
  import it; prefer `verify-level: syntax` there.
- **You merge slowly and dislike a long-lived PR updating under you** — the rolling
  PR advances to newer targets over time. Merge promptly, or pin `target: patch`
  to keep it on the calmest cadence.

## What a PR looks like

For scenario 2 the action edits exactly this:

```diff
# Dockerfile
-FROM astrocrpublic.azurecr.io/runtime:3.1-12
+FROM astrocrpublic.azurecr.io/runtime:3.2-5

# requirements.txt
-apache-airflow-providers-amazon==9.0.0
+apache-airflow-providers-amazon==9.30.0
```

…plus any import rewrites Otto applied to your DAGs, and a PR body with:

- a **version table** (component · from · to · tier · notes),
- the **breaking changes handled** and a **manual follow-ups** checklist (when Otto ran),
- the **verification** result.

## Tuning: `target` × `max-upgrade-scope`

`target` is how far to reach; `max-upgrade-scope` is the safety clamp. The clamp
always wins.

| | `max-upgrade-scope: patch` | `max-upgrade-scope: minor` | `max-upgrade-scope: major` |
| --- | --- | --- | --- |
| `target: patch` | newest build on current minor | same | same |
| `target: latest-minor` | clamped to patch | newest within current Airflow major | same |
| `target: latest` | clamped to patch | clamped to minor | newest stable (Airflow majors still advisory) |

Recommended defaults: **`latest-minor` + `minor`**. Conservative shops wanting only
the safe cadence: **`patch` + `patch`**.

## Verification

The `verify-level` input controls the post-upgrade check:

- `syntax` — byte-compile every DAG (fast, no network).
- `import` (default) — additionally import every DAG inside an ephemeral env built
  from your project's (bumped) `requirements.txt` plus the target Airflow. Catches
  the failure mode upgrades actually cause: a moved/removed import or renamed call site.
- `none` — skip.

Verification only ever reports **failed** on a genuine code error. If the target
env can't be provisioned (no network, resolution error), it reports **skipped** —
infra flakiness never masquerades as a broken upgrade. The DAG-import subprocess
runs with the Astro and GitHub tokens stripped from its environment.

## The rolling PR

The action maintains **one** branch (`otto/airflow-upgrade` by default). Re-runs
force-push to it (with a fetched lease) and edit the same PR in place rather than
stacking a new PR every run. When a newer target ships next week, this PR updates
to it. Use the `concurrency` group (see quickstart) so overlapping scheduled runs
don't race the branch.

## Inputs

| Name | Default | Description |
| --- | --- | --- |
| `astro-api-token` | env `ASTRO_API_TOKEN` | Token for the Otto migration step. Without it, the action still opens the version-bump PR and flags breaking changes for manual review. |
| `astro-organization` | env `ASTRO_ORGANIZATION` | Org ID for gateway routing. |
| `astro-domain` | `astronomer.io` | Override for non-prod. |
| `github-token` | `${{ github.token }}` | Pushes the branch and opens the PR. Needs `contents:write` + `pull-requests:write`. Use a PAT/App token if you want the PR to trigger your other CI (see FAQ). |
| `project-path` | `.` | Astro project root (holds the Dockerfile + requirements.txt). |
| `target` | `latest-minor` | `patch`, `latest-minor`, or `latest`. |
| `max-upgrade-scope` | `minor` | `patch`, `minor`, or `major`. Airflow majors stay advisory-only regardless. |
| `include-providers` | `true` | Also bump pinned providers. Unpinned are reported, never changed. |
| `run-otto` | `true` | Run the Otto code migration for minor/major jumps. `false` = pure version bump. |
| `verify-level` | `import` | `syntax`, `import`, or `none`. |
| `base-branch` | _(repo default branch)_ | PR base. |
| `branch` | `otto/airflow-upgrade` | Rolling head branch. |
| `labels` | `airflow-upgrade,dependencies` | Comma-separated PR labels (best-effort). |
| `model` | _(persona default)_ | `--model` passed to Otto. |
| `astro-cli-version` | _(latest)_ | Astro CLI version for the Otto step. |
| `dry-run` | `false` | Compute + preview the would-be PR in the job summary, open nothing, don't mutate the tree. |

## Outputs

| Name | Description |
| --- | --- |
| `current-runtime` / `target-runtime` | Detected and resolved Runtime tags. |
| `overall-scope` | Largest tier across the plan: `patch`/`minor`/`major`/`none`. |
| `no-update` | `true` when nothing is behind. |
| `verify-status` | `passed`/`failed`/`skipped` (`skipped` when verification didn't run). |
| `pr-number` / `pr-url` / `branch` | The opened/updated PR (empty in dry-run / no-op). |

## How it works

1. **Detect** — parse the Dockerfile `FROM` Runtime tag (and any `@sha256:` digest) and `apache-airflow-providers-*` pins.
2. **Resolve** — query the public Runtime feed and PyPI (no credentials needed), pick the target per `target`/`max-upgrade-scope`, tier the jump. Digest-pinned `FROM` lines are reported, not silently bumped.
3. **Apply** — rewrite the Runtime tag and provider pins in place (extras, markers, and comments preserved).
4. **Migrate** (minor/major jumps, Otto) — `astro otto --persona upgrader` rewrites moved imports / renamed call sites over the bumped project. Degrades gracefully: if Otto is unavailable or errors, the version-bump PR still opens with a manual-review note.
5. **Verify** — syntax + (default) import-at-target check.
6. **Open/update PR** — single rolling branch, body with the version table, migration summary, and verification result.

## Permissions & tokens

- `permissions: { contents: write, pull-requests: write }` on the workflow.
- `actions/checkout` with `persist-credentials: false` — the action pushes with an
  explicit token instead, so the write-scoped credential isn't left in the
  workspace for the Otto step or imported DAG code to read.
- The Astro token authenticates Otto; it is masked and never written to disk.

## Security notes

- Run on a schedule / `workflow_dispatch` against your own default branch — never
  on `pull_request_target` from forks.
- `verify-level: import` runs your repository's DAG code (to import it). It runs
  with secrets stripped from its environment; for repos with untrusted DAG
  authors, prefer `verify-level: syntax`.
- The resolve step is intentionally unauthenticated so it works in CI / `act`
  without secrets.

## FAQ

**The upgrade PR doesn't trigger my CI.** PRs opened with the default
`GITHUB_TOKEN` don't trigger further `workflow` runs (GitHub's recursion guard).
Pass a PAT or GitHub App installation token as `github-token` if you want the PR
to kick off your test workflows.

**Will it open a PR every day?** No. It maintains one rolling PR and edits it in
place; a run with nothing new is a clean no-op.

**Can it do the Airflow 2 → 3 migration?** No — that's advisory-only by design.
Use the guided upgrade (`astro otto`) for majors and review the breaking changes
interactively.

**My base image is digest-pinned.** The action reports it but won't bump it (a
digest pin ignores the tag, so bumping the tag wouldn't change the build). Remove
the digest pin to let the action manage the Runtime tag.

## Development

```bash
uv run --with pytest python -m pytest tests/ -q   # unit tests (no network — fixtures)
act -j dry-run                                    # e2e dry-run locally (needs Docker)
```

See [`e2e/`](./e2e) for the sample Astro project the e2e workflow upgrades, and
[`examples/`](./examples) for a copy-paste consumer workflow.
