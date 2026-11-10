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
# Optional env:
#   BUILD_SECRETS docker build --secret spec(s) for the parse-level image
#                 builds, one per line (e.g. id=netrc,env=NETRC_CONTENT)
#
# Writes $WORKDIR/{verify-report.md,verify-status.txt,verify-effective-level.txt}
# and `status` + `effective-level` step outputs. The effective level is the
# level the verdict actually came from (parse, import, syntax, or none), which
# is lower than VERIFY_LEVEL whenever a level couldn't run and the check fell
# back; the PR leads with a warning when that happens.

set -euo pipefail

: "${PROJECT_PATH:?}"
: "${PLAN_FILE:?}"
: "${ACTION_PATH:?}"
VERIFY_LEVEL="${VERIFY_LEVEL:-import}"
WORKDIR="${WORKDIR:-/tmp/otto-upgrade}"
mkdir -p "$WORKDIR"

# Keep byte-compilation artifacts out of the project tree.
export PYTHONPYCACHEPREFIX="$WORKDIR/pycache"

# `env -u` args stripping the env vars named by BUILD_SECRETS specs from the
# import-level checks: those execute repository DAG code directly (no image
# in between), and the build-secrets contract is Dockerfile-build exposure
# only. The parse-level build (run_parse) keeps them — it's the consumer.
# Seeded with BUILD_SECRETS itself so the array is never empty (bash 3.2's
# set -u treats expanding an empty array as an unbound variable).
secret_env_unsets=("-u" "BUILD_SECRETS")
while IFS= read -r name; do
  [[ -n "$name" ]] && secret_env_unsets+=("-u" "$name")
done < <(python3 "$ACTION_PATH/scripts/build_secret_env_names.py")

report="$WORKDIR/verify-report.md"
status="skipped"
effective="none"

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
  echo "$effective" > "$WORKDIR/verify-effective-level.txt"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    { echo "status=$status"; echo "effective-level=$effective"; } >> "$GITHUB_OUTPUT"
  fi
  rm -rf "$WORKDIR/baseline" "$WORKDIR/baseline-repo"
}
trap emit EXIT

# Both sides of the comparison start as the same working-tree copy, so a file
# the workflow wrote before the action (a gitignored .env the DAGs read) exists
# on both. Otherwise an env-dependent DAG fails at the baseline for lack of
# .env, and an upgrade-only failure in it reads as pre-existing.
# Symlinks that point outside the tree are copied as the files they point to:
# relative to the copy they'd dangle, and appending to one (the nonce in .env,
# the reporter in .astro/conftest.py) would write through to the real file.
copy_tree() {  # <src> <dest>
  rm -rf "$2" && mkdir -p "$2" && rsync -a --copy-unsafe-links --exclude .git "$1/" "$2/"
}
baseline_dir=""
# The target copy minus this run's edits (run_changes.py).
make_baseline_copy() {
  baseline_dir="$WORKDIR/baseline"
  copy_tree "$PROJECT_PATH" "$baseline_dir" &&
    python3 "$ACTION_PATH/scripts/run_changes.py" revert "$PROJECT_PATH" "$WORKDIR" "$baseline_dir"
}
# The import check runs the target in place, where paths above the project
# resolve (a DAG adding ../shared to sys.path, `-r ../common.txt`), so its
# baseline mirrors the whole work tree the same way, with the project's copy
# inside it reverted.
make_import_baseline() {
  local top prefix
  prefix=$(git -C "$PROJECT_PATH" rev-parse --show-prefix 2>/dev/null) || return 1
  if [[ -z "$prefix" ]]; then make_baseline_copy; return; fi
  top=$(git -C "$PROJECT_PATH" rev-parse --show-toplevel) || return 1
  # Outside the project: what git sees (tracked and untracked, not ignored), so
  # a monorepo's node_modules or .venv isn't copied. The project itself is
  # copied whole, as for the target.
  rm -rf "$WORKDIR/baseline-repo" && mkdir -p "$WORKDIR/baseline-repo" || return 1
  (cd "$top" && git ls-files -z --cached --others --exclude-standard \
    | python3 -c 'import os, sys; skip = sys.argv[1].encode()
sys.stdout.buffer.write(b"".join(p + b"\0" for p in sys.stdin.buffer.read().split(b"\0")
                                 if p and not p.startswith(skip) and os.path.lexists(p)))' "$prefix" \
    | tar --null -T - -cf -) | tar -xf - -C "$WORKDIR/baseline-repo" || return 1
  baseline_dir="$WORKDIR/baseline-repo/${prefix%/}"
  copy_tree "$PROJECT_PATH" "$baseline_dir" &&
    python3 "$ACTION_PATH/scripts/run_changes.py" revert "$PROJECT_PATH" "$WORKDIR" "$baseline_dir"
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

# Every verdict below has at least passed the syntax check.
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

effective="syntax"
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

# The parse invocation, gaining `--docker` from detect_parse_mode; see there.
# Carrying the subcommand in the array keeps it non-empty, so expanding it can't
# trip `set -u` on bash 3.2 (a self-hosted macOS runner), where "${arr[@]}" on an
# empty array is an unbound-variable error rather than nothing.
parse_cmd=(astro dev parse)

# A project that commits `dev.mode: standalone` to .astro/config.yaml routes
# `astro dev parse` to a local .venv instead of the image. That venv is
# gitignored, so on a fresh CI checkout the CLI exits in ~75ms with "no virtual
# environment found — run 'astro dev start' first" and image-level verification
# silently never happens (field case: astronomer/circus, which drives standalone
# locally on purpose). Building the image is the entire point of this level, so
# state the mode explicitly rather than inheriting the project's local
# preference. `--docker` is a persistent flag on `astro dev`, added with
# standalone mode itself; probe for it so an older pinned `astro-cli-version`
# degrades to the import-level fallback instead of dying on an unknown flag.
detect_parse_mode() {
  local help_out
  # Capture-then-grep, not `grep -q` on a pipe: grep closing the pipe early
  # kills astro with SIGPIPE (141), which pipefail surfaces as a false negative.
  help_out="$(astro dev parse --help 2>&1 || true)"
  if printf '%s\n' "$help_out" | grep -q -- '--docker'; then
    parse_cmd+=(--docker)
  else
    echo "::warning::This Astro CLI has no 'astro dev parse --docker' flag; a project configured for standalone dev mode will fall back to import-level verification."
  fi

  # Forward the user's docker build secrets so a Dockerfile that needs
  # credentials to build — e.g. `RUN --mount=type=secret,id=netrc` pip-installing
  # from a private repo — builds here the way it does in their own CI. Both
  # image builds (target and baseline) share parse_cmd, so both get them.
  # Flag probe: `--build-secret` (CLI >= 1.44) takes one complete spec per
  # occurrence; its predecessor `--build-secrets` (hidden as a deprecated alias
  # on newer CLIs) comma-split each value and rejoined ALL values into a single
  # `docker build --secret` spec — an identity transform for exactly one secret,
  # silently wrong for several. The trailing space in the first probe keeps it
  # from matching the plural form.
  if [[ -n "${BUILD_SECRETS:-}" ]]; then
    local secret_flag="" line n=0 proj_abs field rebuilt src
    local -a fields
    if printf '%s\n' "$help_out" | grep -q -- '--build-secret '; then
      secret_flag="--build-secret"
    elif printf '%s\n' "$help_out" | grep -q -- '--build-secrets'; then
      secret_flag="--build-secrets"
    fi
    if [[ -z "$secret_flag" ]]; then
      echo "::warning::This Astro CLI has no build-secret flag; the image builds run WITHOUT the provided build-secrets and may fail."
    else
      proj_abs=$(cd "$PROJECT_PATH" && pwd)
      while IFS= read -r line; do
        # Trim like the CLI's own env fallback does; skip blank lines.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        # Resolve relative src= paths against the project root NOW: the builds
        # run from copies in other directories, and a path relative to the
        # project (including one above it, ../netrc) must still name the same
        # file for both. A `type=env` spec's src names an env var, not a
        # file — leave those verbatim.
        case ",$line," in
          *,type=env,*) ;;
          *)
            IFS=',' read -r -a fields <<< "$line"
            rebuilt=""
            for field in "${fields[@]}"; do
              case "$field" in
                src=/*|source=/*) ;;
                src=*|source=*) field="${field%%=*}=${proj_abs}/${field#*=}" ;;
              esac
              case "$field" in
                src=*|source=*)
                  src="${field#*=}"
                  [[ -e "$src" ]] || echo "::warning::build secret file '$src' does not exist; the image builds will likely fail."
                  ;;
              esac
              rebuilt+="${field},"
            done
            line="${rebuilt%,}"
            ;;
        esac
        parse_cmd+=("$secret_flag" "$line")
        n=$((n + 1))
      done <<< "$BUILD_SECRETS"
      if [[ "$secret_flag" == "--build-secrets" && "$n" -gt 1 ]]; then
        echo "::warning::This Astro CLI predates '--build-secret' (>= 1.44) and folds multiple build secrets into one malformed spec; only a single secret works reliably here. Pin astro-cli-version to 1.44+ or provide one secret."
      fi
    fi
  fi
}

# Strip secrets: the build and the in-image pytest execute repository code.
# Env vars named by BUILD_SECRETS specs intentionally pass through — supplying
# them is the user's explicit opt-in to expose those to their own Dockerfile
# (which is why the docs forbid reusing the four names stripped here).
# NO_COLOR/PY_COLORS keep the log readable; the verdict doesn't depend on them.
run_parse() {
  ( cd "$1" &&
    env -u ASTRO_TOKEN -u ASTRO_API_TOKEN -u GH_TOKEN -u GITHUB_TOKEN \
    NO_COLOR=1 PY_COLORS=0 FORCE_COLOR=0 \
    timeout 1800 "${parse_cmd[@]}" ) > "$2" 2>&1
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

# Make a throwaway project copy ready for `astro dev parse`, and install the
# result reporter (parse_probe/conftest.py) next to the integrity test. $2 is
# the Airflow version that copy builds at. Fills in the scaffolding a project
# made without `astro dev init` lacks: without it the CLI refuses the directory
# ("not an Astro project") or exits 0 having run nothing (no integrity test).
prepare_parse_dir() {
  local dir="$1" af_major="${2%%.*}" astro_dir="$1/.astro"
  mkdir -p "$astro_dir"
  if [[ ! -f "$astro_dir/config.yaml" ]]; then
    printf 'project:\n    name: otto-verify\n' > "$astro_dir/config.yaml"
  fi
  [[ -e "$dir/packages.txt" ]] || : > "$dir/packages.txt"
  [[ -e "$dir/requirements.txt" ]] || : > "$dir/requirements.txt"
  if [[ ! -f "$astro_dir/test_dag_integrity_default.py" ]]; then
    [[ "$af_major" == "2" ]] || af_major=3
    cp "$ACTION_PATH/scripts/parse_probe/dag_integrity_default_af${af_major}.py" \
      "$astro_dir/test_dag_integrity_default.py"
  fi
  shim_integrity_test "$dir"
  # Appended, so a conftest the project already has keeps loading as itself.
  # The leading newline guards a project file without a trailing one.
  { echo; cat "$ACTION_PATH/scripts/parse_probe/conftest.py"; } >> "$astro_dir/conftest.py"
  # The CLI hands .env to the integrity container only (never to the image
  # build), so a result line carrying this nonce came from that run.
  [[ ! -s "$dir/.env" || -z "$(tail -c1 "$dir/.env")" ]] || echo >> "$dir/.env"
  echo "OTTO_PARSE_NONCE=$parse_nonce" >> "$dir/.env"
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

  detect_parse_mode
  parse_nonce=$(python3 -c 'import secrets; print(secrets.token_hex(12))')

  tgt_tag=$(jq -r '.runtime.target_tag // empty' "$PLAN_FILE")

  # Parse runs on a COPY: the build and any astro-generated files must never
  # dirty the tree open-pr.sh commits.
  parse_target_dir="$WORKDIR/parse-target"
  if ! copy_tree "$PROJECT_PATH" "$parse_target_dir"; then
    fallback_note="ℹ️ Image-level verification could not run (the project copy failed). Results below come from the import-level fallback."
    echo "::warning::verify-level parse: project copy failed; falling back to import-level verification."
    return 1
  fi
  prepare_parse_dir "$parse_target_dir" \
    "$(jq -r '.runtime.target_airflow // .runtime.current_airflow // empty' "$PLAN_FILE")"

  echo "::group::Image parse (target Runtime ${tgt_tag:-per project Dockerfile})"
  rm -f "$WORKDIR/import-failures.json"
  set +e
  run_parse "$parse_target_dir" "$WORKDIR/parse-target.log"
  target_run_rc=$?
  IMPORT_JSON="$WORKDIR/import-failures.json" OTTO_PARSE_NONCE="$parse_nonce" \
    python3 "$ACTION_PATH/scripts/parse_check.py" "$WORKDIR/parse-target.log"
  target_rc=$?
  set -e
  tail -n 40 "$WORKDIR/parse-target.log"
  echo "::endgroup::"

  # The kill outranks the scrape. A `timeout` can land on a log that still looks
  # finished — the image build's own pytest output, a session cut off after its
  # summary — and nothing was fully verified either way, so decide on the signal
  # we own rather than on what the output resembles.
  if [[ "$target_run_rc" -eq 124 ]]; then
    fallback_note="ℹ️ Image-level verification could not run (\`astro dev parse\` timed out before completing). Results below come from the import-level fallback."
    echo "::warning::verify-level parse: astro dev parse timed out before completing; falling back to import-level verification."
    return 1
  fi

  # Both "harness produced no verdict" classes degrade to the import-level
  # check rather than reporting nothing: rc 5 = the project's integrity test is
  # incompatible with the target Airflow; rc 2 = the run never completed
  # (timeout, unrecognized output). Only genuine verdicts (0/3/4) stay here.
  if [[ "$target_rc" -eq 5 ]]; then
    cause=$(jq -r '.collection_error // empty' "$WORKDIR/import-failures.json" 2>/dev/null)
    fallback_note="ℹ️ Image-level verification could not run: the project's \`.astro/test_dag_integrity_default.py\` fails to start at the target Airflow${cause:+ (\`${cause}\`)}. Regenerate it with an updated Astro CLI (\`astro dev init\`). Results below come from the import-level fallback."
    echo "::warning::verify-level parse: the project's DAG integrity test is incompatible with the target Airflow; falling back to import-level verification."
    return 1
  elif [[ "$target_rc" -ne 0 && "$target_rc" -ne 3 && "$target_rc" -ne 4 ]]; then
    # 2 = no verdict; anything else is parse_check itself failing, which is no
    # verdict either, never a code defect in the project.
    # The CLI usually states exactly why it refused (a project configured for
    # standalone dev mode, a venv without pytest, …). Its words beat the
    # catch-all: "produced no recognizable result" is what made a field case take
    # a full log dig to explain. Timeouts already returned above.
    local reason="produced no recognizable result"
    local cli_error="" why=""
    cli_error=$(jq -r '.cli_error // empty' "$WORKDIR/import-failures.json" 2>/dev/null || true)
    why=$(jq -r '.reason // empty' "$WORKDIR/import-failures.json" 2>/dev/null || true)
    if [[ -n "$cli_error" ]]; then reason="failed: ${cli_error}"
    elif [[ -n "$why" ]]; then reason="produced no verdict: ${why}"; fi
    fallback_note="ℹ️ Image-level verification could not run (\`astro dev parse\` ${reason}). Results below come from the import-level fallback."
    echo "::warning::verify-level parse: astro dev parse ${reason}; falling back to import-level verification."
    return 1
  fi

  # From here on every outcome is a verdict from inside the image.
  effective="parse"
  if [[ "$target_rc" -eq 0 ]]; then
    status="passed"
    checked=$(jq -r '.checked' "$WORKDIR/import-failures.json")
    echo "✅ All ${checked} DAG file(s) import cleanly inside the target Runtime image (\`${tgt_tag:-Dockerfile}\`)." > "$report"
    exit 0
  fi

  # Baseline: the same parse at the pre-upgrade state (current image + pins).
  set +e
  make_baseline_copy
  baseline_made=$?
  baseline_rc=""
  if [[ "$baseline_made" -eq 0 ]]; then
    prepare_parse_dir "$baseline_dir" "$(jq -r '.runtime.current_airflow // empty' "$PLAN_FILE")"
    echo "::group::Image parse (baseline: current Runtime)"
    run_parse "$baseline_dir" "$WORKDIR/parse-baseline.log"
    IMPORT_JSON="$WORKDIR/baseline-failures.json" OTTO_PARSE_NONCE="$parse_nonce" \
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
      effective="syntax"  # no image-level verdict exists
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
      # No baseline build verdict (no baseline copy, baseline parse timed out
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
    "${secret_env_unsets[@]}" \
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

if [[ "$rc" -eq 0 || "$rc" -eq 3 ]]; then effective="import"; fi
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
  # state — the baseline copy, with the pre-bump requirements and the
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
    make_import_baseline \
      || { baseline_note="a baseline copy could not be created"; return 1; }
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
        "${secret_env_unsets[@]}" \
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
