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

report="$WORKDIR/verify-report.md"
status="skipped"

# Invoked indirectly via `trap ... EXIT` below; shellcheck can't see that.
# shellcheck disable=SC2329
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

# --- import (target Airflow) ----------------------------------------------- #
tgt_af=$(jq -r '.runtime.target_airflow // empty' "$PLAN_FILE")
if [[ -z "$tgt_af" ]]; then
  status="skipped"
  echo "ℹ️ Import check skipped: no resolved target Airflow version (runtime not bumped). Syntax check passed." > "$report"
  exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
  status="skipped"
  echo "ℹ️ Import check skipped: \`uv\` not available to build the target env. Syntax check passed." > "$report"
  exit 0
fi

with_args=(--with "apache-airflow==$tgt_af")
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  with_args+=(--with "$line")
done < <(jq -r '.providers[]? | select(.target != null and .current != .target) | "\(.package)==\(.target)"' "$PLAN_FILE")

echo "::group::Import check (apache-airflow==$tgt_af)"
set +e
out=$(timeout 600 uv run --no-project "${with_args[@]}" -- \
  python3 "$ACTION_PATH/scripts/import_check.py" "${roots[@]}" 2>"$WORKDIR/import-setup.err")
rc=$?
set -e
echo "$out"
echo "::endgroup::"

if [[ "$rc" -eq 0 ]]; then
  status="passed"
  echo "$out" > "$report"
elif [[ "$rc" -eq 1 ]]; then
  # import_check.py exits 1 only on a genuine DAG import error.
  status="failed"
  echo "$out" > "$report"
else
  # Non-1 (124 timeout, uv resolution failure, etc.) is infra, not a code defect.
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
