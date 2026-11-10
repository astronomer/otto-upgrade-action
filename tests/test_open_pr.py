"""open-pr.sh against a real git remote and a recording fake `gh`.

The script pushes to https://x-access-token:...@github.com/<repo>.git; a
url.insteadOf rule in an isolated git config sends that to a local bare repo,
so commits, fetches and force-pushes are real.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from conftest import git as _git
from conftest import snapshot


def git(cwd, *args, env=None):
    return _git(cwd, *args, env=env, text=True)

ACTION = Path(__file__).resolve().parent.parent
REPO = "example/fixture"

FAKE_GH = """#!/usr/bin/env bash
# Records every call; answers from $FAKE_GH_DIR.
d="$FAKE_GH_DIR"; echo "$*" >> "$d/calls.log"
case "$1 $2" in
  "pr list") cat "$d/existing" 2>/dev/null ;;
  "pr view")
    if [[ "$*" == *"title,body"* ]]; then cat "$d/current" 2>/dev/null
    elif [[ "$*" == *number* ]]; then echo 7
    else echo "https://github.com/example/fixture/pull/7"; fi ;;
  "pr edit")
    for ((i=1; i<=$#; i++)); do
      if [[ "${!i}" == "--body-file" ]]; then j=$((i+1)); cp "${!j}" "$d/edited-body"; fi
    done ;;
  "pr create") echo "https://github.com/example/fixture/pull/7" ;;
  "repo view") echo main ;;
esac
"""


@pytest.fixture
def world(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        f'[url "{remote}"]\n\tinsteadOf = https://x-access-token:tok@github.com/{REPO}.git\n'
        "[commit]\n\tgpgsign = false\n[init]\n\tdefaultBranch = main\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(FAKE_GH)
    (bindir / "gh").chmod(0o755)
    ghdir = tmp_path / "gh"
    ghdir.mkdir()
    work = tmp_path / "work"
    workdir = tmp_path / "otto"
    workdir.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1",
        "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_GH_DIR": str(ghdir),
        "GIT_AUTHOR_NAME": "bot", "GIT_AUTHOR_EMAIL": "bot@example.invalid",
        "GIT_COMMITTER_NAME": "bot", "GIT_COMMITTER_EMAIL": "bot@example.invalid",
    }
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, env=env,
                   capture_output=True)
    (work / "dags").mkdir()
    (work / "dags" / "a.py").write_text("x = 1\n")
    (work / "requirements.txt").write_text("apache-airflow-providers-slack==9.10.2\n")
    git(work, "add", "-A", env=env)
    git(work, "commit", "-qm", "init", env=env)
    git(work, "push", "-q", "origin", "main", env=env)
    plan = workdir / "plan.json"
    plan.write_text(json.dumps({
        "runtime": {"current_tag": "3.3-10", "target_tag": "3.3-12", "tier": "patch"},
        "providers": [], "overall_tier": "patch"}))
    return {"work": work, "workdir": workdir, "env": env, "ghdir": ghdir, "remote": remote,
            "plan": plan}


def next_run_with_open_pr(w, current_description):
    """A later scheduled run: same target, PR #7 open with this description."""
    git(w["work"], "checkout", "-q", "main", env=w["env"])
    (w["work"] / "requirements.txt").write_text("apache-airflow-providers-slack==9.11.1\n")
    (w["ghdir"] / "existing").write_text("7\n")
    (w["ghdir"] / "current").write_text(current_description)


def run_open_pr(w):
    verify = w["workdir"] / "verify-report.md"
    if not verify.exists():
        verify.write_text("✅ ok\n")
    env = {**w["env"], "GITHUB_REPOSITORY": REPO, "PROJECT_PATH": ".",
           "PLAN_FILE": str(w["plan"]), "ACTION_PATH": str(ACTION),
           "WORKDIR": str(w["workdir"]), "GH_TOKEN": "tok", "DRY_RUN": "false",
           "VERIFY_FILE": str(verify), "GITHUB_OUTPUT": str(w["workdir"] / "out.txt")}
    proc = subprocess.run(["bash", str(ACTION / "scripts" / "open-pr.sh")], cwd=w["work"],
                          env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def remote_files(w):
    return git(w["remote"], "ls-tree", "-r", "--name-only", "otto/airflow-upgrade").split()


def test_untracked_files_present_before_the_run_are_not_committed(world):
    w = world
    (w["work"] / "netrc").write_text("machine github.com password s3cret\n")
    snapshot(w["work"], w["workdir"], env=w["env"])
    # The run itself: a bump, plus a new file (Otto can add files).
    (w["work"] / "requirements.txt").write_text("apache-airflow-providers-slack==9.11.1\n")
    (w["work"] / "dags" / "migrated_helper.py").write_text("y = 2\n")
    run_open_pr(w)
    files = remote_files(w)
    assert "netrc" not in files
    assert "dags/migrated_helper.py" in files
    assert "s3cret" not in git(w["remote"], "show", "otto/airflow-upgrade:requirements.txt")


def test_unchanged_tree_still_refreshes_the_open_pr_description(world):
    w = world
    snapshot(w["work"], w["workdir"], env=w["env"])
    (w["work"] / "requirements.txt").write_text("apache-airflow-providers-slack==9.11.1\n")
    run_open_pr(w)
    first_head = git(w["remote"], "rev-parse", "otto/airflow-upgrade")
    # Next run: same target, but verification now says something different.
    next_run_with_open_pr(w, "old title\nold body\n")
    (w["workdir"] / "verify-report.md").write_text("❌ 1 NEW import failure\n")
    out = run_open_pr(w)
    assert git(w["remote"], "rev-parse", "otto/airflow-upgrade") == first_head  # no push
    assert "refreshing its description" in out
    assert "1 NEW import failure" in (w["ghdir"] / "edited-body").read_text()


def test_identical_description_is_not_re_edited(world):
    w = world
    snapshot(w["work"], w["workdir"], env=w["env"])
    (w["work"] / "requirements.txt").write_text("apache-airflow-providers-slack==9.11.1\n")
    run_open_pr(w)
    body = (w["workdir"] / "pr-body.md").read_text()
    next_run_with_open_pr(w, "Upgrade Airflow Runtime to 3.3-12\n" + body)
    out = run_open_pr(w)
    assert "already says this" in out
    assert not (w["ghdir"] / "edited-body").exists()
