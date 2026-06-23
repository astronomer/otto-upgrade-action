# Otto Airflow Upgrade

> **Status: internal preview.** Otto performs the migration via its
> `airflow-upgrade` skill (this KB), so an Astronomer API token is required for
> any real run. There's no Otto-less mode; a token-free `dry-run` previews the
> version plan only. It pairs with
> [`otto-review-action`](https://github.com/astronomer/otto-review-action).

This action upgrades an Astro/Airflow project on a schedule. It finds the safe
target version (within a scope you set), bumps the Runtime tag and pinned
providers, has Otto migrate the DAG code for any breaking changes, verifies the
DAGs still import at the target, and opens one pull request.

The version bump is the easy part; the reason to use it is the code migration.
Otto does that through its `airflow-upgrade` skill. The action is the harness
that runs it deterministically on a schedule: resolve the target, run Otto,
verify, keep a single PR up to date.

`otto-review-action` reviews a PR a human wrote. This action writes the upgrade
PR for you.

| | Trigger | Otto's job | Output |
| --- | --- | --- | --- |
| `otto-review-action` | a PR opens | review the human's diff | inline comments + verdict |
| **`otto-upgrade-action`** | a schedule fires | author the upgrade diff | a version-bump + migration PR |

## Why not just point dependabot at `requirements.txt`?

Dependabot bumps version strings. It doesn't know four things this action does:

1. **Runtime ↔ Airflow mapping.** Your base image is `FROM .../runtime:3.1-12`,
   not `apache-airflow==3.1.7`. This action bumps the Runtime tag.
2. **Compatibility clamping.** It won't propose a provider major your pinned
   Airflow can't take; `max-upgrade-scope` caps how far a jump can go.
3. **Breaking-change code migration.** The Otto step rewrites moved imports and
   renamed call sites, not just the pin.
4. **Verification at the target.** The PR carries an "all N DAGs still import at
   the target Airflow" check, so the bump is trustworthy rather than plausible.

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
      - uses: actions/checkout@v7        # pin to a commit SHA for supply-chain safety
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

**Pinning.** `@v0` (above) tracks the latest `v0.x`. Pin `@v0.1.0` for an exact
version, or `@<commit-sha>` for the strongest supply-chain guarantee.

## What a PR looks like

The version bump is the cheap part:

```diff
# Dockerfile
-FROM astrocrpublic.azurecr.io/runtime:3.1-12
+FROM astrocrpublic.azurecr.io/runtime:3.2-5
# requirements.txt
-apache-airflow-providers-amazon==9.0.0
+apache-airflow-providers-amazon==9.30.0
```

The value is the **code migration Otto applies to your DAGs** so they actually run
at the new version — moved imports, renamed parameters, removed operators. From a
real run in this repo's tests:

```diff
# dags/sales_etl.py
-from airflow.operators.python import PythonOperator
-from airflow.operators.bash import BashOperator
-from airflow.operators.dummy import DummyOperator
-from airflow.utils.dates import days_ago
+from airflow.providers.standard.operators.python import PythonOperator
+from airflow.providers.standard.operators.bash import BashOperator
+from airflow.providers.standard.operators.empty import EmptyOperator

-    schedule_interval="@daily",
-    start_date=days_ago(1),
+    schedule="@daily",
+    start_date=datetime(2024, 1, 1),

-    start = DummyOperator(task_id="start")
+    start = EmptyOperator(task_id="start")

# dags/ml_features.py
-from airflow import DAG, Dataset
+from airflow.sdk import DAG, Asset
-FEATURES = Dataset("s3://lake/features")
+FEATURES = Asset("s3://lake/features")
```

The PR body then summarizes it: a version table, the breaking changes Otto
handled, a **manual follow-ups** checklist for anything it wouldn't touch blindly,
and the verification result (all DAGs import at the target).

## What Otto migrates (it's not just imports)

Moved imports are the visible part. The migration also covers things a
flag-only linter (e.g. ruff's `AIR` rules, which see Airflow core + the standard
provider) doesn't:

- **Non-standard providers.** Amazon, Google, Snowflake, Databricks, Kubernetes,
  and more — down to renamed parameters and changed hook/operator behavior, not
  just core. The KB carries provider-level migration detail across dozens of providers.
- **Semantic rewrites, not just flags.** Replacing a removed operator *and* its
  call site; rewriting a custom operator's base class when the base moved (e.g.
  `BaseOperator` → `airflow.sdk`); moving direct-ORM `session.query(...)` access to
  the Task SDK API. A linter can flag these; it can't rewrite them.
- **Renamed parameters and moved context keys.** `schedule_interval` → `schedule`,
  `execution_date` → `logical_date`, and connection/operator kwargs.
- **Any version to any version.** Not only 2.x → 3.x, but 3.1 → 3.2 behavioral and
  patch changes that have no linter rule at all.
- **Dependency resolution.** It confirms the bumped providers and Airflow actually
  resolve together (diamond conflicts like protobuf / SQLAlchemy / common-sql
  floors) before the PR lands, not after.

## Scenarios

What the action does for common starting states (assuming the quickstart config:
`target: latest-minor`, `max-upgrade-scope: minor`).

<details>
<summary><b>Eight worked scenarios</b> — patch · minor · provider-only · major (advisory) · clamped · nothing-to-do · dry-run preview · verification failure</summary>

### 1. Patch — a newer Runtime build on the same Airflow

Your `Dockerfile` says `FROM .../runtime:3.1-16` and Astronomer ships `3.1-17`
(same Airflow 3.1.x — a base-image CVE or provider-bundle fix).

This is a **patch**-tier bump. The action bumps the tag, **runs the Otto migration**,
verifies, and opens a PR. Otto usually finds nothing to change here — but it
still runs, because a build/patch release can quietly carry a breaking change
(a bundled provider renaming a class), and "patch" is exactly where teams stop
looking. Otto + import-verify are the backstop. This is the boring, high-value
cadence teams skip — safe to merge once green.

### 2. Minor — `3.1` → `3.2` with breaking changes

`FROM .../runtime:3.1-12` and the newest Runtime on your Airflow major is `3.2-5`
(Airflow 3.2.x).

This is a **minor**-tier bump. The action bumps the tag, runs the Otto migration (rewrites
moved imports, renames changed params), verifies every DAG imports at 3.2, and
opens a PR whose body has the version table, a "breaking changes handled" list,
a "manual follow-ups" checklist, and the verification result. Always
human-reviewed.

### 3. Provider-only — Runtime current, providers behind

Runtime is already newest, but `apache-airflow-providers-amazon==9.0.0` is pinned
and `9.30.0` is out.

The action bumps the provider pin, runs the Otto migration framed around the
provider transition, and verifies DAGs import against your **current** Airflow
plus the new provider. PR title: "Upgrade 1 Airflow provider(s)".

### 4. Major — Airflow 2 → 3 available

You're on a 2.x Runtime and Airflow 3 is available.

This is a **major**-tier jump. The action **does not author a PR**. It emits a job-summary
advisory pointing you at the guided upgrade (`astro otto`, the Airflow upgrade
workflow), because a 2→3 migration is not something a scheduled bot should ship
unattended. (Provider majors *are* authored — only Airflow majors are advisory.)

### 5. Clamped — a bigger jump held back by `max-upgrade-scope`

You set `target: latest` but `max-upgrade-scope: minor`.

The action clamps to the newest in-scope target, opens that PR, and notes what it
held back — tailored to *what* was held:

- A **provider** major (or a minor held by a `patch` cap): "A larger upgrade was
  available but held back by `max-upgrade-scope`. Raise the input to go further."
  — raising the cap authors it.
- The **Airflow** major (2→3): raising the cap won't help (a scheduled run never
  auto-authors an Airflow major), so the note points to the guided upgrade
  instead — see scenario 4.

### 6. Nothing to do

Everything is already at the newest in-scope version.

No-op. `no-update` output is `true`, no branch, no PR, no noise.

### 7. Tokenless preview (`dry-run`)

`dry-run: true` with no `ASTRO_API_TOKEN`.

Previews the plan: detect → resolve → apply the bump to the working tree →
render the would-be PR body to the job summary. Otto is skipped (no token) and
no PR is opened. This is the only Otto-less path — it's a preview, not a real
upgrade. A real run **requires** a token (the migration is Otto's job).

### 8. Verification fails at the target

A DAG won't import at the bumped version (a provider dropped a class you use).

The PR **still opens** (so you see the proposed upgrade and the failure
together), its body leads with a ⚠️ "verification failed" banner, and the
scheduled run goes **red** so you notice. Merge-gating stays with your repo's CI.

</details>

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

At `import`, the action provisions `uv` itself (you don't need a separate setup
step). Verification only ever reports **failed** on a genuine code error — a real
DAG import error. If the target env can't be provisioned (no network, resolver
cutoff, timeout) it reports **skipped**, so infra flakiness never masquerades as a
broken upgrade. The DAG-import subprocess runs with the Astro and GitHub tokens
stripped from its environment.

## The rolling PR

The action maintains **one** branch (`otto/airflow-upgrade` by default). Re-runs
force-push to it (with a fetched lease) and edit the same PR in place rather than
stacking a new PR every run. When a newer target ships next week, this PR updates
to it. Use the `concurrency` group (see quickstart) so overlapping scheduled runs
don't race the branch.

## Inputs

| Name | Default | Description |
| --- | --- | --- |
| `astro-api-token` | env `ASTRO_API_TOKEN` | **Required for real runs** — Otto performs the migration. A real run without it fails fast; only a `dry-run` preview may omit it. |
| `astro-organization` | env `ASTRO_ORGANIZATION` | Org ID for gateway routing. |
| `astro-domain` | `astronomer.io` | Override for non-prod. |
| `github-token` | `${{ github.token }}` | Pushes the branch and opens the PR. Needs `contents:write` + `pull-requests:write`. Use a PAT/App token if you want the PR to trigger your other CI (see FAQ). |
| `project-path` | `.` | Astro project root (holds the Dockerfile + requirements.txt). |
| `target` | `latest-minor` | `patch`, `latest-minor`, or `latest`. |
| `max-upgrade-scope` | `minor` | `patch`, `minor`, or `major`. Airflow majors stay advisory-only regardless. |
| `include-providers` | `true` | Also bump pinned providers. Unpinned are reported, never changed. |
| `verify-level` | `import` | `syntax`, `import`, or `none`. |
| `base-branch` | _(repo default branch)_ | PR base. |
| `branch` | `otto/airflow-upgrade` | Rolling head branch. |
| `labels` | `airflow-upgrade,dependencies` | Comma-separated PR labels (best-effort). |
| `model` | _(Otto default)_ | `--model` passed to Otto. |
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
4. **Migrate** (minor/major jumps, Otto) — drives Otto's hosted **airflow-upgrade skill** (this KB) to rewrite moved imports / renamed call sites over the bumped project. The skill is engaged *deterministically*: the action both scopes it (`--allowed-skills airflow-upgrade`) and names it in the prompt with `currentVersion`/`targetVersion`. This matters — a bare "upgrade this project" prompt makes Otto route to generic documentation search and skip the curated skill entirely. If Otto errors, the run **fails** (no PR) and retries next schedule — it never ships an unmigrated bump as a pretend upgrade.
5. **Verify** — syntax + (default) import-at-target check.
6. **Open/update PR** — single rolling branch, body with the version table, migration summary, and verification result.

## Security & permissions

- `permissions: { contents: write, pull-requests: write }` on the workflow.
- **Allow Actions to create PRs.** New repos default this OFF. Enable
  *Settings → Actions → General → "Allow GitHub Actions to create and approve
  pull requests"* (org-level for org repos), or pass a PAT / GitHub App token as
  `github-token`. Without it the branch still pushes but PR creation is blocked;
  the action fails with an actionable message and opens the PR on the next run
  once the setting is fixed (the pushed branch is reused).
- **Set `persist-credentials: false` on `actions/checkout`** — the action can't
  enforce your checkout config. Without it the write token sits in `.git/config`
  while the Otto step and DAG-import verifier run repo code. The action pushes via
  an explicit token URL regardless, so this costs nothing.
- The Astro token authenticates Otto; it's masked and stripped from the
  DAG-import subprocess.
- `verify-level: import` runs your repository's DAG code (to import it), with
  secrets stripped from that subprocess; for repos with untrusted DAG authors,
  prefer `verify-level: syntax`.
- Run on a schedule / `workflow_dispatch` against your own default branch, never
  on `pull_request_target` from forks. The resolve step is intentionally
  unauthenticated so it works in CI / `act` without secrets.

See [SECURITY.md](./SECURITY.md) for the full threat model, including what a fork
PR against this repo can and cannot do.

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

**My base image is digest-pinned.** The action reports it and won't bump the
Runtime tag (a digest pin ignores the tag, so bumping it wouldn't change the
build). It still resolves your current Airflow version from the tag and bumps
*providers*. Remove the digest pin to let the action manage the Runtime tag.

## Known limitations

- **Cost.** A real bump triggers an Otto (LLM) run. Most scheduled runs are no-ops
  (nothing new shipped) and cost nothing beyond detect/resolve; budget for an Otto
  invocation whenever there's an actual upgrade to author.
- **Otto floats to latest.** The migration agent auto-updates; the action can't
  pin a specific Otto build today. Behavior can change between runs as Otto ships.
- **Depends on the hosted `airflow-upgrade` skill.** The migration intelligence is
  the skill (this KB), served through your Otto/Core. It must be available for your
  org/domain. The action engages it deterministically (`--allowed-skills` + a named
  invocation) — but typing a free-form *"upgrade my project"* into Otto **directly**
  may route to generic doc-search instead; that's an Otto-side routing behavior, not
  something this action controls.
- **Airflow majors (2 → 3) are advisory-only.** Use the guided upgrade for those.
- **`import` verify approximates your image, it isn't your image.** It's a fresh
  `uv` resolve of `apache-airflow==<target>` + your requirements — not the actual
  Astro Runtime image (system libs, the Astronomer-curated provider set). Treat a
  pass as strong evidence, not a guarantee for your exact deploy.
- **Digest-pinned runtimes assume a stable tag↔Airflow mapping.** The current
  Airflow version is read from the pinned tag (e.g. `3.1-12` → 3.1.x); for Astro
  Runtime a tag maps to a fixed build, so this holds.
- **Interactive vs CI.** This action is the deterministic, scheduled path; it is not
  a substitute for the interactive `astro otto` upgrade for large, hands-on migrations.

## Development

```bash
uv run --with pytest python -m pytest tests/ -q   # unit tests (no network — fixtures)
act -j dry-run                                    # e2e dry-run locally (needs Docker)
```

See [`e2e/`](./e2e) for the sample Astro project the e2e workflow upgrades, and
[`examples/`](./examples) for a copy-paste consumer workflow.
