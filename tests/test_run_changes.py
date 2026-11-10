"""run_changes.py: what the run changed, against a pre-run snapshot."""

import shutil
import subprocess
import sys

import pytest
import run_changes
from conftest import GIT_ENV, SCRIPTS, commit, git, snapshot


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A repo whose Astro project lives in airflow/ (project-path: airflow),
    snapshotted after the consumer's workflow ran and before the action edits."""
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    root = tmp_path / "repo"
    proj = root / "airflow"
    (proj / "dags").mkdir(parents=True)
    (proj / "dags" / "a.py").write_text("before\n")
    (proj / "dags" / "doomed.py").write_text("tracked, deleted by the run\n")
    (proj / "requirements.txt").write_text("slack==1\n")
    (proj / "Dockerfile").write_text("FROM runtime:3.3-10\n")
    (proj / ".gitignore").write_text(".env\n")
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    commit(root, "init")
    # The consumer's workflow, before the action.
    (proj / ".env").write_text("BUCKET=x\n")
    (proj / "netrc").write_text("machine github.com password s3cret\n")
    (proj / "Dockerfile").write_text("FROM runtime:3.3-10\nENV FROM_WORKFLOW=1\n")
    (proj / "dags" / "__pycache__").mkdir()  # a pytest run in the workflow
    (proj / "dags" / "__pycache__" / "a.pyc").write_bytes(b"\0")
    workdir = tmp_path / "work"
    workdir.mkdir()
    snapshot(proj, workdir)
    # The run.
    (proj / "dags" / "a.py").write_text("after\n")
    (proj / "requirements.txt").write_text("slack==2\n")
    (proj / "dags" / "doomed.py").unlink()
    (proj / "dags" / "new_by_otto.py").write_text("added by the run\n")
    (proj / "dags" / "__pycache__" / "a.pyc").unlink()  # bytecode removed during the run
    return proj, workdir


def test_changed_lists_exactly_the_runs_edits(project):
    proj, workdir = project
    # Bytecode is never the run's change, deleted or not.
    assert sorted(run_changes.changed(str(proj), str(workdir))) == [
        "dags/a.py", "dags/doomed.py", "dags/new_by_otto.py", "requirements.txt"]


def test_revert_rebuilds_the_project_as_it_was_before_the_run(project, tmp_path):
    proj, workdir = project
    copy = tmp_path / "baseline"
    shutil.copytree(proj, copy)
    run_changes.revert(str(proj), str(workdir), str(copy))
    assert (copy / "dags" / "a.py").read_text() == "before\n"
    assert (copy / "requirements.txt").read_text() == "slack==1\n"
    assert (copy / "dags" / "doomed.py").read_text() == "tracked, deleted by the run\n"
    assert not (copy / "dags" / "new_by_otto.py").exists()
    # What the workflow put there stays: the baseline must see the same
    # environment as the target, or an environment difference reads as an
    # upgrade difference.
    assert (copy / ".env").read_text() == "BUCKET=x\n"
    assert (copy / "netrc").exists()
    assert "FROM_WORKFLOW" in (copy / "Dockerfile").read_text()


def test_without_a_snapshot_head_is_the_reference(project):
    proj, workdir = project
    (workdir / "pre-tree").unlink()
    changed = run_changes.changed(str(proj), str(workdir))
    assert "Dockerfile" in changed and "netrc" in changed


def test_cli_exits_2_outside_a_git_work_tree(tmp_path):
    (tmp_path / "proj").mkdir()
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "run_changes.py"), "diff", str(tmp_path / "proj"),
         str(tmp_path)], capture_output=True, env=GIT_ENV, check=False)
    assert proc.returncode == 2


def test_stage_skips_untracked_files_the_run_removed(project):
    proj, workdir = project
    run_changes.stage(str(proj), str(workdir))
    staged = git(proj, "diff", "--cached", "--name-only", "--relative").decode().split()
    assert sorted(staged) == ["dags/a.py", "dags/doomed.py", "dags/new_by_otto.py",
                              "requirements.txt"]


def test_stage_reads_paths_literally(project):
    proj, workdir = project
    (proj / "dags" / "ab.py").write_text("pre-existing, untracked\n")
    snapshot(proj, workdir)
    (proj / "dags" / "a[b].py").write_text("added by the run\n")
    run_changes.stage(str(proj), str(workdir))
    staged = git(proj, "diff", "--cached", "--name-only", "--relative").decode().split()
    assert "dags/a[b].py" in staged
    assert "dags/ab.py" not in staged


def test_revert_keeps_file_modes_and_symlinks(project, tmp_path):
    proj, workdir = project
    script = proj / "include" / "setup.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho before\n")
    script.chmod(0o755)
    (proj / "include" / "current").symlink_to("setup.sh")
    commit(proj, "script")
    snapshot(proj, workdir)
    script.write_text("#!/bin/sh\necho after\n")
    (proj / "include" / "current").unlink()
    copy = tmp_path / "baseline"
    shutil.copytree(proj, copy, symlinks=True)
    run_changes.revert(str(proj), str(workdir), str(copy))
    restored = copy / "include" / "setup.sh"
    assert restored.read_text().endswith("before\n")
    assert restored.stat().st_mode & 0o111
    assert (copy / "include" / "current").is_symlink()


def test_revert_ignores_archive_only_attributes(project, tmp_path):
    proj, workdir = project
    (proj / ".gitattributes").write_text("requirements.txt export-ignore\n")
    commit(proj, "attrs")
    snapshot(proj, workdir)
    (proj / "requirements.txt").write_text("slack==3\n")
    copy = tmp_path / "baseline"
    shutil.copytree(proj, copy)
    run_changes.revert(str(proj), str(workdir), str(copy))
    assert (copy / "requirements.txt").read_text() == "slack==2\n"


def test_revert_of_a_module_turned_package_leaves_no_empty_package(project, tmp_path):
    proj, workdir = project
    (proj / "include").mkdir()
    (proj / "include" / "utils.py").write_text("X = 1\n")
    commit(proj, "utils")
    snapshot(proj, workdir)
    (proj / "include" / "utils.py").unlink()
    (proj / "include" / "utils").mkdir()
    (proj / "include" / "utils" / "__init__.py").write_text("X = 2\n")
    copy = tmp_path / "baseline"
    shutil.copytree(proj, copy)
    run_changes.revert(str(proj), str(workdir), str(copy))
    assert (copy / "include" / "utils.py").read_text() == "X = 1\n"
    assert not (copy / "include" / "utils").exists()
