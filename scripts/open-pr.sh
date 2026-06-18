#!/usr/bin/env bash
# Commit the applied upgrade and open (or update in place) a single rolling PR.
#
# Branch model: one stable branch (default `otto/airflow-upgrade`) that always
# reflects the latest resolved target. Re-runs force-push to it and edit the
# same PR rather than stacking a new PR per run — the same "edit our footprint
# in place" philosophy otto-review-action uses for comments. A newer target that
# ships next week updates this PR; it does not open a second one.
#
# In dry-run, everything up to the commit is computed and the body is written to
# the step summary, but nothing is pushed and no PR is opened.
#
# Required env:
#   GH_TOKEN          github token with contents:write + pull-requests:write
#   GITHUB_REPOSITORY owner/repo (runner-set)
#   PROJECT_PATH      project root that was modified
#   PLAN_FILE         resolve_target.py output
#   APPLY_FILE        apply_bump.py output
#   ACTION_PATH       this action's checkout
#   WORKDIR           scratch dir (default /tmp/otto-upgrade)
#   BASE_BRANCH       PR base (default: the repo default branch)
#   BRANCH            head branch name (default otto/airflow-upgrade)
#   LABELS            comma-separated labels
#   DRY_RUN           "true" => compute + summarize only
#   OTTO_FILE         extract_result.py output (optional)
#   VERIFY_FILE       verify-report.md (optional)
#   ACTION_REF        action version for the footer (optional)
#
# Writes step outputs: pr-number, pr-url, branch, changed.

set -euo pipefail

: "${GH_TOKEN:?}"
: "${GITHUB_REPOSITORY:?}"
: "${PROJECT_PATH:?}"
: "${PLAN_FILE:?}"
: "${APPLY_FILE:?}"
: "${ACTION_PATH:?}"
WORKDIR="${WORKDIR:-/tmp/otto-upgrade}"
BRANCH="${BRANCH:-otto/airflow-upgrade}"
DRY_RUN="${DRY_RUN:-false}"
mkdir -p "$WORKDIR"

step_output() { [[ -n "${GITHUB_OUTPUT:-}" ]] && printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"; }
step_output changed "false"
step_output branch "$BRANCH"
step_output pr-number ""
step_output pr-url ""

# Render the PR body once; reused for the real PR and the dry-run summary.
body_file="$WORKDIR/pr-body.md"
PLAN_FILE="$PLAN_FILE" APPLY_FILE="$APPLY_FILE" \
  OTTO_FILE="${OTTO_FILE:-}" VERIFY_FILE="${VERIFY_FILE:-}" \
  ACTION_REF="${ACTION_REF:-}" \
  python3 "$ACTION_PATH/scripts/build_pr_body.py" > "$body_file"

# Title from the resolved target.
rt_to=$(jq -r '.runtime.target_tag // empty' "$PLAN_FILE")
rt_from=$(jq -r '.runtime.current_tag // empty' "$PLAN_FILE")
n_prov=$(jq '[.providers[]? | select(.target != null and .current != .target)] | length' "$PLAN_FILE")
if [[ -n "$rt_to" && "$rt_to" != "$rt_from" ]]; then
  title="Upgrade Airflow Runtime to ${rt_to}"
  [[ "$n_prov" -gt 0 ]] && title+=" + ${n_prov} provider(s)"
elif [[ "$n_prov" -gt 0 ]]; then
  title="Upgrade ${n_prov} Airflow provider(s)"
else
  echo "Plan contains no version changes; nothing to open a PR for."
  exit 0
fi

# Stage only what's under the project path so we never sweep in unrelated files.
git -C "$GITHUB_WORKSPACE" config user.name "${GIT_AUTHOR_NAME:-otto-upgrade-action[bot]}" 2>/dev/null || \
  git config user.name "${GIT_AUTHOR_NAME:-otto-upgrade-action[bot]}"
git config user.email "${GIT_AUTHOR_EMAIL:-otto-upgrade-action[bot]@users.noreply.github.com}"

base_ref="${BASE_BRANCH:-$(git symbolic-ref --short HEAD 2>/dev/null || echo main)}"

# Create/reset the rolling branch from the current checkout (the base).
git checkout -B "$BRANCH"
git add -A -- "$PROJECT_PATH"

if git diff --cached --quiet; then
  echo "No staged changes under $PROJECT_PATH — the resolved bump produced no diff. No PR."
  exit 0
fi
step_output changed "true"

git commit -q -m "$title" -m "Automated Airflow upgrade opened by otto-upgrade-action."
echo "Committed upgrade on branch $BRANCH."

if [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run: not pushing or opening a PR. Rendered body:"
  echo "----------------------------------------"
  cat "$body_file"
  echo "----------------------------------------"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    { echo "# otto-upgrade-action (dry-run)"; echo; echo "**Would open:** \`$title\`"; echo;
      echo "**Head:** \`$BRANCH\` → **base:** \`$base_ref\`"; echo; cat "$body_file"; } >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 0
fi

# Push the branch (force-with-lease so a re-run updates it without clobbering
# anything pushed out-of-band).
git push --force-with-lease origin "$BRANCH"

# Open or update the single rolling PR.
existing=$(gh pr list --repo "$GITHUB_REPOSITORY" --head "$BRANCH" --state open \
  --json number --jq '.[0].number // empty' 2>/dev/null || true)

if [[ -n "$existing" ]]; then
  echo "Updating existing PR #$existing in place."
  gh pr edit "$existing" --repo "$GITHUB_REPOSITORY" \
    --title "$title" --body-file "$body_file" >/dev/null
  pr_number="$existing"
  pr_url=$(gh pr view "$existing" --repo "$GITHUB_REPOSITORY" --json url --jq '.url')
else
  echo "Opening a new PR."
  pr_url=$(gh pr create --repo "$GITHUB_REPOSITORY" \
    --base "$base_ref" --head "$BRANCH" \
    --title "$title" --body-file "$body_file")
  pr_number=$(gh pr view "$BRANCH" --repo "$GITHUB_REPOSITORY" --json number --jq '.number')
fi

# Apply labels best-effort (a missing label shouldn't fail the run).
if [[ -n "${LABELS:-}" ]]; then
  IFS=',' read -ra label_arr <<<"$LABELS"
  for l in "${label_arr[@]}"; do
    l="$(echo "$l" | xargs)"
    [[ -z "$l" ]] && continue
    gh pr edit "$pr_number" --repo "$GITHUB_REPOSITORY" --add-label "$l" >/dev/null 2>&1 \
      || echo "::warning::Could not add label '$l' (does it exist in the repo?)."
  done
fi

echo "PR #$pr_number: $pr_url"
step_output pr-number "$pr_number"
step_output pr-url "$pr_url"
