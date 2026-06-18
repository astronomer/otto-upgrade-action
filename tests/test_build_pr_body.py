"""PR body renders the version table, migration notes, and advisories."""

import contextlib
import io
import json

import build_pr_body as bpb


def _render(tmp_path, monkeypatch, plan, apply, otto=None, verify=None):
    plan_f = tmp_path / "plan.json"
    apply_f = tmp_path / "apply.json"
    plan_f.write_text(json.dumps(plan))
    apply_f.write_text(json.dumps(apply))
    monkeypatch.setenv("PLAN_FILE", str(plan_f))
    monkeypatch.setenv("APPLY_FILE", str(apply_f))
    if otto is not None:
        otto_f = tmp_path / "otto.json"
        otto_f.write_text(json.dumps(otto))
        monkeypatch.setenv("OTTO_FILE", str(otto_f))
    if verify is not None:
        v_f = tmp_path / "verify.md"
        v_f.write_text(verify)
        monkeypatch.setenv("VERIFY_FILE", str(v_f))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bpb.main()
    return buf.getvalue()


def test_body_has_marker_and_version_table(tmp_path, monkeypatch):
    plan = {
        "overall_tier": "minor", "scope_exceeded": False, "needs_migration": True,
        "advisory": "",
        "runtime": {"current_tag": "3.1-12", "target_tag": "3.2-3",
                    "tier": "minor", "current_airflow": "3.1.0", "target_airflow": "3.2.2"},
        "providers": [{"package": "apache-airflow-providers-amazon", "current": "9.0.0",
                       "target": "9.30.0", "tier": "minor"}],
    }
    out = _render(tmp_path, monkeypatch, plan, {"files": []})
    assert bpb.MARKER in out
    assert "`3.1-12`" in out and "`3.2-3`" in out
    assert "amazon" in out
    assert "🟡 minor" in out


def test_body_warns_when_migration_skipped(tmp_path, monkeypatch):
    plan = {"overall_tier": "minor", "needs_migration": True, "scope_exceeded": False,
            "advisory": "", "runtime": {"current_tag": "3.1-12", "target_tag": "3.2-3",
                                        "tier": "minor", "current_airflow": "3.1.0",
                                        "target_airflow": "3.2.2"}, "providers": []}
    out = _render(tmp_path, monkeypatch, plan, {"files": []})  # no OTTO_FILE
    assert "Review breaking changes manually" in out


def test_body_renders_otto_followups(tmp_path, monkeypatch):
    plan = {"overall_tier": "minor", "needs_migration": True, "scope_exceeded": False,
            "advisory": "", "runtime": {"current_tag": "3.1-12", "target_tag": "3.2-3",
                                        "tier": "minor", "current_airflow": "3.1.0",
                                        "target_airflow": "3.2.2"}, "providers": []}
    otto = {"summary": "Rewrote 2 imports.",
            "changes_made": ["airflow.operators.python -> airflow.sdk"],
            "manual_followups": ["Confirm the custom timetable still imports"]}
    out = _render(tmp_path, monkeypatch, plan, {"files": []}, otto=otto)
    assert "Rewrote 2 imports." in out
    assert "- [ ] Confirm the custom timetable still imports" in out


def test_body_shows_major_advisory(tmp_path, monkeypatch):
    plan = {"overall_tier": "major", "needs_migration": True, "scope_exceeded": False,
            "advisory": "A major Airflow upgrade is available (2.10.5 -> 3.2.2).",
            "runtime": None, "providers": []}
    out = _render(tmp_path, monkeypatch, plan, {"files": []})
    assert "major Airflow upgrade is available" in out
