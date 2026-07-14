"""deprecation_cleanup.py sweeps deprecated Airflow usage via pinned ruff.

The ruff invocation is stubbed with canned diagnostic JSON; the fixed-count
arithmetic, mode demotion, and failure paths are what's under test.
"""

import contextlib
import io
import json

import deprecation_cleanup as dc
import pytest


def _diag(code, filename, row, message="deprecated"):
    return {"code": code, "filename": filename, "message": message,
            "location": {"row": row, "column": 1}}


FOUND = [
    _diag("AIR312", "/proj/dags/a.py", 3, "`EmptyOperator` moved to `standard` provider"),
    _diag("AIR311", "/proj/dags/a.py", 5, "`airflow.models.DAG` is removed in Airflow 3.0"),
    _diag("AIR301", "/proj/dags/a.py", 7, "`days_ago` is removed in Airflow 3.0"),
    _diag("AIR301", "/proj/dags/b.py", 2, "`days_ago` is removed in Airflow 3.0"),
]
# After the fix pass: the two AIR301s survive (no autofix); rows shifted by
# the applied rewrites — matching must not key on line numbers.
REMAINING = [
    _diag("AIR301", "/proj/dags/a.py", 6, "`days_ago` is removed in Airflow 3.0"),
    _diag("AIR301", "/proj/dags/b.py", 2, "`days_ago` is removed in Airflow 3.0"),
]


def _run(tmp_path, monkeypatch, mode, ruff_results, plan=None, project="/proj"):
    """ruff_results: list of (rc, stdout, stderr) consumed per _ruff call."""
    if plan is None:
        plan = {"runtime": {"target_airflow": "3.3.1"}}
    plan_f = tmp_path / "plan.json"
    plan_f.write_text(json.dumps(plan))
    monkeypatch.setenv("PLAN_FILE", str(plan_f))
    monkeypatch.setenv("PROJECT_PATH", project)
    monkeypatch.setenv("DEPRECATION_MODE", mode)
    calls = iter(ruff_results)
    seen_fix_flags = []

    def fake_ruff(_path, *, fix):
        seen_fix_flags.append(fix)
        return next(calls)

    monkeypatch.setattr(dc, "_ruff", fake_ruff)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert dc.main() == 0
    return json.loads(buf.getvalue()), seen_fix_flags


def test_fix_mode_counts_fixed_and_groups_remaining(tmp_path, monkeypatch):
    summary, flags = _run(
        tmp_path, monkeypatch, "fix",
        [(1, json.dumps(FOUND), ""), (1, json.dumps(REMAINING), "")])
    assert flags == [False, True]  # scan first, then the fix pass
    assert summary["status"] == "ok"
    assert summary["found"] == 4
    assert summary["fixed"] == 2
    assert summary["files_changed"] == ["dags/a.py"]
    assert len(summary["remaining"]) == 1
    grp = summary["remaining"][0]
    assert grp["rule"] == "AIR301" and grp["count"] == 2
    assert "dags/a.py:6" in grp["locations"] and "dags/b.py:2" in grp["locations"]


def test_advisory_mode_never_runs_fix_pass(tmp_path, monkeypatch):
    summary, flags = _run(
        tmp_path, monkeypatch, "advisory", [(1, json.dumps(FOUND), "")])
    assert flags == [False]
    assert summary["fixed"] == 0
    assert summary["found"] == 4
    assert sum(g["count"] for g in summary["remaining"]) == 4


@pytest.mark.parametrize("plan", [
    {"runtime": {"target_airflow": "2.10.5"}},
    {"runtime": None},
    {},
])
def test_fix_demotes_to_advisory_when_target_airflow_not_3(tmp_path, monkeypatch, plan):
    summary, flags = _run(
        tmp_path, monkeypatch, "fix", [(1, json.dumps(FOUND), "")], plan=plan)
    assert flags == [False]  # no fix pass ran
    assert summary["mode"] == "advisory"
    assert "Airflow 3 forms" in summary["demoted"]
    assert summary["fixed"] == 0


def test_clean_project_reports_ok_zero(tmp_path, monkeypatch):
    summary, flags = _run(tmp_path, monkeypatch, "fix", [(0, "[]", "")])
    assert flags == [False]  # nothing found -> no fix pass
    assert summary["status"] == "ok"
    assert summary["found"] == 0 and summary["fixed"] == 0
    assert summary["remaining"] == []


def test_ruff_unavailable_is_loud_and_exits_zero(tmp_path, monkeypatch):
    summary, _ = _run(
        tmp_path, monkeypatch, "fix", [(127, "", "uvx: command not found")])
    assert summary["status"] == "unavailable"
    assert "command not found" in summary["reason"]


def test_fix_pass_failure_is_loud_not_silent(tmp_path, monkeypatch):
    summary, _ = _run(
        tmp_path, monkeypatch, "fix",
        [(1, json.dumps(FOUND), ""), (2, "", "ruff panicked")])
    assert summary["status"] == "unavailable"
    assert "--fix run failed" in summary["reason"]


def test_unparseable_ruff_output_is_loud(tmp_path, monkeypatch):
    summary, _ = _run(tmp_path, monkeypatch, "advisory", [(1, "not json", "")])
    assert summary["status"] == "unavailable"
    assert "unparseable" in summary["reason"]


def test_locations_cap_at_five_with_count_intact(tmp_path, monkeypatch):
    many = [_diag("AIR301", f"/proj/dags/f{i}.py", 1) for i in range(8)]
    summary, _ = _run(tmp_path, monkeypatch, "advisory", [(1, json.dumps(many), "")])
    grp = summary["remaining"][0]
    assert grp["count"] == 8
    assert len(grp["locations"]) == 5
