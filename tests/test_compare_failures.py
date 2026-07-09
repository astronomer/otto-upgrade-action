"""compare_failures.py classification: new vs pre-existing vs fixed.

Exit contract mirrors import_check: 3 = at least one NEW failure (fails the
run), 0 = pre-existing-only or clean (passes).
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "compare_failures.py"


def _fail(path, exc="ModuleNotFoundError", msg="ModuleNotFoundError: No module named 'x'"):
    return {"path": path, "exc_class": exc, "msg": msg}


def _run(tmp_path, target_failures, baseline_failures, checked=10):
    t = tmp_path / "target.json"
    b = tmp_path / "baseline.json"
    t.write_text(json.dumps({"checked": checked, "failures": target_failures}))
    b.write_text(json.dumps({"checked": checked, "failures": baseline_failures}))
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(t), str(b)],
        capture_output=True, text=True,
    )


def test_new_failure_exits_three_and_reports(tmp_path):
    r = _run(tmp_path, [_fail("dags/new.py")], [])
    assert r.returncode == 3
    assert "1 NEW import failure" in r.stdout
    assert "dags/new.py" in r.stdout


def test_preexisting_only_passes(tmp_path):
    # Tamara's dbt DAGs: fail identically on both sides -> not upgrade breakage.
    fails = [_fail("dags/dbt_a.py", "TypeError", "TypeError: fspath None"),
             _fail("dags/dbt_b.py", "TypeError", "TypeError: fspath None")]
    r = _run(tmp_path, fails, fails)
    assert r.returncode == 0
    assert "No new import failures" in r.stdout
    assert "2 pre-existing import issue(s)" in r.stdout
    assert "not caused by this upgrade" in r.stdout


def test_mixed_reports_both_and_fails(tmp_path):
    pre = _fail("dags/old.py")
    r = _run(tmp_path, [pre, _fail("dags/new.py")], [pre])
    assert r.returncode == 3
    assert "1 NEW import failure" in r.stdout
    assert "1 pre-existing import issue" in r.stdout


def test_changed_exception_class_stays_preexisting(tmp_path):
    # Same file failing with a different error at the target: root cause almost
    # always predates the upgrade — annotate, don't fail.
    r = _run(tmp_path,
             [_fail("dags/flaky.py", "ImportError", "ImportError: cannot import name 'y'")],
             [_fail("dags/flaky.py", "TypeError", "TypeError: fspath None")])
    assert r.returncode == 0
    assert "error changed at the target version" in r.stdout


def test_fixed_by_upgrade_reported(tmp_path):
    r = _run(tmp_path, [], [_fail("dags/was_broken.py")])
    assert r.returncode == 0
    assert "import cleanly at the target" in r.stdout


def test_clean_both_sides(tmp_path):
    r = _run(tmp_path, [], [], checked=7)
    assert r.returncode == 0
    assert "No new import failures" in r.stdout
    assert "7 file(s) checked" in r.stdout
