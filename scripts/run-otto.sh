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
#   INPUT_MODEL      - optional --model override (empty = persona default)
#
# Writes $WORKDIR/{otto-stdout.jsonl,result.json}.

set -euo pipefail

: "${ASTRO_TOKEN:?}"
: "${ASTRO_ORGANIZATION:?}"
: "${ASTRO_CLI_PATH:?}"
: "${ACTION_PATH:?}"
WORKDIR="${WORKDIR:-/tmp/otto-upgrade}"
mkdir -p "$WORKDIR"

# --persona upgrader binds Otto's bundled Airflow-upgrade prompt, its
# edit-capable tool allowlist, and the upgrade output schema. Symmetric to the
# reviewer persona the review action uses. The verify step (action.yaml) fails
# loud if the bundled Otto lacks this persona, so reaching here means it exists.
otto_args=(
  otto
  --mode json
  --no-session
  --skip-permissions
  --persona upgrader
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
  # Degrade gracefully: the version bumps are already on disk, so we let the
  # PR open with them and flag the failed migration in the body rather than
  # aborting the whole action. (Composite steps can't use continue-on-error,
  # so this has to be handled here.)
  echo "::warning::Otto exited non-zero ($otto_exit); skipping the code migration. The version-bump PR will still open — review breaking changes manually. See the 'Otto run' group above."
  head -n 5 "$WORKDIR/otto-stdout.jsonl" >&2 || true
  exit 0
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
