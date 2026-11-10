"""verify.sh's parse level, end to end, with a fake `astro` and `docker`.

The fake `astro dev parse` does what the real one does after the image build:
loads `.env` and runs `pytest .astro/test_dag_integrity_default.py` in the
directory it was started in (a copy verify.sh prepared, with the result
reporter installed). The stand-in integrity test imports each DAG file the way
DagBag would and fails with the template's message shape. This covers the copy,
probe install, result reading, baseline and comparison without Docker; the
consumer-shapes e2e job covers the real image.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import GIT_ENV, commit, git, snapshot

ACTION = Path(__file__).resolve().parent.parent

FAKE_ASTRO = """#!/usr/bin/env bash
case "$*" in
  "dev parse --help") echo "  --docker   Run in Docker mode" ;;
  "dev parse --docker")
    if [[ -f .env ]]; then set -a; . ./.env; set +a; fi
    exec "$PYTHON" -m pytest .astro/test_dag_integrity_default.py -p no:cacheprovider ;;
  *) echo "fake astro: unexpected $*" >&2; exit 2 ;;
esac
"""

STAND_IN_INTEGRITY_TEST = '''\
import pathlib
import runpy
import traceback

import pytest

FILES = sorted(str(p) for p in pathlib.Path("dags").glob("*.py"))


@pytest.mark.parametrize("rel_path", FILES, ids=FILES)
def test_file_imports(rel_path):
    try:
        runpy.run_path(rel_path)
    except Exception:
        rv = traceback.format_exc()
        raise Exception(f"{rel_path} failed to import with message \\n {rv}")
'''

# Reads the workflow's .env at parse time, and breaks only once the run has
# bumped requirements.txt: an upgrade-only failure in an env-dependent DAG.
ENV_DEPENDENT_DAG = """\
import os
import pathlib

BUCKET = os.environ["BUCKET"]
if "slack==2" in pathlib.Path("requirements.txt").read_text():
    raise TypeError("upgrade-only regression")
"""


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "repo"
    (root / "dags").mkdir(parents=True)
    (root / ".astro").mkdir()
    (root / ".astro" / "config.yaml").write_text("project:\n    name: t\n")
    (root / ".astro" / "test_dag_integrity_default.py").write_text(STAND_IN_INTEGRITY_TEST)
    (root / "dags" / "plain.py").write_text("x = 1\n")
    (root / "requirements.txt").write_text("slack==1\n")
    (root / ".gitignore").write_text(".env\n")
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    commit(root, "init")
    return root


def run_verify(tmp_path, project, workdir, astro_script=FAKE_ASTRO):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("astro", astro_script), ("docker", "#!/bin/sh\nexit 0\n")):
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    plan = workdir / "plan.json"
    plan.write_text(json.dumps({"runtime": {
        "target_tag": "3.3-12", "current_airflow": "3.3.2", "target_airflow": "3.3.2"}}))
    env = {**GIT_ENV, "PATH": f"{bindir}:{os.environ['PATH']}", "PYTHON": sys.executable,
           "PROJECT_PATH": str(project), "PLAN_FILE": str(plan), "VERIFY_LEVEL": "parse",
           "WORKDIR": str(workdir), "ACTION_PATH": str(ACTION)}
    proc = subprocess.run(["bash", str(ACTION / "scripts" / "verify.sh")], env=env,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return {
        "status": (workdir / "verify-status.txt").read_text().strip(),
        "effective": (workdir / "verify-effective-level.txt").read_text().strip(),
        "report": (workdir / "verify-report.md").read_text(),
        "log": proc.stdout + proc.stderr,
    }


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "work").mkdir()
    return tmp_path / "work"


def test_clean_project_passes_at_parse_level(tmp_path, project, workdir):
    snapshot(project, workdir)
    (project / "requirements.txt").write_text("slack==1.5\n")
    out = run_verify(tmp_path, project, workdir)
    assert (out["status"], out["effective"]) == ("passed", "parse"), out["log"]
    assert "All 1 DAG file(s) import cleanly" in out["report"]


def test_project_pytest_addopts_do_not_change_the_verdict(tmp_path, project, workdir):
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["--color=yes", "--no-header", "-q", "--tb=no"]\n')
    commit(project, "addopts")
    snapshot(project, workdir)
    out = run_verify(tmp_path, project, workdir)
    assert (out["status"], out["effective"]) == ("passed", "parse"), out["log"]


def test_upgrade_only_failure_in_env_dependent_dag_is_new(tmp_path, project, workdir):
    (project / "dags" / "env_dag.py").write_text(ENV_DEPENDENT_DAG)
    commit(project, "env dag")
    (project / ".env").write_text("BUCKET=b\n")  # the workflow, before the action
    snapshot(project, workdir)
    (project / "requirements.txt").write_text("slack==2\n")  # the run
    out = run_verify(tmp_path, project, workdir)
    assert (out["status"], out["effective"]) == ("failed", "parse"), out["log"]
    assert "NEW import failure" in out["report"]
    assert "TypeError: upgrade-only regression" in out["report"]


def test_failure_present_before_the_upgrade_is_pre_existing(tmp_path, project, workdir):
    (project / "dags" / "broken.py").write_text("raise KeyError('needs a connection')\n")
    commit(project, "broken dag")
    snapshot(project, workdir)
    (project / "requirements.txt").write_text("slack==1.5\n")
    out = run_verify(tmp_path, project, workdir)
    assert out["status"] == "passed", out["log"]
    assert "pre-existing" in out["report"]


def test_no_result_line_falls_back_and_says_so(tmp_path, project, workdir):
    # A reporter that never loads (here: pytest told to skip conftests) gives
    # no verdict, and verify.sh drops to the import level instead of passing.
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["--noconftest"]\n')
    commit(project, "noconftest")
    snapshot(project, workdir)
    out = run_verify(tmp_path, project, workdir)
    assert "falling back to import-level verification" in out["log"]
    assert out["effective"] != "parse"


def test_project_astro_conftest_stays_in_charge_of_its_options(tmp_path, project, workdir):
    # A project .astro/conftest.py that adds a command-line option its addopts
    # use: it must load as a real conftest, or pytest rejects the option.
    (project / ".astro" / "conftest.py").write_text(
        'def pytest_addoption(parser):\n    parser.addoption("--project-flag", action="store_true")\n')
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["--project-flag"]\n')
    commit(project, "project conftest")
    snapshot(project, workdir)
    out = run_verify(tmp_path, project, workdir)
    assert (out["status"], out["effective"]) == ("passed", "parse"), out["log"]


def test_neither_image_building_reports_no_parse_verdict(tmp_path, project, workdir):
    snapshot(project, workdir)
    build_fails = (
        '#!/usr/bin/env bash\n'
        'if [[ "$*" == "dev parse --help" ]]; then echo "  --docker"; exit 0; fi\n'
        'echo "Error: something went wrong while parsing your Dags: an error was '
        'encountered while building the image, see the build logs for details"; exit 1\n')
    out = run_verify(tmp_path, project, workdir, astro_script=build_fails)
    assert (out["status"], out["effective"]) == ("skipped", "syntax"), out["log"]


@pytest.mark.parametrize("absolute", [True, False], ids=["absolute", "relative"])
def test_env_symlinked_outside_the_project_is_read_not_written(tmp_path, project, workdir,
                                                               absolute):
    (project / "dags" / "env_dag.py").write_text('import os\nBUCKET = os.environ["BUCKET"]\n')
    commit(project, "env dag")
    real_env = project.parent / "ci.env"
    real_env.write_text("BUCKET=b\n")
    # The workflow, before the action: a link above the project.
    (project / ".env").symlink_to(real_env if absolute else "../ci.env")
    snapshot(project, workdir)
    out = run_verify(tmp_path, project, workdir)
    assert (out["status"], out["effective"]) == ("passed", "parse"), out["log"]
    assert real_env.read_text() == "BUCKET=b\n"


def test_incomplete_target_session_is_never_cleared_by_the_baseline(tmp_path, project, workdir):
    # A project plugin stops the session at the first failure, which the
    # baseline shares. The file after it, broken only by the upgrade, never
    # runs; comparing would call the shared failure pre-existing and pass.
    (project / ".astro" / "conftest.py").write_text(
        "def pytest_runtest_makereport(item, call):\n"
        "    if call.excinfo is not None:\n"
        "        item.session.shouldstop = 'project plugin stop'\n")
    (project / "dags" / "a_broken.py").write_text("raise KeyError('needs a connection')\n")
    (project / "dags" / "z_upgrade.py").write_text(
        'import pathlib\nif "slack==2" in pathlib.Path("requirements.txt").read_text():\n'
        '    raise ImportError("moved")\n')
    commit(project, "dags")
    snapshot(project, workdir)
    (project / "requirements.txt").write_text("slack==2\n")
    out = run_verify(tmp_path, project, workdir)
    assert (out["status"], out["effective"]) == ("failed", "parse"), out["log"]
    assert out["report"].startswith("⚠️ **The DAG integrity run was incomplete**")
    assert "Image parse (baseline" not in out["log"]  # no baseline built for it
