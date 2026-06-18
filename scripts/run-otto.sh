#!/usr/bin/env bash
# Run Otto (via the Astro CLI) to apply the code migration over the bumped
# project, and capture the structured result. Otto ships bundled with the Astro
# CLI, so the only supported invocation is `astro otto ...`.
#
# Required env:
#   ASTRO_TOKEN / ASTRO_ORGANIZATION - gateway auth (set by action.yaml)
#   ASTRO_CLI_PATH   - absolute path to the astro binary
#   ACTION_PATH      - path to this action's checkout
#   WORKDIR          - scratch dir holding the prompt (default /tmp/otto-upgrade)
#   INPUT_MODEL      - optional --model override (empty = Otto default)
#
# Writes $WORKDIR/{otto-stdout.jsonl,result.json}.

set -euo pipefail

: "${ASTRO_TOKEN:?}"
: "${ASTRO_ORGANIZATION:?}"
: "${ASTRO_CLI_PATH:?}"
: "${ACTION_PATH:?}"
WORKDIR="${WORKDIR:-/tmp/otto-upgrade}"
mkdir -p "$WORKDIR"

# Drive Otto's existing airflow-upgrade skill (this KB) over the bumped project.
# No persona is involved — the upgrade intelligence is the skill. Engaging it is
# deterministic ONLY when we both scope and name it: a free-text "upgrade" prompt
# alone makes Otto route to generic doc-search and skip the skill (verified).
#   --allowed-skills airflow-upgrade  restricts the skill tool to this skill; the
#       prompt (build-prompt.sh) names it explicitly with currentVersion/targetVersion
#   --output-schema  forces the structured result (submit_final_answer)
#   --skip-permissions  applies edits non-interactively (CI has no TTY)
#   (no --allowed-tools: the migration needs Otto's edit/bash tools)
otto_args=(
  otto
  --mode json
  --no-session
  --skip-permissions
  --allowed-skills airflow-upgrade
  --output-schema "@$ACTION_PATH/scripts/upgrade-schema.json"
)
if [[ -n "${INPUT_MODEL:-}" ]]; then
  otto_args+=(--model "$INPUT_MODEL")
fi

prompt_file="$WORKDIR/user-prompt.txt"
if [[ ! -s "$prompt_file" ]]; then
  echo "::error::User prompt file is empty or missing: $prompt_file"
  exit 1
fi
prompt="$(cat "$prompt_file")"

echo "::group::Otto run"
set +e
"$ASTRO_CLI_PATH" "${otto_args[@]}" "$prompt" \
  > "$WORKDIR/otto-stdout.jsonl" \
  2> "$WORKDIR/otto-stderr.log"
otto_exit=$?
set -e
echo "Otto exited with $otto_exit"
echo "--- last 50 stderr lines ---"
tail -n 50 "$WORKDIR/otto-stderr.log" || true
echo "--- end ---"
echo ::endgroup::

if [[ "$otto_exit" -ne 0 ]]; then
  # Otto is mandatory — we do NOT ship an unmigrated version bump as a pretend
  # upgrade. Fail the run (no PR); the next scheduled run retries. A transient
  # gateway blip costs a day, not a misleading PR.
  echo "::error::Otto exited non-zero ($otto_exit) — aborting without opening a PR (the migration is the point of this action). See the 'Otto run' group above; the run will retry on the next schedule."
  head -n 5 "$WORKDIR/otto-stdout.jsonl" >&2 || true
  exit "$otto_exit"
fi

python3 "$ACTION_PATH/scripts/extract_result.py" \
  < "$WORKDIR/otto-stdout.jsonl" \
  > "$WORKDIR/result.json"

if [[ ! -s "$WORKDIR/result.json" ]]; then
  echo "::warning::Could not find a structured upgrade result in Otto's output. The code edits (if any) are still on disk; the PR body will omit the migration summary."
  rm -f "$WORKDIR/result.json"
else
  echo "Extracted upgrade result: $(wc -c < "$WORKDIR/result.json") bytes"
fi
