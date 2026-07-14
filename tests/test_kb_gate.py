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


def test_core_unreachable_degrades_to_unchecked_with_note(tmp_path, monkeypatch):
    summary, plan, _ = _run(tmp_path, monkeypatch, _plan(),
                            [(0, "URLError: timed out")])
    assert summary["status"] == "unchecked"
    assert summary["checked"] is False
    assert "NOT verified" in plan["runtime"]["note"]
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
