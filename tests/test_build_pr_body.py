"""PR body renders the version table, migration notes, and advisories."""

import contextlib
import io
import json

import build_pr_body as bpb


def _render(tmp_path, monkeypatch, plan, apply, otto=None, verify=None, verify_status=None):
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
    if verify_status is not None:
        s_f = tmp_path / "verify-status.txt"
        s_f.write_text(verify_status + "\n")
        monkeypatch.setenv("VERIFY_STATUS_FILE", str(s_f))
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


def test_body_shows_not_changed_notes(tmp_path, monkeypatch):
    # Digest-pinned runtime + unpinned provider must surface in a "Not changed"
    # section, not vanish silently (the scenario-B finding).
    plan = {
        "overall_tier": "minor", "needs_migration": True, "scope_exceeded": False,
        "advisory": "",
        "runtime": {"current_tag": "11.3.0", "target_tag": "11.3.0", "tier": "none",
                    "note": "FROM line is digest-pinned (@sha256:...); not auto-bumped."},
        "providers": [
            {"package": "apache-airflow-providers-amazon", "current": "9.0.0",
             "target": "9.30.0", "tier": "minor"},
            {"package": "apache-airflow-providers-snowflake", "current": None,
             "target": None, "tier": "none", "note": "unpinned; skipped (can only bump exact pins safely)"},
        ],
    }
    out = _render(tmp_path, monkeypatch, plan, {"files": []})
    assert "### Not changed" in out
    assert "digest-pinned" in out
    assert "snowflake" in out and "unpinned" in out
    # The amazon bump still appears in the version table.
    assert "9.30.0" in out


def test_verification_is_collapsible_with_status(tmp_path, monkeypatch):
    plan = {"overall_tier": "minor", "needs_migration": True, "scope_exceeded": False,
            "advisory": "", "runtime": {"current_tag": "3.1-12", "target_tag": "3.2-5",
                                        "tier": "minor", "current_airflow": "3.1.0",
                                        "target_airflow": "3.2.2"}, "providers": []}
    report = "❌ 1 of 4 DAG file(s) failed to import at the target version:\n  - `dags/x.py`: boom"
    out = _render(tmp_path, monkeypatch, plan, {"files": []}, verify=report)
    assert "<details>" in out and "</details>" in out
    assert "<summary><b>Verification — failed</b></summary>" in out
    # passed/skipped labels too
    out_ok = _render(tmp_path, monkeypatch, plan, {"files": []},
                     verify="✅ All 3 DAG file(s) import cleanly.")
    assert "Verification — passed" in out_ok


def test_skipped_verification_gets_warning_banner(tmp_path, monkeypatch):
    # An un-run verification must be as loud as a failed one — in the field the
    # collapsed "skipped" read as success. This report deliberately does NOT
    # start with ℹ️ (the emoji sniff misses it); the status file is what counts.
    plan = {"overall_tier": "minor", "needs_migration": True, "scope_exceeded": False,
            "advisory": "", "runtime": {"current_tag": "3.1-12", "target_tag": "3.2-5",
                                        "tier": "minor", "current_airflow": "3.1.0",
                                        "target_airflow": "3.2.2"}, "providers": []}
    out = _render(tmp_path, monkeypatch, plan, {"files": []},
                  verify="Verification disabled (`verify-level: none`).",
                  verify_status="skipped")
    assert "> [!WARNING]" in out
    assert "Verification did not run" in out
    assert "NOT checked" in out
    assert "Verification — skipped" in out


def test_failed_banner_keyed_on_status_file(tmp_path, monkeypatch):
    plan = {"overall_tier": "minor", "needs_migration": True, "scope_exceeded": False,
            "advisory": "", "runtime": {"current_tag": "3.1-12", "target_tag": "3.2-5",
                                        "tier": "minor", "current_airflow": "3.1.0",
                                        "target_airflow": "3.2.2"}, "providers": []}
    out = _render(tmp_path, monkeypatch, plan, {"files": []},
                  verify="❌ 1 NEW import failure(s) at the target version:\n  - `dags/x.py`: boom",
                  verify_status="failed")
    assert "> [!CAUTION]" in out
    assert "new import failures at the target version" in out


def test_passed_verification_gets_no_banner(tmp_path, monkeypatch):
    plan = {"overall_tier": "minor", "needs_migration": True, "scope_exceeded": False,
            "advisory": "", "runtime": {"current_tag": "3.1-12", "target_tag": "3.2-5",
                                        "tier": "minor", "current_airflow": "3.1.0",
                                        "target_airflow": "3.2.2"}, "providers": []}
    out = _render(tmp_path, monkeypatch, plan, {"files": []},
                  verify="✅ No new import failures at the target version (12 file(s) checked).",
                  verify_status="passed")
    assert "[!WARNING]" not in out and "[!CAUTION]" not in out
    assert "Verification — passed" in out


def test_body_shows_major_advisory(tmp_path, monkeypatch):
    plan = {"overall_tier": "major", "needs_migration": True, "scope_exceeded": False,
            "advisory": "A major Airflow upgrade is available (2.10.5 -> 3.2.2).",
            "runtime": None, "providers": []}
    out = _render(tmp_path, monkeypatch, plan, {"files": []})
    assert "major Airflow upgrade is available" in out


def test_scope_exceeded_suggests_raising_cap_for_non_major(tmp_path, monkeypatch):
    # A genuine patch/minor clamp (no Airflow major withheld) keeps the
    # actionable "raise the cap" hint.
    plan = {"overall_tier": "minor", "needs_migration": True, "scope_exceeded": True,
            "held_airflow_major": False, "advisory": "",
            "runtime": {"current_tag": "3.1-5", "target_tag": "3.1-7", "tier": "patch",
                        "current_airflow": "3.1.0", "target_airflow": "3.1.2"}, "providers": []}
    out = _render(tmp_path, monkeypatch, plan, {"files": []})
    assert "Raise the input to go further" in out


def test_scope_exceeded_points_to_guided_upgrade_for_held_major(tmp_path, monkeypatch):
    # When the withheld jump is an Airflow major, don't tell the user to raise
    # the cap (it wouldn't help) — point at the guided upgrade instead.
    plan = {"overall_tier": "minor", "needs_migration": True, "scope_exceeded": True,
            "held_airflow_major": True,
            "advisory": "A major Airflow upgrade is available (2.10.5 -> 3.2.2).",
            "runtime": {"current_tag": "2.10-12", "target_tag": "2.11-1", "tier": "minor",
                        "current_airflow": "2.10.5", "target_airflow": "2.11.0"}, "providers": []}
    out = _render(tmp_path, monkeypatch, plan, {"files": []})
    assert "Raise the input to go further" not in out
    assert "never auto-authored" in out
    assert "major Airflow upgrade is available" in out  # Heads up section still present
