#!/usr/bin/env bash
# Verify the upgraded project. Two levels:
#
#   syntax  - byte-compile every DAG/include/plugin .py (catches syntax breakage;
#             cheap; no network). Always available.
#   import  - additionally import every DAG file inside an ephemeral env pinned to
#             the TARGET Airflow + provider versions (catches moved/removed imports
#             and renamed call sites — the failure mode upgrades actually cause).
#
# Verification can only ever report `failed` on a *genuine* code problem. If the
# target env can't be provisioned (no network, resolution error, no uv), we
# report `skipped` with the reason — infra flakiness must never look like a
# broken upgrade and block a PR that is actually fine.
#
# Required env:
#   PROJECT_PATH  project root
#   PLAN_FILE     resolve_target.py output (for the target Airflow version)
#   VERIFY_LEVEL  syntax | import | none (default import)
#   WORKDIR       scratch dir (default /tmp/otto-upgrade)
#   ACTION_PATH   path to this action's checkout
#
# Writes $WORKDIR/{verify-report.md,verify-status.txt} and a `status` step output.

set -euo pipefail

: "${PROJECT_PATH:?}"
: "${PLAN_FILE:?}"
: "${ACTION_PATH:?}"
VERIFY_LEVEL="${VERIFY_LEVEL:-import}"
WORKDIR="${WORKDIR:-/tmp/otto-upgrade}"
mkdir -p "$WORKDIR"

# Keep byte-compilation artifacts out of the project tree so open-pr.sh's
# `git add -A` can't sweep __pycache__/*.pyc into the upgrade PR.
export PYTHONPYCACHEPREFIX="$WORKDIR/pycache"

report="$WORKDIR/verify-report.md"
status="skipped"

# Invoked indirectly via `trap ... EXIT` below; shellcheck can't see that, so it
# flags the function as never-invoked (SC2329, v0.11+) and its body as
# unreachable (SC2317, v0.9). Both are false positives for a trap handler.
# shellcheck disable=SC2329,SC2317
emit() {
  echo "$status" > "$WORKDIR/verify-status.txt"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then echo "status=$status" >> "$GITHUB_OUTPUT"; fi
}
trap emit EXIT

# Collect the source roots that exist.
roots=()
for d in dags include plugins; do
  [[ -d "$PROJECT_PATH/$d" ]] && roots+=("$PROJECT_PATH/$d")
done
if [[ ${#roots[@]} -eq 0 ]]; then
  status="skipped"
  echo "No dags/include/plugins directories under \`$PROJECT_PATH\` — nothing to verify." > "$report"
  exit 0
fi

if [[ "$VERIFY_LEVEL" == "none" ]]; then
  status="skipped"
  echo "Verification disabled (\`verify-level: none\`)." > "$report"
  exit 0
fi

# --- syntax (always) ------------------------------------------------------- #
syntax_fail=0
syntax_errs=""
while IFS= read -r -d '' f; do
  if ! err=$(python3 -m py_compile "$f" 2>&1); then
    syntax_fail=$((syntax_fail + 1))
    syntax_errs+="  - \`$f\`: ${err##*: }"$'\n'
  fi
done < <(find "${roots[@]}" -name '*.py' ! -name '.*' -print0)

if [[ "$syntax_fail" -gt 0 ]]; then
  status="failed"
  { echo "❌ $syntax_fail file(s) failed to byte-compile:"; echo; echo "$syntax_errs"; } > "$report"
  exit 0
fi

if [[ "$VERIFY_LEVEL" == "syntax" ]]; then
  status="passed"
  echo "✅ All DAG files byte-compile (syntax level)." > "$report"
  exit 0
fi

# --- import (target Airflow + the project's full deps) --------------------- #
# Pin Airflow to the target if the runtime moved, else to the current Airflow
# (provider-only bumps still need an Airflow to import against).
af_pin=$(jq -r '.runtime.target_airflow // .runtime.current_airflow // empty' "$PLAN_FILE")
if [[ -z "$af_pin" ]]; then
  # No resolved Airflow version — e.g. the runtime is digest-pinned, so we don't
  # know which Airflow the image ships. Importing against whatever Airflow `uv`
  # resolves transitively would be meaningless (and could report a misleading
  # failure). Report skipped; syntax already passed.
  status="skipped"
  echo "ℹ️ Import check skipped: the runtime wasn't bumped and its Airflow version is unknown (e.g. digest-pinned), so there's no target to import against. Syntax check passed." > "$report"
  exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
  status="skipped"
  echo "ℹ️ Import check skipped: \`uv\` not available to build the target env. Syntax check passed." > "$report"
  exit 0
fi

# Build the env from the project's *full* requirements (already rewritten to the
# bumped pins by apply_bump.py) so unchanged providers and other deps a DAG
# imports are present — otherwise we'd get false ModuleNotFoundErrors. Then pin
# Airflow on top.
with_args=()
if [[ -f "$PROJECT_PATH/requirements.txt" ]]; then
  with_args+=(--with-requirements "$PROJECT_PATH/requirements.txt")
fi
if [[ -n "$af_pin" ]]; then
  with_args+=(--with "apache-airflow==$af_pin")
fi
if [[ ${#with_args[@]} -eq 0 ]]; then
  status="skipped"
  echo "ℹ️ Import check skipped: no requirements.txt and no resolvable Airflow pin. Syntax check passed." > "$report"
  exit 0
fi

echo "::group::Import check (apache-airflow==${af_pin:-from-requirements})"
# import_check writes a CLEAN summary here; Airflow's chatty import-time logging
# (alembic plugin setup, etc.) goes to stdout/stderr and stays in the CI log
# only — it must not leak into the PR body.
import_report="$WORKDIR/import-report.md"
rm -f "$import_report"
set +e
# Strip secrets from the subprocess: it imports repository DAG code, which we
# treat as untrusted. It must not be able to read the Astro token or any GitHub
# token from its environment.
out=$(IMPORT_REPORT="$import_report" \
  env -u ASTRO_TOKEN -u ASTRO_API_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
  timeout 600 uv run --no-project "${with_args[@]}" -- \
  python3 "$ACTION_PATH/scripts/import_check.py" "${roots[@]}" 2>"$WORKDIR/import-setup.err")
rc=$?
set -e
echo "$out"
echo "::endgroup::"

# Prefer the clean summary file; fall back to captured stdout if it's missing.
clean_report() { if [[ -s "$import_report" ]]; then cat "$import_report"; else printf '%s\n' "$out"; fi; }

if [[ "$rc" -eq 0 ]]; then
  status="passed"
  clean_report > "$report"
elif [[ "$rc" -eq 3 ]]; then
  # import_check.py exits 3 ONLY on a genuine DAG import error. uv's own
  # env-build failure exits 1/2 — which must NOT be read as a code defect, so
  # we key the hard fail on 3 specifically (otherwise a provider that's too new
  # for the resolver's cutoff, a registry blip, etc. would red the run).
  status="failed"
  clean_report > "$report"
else
  # Anything else (1/2 uv resolution, 124 timeout, …) is infra, not a code defect.
  status="skipped"
  {
    echo "ℹ️ Import check could not run (env setup failed or timed out, exit $rc); reporting syntax-only. This does **not** mean the upgrade is broken."
    echo
    echo '```'
    tail -n 20 "$WORKDIR/import-setup.err" 2>/dev/null || true
    echo '```'
  } > "$report"
fi
exit 0
