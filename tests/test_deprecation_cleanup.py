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


def _run(tmp_path, monkeypatch, mode, ruff_results, plan=None, project="/proj",
         verify_level="parse", dirty=None):
    """ruff_results: list of (rc, stdout, stderr) consumed per _ruff call.
    dirty: list of git-dirty-file sets consumed per _dirty_files call
    (default: git unavailable -> Counter-based files_changed fallback)."""
    if plan is None:
        plan = {"runtime": {"target_airflow": "3.3.1"}}
    plan_f = tmp_path / "plan.json"
    plan_f.write_text(json.dumps(plan))
    monkeypatch.setenv("PLAN_FILE", str(plan_f))
    monkeypatch.setenv("PROJECT_PATH", project)
    monkeypatch.setenv("DEPRECATION_MODE", mode)
    monkeypatch.setenv("VERIFY_LEVEL", verify_level)
    monkeypatch.setattr(dc, "_scan_paths", lambda p: [f"{p}/dags"])
    dirty_calls = iter(dirty if dirty is not None else [])

    def fake_dirty(_p):
        return next(dirty_calls, None) if dirty is not None else None

    monkeypatch.setattr(dc, "_dirty_files", fake_dirty)
    calls = iter(ruff_results)
    seen_fix_flags = []

    def fake_ruff(_paths, *, fix):
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
    canary_hit = json.dumps([_diag("AIR301", "/tmp/otto_air3_canary.py", 2)])
    summary, flags = _run(tmp_path, monkeypatch, "fix",
                          [(0, "[]", ""), (1, canary_hit, "")])
    assert flags == [False, False]  # scan + canary, never a fix pass
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


@pytest.mark.parametrize("level", ["syntax", "none", ""])
def test_fix_demotes_when_verification_would_not_gate_rewrites(tmp_path, monkeypatch, level):
    summary, flags = _run(
        tmp_path, monkeypatch, "fix", [(1, json.dumps(FOUND), "")],
        verify_level=level)
    assert flags == [False]
    assert summary["mode"] == "advisory"
    assert "verify-level" in summary["demoted"]
    assert summary["fixed"] == 0


def test_malformed_fix_pass_output_is_loud_not_ok_zero(tmp_path, monkeypatch):
    # The fix pass already mutated files; falling back to "ok, 0 fixed"
    # would hide those edits from the PR summary.
    summary, _ = _run(
        tmp_path, monkeypatch, "fix",
        [(1, json.dumps(FOUND), ""), (1, "garbage{", "")])
    assert summary["status"] == "unavailable"
    assert "rewrites may be in the diff" in summary["reason"]


def test_files_changed_prefers_git_snapshot_over_diagnostic_delta(tmp_path, monkeypatch):
    # Otto already dirtied dags/otto.py before this step; only the fix-pass
    # delta belongs to the sweep.
    summary, _ = _run(
        tmp_path, monkeypatch, "fix",
        [(1, json.dumps(FOUND), ""), (1, json.dumps(REMAINING), "")],
        dirty=[{"dags/otto.py"}, {"dags/otto.py", "dags/a.py", "dags/c.py"}])
    assert summary["files_changed"] == ["dags/a.py", "dags/c.py"]
    assert summary["fixed"] == 2  # count still from the diagnostic delta


def test_no_scan_roots_reports_ok_zero(tmp_path, monkeypatch):
    plan_f = tmp_path / "plan.json"
    plan_f.write_text(json.dumps({"runtime": {"target_airflow": "3.3.1"}}))
    monkeypatch.setenv("PLAN_FILE", str(plan_f))
    monkeypatch.setenv("PROJECT_PATH", str(tmp_path))  # no dags/plugins/include
    monkeypatch.setenv("DEPRECATION_MODE", "fix")
    monkeypatch.setenv("VERIFY_LEVEL", "parse")
    monkeypatch.setattr(dc, "_ruff",
                        lambda *_a, **_k: pytest.fail("ruff must not run"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert dc.main() == 0
    summary = json.loads(buf.getvalue())
    assert summary["status"] == "ok" and summary["found"] == 0
    assert summary["scanned"] == []


def test_scan_paths_only_existing_verification_roots(tmp_path):
    (tmp_path / "dags").mkdir()
    (tmp_path / "include").mkdir()
    (tmp_path / "vendored").mkdir()  # never scanned
    paths = dc._scan_paths(str(tmp_path))
    assert paths == [str(tmp_path / "dags"), str(tmp_path / "include")]


@pytest.mark.parametrize("plan,expect_fix", [
    # Digest-pinned runtime: target_airflow absent, current_airflow rules.
    ({"runtime": {"current_airflow": "2.10.5"}}, False),
    ({"runtime": {"current_airflow": "3.0.1"}}, True),
])
def test_current_airflow_fallback_drives_demotion(tmp_path, monkeypatch, plan, expect_fix):
    results = [(1, json.dumps(FOUND), "")]
    if expect_fix:
        results.append((1, json.dumps(REMAINING), ""))
    summary, flags = _run(tmp_path, monkeypatch, "fix", results, plan=plan)
    assert (True in flags) is expect_fix
    assert summary["mode"] == ("fix" if expect_fix else "advisory")


def test_ruff_timeout_becomes_failure_tuple_not_crash(monkeypatch):
    import subprocess as sp

    def boom(*_a, **_k):
        raise sp.TimeoutExpired(cmd="ruff", timeout=300)
    monkeypatch.setattr(dc.subprocess, "run", boom)
    rc, out, err = dc._ruff(["/x"], fix=False)
    assert rc == -1 and out == "" and "TimeoutExpired" in err


def test_missing_uvx_becomes_failure_tuple_not_crash(monkeypatch):
    def boom(*_a, **_k):
        raise FileNotFoundError("uvx")
    monkeypatch.setattr(dc.subprocess, "run", boom)
    rc, _out, err = dc._ruff(["/x"], fix=False)
    assert rc == -1 and "FileNotFoundError" in err


def test_zero_findings_with_dead_canary_refuses_clean_verdict(tmp_path, monkeypatch):
    summary, flags = _run(
        tmp_path, monkeypatch, "fix",
        [(0, "[]", ""), (0, "[]", "")])  # scan clean, canary ALSO silent
    assert flags == [False, False]
    assert summary["status"] == "unavailable"
    assert "canary" in summary["reason"]


def test_unexpected_exception_reports_unavailable_not_crash(tmp_path, monkeypatch):
    # ruff emitting a JSON object instead of an array must not abort the
    # action after Otto already ran.
    summary, _ = _run(tmp_path, monkeypatch, "advisory",
                      [(1, json.dumps({"not": "an array"}), "")])
    assert summary["status"] == "unavailable"
    assert "unexpected error" in summary["reason"]


class TestImportMerge:
    def _merge(self, tmp_path, text, before):
        f = tmp_path / "x.py"
        # Bytes in/out: Path.write_text/read_text translate newlines, which
        # would hide a CRLF-mangling bug.
        f.write_bytes(text.encode())
        changed = dc._merge_adjacent_from_imports(str(f), before)
        return changed, f.read_bytes().decode()

    def test_fixer_introduced_pair_merges(self, tmp_path):
        changed, out = self._merge(
            tmp_path,
            "from airflow.sdk import dag\nfrom airflow.sdk import task\n",
            before="from airflow.decorators import dag, task\n")
        assert changed
        assert out == "from airflow.sdk import dag, task\n"

    def test_triple_run_merges_into_one_line(self, tmp_path):
        changed, out = self._merge(
            tmp_path,
            "from airflow.sdk import dag\nfrom airflow.sdk import task\n"
            "from airflow.sdk import chain\n",
            before="")
        assert changed
        assert out == "from airflow.sdk import dag, task, chain\n"

    def test_user_authored_pair_left_alone(self, tmp_path):
        text = "from x import a\nfrom x import b\n"
        changed, out = self._merge(tmp_path, text, before=text)
        assert not changed
        assert out == text

    def test_pair_with_one_preexisting_line_merges(self, tmp_path):
        # Ruff inserted `task` next to the user's existing sdk import.
        changed, out = self._merge(
            tmp_path,
            "from airflow.sdk import dag\nfrom airflow.sdk import task\n",
            before="from airflow.sdk import dag\n")
        assert changed
        assert out == "from airflow.sdk import dag, task\n"

    @pytest.mark.parametrize("text", [
        "from a import x\nfrom b import y\n",              # different modules
        "from a import x\n    from a import y\n",          # indent mismatch
        "from a import *\nfrom a import y\n",              # star import
        "from a import x  # keep\nfrom a import y\n",      # trailing comment
        "from a import (\n    x,\n)\nfrom a import y\n",   # parenthesized
    ])
    def test_non_mergeable_shapes_untouched(self, tmp_path, text):
        changed, out = self._merge(tmp_path, text, before="")
        assert not changed
        assert out == text

    def test_aliases_merge_and_crlf_preserved(self, tmp_path):
        changed, out = self._merge(
            tmp_path,
            "from a import x as y\r\nfrom a import z\r\n",
            before="")
        assert changed
        assert out == "from a import x as y, z\r\n"
