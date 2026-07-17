#!/usr/bin/env bash
# Build the prompt that drives Otto's code migration over the already-bumped
# project. The version pins are bumped *before* Otto runs (apply_bump.py), so
# Otto's job is purely the code-level migration: rewrite deprecated imports,
# rename changed parameters, and flag anything that needs a human.
#
# The plan goes to a sidecar file Otto reads via its `read` tool rather than into
# argv, mirroring otto-review-action so a large project context never trips
# ARG_MAX.
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

cur_af=$(jq -r '.runtime.current_airflow // empty' "$PLAN_FILE")
tgt_af=$(jq -r '.runtime.target_airflow // empty' "$PLAN_FILE")
# Human-readable list of provider bumps, e.g. "amazon 9.0.0 -> 9.30.0".
prov_lines=$(jq -r '.providers[]? | select(.target != null and .current != .target)
  | "\(.package | sub("apache-airflow-providers-"; "")) \(.current) -> \(.target)"' "$PLAN_FILE")
# User pins the run raised (bump-blocking-pins). These are NOT Airflow
# packages — the skill's KB says nothing about them — so Otto must be told
# to reason about the user's own usage of them, especially across majors.
pin_lines=$(jq -r '.user_pin_bumps[]?
  | "\(.pin) \(.from) -> \(.to) (raised to take \(.unblocks.package | sub("apache-airflow-providers-"; "")) \(.unblocks.version))"' "$PLAN_FILE")

# A runtime (Airflow) move and a provider-only move want different framing.
# Both EXPLICITLY invoke the hosted `airflow-upgrade` skill (this KB). Without
# naming the skill, Otto routes a free-text "upgrade" prompt to generic doc
# search and never engages the curated breaking-change/import-mapping data —
# verified empirically. Pairing this prompt with `--allowed-skills
# airflow-upgrade` (run-otto.sh) makes the skill the deterministic path.
if [[ -n "$tgt_af" && "$tgt_af" != "$cur_af" ]]; then
  scope_line="Airflow ${cur_af:-unknown} -> ${tgt_af}"
  goal="Use the airflow-upgrade skill (currentVersion=${cur_af:-unknown}, targetVersion=${tgt_af}) to upgrade the Astro project under '${PROJECT_PATH}' to Airflow ${tgt_af}."
  focus="Apply only the CODE migrations the ${cur_af:-current} -> ${tgt_af} transition requires"
else
  scope_line="Provider upgrades only (Airflow unchanged at ${cur_af:-current})"
  goal="Use the airflow-upgrade skill (currentVersion=${cur_af:-unknown}, targetVersion=${cur_af:-unknown}) to migrate the Astro project under '${PROJECT_PATH}' for the provider upgrades below (Airflow itself is unchanged)."
  focus="Apply only the CODE migrations these provider version bumps require"
fi

{
  echo "# Upgrade context"
  echo
  echo "The version pins in this project have ALREADY been bumped (Dockerfile"
  echo "Runtime tag and/or requirements.txt provider pins). Do NOT change version"
  echo "pins again. Your job is the code-level migration only."
  echo
  echo "- Scope: ${scope_line}"
  echo "- Project root: ${PROJECT_PATH}"
  if [[ -n "$prov_lines" ]]; then
    echo "- Provider bumps:"
    while IFS= read -r l; do [[ -n "$l" ]] && echo "    - $l"; done <<<"$prov_lines"
  fi
  if [[ -n "$pin_lines" ]]; then
    echo "- User pins raised by this run (bump-blocking-pins):"
    while IFS= read -r l; do [[ -n "$l" ]] && echo "    - $l"; done <<<"$pin_lines"
    echo
    echo "## Raised user pins need code review"
    echo
    echo "The pins above are the USER'S OWN dependencies, raised so newer"
    echo "providers could resolve. They are not Airflow packages — the Airflow"
    echo "upgrade knowledge does not cover them. For each one: scan the project"
    echo "for code that imports or uses the package. When the raise crosses a"
    echo "major version, treat the user's usage as potentially broken — apply"
    echo "only migrations you are confident about, and otherwise add a"
    echo "manual_followups item naming the affected files and the version jump."
    echo "As with everything else, do not edit the pins themselves."
  fi
  echo
  echo "## Environment (headless CI)"
  echo
  echo "This is an unattended CI run. There is NO local or remote Airflow"
  echo "instance: \`af\` commands, \`astro dev restart\`, and any rebuild-and-"
  echo "validate phase of the skill CANNOT run here — skip them. The action"
  echo "performs its own post-migration verification (it imports every DAG"
  echo "against the target Airflow and providers), so do not treat skipped"
  echo "runtime validation as a gap to escalate."
  echo
  echo "Reserve manual_followups for action items the UPGRADE requires of a"
  echo "human — code changes you could not safely make, and platform or"
  echo "control-plane steps (RBAC, connections, deployment settings). Do NOT"
  echo "list limitations of this CI environment (a missing tool, no Airflow"
  echo "instance, validation you could not run here) as follow-ups."
  echo
  echo "changes_made is read by a human reviewing the PR. Every item must be"
  echo "about THEIR code: an edit you made (file, what, why) or a decision"
  echo "you took about their code — a specific usage you reviewed and"
  echo "deliberately left unchanged (only when you are confident no change is"
  echo "needed; ambiguous or risky cases go in manual_followups), or an edit"
  echo "you reverted, with the reason. A decision names the specific usage"
  echo "and where it lives; a list of what you scanned is process, not a"
  echo "decision. Do not narrate your process — loading guidance, running"
  echo "scanners or greps, or enumerating the patterns you checked is not"
  echo "information a reviewer can act on. When the checks found nothing"
  echo "else to change, say so in one changes_made item."
  echo
  echo "## Resolved plan"
  echo
  echo '<plan>'
  cat "$PLAN_FILE"
  echo
  echo '</plan>'
} > "$WORKDIR/upgrade-context.md"

{
  echo "$goal"
  echo
  echo "Use the read tool to load ${WORKDIR}/upgrade-context.md first. The version"
  echo "pins are already bumped — do not touch the Dockerfile FROM tag or the"
  echo "requirements.txt provider versions. ${focus}: rewrite deprecated/moved"
  echo "imports, rename changed operator/parameter names, and adjust call sites"
  echo "per the Airflow upgrade knowledge you are given."
  echo
  echo "Scan dags/, include/, and plugins/ under the project root. Make the edits"
  echo "directly. Do not guess: if a change is ambiguous or risky, leave the code"
  echo "as-is and record it under manual_followups instead."
  if [[ -n "$pin_lines" ]]; then
    echo
    echo "This run also raised user-owned dependency pins — see 'Raised user"
    echo "pins need code review' in the context file. Review the project's"
    echo "usage of those packages as described there: migrate what you are"
    echo "confident about, record the rest as manual follow-ups."
  fi
  echo
  echo "This is a headless CI run with no Airflow instance — skip any af/rebuild"
  echo "validation steps (the action verifies separately), and keep environment"
  echo "limitations OUT of manual_followups: follow-ups are only for code or"
  echo "platform actions the upgrade itself requires of a human."
  echo
  echo "Submit your final answer via the submit_final_answer tool using the schema"
  echo "you were given (summary, changes_made, manual_followups, files_changed)."
} > "$WORKDIR/user-prompt.txt"

echo "Upgrade context: $(wc -c < "$WORKDIR/upgrade-context.md") bytes"
echo "User prompt: $(wc -c < "$WORKDIR/user-prompt.txt") bytes"
