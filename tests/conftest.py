"""Make the action's scripts importable as modules for unit tests."""

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}


def git(cwd, *args, env=None, text=False):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          env=env or GIT_ENV, text=text).stdout


def commit(repo, message):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


def snapshot(project, workdir, env=None):
    """What action.yaml's detect step does before the run edits anything."""
    subprocess.run([sys.executable, str(SCRIPTS / "run_changes.py"), "snapshot",
                    str(project), str(workdir)], check=True, env=env or GIT_ENV)
