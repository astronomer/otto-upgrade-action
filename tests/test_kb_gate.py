"""kb_gate.py validates plan targets against the KB via Core's archive API.

Probes are stubbed; what's under test is the adjustment logic: provider
holds with Core's reason, runtime step-downs through ranked candidates,
fail-closed on uninterpretable rejections, and graceful degradation when
Core is unreachable or no token exists.
"""

import contextlib
import io
import json

import kb_gate


def _plan(target_af="3.3.0", candidates=None):
    return {
        "runtime": {"current_tag": "3.2-3", "current_airflow": "3.2.2",
                    "target_tag": "3.3-2", "target_airflow": target_af,
                    "tier": "minor",
                    "kb_step_candidates": candidates if candidates is not None else [
                        {"tag": "3.3-2", "airflow": "3.3.0"},
                        {"tag": "3.2-6", "airflow": "3.2.2"},
                    ],
                    "note": ""},
        "providers": [
            {"package": "apache-airflow-providers-common-ai", "current": "0.5.0",
             "target": "0.6.0", "tier": "minor", "note": ""},
            {"package": "apache-airflow-providers-amazon", "current": "9.19.0",
             "target": "9.32.0", "tier": "minor", "note": ""},
        ],
        "overall_tier": "minor", "needs_migration": True,
    }


def _run(tmp_path, monkeypatch, plan, probes, token=True):
    """probes: list of (status, message) consumed per probe call; returns
    (summary, updated_plan, calls) where calls captures (target_af, providers)."""
    plan_f = tmp_path / "plan.json"
    plan_f.write_text(json.dumps(plan))
    monkeypatch.setenv("PLAN_FILE", str(plan_f))
    if token:
        monkeypatch.setenv("ASTRO_API_TOKEN", "tok")
        monkeypatch.setenv("ASTRO_ORGANIZATION", "org123")
        monkeypatch.setenv("ASTRO_DOMAIN", "astronomer-test.io")
    else:
        monkeypatch.delenv("ASTRO_API_TOKEN", raising=False)
        monkeypatch.delenv("ASTRO_TOKEN", raising=False)
        monkeypatch.delenv("ASTRO_ORGANIZATION", raising=False)
    calls = []
    results = iter(probes)

    def fake_probe(url, token_, current_af, target_af, providers):
        calls.append((target_af, [(p["package"], p["target"]) for p in providers]))
        return next(results)

    monkeypatch.setattr(kb_gate, "probe", fake_probe)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert kb_gate.main() == 0
    return json.loads(buf.getvalue()), json.loads(plan_f.read_text()), calls


def test_tokenless_run_skips_gate_with_reason(tmp_path, monkeypatch):
    summary, plan, calls = _run(tmp_path, monkeypatch, _plan(), [], token=False)
    assert summary["checked"] is False
    assert "dry-run" in summary["reason"]
    assert calls == []
    assert plan["runtime"]["target_tag"] == "3.3-2"  # untouched


def test_covered_plan_passes_untouched(tmp_path, monkeypatch):
    summary, plan, calls = _run(tmp_path, monkeypatch, _plan(), [(200, "")])
    assert summary["status"] == "covered"
    assert len(calls) == 1
    assert calls[0][0] == "3.3.0"
    assert plan["runtime"]["target_tag"] == "3.3-2"
    assert plan["providers"][0]["target"] == "0.6.0"


def test_rejected_provider_is_held_with_core_reason(tmp_path, monkeypatch):
    reject = (400, 'provider apache-airflow-providers-common-ai target "0.6.0" '
                   "is not a known version")
    summary, plan, calls = _run(tmp_path, monkeypatch, _plan(), [reject, (200, "")])
    held = plan["providers"][0]
    assert held["target"] == "0.5.0" and held["tier"] == "none"
    assert "rejected by the upgrade KB" in held["note"]
    assert "not a known version" in held["note"]
    assert plan["providers"][1]["target"] == "9.32.0"  # other provider untouched
    # Second probe no longer offers the held provider.
    assert calls[1][1] == [("apache-airflow-providers-amazon", "9.32.0")]
    assert summary["adjustments"][0]["kind"] == "provider-held"


def test_yank_reason_surfaces_in_note(tmp_path, monkeypatch):
    reject = (400, "provider apache-airflow-providers-amazon target 9.32.0 was "
                   "yanked: premature release")
    _, plan, _ = _run(tmp_path, monkeypatch, _plan(), [reject, (200, "")])
    assert "premature release" in plan["providers"][1]["note"]


def test_rejected_airflow_steps_down_ranked_candidates(tmp_path, monkeypatch):
    reject = (400, 'targetVersion "3.3.0" is not a known version')
    summary, plan, calls = _run(tmp_path, monkeypatch, _plan(), [reject, (200, "")])
    rt_entry = plan["runtime"]
    assert rt_entry["target_tag"] == "3.2-6"
    assert rt_entry["target_airflow"] == "3.2.2"
    assert rt_entry["tier"] == "patch"  # same Airflow, newer build
    assert "isn't covered by the upgrade KB yet" in rt_entry["note"]
    assert calls[1][0] == "3.2.2"
    assert summary["adjustments"][0]["kind"] == "runtime-stepped"


def test_rejected_airflow_with_no_candidates_holds_runtime(tmp_path, monkeypatch):
    reject = (400, 'targetVersion "3.3.0" is not a known version')
    summary, plan, _ = _run(tmp_path, monkeypatch, _plan(candidates=[]),
                            [reject, (200, "")])
    rt_entry = plan["runtime"]
    assert rt_entry["target_tag"] == "3.2-3"
    assert rt_entry["tier"] == "none"
    assert "runtime held" in rt_entry["note"]
    assert summary["adjustments"][0]["kind"] == "runtime-held"


def test_core_unreachable_degrades_to_unchecked_with_disclosure(tmp_path, monkeypatch):
    summary, plan, _ = _run(tmp_path, monkeypatch, _plan(),
                            [(0, "URLError: timed out")])
    assert summary["status"] == "unchecked"
    assert summary["checked"] is False
    # Plan-level flag: rendered by build_pr_body even for provider-only plans.
    assert "Core unreachable" in plan["kb_gate_unchecked"]
    assert plan["providers"][0]["target"] == "0.6.0"  # plan not held


def test_uninterpretable_400_fails_closed_on_everything(tmp_path, monkeypatch):
    summary, plan, _ = _run(tmp_path, monkeypatch, _plan(),
                            [(400, "something entirely unexpected")])
    assert summary["status"] == "held-all"
    assert plan["runtime"]["target_tag"] == "3.2-3"
    assert all(p["target"] == p["current"] for p in plan["providers"])
    assert plan["no_update"] is True  # roll_up recomputed


def test_probe_encodes_params_and_parses_error_json(monkeypatch):
    seen = {}

    class FakeResp:
        status = 200
        def read(self, _n): return b"x"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return FakeResp()

    monkeypatch.setattr(kb_gate.urllib.request, "urlopen", fake_urlopen)
    status, msg = kb_gate.probe(
        "https://api.x/v1alpha1/organizations/o/agent/skills/airflow-upgrade/archive",
        "tok", "3.2.2", "3.3.0",
        [{"package": "apache-airflow-providers-amazon",
          "current": "9.19.0", "target": "9.32.0"}])
    assert status == 200 and msg == ""
    assert "currentVersion=3.2.2" in seen["url"]
    assert "targetVersion=3.3.0" in seen["url"]
    assert "apache-airflow-providers-amazon%3A9.19.0%3A9.32.0" in seen["url"]
    assert seen["auth"] == "Bearer tok"


def test_probe_same_version_omits_target_param(monkeypatch):
    seen = {}

    class FakeResp:
        status = 204
        def read(self, _n): return b""
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(
        kb_gate.urllib.request, "urlopen",
        lambda req, timeout: seen.update(url=req.full_url) or FakeResp())
    status, _ = kb_gate.probe("https://api.x/base", "tok", "3.2.2", "3.2.2", [])
    assert status == 204
    assert "targetVersion" not in seen["url"]


def test_budget_exhaustion_fails_closed_not_half_adjusted(tmp_path, monkeypatch):
    # Backstop: if the probe budget somehow exhausts with rejections still
    # standing (unreachable under normal Core semantics — every attributable
    # 400 shrinks work), the leftovers must be held, never leaked to Otto's
    # own fetch as a mid-run 400.
    plan = _plan()
    plan["providers"] = [
        {"package": f"apache-airflow-providers-p{i}", "current": "1.0.0",
         "target": "2.0.0", "tier": "major", "note": ""}
        for i in range(30)
    ]
    monkeypatch.setattr(kb_gate, "_probe_budget", lambda *_a: 3)
    rejects = [(400, f'provider apache-airflow-providers-p{i} target "2.0.0" '
                     "is not a known version") for i in range(3)]
    summary, updated, calls = _run(tmp_path, monkeypatch, plan, rejects)
    assert summary["status"] == "held-all"
    assert "budget exhausted" in summary["reason"]
    assert all(p["target"] == p["current"] for p in updated["providers"])
    assert updated["runtime"]["target_tag"] == "3.2-3"  # runtime held too


def test_budget_scales_with_plan_size(tmp_path, monkeypatch):
    # 10 providers all rejected one-by-one then covered: the dynamic budget
    # (bumped + candidates + 2) must allow every hold plus the final pass.
    plan = _plan()
    plan["providers"] = [
        {"package": f"apache-airflow-providers-p{i}", "current": "1.0.0",
         "target": "2.0.0", "tier": "major", "note": ""}
        for i in range(10)
    ]
    probes = [(400, f'provider apache-airflow-providers-p{i} target "2.0.0" '
                    "is not a known version") for i in range(10)] + [(200, "")]
    summary, updated, calls = _run(tmp_path, monkeypatch, plan, probes)
    assert summary["status"] == "covered"
    assert all(p["target"] == p["current"] for p in updated["providers"])
    assert len(calls) == 11


def test_provider_overflow_beyond_probe_cap_is_held_upfront(tmp_path, monkeypatch):
    plan = _plan()
    plan["providers"] = [
        {"package": f"apache-airflow-providers-p{i}", "current": "1.0.0",
         "target": "2.0.0", "tier": "major", "note": ""}
        for i in range(55)
    ]
    summary, updated, calls = _run(tmp_path, monkeypatch, plan, [(200, "")])
    held = [p for p in updated["providers"] if p["target"] == p["current"]]
    assert len(held) == 5  # the overflow past the 50-provider probe cap
    assert all("probe limit" in p["note"] for p in held)
    assert len(calls[0][1]) == 50  # probe carries exactly the cap


def test_bad_token_401_discloses_in_plan(tmp_path, monkeypatch):
    summary, updated, _ = _run(tmp_path, monkeypatch, _plan(),
                               [(401, "unauthorized")])
    assert summary["status"] == "unchecked"
    assert summary["checked"] is False
    assert "HTTP 401" in updated["kb_gate_unchecked"]
    assert updated["providers"][0]["target"] == "0.6.0"  # not held


def test_stepdown_reclamps_providers_against_new_airflow(tmp_path, monkeypatch):
    # Provider targets were compat-clamped against the REJECTED Airflow; the
    # step-down must re-clamp them against where we actually land.
    reclamped = []

    def fake_latest(pkg, cur, scope, target_airflow=None):
        reclamped.append((pkg, target_airflow))
        return {"package": pkg, "current": cur, "target": "0.5.5",
                "tier": "patch", "clamped": True,
                "note": f"held at 0.5.5 — newest release compatible with Airflow {target_airflow}"}

    monkeypatch.setattr(kb_gate.rt, "_provider_latest", fake_latest)
    monkeypatch.setenv("MAX_SCOPE", "minor")
    reject = (400, 'targetVersion "3.3.0" is not a known version')
    summary, plan, _ = _run(tmp_path, monkeypatch, _plan(), [reject, (200, "")])
    assert plan["runtime"]["target_tag"] == "3.2-6"
    assert all(af == "3.2.2" for _, af in reclamped)
    assert plan["providers"][0]["target"] == "0.5.5"
    assert "re-resolved from 0.6.0 for Airflow 3.2.2" in plan["providers"][0]["note"]
    kinds = [a["kind"] for a in summary["adjustments"]]
    assert "runtime-stepped" in kinds and "provider-reclamped" in kinds


def test_regate_mode_syncs_held_provider_back_into_files(tmp_path, monkeypatch):
    # Re-gate pass (PROJECT_PATH set): the tree already carries the reconciled
    # pins, so a hold must land in requirements.txt too.
    (tmp_path / "requirements.txt").write_text(
        "apache-airflow-providers-common-ai==0.6.0\n"
        "apache-airflow-providers-amazon==9.32.0\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM astrocrpublic.azurecr.io/runtime:3.3-2\n")
    monkeypatch.setenv("PROJECT_PATH", str(tmp_path))
    reject = (400, 'provider apache-airflow-providers-common-ai target "0.6.0" '
                   "is not a known version")
    _, plan, _ = _run(tmp_path, monkeypatch, _plan(), [reject, (200, "")])
    reqs = (tmp_path / "requirements.txt").read_text()
    assert "apache-airflow-providers-common-ai==0.5.0" in reqs  # held -> synced
    assert "apache-airflow-providers-amazon==9.32.0" in reqs    # untouched


def test_regate_mode_syncs_runtime_stepdown_into_dockerfile(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text(
        "apache-airflow-providers-common-ai==0.6.0\n"
        "apache-airflow-providers-amazon==9.32.0\n")
    (tmp_path / "Dockerfile").write_text(
        "FROM astrocrpublic.azurecr.io/runtime:3.3-2\n")
    monkeypatch.setenv("PROJECT_PATH", str(tmp_path))
    monkeypatch.setattr(kb_gate, "_reclamp_providers", lambda *_a: None)
    reject = (400, 'targetVersion "3.3.0" is not a known version')
    _, plan, _ = _run(tmp_path, monkeypatch, _plan(), [reject, (200, "")])
    assert "runtime:3.2-6" in (tmp_path / "Dockerfile").read_text()


def test_plan_only_mode_never_touches_files(tmp_path, monkeypatch):
    # First-gate pass (no PROJECT_PATH): plan-only, tree is pre-apply.
    (tmp_path / "requirements.txt").write_text(
        "apache-airflow-providers-common-ai==0.5.0\n")
    monkeypatch.delenv("PROJECT_PATH", raising=False)
    reject = (400, 'provider apache-airflow-providers-common-ai target "0.6.0" '
                   "is not a known version")
    _, plan, _ = _run(tmp_path, monkeypatch, _plan(), [reject, (200, "")])
    assert (tmp_path / "requirements.txt").read_text() == \
        "apache-airflow-providers-common-ai==0.5.0\n"
