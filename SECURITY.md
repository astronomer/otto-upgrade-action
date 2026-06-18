# Security

## Reporting a vulnerability

Please report security issues to the Astronomer security team rather than
opening a public issue. Do not include live tokens in a report.

## What this action can access

It runs on a schedule with `contents: write` + `pull-requests: write`, holds an
Astronomer API token, and — at `verify-level: import` — imports your repository's
DAG code. Understand these before adopting it.

### Tokens

- **Astro API token** — required for real runs (Otto performs the migration). Use
  an **Organization API token with the `ORGANIZATION_MEMBER` role** — the least
  privilege Otto needs. It's masked in logs and never written to disk.
- **GitHub token** — used only to push the branch and open/update the PR. It is
  **never** exposed to Otto or to the DAG-import subprocess.

### Hardening the action applies for you

- **The Astro token is stripped** from the DAG-import subprocess — `verify.sh`
  runs the import under `env -u ASTRO_TOKEN -u ASTRO_API_TOKEN -u GH_TOKEN -u
  GITHUB_TOKEN`, so imported DAG code (treated as untrusted) can't read the Astro
  token (the load-bearing strip; the GitHub-token entries are belt-and-suspenders,
  since the GitHub token is only ever in the PR-opening step's environment).
- **Push uses an explicit token-authenticated URL**, not a persisted credential —
  the action never needs the GitHub token in `.git/config` to push.
- **Third-party actions are SHA-pinned** (`setup-astro-cli`, `setup-uv`) with a
  `# vX.Y.Z` comment; Dependabot bumps them weekly with a 7-day cooldown.

### Your responsibilities

- **Set `persist-credentials: false` on your `actions/checkout`.** This is the
  one piece of hardening the action **cannot** enforce — it's your checkout, not
  ours. Without it, `actions/checkout` leaves the write-scoped GitHub token in
  `.git/config`, and the Otto step + the DAG-import verifier run repo code in that
  same workspace. The quickstart sets it; keep it. (The action still pushes via an
  explicit token URL either way, so this costs you nothing.)
- **Run it on a schedule / `workflow_dispatch` against your own default branch.**
  Never wire it to `pull_request_target` from forks — that's the textbook
  pwn-request pattern (fork code running with your write token + Astro token).
- **`verify-level: import` runs your DAG code** to import it (in a secret-stripped
  subprocess). If your repo accepts DAGs from untrusted contributors, use
  `verify-level: syntax` instead.
- **The migration agent (Otto) auto-updates to the latest build.** The action does
  not pin a specific Otto version; behavior can change between runs as Otto ships.
- **Review the PR before merging.** The action opens (or fails on) a PR; it never
  merges. Gate merges on your own CI / branch protection.

## This repo's own CI — what a fork PR can and cannot do

This matters once the repo is public. The threat model for a pull request opened
from a fork:

**A fork PR CANNOT:**

- **Read this repo's secrets.** GitHub does not pass repository secrets to fork
  `pull_request` runs, and our CI/e2e use `pull_request` (never
  `pull_request_target`). The `secrets.ASTRO_API_TOKEN` context resolves to empty
  for a fork PR even if the PR edits the workflow.
- **Reach the secret-using job.** The only job that touches `ASTRO_API_TOKEN` is
  gated to `workflow_dispatch` (a maintainer action), so no PR event triggers it.
- **Write to the repo.** Workflow `permissions:` are `contents: read`, and a fork
  PR's `GITHUB_TOKEN` is read-only regardless.
- **Re-route a pinned action.** Third-party actions are SHA-pinned.

**A fork PR CAN (and this is normal for OSS CI):**

- Run its own code in our runners — `pytest`, `shellcheck`, and the action under
  test via `uses: ./`. This executes in an ephemeral runner with **no secrets and
  a read-only token**, so the blast radius is "use CI compute / read public repo
  contents," nothing more.

**Recommended repo setting once public:** *Settings → Actions → require approval to
run workflows for first-time / outside contributors*, so even that compute use is
gated behind a maintainer click.

## Supply-chain notes

- The version-resolution step is intentionally unauthenticated (public Runtime
  feed + PyPI) so it needs no secrets.
- Provider/runtime version data influences which versions are bumped to, but all
  external values flow into commands as quoted array elements — they can cause a
  resolution to fail, not inject a shell command.
