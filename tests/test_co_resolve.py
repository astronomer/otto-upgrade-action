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
      depends on pydantic-ai-slim>=2.0.0 and you require
      pydantic-ai-slim[openai]==1.107.0, we can conclude that
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


def _run(tmp_path, monkeypatch, compile_results, versions=(), dep_spec=">=2.0.0"):
    """compile_results: list of (rc, stderr) consumed per compile call."""
    calls = iter(compile_results)
    monkeypatch.setattr(co_resolve, "compile_requirements", lambda _p: next(calls))
    monkeypatch.setattr(co_resolve, "in_scope_versions", lambda *_a: list(versions))
    monkeypatch.setattr(co_resolve, "_dependency_spec_for", lambda *_a: dep_spec)
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
    # The concrete change, not just the direction (Tamara's field request).
    assert ("to take 0.6.0, raise your `pydantic-ai-slim[openai]` pin "
            "to satisfy `pydantic-ai-slim>=2.0.0`") in provider["note"]
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
    assert co_resolve._blocking_pin_for(
        stderr, "apache-airflow-providers-common-ai") == expected


def test_note_degrades_to_direction_only_when_metadata_unavailable(tmp_path, monkeypatch):
    # PyPI metadata can be unreachable or the dep indirect; the advice then
    # degrades to direction-only rather than guessing a bound.
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    _run(tmp_path, monkeypatch,
         [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")],
         versions=[], dep_spec=None)
    note = json.loads(plan_file.read_text())["providers"][0]["note"]
    assert "to take 0.6.0, adjust your `pydantic-ai-slim[openai]` pin" in note
    assert "satisfy" not in note


def test_note_carries_full_compound_specifier(tmp_path, monkeypatch):
    # An upper-bounded requirement must be shown whole — advising only the
    # lower bound could send the user to an incompatible major.
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    _run(tmp_path, monkeypatch,
         [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")],
         versions=[], dep_spec=">=2.0.0,<3")
    note = json.loads(plan_file.read_text())["providers"][0]["note"]
    assert "satisfy `pydantic-ai-slim>=2.0.0,<3`" in note
    # A capped range is not provably "raise" territory.
    assert "adjust your" in note


def test_note_says_adjust_not_raise_for_upper_bound_requirement(tmp_path, monkeypatch):
    # A provider capping a dep BELOW the user's pin (the dbt/mashumaro shape):
    # telling the user to "raise" would point the wrong way.
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    _run(tmp_path, monkeypatch,
         [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")],
         versions=[], dep_spec="<=1.0.0")
    note = json.loads(plan_file.read_text())["providers"][0]["note"]
    assert "adjust your `pydantic-ai-slim[openai]` pin to satisfy `pydantic-ai-slim<=1.0.0`" in note
    assert "raise" not in note


def test_spec_fetched_for_original_target_not_walked_back_version(tmp_path, monkeypatch):
    # The note explains what the ORIGINAL target requires ("to take 0.6.0...");
    # fetching the walked-back version's metadata instead would stay green in
    # every other test (they stub _dependency_spec_for), so lock the URL here.
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    urls: list[str] = []

    def fake_http_json(url):
        urls.append(url)
        return {"info": {"requires_dist": ["pydantic-ai-slim>=2.0.0"]}}

    monkeypatch.setattr(co_resolve.rt, "_http_json", fake_http_json)
    calls = iter([(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")])
    monkeypatch.setattr(co_resolve, "compile_requirements", lambda _p: next(calls))
    monkeypatch.setattr(co_resolve, "in_scope_versions", lambda *_a: ["0.5.2"])
    monkeypatch.setenv("PROJECT_PATH", str(tmp_path))
    monkeypatch.setenv("PLAN_FILE", str(tmp_path / "plan.json"))
    assert co_resolve.main() == 0
    assert urls == [
        f"{co_resolve.rt.PYPI_BASE_URL}/apache-airflow-providers-common-ai/0.6.0/json"
    ]
    note = json.loads(plan_file.read_text())["providers"][0]["note"]
    assert "to satisfy `pydantic-ai-slim>=2.0.0`" in note


def _pypi(monkeypatch, requires_dist):
    monkeypatch.setattr(
        co_resolve.rt, "_http_json",
        lambda _url: {"info": {"requires_dist": requires_dist}})


@pytest.mark.parametrize("requires_dist,expected", [
    (["pydantic-ai-slim>=2.0.0"], ">=2.0.0"),
    # Legacy metadata: parenthesized spec, non-normalized name (PEP 503).
    (["Pydantic_AI.Slim (>=2.0.0,<3)"], ">=2.0.0,<3"),
    (["pydantic-ai-slim[openai]>=2.0.0"], ">=2.0.0"),
    # Extras-gated requirement is optional, not the blocking constraint.
    (["pydantic-ai-slim>=2.0.0; extra == 'fancy'"], None),
    (["pydantic-ai-slim>=2.0.0; python_version >= '3.9'"], ">=2.0.0"),
    (["unrelated>=1.0"], None),
    ([], None),
    (None, None),
])
def test_dependency_spec_extraction(monkeypatch, requires_dist, expected):
    _pypi(monkeypatch, requires_dist)
    assert co_resolve._dependency_spec_for(
        "apache-airflow-providers-common-ai", "0.6.0", "pydantic-ai-slim") == expected


def test_dependency_spec_none_on_network_error(monkeypatch):
    def boom(_url):
        raise OSError("offline")
    monkeypatch.setattr(co_resolve.rt, "_http_json", boom)
    assert co_resolve._dependency_spec_for("pkg", "1.0", "dep") is None


def test_blocking_pin_picks_clause_nearest_the_offender():
    # A multi-conflict error carries clauses for unrelated pins; the advice
    # must name the pin gating THIS provider, not the first clause in the text.
    err = (
        "Because somelib depends on x and you require otherlib==2.0, ...\n"
        "Because apache-airflow-providers-common-ai==0.6.0 depends on\n"
        "pydantic-ai-slim>=2.0.0 and you require pydantic-ai-slim[openai]==1.107.0, ...\n"
    )
    assert co_resolve._blocking_pin_for(
        err, "apache-airflow-providers-common-ai") == "pydantic-ai-slim[openai]==1.107.0"


def test_multi_conflict_walks_second_offender_after_first_exhausts(tmp_path, monkeypatch):
    # Offender A has no candidates (held at current); the same error also names
    # bumped offender B, which IS resolvable — B must still be walked instead
    # of the loop abandoning after A.
    plan = _plan()
    plan["providers"].append(
        {"package": "apache-airflow-providers-snowflake", "current": "6.8.0",
         "target": "6.14.0", "tier": "minor", "clamped": False, "note": ""})
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\n"
        "apache-airflow-providers-snowflake==6.14.0\n"
        "pydantic-ai-slim[openai]==1.107.0\n",
        plan,
    )
    err_a_bumped = (
        "you require pydantic-ai-slim[openai]==1.107.0 and "
        "apache-airflow-providers-common-ai==0.6.0 are incompatible; "
        "apache-airflow-providers-snowflake==6.14.0 is also unsatisfiable"
    )
    err_a_held = err_a_bumped.replace("common-ai==0.6.0", "common-ai==0.5.0")
    pools = {"apache-airflow-providers-common-ai": [],
             "apache-airflow-providers-snowflake": ["6.10.0"]}
    monkeypatch.setattr(co_resolve, "in_scope_versions",
                        lambda pkg, *_a: list(pools[pkg]))
    monkeypatch.setattr(co_resolve, "_dependency_spec_for", lambda *_a: None)
    calls = iter([
        (1, err_a_bumped),  # initial: A walked 0.6.0 -> current (empty pool)
        (1, err_a_held),    # still failing, A now named AT current -> exhausted
        (0, ""),            # after B walked -> resolves
    ])
    monkeypatch.setattr(co_resolve, "compile_requirements", lambda _p: next(calls))
    monkeypatch.setenv("PROJECT_PATH", str(tmp_path))
    monkeypatch.setenv("PLAN_FILE", str(tmp_path / "plan.json"))
    assert co_resolve.main() == 0
    updated = json.loads(plan_file.read_text())
    by_pkg = {p["package"]: p for p in updated["providers"]}
    assert by_pkg["apache-airflow-providers-common-ai"]["target"] == "0.5.0"  # held
    assert by_pkg["apache-airflow-providers-snowflake"]["target"] == "6.10.0"  # walked
    reqs = (tmp_path / "requirements.txt").read_text()
    assert "apache-airflow-providers-snowflake==6.10.0" in reqs


def _run_with_flag(tmp_path, monkeypatch, compile_results, versions=(),
                   dep_spec=">=2.0.0", choice="2.1.3"):
    """_run with BUMP_BLOCKING_PINS=true and a stubbed uv pin choice."""
    monkeypatch.setenv("BUMP_BLOCKING_PINS", "true")
    calls = []

    def fake_choice(_project, base):
        calls.append(base)
        return choice

    monkeypatch.setattr(co_resolve, "resolve_pin_choice", fake_choice)
    _run(tmp_path, monkeypatch, compile_results, versions=versions,
         dep_spec=dep_spec)
    return calls


def test_flag_off_never_touches_user_pins(tmp_path, monkeypatch):
    _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    monkeypatch.setattr(
        co_resolve, "resolve_pin_choice",
        lambda *_a: pytest.fail("resolve_pin_choice must not run when the flag is off"))
    _run(tmp_path, monkeypatch,
         [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")],
         versions=["0.5.2"])
    assert "pydantic-ai-slim[openai]==1.107.0" in (tmp_path / "requirements.txt").read_text()


def test_flag_raises_user_pin_and_keeps_provider(tmp_path, monkeypatch):
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    calls = _run_with_flag(
        tmp_path, monkeypatch,
        [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")])
    assert calls == ["pydantic-ai-slim"]
    plan = json.loads(plan_file.read_text())
    provider = plan["providers"][0]
    assert provider["target"] == "0.6.0"  # provider NOT walked back
    assert "raised 1.107.0 → 2.1.3" in provider["note"]
    assert plan["user_pin_bumps"] == [
        {"pin": "pydantic-ai-slim[openai]", "from": "1.107.0", "to": "2.1.3",
         "unblocks": {"package": "apache-airflow-providers-common-ai",
                      "version": "0.6.0"}}]
    reqs = (tmp_path / "requirements.txt").read_text()
    assert "pydantic-ai-slim[openai]==2.1.3" in reqs
    assert "apache-airflow-providers-common-ai==0.6.0" in reqs


def test_flag_falls_back_to_walk_when_uv_cannot_resolve(tmp_path, monkeypatch):
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    _run_with_flag(
        tmp_path, monkeypatch,
        [(1, UV_CONFLICT.format(ver="0.6.0")), (0, "")],
        versions=["0.5.2"], choice=None)
    plan = json.loads(plan_file.read_text())
    assert plan["providers"][0]["target"] == "0.5.2"  # walked back as before
    assert "user_pin_bumps" not in plan
    assert "pydantic-ai-slim[openai]==1.107.0" in (tmp_path / "requirements.txt").read_text()


def test_flag_tries_each_pin_once_then_walks(tmp_path, monkeypatch):
    # The raise applies but the same offender/pin still conflicts (e.g. a
    # second, transitive constraint) — the second iteration must walk back
    # instead of raising forever. Both edits surface in the plan.
    plan_file = _project(
        tmp_path,
        "apache-airflow-providers-common-ai==0.6.0\npydantic-ai-slim[openai]==1.107.0\n",
        _plan(),
    )
    calls = _run_with_flag(
        tmp_path, monkeypatch,
        [(1, UV_CONFLICT.format(ver="0.6.0")),
         (1, UV_CONFLICT.format(ver="0.6.0")),
         (0, "")],
        versions=["0.5.2"])
    assert calls == ["pydantic-ai-slim"]  # single attempt
    plan = json.loads(plan_file.read_text())
    provider = plan["providers"][0]
    assert provider["target"] == "0.5.2"          # walked after the raise failed
    assert provider["note"].startswith("held at")  # hold note wins
    assert plan["user_pin_bumps"][0]["to"] == "2.1.3"  # raise still reported


def test_resolve_pin_choice_reads_uv_lockfile(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("pydantic-ai-slim[openai]==1.107.0\n")

    def fake_run(cmd, **_kw):
        out_file = cmd[cmd.index("-o") + 1]
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write("other-lib==1.0\nPydantic_AI.Slim[openai]==2.1.3  # via x\n")
        override = cmd[cmd.index("--override") + 1]
        assert open(override).read().strip() == "pydantic-ai-slim"
        class P:
            returncode = 0
        return P()

    monkeypatch.setattr(co_resolve.subprocess, "run", fake_run)
    assert co_resolve.resolve_pin_choice(str(tmp_path), "pydantic-ai-slim") == "2.1.3"


def test_resolve_pin_choice_none_when_uv_fails(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("x==1\n")

    class P:
        returncode = 1

    monkeypatch.setattr(co_resolve.subprocess, "run", lambda *a, **k: P())
    assert co_resolve.resolve_pin_choice(str(tmp_path), "x") is None
