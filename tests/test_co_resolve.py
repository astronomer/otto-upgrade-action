"""co_resolve.py walks conflicting provider bumps back to co-resolving versions.

The uv resolver and PyPI are stubbed; requirements.txt edits are real (via
apply_bump) so the file/plan stay consistent.
"""

import json

import co_resolve
import pytest

UV_CONFLICT = """\
  x No solution found when resolving dependencies:
  |-> Because apache-airflow-providers-common-ai=={ver}
      requirements and pydantic-ai-slim[openai]==1.107.0 are incompatible.
      And because you require pydantic-ai-slim[openai]==1.107.0, we can
      conclude that your requirements are unsatisfiable.
"""


def _project(tmp_path, requirements: str, plan: dict):
    (tmp_path / "requirements.txt").write_text(requirements)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan))
    return plan_file


def _plan(target="0.6.0"):
    return {
        "runtime": {"current_tag": "3.2-3", "target_tag": "3.3-1", "tier": "minor"},
        "providers": [
            {"package": "apache-airflow-providers-common-ai", "current": "0.5.0",
             "target": target, "tier": "minor", "clamped": False, "note": ""},
            {"package": "apache-airflow-providers-amazon", "current": "9.19.0",
             "target": "9.32.0", "tier": "minor", "clamped": False, "note": ""},
        ],
        "overall_tier": "minor", "needs_migration": True,
    }


def _run(tmp_path, monkeypatch, compile_results, versions=()):
    """compile_results: list of (rc, stderr) consumed per compile call."""
    calls = iter(compile_results)
    monkeypatch.setattr(co_resolve, "compile_requirements", lambda _p: next(calls))
    monkeypatch.setattr(co_resolve, "in_scope_versions", lambda *_a: list(versions))
    monkeypatch.setenv("PROJECT_PATH", str(tmp_path))
    monkeypatch.setenv("PLAN_FILE", str(tmp_path / "plan.json"))
    assert co_resolve.main() == 0


def test_clean_resolution_is_a_noop(tmp_path, monkeypatch, capsys):
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    _run(tmp_path, monkeypatch, [(0, "")])
    assert json.loads(plan_file.read_text())["providers"][0]["target"] == "0.6.0"
    assert json.loads(capsys.readouterr().out)["adjustments"] == []


def test_conflict_steps_down_to_coresolving_version(tmp_path, monkeypatch):
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    _run(tmp_path, monkeypatch,
         [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")],
         versions=["0.5.2"])
    plan = json.loads(plan_file.read_text())
    provider = plan["providers"][0]
    assert provider["target"] == "0.5.2"
    assert provider["tier"] == "patch"
    assert "pydantic-ai-slim[openai]==1.107.0" in provider["note"]
    assert "raise `pydantic-ai-slim[openai]` to take 0.6.0" in provider["note"]
    reqs = (tmp_path / "requirements.txt").read_text()
    assert "apache-airflow-providers-common-ai==0.5.2" in reqs
    assert "pydantic-ai-slim[openai]==1.107.0" in reqs  # user pin untouched


def test_conflict_with_no_candidates_holds_current(tmp_path, monkeypatch):
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    _run(tmp_path, monkeypatch,
         [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")],
         versions=[])
    plan = json.loads(plan_file.read_text())
    provider = plan["providers"][0]
    assert provider["target"] == "0.5.0"
    assert provider["tier"] == "none"
    assert provider["note"].startswith("left at 0.5.0")
    assert "apache-airflow-providers-common-ai==0.5.0" in (tmp_path / "requirements.txt").read_text()


def test_unattributable_conflict_is_left_to_verification(tmp_path, monkeypatch):
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\nsomelib==1.0\notherlib==2.0\n",
        _plan(),
    )
    _run(tmp_path, monkeypatch,
         [(1, "somelib==1.0 and otherlib==2.0 are incompatible")])
    plan = json.loads(plan_file.read_text())
    assert plan["providers"][0]["target"] == "0.6.0"  # untouched


def test_overall_tier_rerolled_when_all_bumps_held(tmp_path, monkeypatch):
    plan = _plan()
    plan["runtime"]["tier"] = "none"
    plan["providers"] = [plan["providers"][0]]  # only the conflicting one
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        plan,
    )
    _run(tmp_path, monkeypatch,
         [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")],
         versions=[])
    updated = json.loads(plan_file.read_text())
    assert updated["overall_tier"] == "none"
    assert updated["needs_migration"] is False


@pytest.mark.parametrize("stderr,expected", [
    (UV_CONFLICT.format(ver="0.6.0"), "pydantic-ai-slim[openai]==1.107.0"),
    ("no pin mentioned here", None),
])
def test_blocking_pin_extraction(stderr, expected):
    m = co_resolve._BLOCKING_PIN.search(stderr)
    assert (m.group("pin") if m else None) == expected
