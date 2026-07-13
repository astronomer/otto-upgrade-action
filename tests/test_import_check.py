"""import_check.py: Airflow-faithful discovery + the exit-code contract.

Exit codes: 0 = all import, 3 = a genuine import error. The distinct 3 (not 1)
is what lets verify.sh tell a real DAG import failure apart from `uv` failing
to build the target env (exit 1/2) — only the former should red the run.

Fixtures never import real Airflow: the safe-mode heuristic reads file CONTENT,
so plain-python files carrying the right keywords exercise discovery cheaply.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_check.py"

# Passes the safe-mode heuristic (mentions airflow + dag) without importing anything.
DAG_HEADER = "# airflow dag fixture\n"


def _write(project: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return project


def _run(project: Path, *extra: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    args = [sys.executable, str(SCRIPT), "--project-root", str(project)]
    if (project / "dags").is_dir():
        args += ["--dags-root", str(project / "dags")]
    if (project / "plugins").is_dir():
        args += ["--plugins-root", str(project / "plugins")]
    args += list(extra)
    env = dict(os.environ, **(env_extra or {}))
    return subprocess.run(args, capture_output=True, text=True, env=env)


def test_clean_dag_exits_zero(tmp_path):
    _write(tmp_path, {"dags/ok.py": DAG_HEADER + "x = 1 + 1\n"})
    assert _run(tmp_path).returncode == 0


def test_broken_import_exits_three(tmp_path):
    _write(tmp_path, {"dags/bad.py": DAG_HEADER + "import a_module_that_does_not_exist_xyz\n"})
    assert _run(tmp_path).returncode == 3


def test_runtime_error_at_import_exits_three(tmp_path):
    # A NameError at module top level is the moved/removed-symbol failure mode.
    _write(tmp_path, {"dags/bad.py": DAG_HEADER + "Operator = SomeRemovedSymbol\n"})
    assert _run(tmp_path).returncode == 3


def test_non_dag_helper_in_dags_is_skipped(tmp_path):
    # No airflow/dag/asset keywords -> Airflow's safe mode never parses it, so
    # neither do we, even though importing it standalone would fail.
    _write(tmp_path, {"dags/helper.py": "import a_module_that_does_not_exist_xyz\n"})
    assert _run(tmp_path).returncode == 0


def test_asset_only_dag_is_checked(tmp_path):
    # Airflow 3 discovers files mentioning airflow+asset with no 'dag' token —
    # they must not be exempted from verification.
    _write(tmp_path, {"dags/pipeline.py": "# airflow asset pipeline\nimport missing_module_xyz\n"})
    assert _run(tmp_path).returncode == 3


def test_include_is_never_imported(tmp_path):
    # Tamara's false positives: helper scripts under include/ that only work
    # with a specific cwd/sys.path. Airflow never imports include/ directly.
    _write(tmp_path, {
        "dags/ok.py": DAG_HEADER + "x = 1\n",
        "include/scripts/utils.py": DAG_HEADER + "import config_that_does_not_exist\n",
        "include/blueprints/__init__.py": DAG_HEADER + "from .missing import x\n",
    })
    assert _run(tmp_path).returncode == 0


def test_package_init_with_relative_import_works(tmp_path):
    # sys.modules registration makes relative imports inside dag packages
    # resolve — previously a false "No module named 'dagcheck_...'" failure.
    _write(tmp_path, {
        "dags/pkg/__init__.py": DAG_HEADER + "from .helper import VALUE\n",
        "dags/pkg/helper.py": "VALUE = 1\n",
    })
    assert _run(tmp_path).returncode == 0


def test_dag_importing_include_via_project_root(tmp_path):
    # Production layout: project root is on sys.path (Runtime PYTHONPATH), so
    # DAGs import include.* as a package. The checker must mirror that.
    _write(tmp_path, {
        "dags/uses_include.py": DAG_HEADER + "from include.helpers import VALUE\n",
        "include/helpers.py": "VALUE = 1\n",
    })
    assert _run(tmp_path).returncode == 0


def test_airflowignore_glob_skips_file(tmp_path):
    _write(tmp_path, {
        "dags/.airflowignore": "ignored_*.py\n",
        "dags/ignored_bad.py": DAG_HEADER + "import missing_module_xyz\n",
        "dags/ok.py": DAG_HEADER + "x = 1\n",
    })
    assert _run(tmp_path).returncode == 0


def test_airflowignore_regexp_syntax(tmp_path):
    _write(tmp_path, {
        "dags/.airflowignore": "^legacy\n",
        "dags/legacy_bad.py": DAG_HEADER + "import missing_module_xyz\n",
    })
    assert _run(tmp_path, "--ignore-syntax", "regexp").returncode == 0
    # Same tree under glob syntax: '^legacy' matches nothing -> failure surfaces.
    assert _run(tmp_path, "--ignore-syntax", "glob").returncode == 3


def test_plugins_imported_without_heuristic(tmp_path):
    # Airflow's plugin manager imports every module under plugins/ at startup;
    # no dag/airflow keywords required for the failure to be real.
    _write(tmp_path, {"plugins/broken.py": "import missing_plugin_dep_xyz\n"})
    assert _run(tmp_path).returncode == 3


def test_json_output_relative_paths(tmp_path):
    _write(tmp_path, {"dags/bad.py": DAG_HEADER + "import a_module_that_does_not_exist_xyz\n"})
    out = tmp_path / "failures.json"
    _run(tmp_path, env_extra={"IMPORT_JSON": str(out)})
    data = json.loads(out.read_text())
    assert data["checked"] == 1
    (failure,) = data["failures"]
    assert failure["path"] == os.path.join("dags", "bad.py")  # project-root-relative
    assert failure["exc_class"] == "ModuleNotFoundError"


def test_module_state_isolated_between_files(tmp_path):
    # One dag imports a project-local module and mutates it; the next dag must
    # see a fresh copy (project-local modules are evicted between files).
    _write(tmp_path, {
        "dags/shared.py": "STATE = []\n",
        "dags/a_first.py": DAG_HEADER + "import shared\nshared.STATE.append(1)\n",
        "dags/b_second.py": DAG_HEADER + "import shared\nassert shared.STATE == []\n",
    })
    assert _run(tmp_path).returncode == 0
