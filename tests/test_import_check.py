"""import_check.py exit codes: 0 = all import, 3 = a genuine import error.

The distinct 3 (not 1) is what lets verify.sh tell a real DAG import failure
apart from `uv` failing to build the target env (exit 1/2) — only the former
should red the run.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_check.py"


def _run(target_dir: Path) -> int:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target_dir)],
        capture_output=True, text=True,
    ).returncode


def test_clean_dag_exits_zero(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1 + 1\n")
    assert _run(tmp_path) == 0


def test_broken_import_exits_three(tmp_path):
    (tmp_path / "bad.py").write_text("import a_module_that_does_not_exist_xyz\n")
    assert _run(tmp_path) == 3


def test_runtime_error_at_import_exits_three(tmp_path):
    # A NameError at module top level is the moved/removed-symbol failure mode.
    (tmp_path / "bad.py").write_text("Operator = SomeRemovedSymbol\n")
    assert _run(tmp_path) == 3
