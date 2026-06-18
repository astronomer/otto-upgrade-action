#!/usr/bin/env bash
# Build the prompt that drives Otto's code migration over the already-bumped
# project. The version pins are bumped *before* Otto runs (apply_bump.py), so
# Otto's job is purely the code-level migration: rewrite deprecated imports,
# rename changed parameters, and flag anything that needs a human.
#
# The plan + apply summary go to a sidecar file Otto reads via its `read` tool
# rather than into argv, mirroring otto-review-action so a large project context
# never trips ARG_MAX.
#
# Required env:
#   WORKDIR      scratch dir (default /tmp/otto-upgrade)
#   PLAN_FILE    resolve_target.py output
#   PROJECT_PATH project root the bumps were applied to
# Writes $WORKDIR/{upgrade-context.md,user-prompt.txt}.

set -euo pipefail

WORKDIR="${WORKDIR:-/tmp/otto-upgrade}"
: "${PLAN_FILE:?}"
: "${PROJECT_PATH:?}"
mkdir -p "$WORKDIR"

cur_af=$(jq -r '.runtime.current_airflow // "unknown"' "$PLAN_FILE")
tgt_af=$(jq -r '.runtime.target_airflow // .runtime.current_airflow // "unknown"' "$PLAN_FILE")

{
  echo "# Upgrade context"
  echo
  echo "The version pins in this project have ALREADY been bumped (Dockerfile"
  echo "Runtime tag and requirements.txt provider pins). Do NOT change version"
  echo "pins again. Your job is the code-level migration only."
  echo
  echo "- Airflow: ${cur_af} -> ${tgt_af}"
  echo "- Project root: ${PROJECT_PATH}"
  echo
  echo "## Resolved plan"
  echo
  echo '<plan>'
  cat "$PLAN_FILE"
  echo
  echo '</plan>'
} > "$WORKDIR/upgrade-context.md"

{
  echo "Upgrade the Astro project under '${PROJECT_PATH}' to Airflow ${tgt_af}."
  echo
  echo "Use the read tool to load ${WORKDIR}/upgrade-context.md first. The version"
  echo "pins are already bumped — do not touch the Dockerfile FROM tag or the"
  echo "requirements.txt provider versions. Apply only the CODE migrations the"
  echo "${cur_af} -> ${tgt_af} transition requires: rewrite deprecated/moved"
  echo "imports, rename changed operator/parameter names, and adjust call sites"
  echo "per the Airflow upgrade knowledge you are given."
  echo
  echo "Scan dags/, include/, and plugins/ under the project root. Make the edits"
  echo "directly. Do not guess: if a change is ambiguous or risky, leave the code"
  echo "as-is and record it under manual_followups instead."
  echo
  echo "Submit your final answer via the submit_final_answer tool using the schema"
  echo "you were given (summary, changes_made, manual_followups, files_changed)."
} > "$WORKDIR/user-prompt.txt"

echo "Upgrade context: $(wc -c < "$WORKDIR/upgrade-context.md") bytes"
echo "User prompt: $(wc -c < "$WORKDIR/user-prompt.txt") bytes"
