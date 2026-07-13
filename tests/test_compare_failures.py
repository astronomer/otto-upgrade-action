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


def test_changed_class_to_import_family_escalates_to_new(tmp_path):
    # A file that failed for an unrelated env reason at the current version but
    # hits a moved/removed import at the target IS upgrade breakage — the
    # pre-existing failure must not mask it.
    r = _run(tmp_path,
             [_fail("dags/etl.py", "ModuleNotFoundError",
                    "ModuleNotFoundError: No module named 'airflow.providers.foo'")],
             [_fail("dags/etl.py", "OperationalError", "OperationalError: db unreachable")])
    assert r.returncode == 3
    assert "1 NEW import failure" in r.stdout
    assert "this import failure is new" in r.stdout


def test_same_class_different_symbol_escalates(tmp_path):
    # Baseline already fails on a missing 'Foo'; the upgrade breaks 'Bar' in
    # the same file with the same exception class. The new break must not hide
    # behind the old one.
    r = _run(tmp_path,
             [_fail("dags/etl.py", "ImportError",
                    "ImportError: cannot import name 'Bar' from 'pkg' (py3.14/pkg/__init__.py)")],
             [_fail("dags/etl.py", "ImportError",
                    "ImportError: cannot import name 'Foo' from 'pkg' (py3.13/pkg/__init__.py)")])
    assert r.returncode == 3
    assert "1 NEW import failure" in r.stdout


def test_same_class_same_symbol_with_path_noise_stays_preexisting(tmp_path):
    # The same missing module reported from two different image paths (python
    # 3.13 vs 3.14) is one pre-existing root cause, not a new break.
    r = _run(tmp_path,
             [_fail("dags/etl.py", "ModuleNotFoundError",
                    "ModuleNotFoundError: No module named 'config'")],
             [_fail("dags/etl.py", "ModuleNotFoundError",
                    "ModuleNotFoundError: No module named 'config'")])
    assert r.returncode == 0
    assert "pre-existing" in r.stdout


def test_attribute_error_change_stays_preexisting(tmp_path):
    # AttributeError is overwhelmingly env-dependent parse-time breakage, not
    # a moved-import signal — escalating it re-reds the both-sides-broken
    # class this comparison exists to suppress.
    r = _run(tmp_path,
             [_fail("dags/flaky.py", "AttributeError",
                    "AttributeError: 'NoneType' object has no attribute 'get'")],
             [_fail("dags/flaky.py", "OperationalError", "OperationalError: db unreachable")])
    assert r.returncode == 0
    assert "error changed at the target version" in r.stdout


def test_changed_non_import_class_stays_preexisting(tmp_path):
    # Env-dependent flapping (a TypeError becoming a ValueError) still predates
    # the upgrade — annotate, don't fail.
    r = _run(tmp_path,
             [_fail("dags/flaky.py", "ValueError", "ValueError: bad profile")],
             [_fail("dags/flaky.py", "TypeError", "TypeError: fspath None")])
    assert r.returncode == 0
    assert "error changed at the target version" in r.stdout


def test_dunder_messages_render_as_code_spans(tmp_path):
    # `__init__` in a raw message renders as bold on GitHub; the report must
    # code-span messages (and neutralize inner backticks).
    msg = "ImportError: cannot import name 'x' from 'pkg' (pkg/__init__.py) `hint`"
    r = _run(tmp_path, [_fail("dags/a.py", "ImportError", msg)], [])
    assert "`ImportError: cannot import name" in r.stdout
    assert "__init__" in r.stdout
    assert "`hint`" not in r.stdout  # inner backticks downgraded


def test_internal_error_fails_closed(tmp_path):
    # Malformed input must exit 3 (we're only invoked when the target run found
    # real failures) — never crash to an exit code the caller reads as a pass.
    t = tmp_path / "target.json"
    b = tmp_path / "baseline.json"
    t.write_text("{not json")
    b.write_text("{}")
    r = subprocess.run([sys.executable, str(SCRIPT), str(t), str(b)],
                       capture_output=True, text=True)
    assert r.returncode == 3
    assert "Baseline comparison error" in r.stdout


def test_fixed_by_upgrade_reported(tmp_path):
    r = _run(tmp_path, [], [_fail("dags/was_broken.py")])
    assert r.returncode == 0
    assert "import cleanly at the target" in r.stdout


def test_clean_both_sides(tmp_path):
    r = _run(tmp_path, [], [], checked=7)
    assert r.returncode == 0
    assert "No new import failures" in r.stdout
    assert "7 file(s) checked" in r.stdout
