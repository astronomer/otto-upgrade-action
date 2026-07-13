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

worktree_top=""
fallback_note=""
# Invoked indirectly via `trap ... EXIT` below; shellcheck can't see that, so it
# flags the function as never-invoked (SC2329, v0.11+) and its body as
# unreachable (SC2317, v0.9). Both are false positives for a trap handler.
# shellcheck disable=SC2329,SC2317
emit() {
  # A parse->import fallback note must lead the report no matter which of the
  # import branches wrote it; prepending here is the single choke point.
  if [[ -n "$fallback_note" && -s "$report" ]]; then
    { echo "$fallback_note"; echo; cat "$report"; } > "$report.tmp" && mv "$report.tmp" "$report"
  fi
  echo "$status" > "$WORKDIR/verify-status.txt"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then echo "status=$status" >> "$GITHUB_OUTPUT"; fi
  if [[ -n "$worktree_top" && -d "$WORKDIR/baseline" ]]; then
    git -C "$worktree_top" worktree remove --force "$WORKDIR/baseline" 2>/dev/null || true
    rm -rf "$WORKDIR/baseline"
  fi
}
trap emit EXIT

# Create the pre-upgrade worktree (HEAD = before apply_bump/Otto, since verify
# runs before open-pr.sh commits). Sets baseline_dir + worktree_top on success.
baseline_dir=""
make_baseline_worktree() {
  local toplevel prefix
  toplevel=$(git -C "$PROJECT_PATH" rev-parse --show-toplevel 2>/dev/null) || return 1
  prefix=$(git -C "$PROJECT_PATH" rev-parse --show-prefix 2>/dev/null)
  # A stale registration from a hard-killed prior run (self-hosted WORKDIR
  # reuse) would fail the add and needlessly degrade to strict mode.
  git -C "$toplevel" worktree prune 2>/dev/null || true
  rm -rf "$WORKDIR/baseline"
  git -C "$toplevel" worktree add --detach "$WORKDIR/baseline" HEAD >/dev/null 2>&1 || return 1
  worktree_top="$toplevel"
  baseline_dir="$WORKDIR/baseline/${prefix%/}"
  baseline_dir="${baseline_dir%/}"
  return 0
}

# Collect the source roots that exist.
roots=()
for d in dags include plugins; do
  [[ -d "$PROJECT_PATH/$d" ]] && roots+=("$PROJECT_PATH/$d")
done
if [[ ${#roots[@]} -eq 0 ]]; then
  status="skipped"
  echo "ℹ️ No dags/include/plugins directories under \`$PROJECT_PATH\` — nothing to verify." > "$report"
  echo "::warning::Verification skipped: no dags/include/plugins directories under '$PROJECT_PATH'."
  exit 0
fi

if [[ "$VERIFY_LEVEL" == "none" ]]; then
  status="skipped"
  echo "ℹ️ Verification disabled (\`verify-level: none\`)." > "$report"
  # A deliberate opt-out, not an unexpected gap — notice, not warning.
  echo "::notice::Verification disabled (verify-level: none)."
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

# --- parse (default): build the real image, verify inside it ---------------- #
# `astro dev parse` builds the project's image (the actual target Runtime +
# requirements — dependency resolution happens against the image's constraints,
# which PyPI-level resolution can silently disagree with) and runs Astro's DAG
# integrity suite inside it. This is the production-faithful check: env vars,
# bundled providers, and the parse harness match a deployment, so a DAG that
# parses on Astro parses here.

# Strip secrets: the build and the in-image pytest execute repository code.
# NO_COLOR/PY_COLORS: a runner exporting FORCE_COLOR would make the in-image
# pytest emit ANSI codes that defeat parse_check's line-anchored patterns —
# the one path where scraping could mis-read a completed run.
run_parse() {
  ( cd "$1" &&
    env -u ASTRO_TOKEN -u ASTRO_API_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
    NO_COLOR=1 PY_COLORS=0 FORCE_COLOR=0 \
    timeout 1800 astro dev parse ) > "$2" 2>&1
}

# Known astro-cli template incompatibility: the generated integrity test
# calls DagBag(include_examples=False), an argument Airflow >= 3.3 removed —
# pytest then dies at collection and no DAG gets tested. Patch the throwaway
# copies only (never the user's tree); the CLI's own current template still
# carries the bad call, so regeneration can't fix it either.
shim_integrity_test() {
  local f="$1/.astro/test_dag_integrity_default.py"
  if [[ -f "$f" ]]; then
    # Tolerate whitespace and additional kwargs; a template reflow must not
    # silently no-op the shim.
    sed -i.bak -E 's/DagBag\(\s*include_examples\s*=\s*False\s*,?\s*/DagBag(/' "$f" && rm -f "$f.bak"
    if grep -qE 'include_examples\s*=\s*False' "$f"; then
      echo "::warning::integrity-test shim did not take effect (template shape changed?); a collection failure at Airflow >= 3.3 will fall back to import-level verification."
    fi
  fi
}

# The whole image-level flow. Every genuine verdict (pass, new failures, image
# build failure) sets status/report and exits the script. A harness-level miss
# — no Astro CLI or Docker, the integrity test incompatible with the target,
# a parse that never completed — sets fallback_note and returns 1, and the
# caller's single demotion site drops to the import level.
verify_parse_level() {
  local prereq=""
  if ! command -v astro >/dev/null 2>&1; then
    prereq="the Astro CLI is not available"
  elif ! docker info >/dev/null 2>&1; then
    prereq="Docker is not available"
  fi
  if [[ -n "$prereq" ]]; then
    fallback_note="ℹ️ Image-level verification (\`verify-level: parse\`) could not run: ${prereq}. Results below come from the import-level fallback — a PyPI approximation of the Runtime image."
    echo "::warning::verify-level parse unavailable (${prereq}); falling back to import-level verification."
    return 1
  fi

  tgt_tag=$(jq -r '.runtime.target_tag // empty' "$PLAN_FILE")

  # Parse runs on a COPY: the build and any astro-generated files must never
  # dirty the tree open-pr.sh commits.
  parse_target_dir="$WORKDIR/parse-target"
  rm -rf "$parse_target_dir"
  mkdir -p "$parse_target_dir"
  if ! rsync -a --exclude .git "$PROJECT_PATH/" "$parse_target_dir/"; then
    fallback_note="ℹ️ Image-level verification could not run (the project copy failed). Results below come from the import-level fallback."
    echo "::warning::verify-level parse: project copy failed; falling back to import-level verification."
    return 1
  fi
  shim_integrity_test "$parse_target_dir"

  echo "::group::Image parse (target Runtime ${tgt_tag:-per project Dockerfile})"
  set +e
  run_parse "$parse_target_dir" "$WORKDIR/parse-target.log"
  target_run_rc=$?
  IMPORT_JSON="$WORKDIR/import-failures.json" \
    python3 "$ACTION_PATH/scripts/parse_check.py" "$WORKDIR/parse-target.log"
  target_rc=$?
  set -e
  tail -n 40 "$WORKDIR/parse-target.log"
  echo "::endgroup::"

  # Both "harness produced no verdict" classes degrade to the import-level
  # check rather than reporting nothing: rc 5 = the project's integrity test is
  # incompatible with the target Airflow; rc 2 = the run never completed
  # (timeout, unrecognized output). Only genuine verdicts (0/3/4) stay here.
  if [[ "$target_rc" -eq 5 ]]; then
    cause=$(jq -r '.collection_error // empty' "$WORKDIR/import-failures.json" 2>/dev/null)
    fallback_note="ℹ️ Image-level verification could not run: the project's \`.astro/test_dag_integrity_default.py\` fails to start at the target Airflow${cause:+ (\`${cause}\`)}. Regenerate it with an updated Astro CLI (\`astro dev init\`). Results below come from the import-level fallback."
    echo "::warning::verify-level parse: the project's DAG integrity test is incompatible with the target Airflow; falling back to import-level verification."
    return 1
  elif [[ "$target_rc" -eq 2 ]]; then
    local reason="produced no recognizable result"
    [[ "$target_run_rc" -eq 124 ]] && reason="timed out before completing"
    fallback_note="ℹ️ Image-level verification could not run (\`astro dev parse\` ${reason}). Results below come from the import-level fallback."
    echo "::warning::verify-level parse: astro dev parse ${reason}; falling back to import-level verification."
    return 1
  fi

  if [[ "$target_rc" -eq 0 ]]; then
    status="passed"
    checked=$(jq -r '.checked' "$WORKDIR/import-failures.json")
    echo "✅ All ${checked} DAG file(s) import cleanly inside the target Runtime image (\`${tgt_tag:-Dockerfile}\`)." > "$report"
    exit 0
  fi

  # Baseline: the same parse at the pre-upgrade state (current image + pins).
  set +e
  make_baseline_worktree
  baseline_made=$?
  baseline_rc=""
  if [[ "$baseline_made" -eq 0 ]]; then
    shim_integrity_test "$baseline_dir"
    echo "::group::Image parse (baseline: current Runtime)"
    run_parse "$baseline_dir" "$WORKDIR/parse-baseline.log"
    IMPORT_JSON="$WORKDIR/baseline-failures.json" \
      python3 "$ACTION_PATH/scripts/parse_check.py" "$WORKDIR/parse-baseline.log"
    baseline_rc=$?
    tail -n 20 "$WORKDIR/parse-baseline.log"
    echo "::endgroup::"
  fi
  set -e

  if [[ "$target_rc" -eq 4 ]]; then
    # The target image itself does not build. If the current image builds,
    # the upgrade caused it — a real, mergeable-PR-blocking failure the
    # PyPI-level check cannot see (image installs resolve against the
    # Runtime's constraints and bundled packages).
    build_excerpt=$(grep -aE "×|╰─▶|─▶|because|unsatisfiable|incompatible|No solution|ERROR: failed to build" \
      "$WORKDIR/parse-target.log" \
      | sed -E 's/^#[0-9]+ //; s/^[0-9]+\.[0-9]+ +//' | awk '!seen[$0]++' | tail -n 14)
    if [[ "$baseline_rc" == "4" ]]; then
      status="skipped"
      {
        echo "ℹ️ Neither the current nor the target Runtime image builds in this environment, so the build failure pre-dates this upgrade (or is an infrastructure issue). Imports were NOT checked."
        echo
        echo '```'
        printf '%s\n' "$build_excerpt"
        echo '```'
      } > "$report"
      echo "::warning::Verification skipped: neither the current nor the target image builds."
    elif [[ "$baseline_rc" == "0" || "$baseline_rc" == "3" || "$baseline_rc" == "5" ]]; then
      # 0/3/5 all mean the current image BUILT (5 = built, then the integrity
      # test died at collection), so the build verdict exists and the blame is
      # honest.
      status="failed"
      {
        echo "❌ **The project does not build at the target Runtime image** (\`${tgt_tag:-Dockerfile}\`). Dependency resolution failed while installing your requirements into the image. Your current image builds cleanly, so this is caused by the upgrade — usually a project pin that conflicts with packages bundled in the newer Runtime:"
        echo
        echo '```'
        printf '%s\n' "$build_excerpt"
        echo '```'
      } > "$report"
    else
      # No baseline build verdict (worktree failed, baseline parse timed out
      # or was unrecognized). Fail closed, but never claim the current image
      # builds — we don't know.
      status="failed"
      {
        echo "❌ **The project does not build at the target Runtime image** (\`${tgt_tag:-Dockerfile}\`). Dependency resolution failed while installing your requirements into the image. The current image could not be verified in this environment, so this failure may or may not pre-date the upgrade:"
        echo
        echo '```'
        printf '%s\n' "$build_excerpt"
        echo '```'
      } > "$report"
    fi
    exit 0
  fi

  # target_rc == 3: real import failures inside the target image.
  if [[ "$baseline_rc" == "0" || "$baseline_rc" == "3" ]]; then
    set +e
    cmp_out=$(python3 "$ACTION_PATH/scripts/compare_failures.py" \
      "$WORKDIR/import-failures.json" "$WORKDIR/baseline-failures.json")
    cmp_rc=$?
    set -e
    printf '%s\n' "$cmp_out" > "$report"
    # Fail CLOSED: the target run found real failures, so a comparison-tool
    # crash must never read as a pass.
    case "$cmp_rc" in
      0) status="passed" ;;
      *) status="failed" ;;
    esac
  else
    status="failed"
    {
      echo "❌ $(jq -r '.failures | length' "$WORKDIR/import-failures.json") of $(jq -r '.checked' "$WORKDIR/import-failures.json") DAG file(s) failed to import inside the target Runtime image:"
      echo
      jq -r '.failures[] | "  - `\(.path)`: `\(.msg | gsub("`"; "'"'"'"))`"' "$WORKDIR/import-failures.json"
      echo
      echo "_Baseline comparison unavailable (the pre-upgrade image could not be parsed); all failures are shown — some may pre-date this upgrade._"
    } > "$report"
  fi
  exit 0
}

if [[ "$VERIFY_LEVEL" == "parse" ]] && ! verify_parse_level; then
  # The single demotion site: every parse-level fallback path lands here with
  # fallback_note already explaining why (emit() leads the report with it).
  VERIFY_LEVEL="import"
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
  echo "::warning::Verification skipped: no resolvable target Airflow version — imports were NOT checked."
  exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
  status="skipped"
  echo "ℹ️ Import check skipped: \`uv\` not available to build the target env. Syntax check passed." > "$report"
  echo "::warning::Verification skipped: uv is not available — imports were NOT checked."
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
  echo "::warning::Verification skipped: nothing to build a target env from — imports were NOT checked."
  exit 0
fi

# Import only what Airflow itself imports: DAG files under dags/ (safe-mode
# heuristic + .airflowignore inside import_check.py) and plugin modules under
# plugins/. include/ is deliberately absent — Airflow never imports it directly,
# and importing it standalone produced false failures on helper scripts.
import_args=(--project-root "$PROJECT_PATH")
[[ -d "$PROJECT_PATH/dags" ]] && import_args+=(--dags-root "$PROJECT_PATH/dags")
[[ -d "$PROJECT_PATH/plugins" ]] && import_args+=(--plugins-root "$PROJECT_PATH/plugins")
if [[ ${#import_args[@]} -le 2 ]]; then
  status="skipped"
  echo "ℹ️ Import check skipped: no dags/ or plugins/ under \`$PROJECT_PATH\` to import. Syntax check passed." > "$report"
  echo "::warning::Verification skipped: no dags/ or plugins/ to import."
  exit 0
fi
# AFTER the no-roots guard: appending this first would pad the array past the
# length check and send a rootless AF2 project into a pointless env build that
# reports "passed" over zero files.
if [[ "${af_pin%%.*}" == "2" ]]; then
  import_args+=(--ignore-syntax regexp)  # .airflowignore default syntax on AF2
fi

echo "::group::Import check (apache-airflow==${af_pin:-from-requirements})"
# import_check writes a CLEAN summary here; Airflow's chatty import-time logging
# (alembic plugin setup, etc.) goes to stdout/stderr and stays in the CI log
# only — it must not leak into the PR body.
import_report="$WORKDIR/import-report.md"

# Strip secrets from the subprocess: it imports repository DAG code, which we
# treat as untrusted. It must not be able to read the Astro token or any GitHub
# token from its environment.
run_import() {
  IMPORT_REPORT="$import_report" IMPORT_JSON="$WORKDIR/import-failures.json" \
    env -u ASTRO_TOKEN -u ASTRO_API_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
    timeout 600 uv run --no-project "$@" "${with_args[@]}" -- \
    python3 "$ACTION_PATH/scripts/import_check.py" "${import_args[@]}"
}

# Attempt 1 — wheels only (--no-build). Avoids compiling source distributions
# that need system build deps the runner lacks (e.g. apache-hdfs -> hdfs[kerberos]
# -> gssapi -> krb5-config), which would otherwise fail env setup and force a
# needless 'skipped'. rc 0 = imported clean, rc 3 = genuine DAG import error —
# both are real verdicts. Anything else is an env-build failure (e.g. a dep with
# no wheel under --no-build); fall through and retry allowing source builds.
rm -f "$import_report"
set +e
out=$(run_import --no-build 2>"$WORKDIR/import-setup.err")
rc=$?
if [[ "$rc" -ne 0 && "$rc" -ne 3 ]]; then
  echo "wheels-only env build failed (exit $rc); retrying with source builds allowed."
  rm -f "$import_report"
  out=$(run_import 2>"$WORKDIR/import-setup.err")
  rc=$?
fi
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
  #
  # Before failing the run, re-run the SAME check at the current (pre-upgrade)
  # state — a git worktree at HEAD with the pre-bump requirements and the
  # current Airflow. Failures present on both sides are pre-existing project
  # issues (field-verified: env-dependent dbt DAGs fail identically on both),
  # not upgrade breakage; only NEW failures fail verification.
  baseline_note=""
  current_af=$(jq -r '.runtime.current_airflow // empty' "$PLAN_FILE")

  # Called right below inside `set +e`; the EXIT trap makes shellcheck think
  # everything after it is unreachable.
  # shellcheck disable=SC2317
  run_baseline() {
    if [[ -z "$current_af" ]]; then
      baseline_note="the current Airflow version is unknown"; return 1
    fi
    local bproj brc
    make_baseline_worktree \
      || { baseline_note="a baseline worktree could not be created"; return 1; }
    bproj="$baseline_dir"
    local bargs=(--project-root "$bproj")
    [[ -d "$bproj/dags" ]] && bargs+=(--dags-root "$bproj/dags")
    [[ -d "$bproj/plugins" ]] && bargs+=(--plugins-root "$bproj/plugins")
    [[ "${current_af%%.*}" == "2" ]] && bargs+=(--ignore-syntax regexp)
    local bwith=()
    [[ -f "$bproj/requirements.txt" ]] && bwith+=(--with-requirements "$bproj/requirements.txt")
    bwith+=(--with "apache-airflow==$current_af")
    run_baseline_import() {
      IMPORT_JSON="$WORKDIR/baseline-failures.json" IMPORT_REPORT="" \
        env -u ASTRO_TOKEN -u ASTRO_API_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
        timeout 600 uv run --no-project "$@" "${bwith[@]}" -- \
        python3 "$ACTION_PATH/scripts/import_check.py" "${bargs[@]}" \
        >/dev/null 2>>"$WORKDIR/baseline-setup.err"
    }
    rm -f "$WORKDIR/baseline-failures.json"
    run_baseline_import --no-build
    brc=$?
    if [[ "$brc" -ne 0 && "$brc" -ne 3 ]]; then
      rm -f "$WORKDIR/baseline-failures.json"
      run_baseline_import
      brc=$?
    fi
    if [[ ("$brc" -ne 0 && "$brc" -ne 3) || ! -s "$WORKDIR/baseline-failures.json" ]]; then
      baseline_note="the current-version env could not be built (exit $brc)"; return 1
    fi
    return 0
  }

  set +e
  run_baseline
  baseline_ok=$?
  if [[ "$baseline_ok" -eq 0 ]]; then
    cmp_out=$(python3 "$ACTION_PATH/scripts/compare_failures.py" \
      "$WORKDIR/import-failures.json" "$WORKDIR/baseline-failures.json")
    cmp_rc=$?
    printf '%s\n' "$cmp_out" > "$report"
    # Fail CLOSED: we are only here because the target run found real import
    # failures, so a comparison-tool crash must never read as a pass.
    case "$cmp_rc" in
      0) status="passed" ;;
      3) status="failed" ;;
      *)
        status="failed"
        {
          clean_report
          echo
          echo "_Baseline comparison failed (exit $cmp_rc); all target failures are shown — some may pre-date this upgrade._"
        } > "$report"
        ;;
    esac
  else
    # No baseline to compare against — keep the strict behavior and say why.
    status="failed"
    {
      clean_report
      echo
      echo "_Baseline comparison unavailable (${baseline_note}); all failures are shown — some may pre-date this upgrade._"
    } > "$report"
  fi
  set -e
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
  echo "::warning::Verification skipped: the target env could not be built (exit $rc) — imports were NOT checked."
fi
exit 0
