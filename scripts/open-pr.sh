#!/usr/bin/env bash
# Commit the applied upgrade and open (or update in place) a single rolling PR.
#
# Branch model: one stable branch (default `otto/airflow-upgrade`) that always
# reflects the latest resolved target. Re-runs force-push to it and edit the
# same PR rather than stacking a new PR per run — the same "edit our footprint
# in place" philosophy otto-review-action uses for comments. A newer target that
# ships next week updates this PR; it does not open a second one.
#
# In dry-run, the would-be PR body + diff are written to the step summary; the
# working tree is NOT mutated and nothing is pushed.
#
# Required env:
#   GITHUB_REPOSITORY owner/repo (runner-set)
#   PROJECT_PATH      project root that was modified
#   PLAN_FILE         resolve_target.py output
#   ACTION_PATH       this action's checkout
#   WORKDIR           scratch dir (default /tmp/otto-upgrade)
#   BASE_BRANCH       PR base (default: the repo default branch)
#   BRANCH            head branch name (default otto/airflow-upgrade)
#   LABELS            comma-separated labels
#   DRY_RUN           "true" => compute + summarize only (no mutation, no push)
#   OTTO_FILE         extract_result.py output (optional)
#   VERIFY_FILE       verify-report.md (optional)
#   ACTION_REF        action version for the footer (optional)
#   GH_TOKEN          github token with contents:write + pull-requests:write
#                     (required only when DRY_RUN != "true")
#
# Writes step outputs: pr-number, pr-url, branch, changed.

set -euo pipefail

: "${GITHUB_REPOSITORY:?}"
: "${PROJECT_PATH:?}"
: "${PLAN_FILE:?}"
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

# Validate the branch names early — they flow into git/gh as positional args, so
# a leading '-' (or shell/path metacharacters) must be rejected, not passed.
valid_ref() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]]; }
if ! valid_ref "$BRANCH"; then
  echo "::error::Invalid 'branch' value '$BRANCH'. Use [A-Za-z0-9._/-], no leading '-'."
  exit 1
fi
if [[ -n "${BASE_BRANCH:-}" ]] && ! valid_ref "$BASE_BRANCH"; then
  echo "::error::Invalid 'base-branch' value '$BASE_BRANCH'."
  exit 1
fi

# Render the PR body once; reused for the real PR and the dry-run summary.
body_file="$WORKDIR/pr-body.md"
PLAN_FILE="$PLAN_FILE" OTTO_FILE="${OTTO_FILE:-}" VERIFY_FILE="${VERIFY_FILE:-}" \
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

# Dry-run: preview only. Show the diff the bumps produced WITHOUT touching the
# branch or committing (so a later step in the consumer's job sees a clean tree).
if [[ "$DRY_RUN" == "true" ]]; then
  echo "Dry-run: not mutating the tree, pushing, or opening a PR."
  diff_out="$(git -C "$GITHUB_WORKSPACE" diff -- "$PROJECT_PATH" || true)"
  if [[ -n "$diff_out" ]]; then step_output changed "true"; fi
  echo "----- would open: $title -----"
  cat "$body_file"
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    {
      echo "# otto-upgrade-action (dry-run)"; echo
      echo "**Would open:** \`$title\`  (head \`$BRANCH\`)"; echo
      cat "$body_file"; echo
      echo "<details><summary>Diff</summary>"; echo; echo '```diff'; echo "$diff_out"; echo '```'; echo "</details>"
    } >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 0
fi

: "${GH_TOKEN:?GH_TOKEN is required to push and open the PR}"
echo "::add-mask::$GH_TOKEN"

git config user.name "${GIT_AUTHOR_NAME:-otto-upgrade-action[bot]}"
git config user.email "${GIT_AUTHOR_EMAIL:-otto-upgrade-action[bot]@users.noreply.github.com}"

# Resolve the PR base. In a scheduled run the workspace is often in detached
# HEAD, so `git symbolic-ref` fails — fall back to the repo's real default
# branch via the API, never a hardcoded 'main'.
if [[ -n "${BASE_BRANCH:-}" ]]; then
  base_ref="$BASE_BRANCH"
else
  base_ref="$(git symbolic-ref --short HEAD 2>/dev/null || true)"
  if [[ -z "$base_ref" || "$base_ref" == "$BRANCH" ]]; then
    base_ref="$(gh repo view "$GITHUB_REPOSITORY" --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || true)"
  fi
  base_ref="${base_ref:-main}"
fi

# Push over an explicitly token-authenticated URL rather than relying on the
# credential actions/checkout persists into .git/config. This lets consumers set
# `persist-credentials: false` so the write token is never left in the workspace
# for the Otto step or imported DAG code to read.
auth_remote="https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}.git"

# Create/reset the rolling branch from the current checkout (the base), stage
# only what's under the project path, and commit.
git checkout -B "$BRANCH"
git add -A -- "$PROJECT_PATH"
if git diff --cached --quiet; then
  echo "No staged changes under $PROJECT_PATH — the resolved bump produced no diff. No PR."
  exit 0
fi
step_output changed "true"
git commit -q -m "$title" -m "Automated Airflow upgrade opened by otto-upgrade-action."
echo "Committed upgrade on branch $BRANCH."

# Fetch the remote branch first so --force-with-lease has an accurate lease
# (a fresh runner has no remote-tracking ref otherwise, making the lease moot).
expected=""
if git fetch "$auth_remote" "refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" 2>/dev/null; then
  expected="$(git rev-parse "refs/remotes/origin/$BRANCH" 2>/dev/null || true)"
fi

# Quiet-day short-circuit: if the rolling branch already exists with the exact
# same tree we'd push (e.g. yesterday's run already proposed this target and
# nothing newer shipped), do NOT push. A fresh commit would differ only by
# timestamp, which would needlessly re-trigger the PR's CI and add "force-pushed"
# noise every run. Leave the existing PR untouched.
if [[ -n "$expected" ]]; then
  local_tree="$(git rev-parse 'HEAD^{tree}')"
  remote_tree="$(git rev-parse "${expected}^{tree}" 2>/dev/null || true)"
  if [[ -n "$remote_tree" && "$local_tree" == "$remote_tree" ]]; then
    echo "Rolling PR branch already reflects this target — nothing to push."
    step_output changed "false"
    existing=$(gh pr list --repo "$GITHUB_REPOSITORY" --head "$BRANCH" --state open \
      --json number,url --jq '.[0] | "\(.number) \(.url)"' 2>/dev/null || true)
    if [[ -n "$existing" ]]; then
      step_output pr-number "${existing%% *}"
      step_output pr-url "${existing##* }"
      echo "Existing PR is up to date: ${existing##* }"
    fi
    exit 0
  fi
  git push --force-with-lease="refs/heads/$BRANCH:$expected" "$auth_remote" "HEAD:refs/heads/$BRANCH"
else
  # Branch doesn't exist remotely yet — a plain push creates it.
  git push "$auth_remote" "HEAD:refs/heads/$BRANCH"
fi

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
    l="${l#"${l%%[![:space:]]*}"}"; l="${l%"${l##*[![:space:]]}"}"  # trim
    [[ -z "$l" ]] && continue
    gh pr edit "$pr_number" --repo "$GITHUB_REPOSITORY" --add-label "$l" >/dev/null 2>&1 \
      || echo "::warning::Could not add label '$l' (does it exist in the repo?)."
  done
fi

echo "PR #$pr_number: $pr_url"
step_output pr-number "$pr_number"
step_output pr-url "$pr_url"
