"""Resolver tiering, clamping, and the majors-are-advisory rule."""

import contextlib
import io
import json
from pathlib import Path

import pytest
import resolve_target as rt
from conftest import load_fixture


@pytest.fixture(autouse=True)
def stub_http(monkeypatch):
    """Route _http_json at the runtime feed and PyPI fixtures."""
    feed = load_fixture("runtime-feed.json")
    amazon = load_fixture("pypi-amazon.json")

    def fake(url: str):
        if "astronomer-runtime" in url or url.endswith("runtime-feed.json"):
            return feed
        if "amazon" in url:
            return amazon
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(rt, "_http_json", fake)


# --- runtime tiering ------------------------------------------------------- #
def test_patch_target_stays_on_minor():
    r = rt.resolve_runtime("3.1-5", target="patch", max_scope="minor")
    assert r["target_tag"] == "3.1-7"
    assert r["tier"] == "patch"
    assert r["clamped"] is False


def test_latest_minor_moves_within_major():
    r = rt.resolve_runtime("3.1-5", target="latest-minor", max_scope="minor")
    assert r["target_tag"] == "3.2-3"
    assert r["target_airflow"] == "3.2.2"
    assert r["tier"] == "minor"


def test_non_stable_channel_is_ignored():
    # 3.3-rc (alpha) must never be picked even with target=latest.
    r = rt.resolve_runtime("3.2-1", target="latest", max_scope="major")
    assert r["target_tag"] == "3.2-3"


def test_unknown_current_tag_is_skipped_not_crashed():
    r = rt.resolve_runtime("9.9-9", target="latest", max_scope="major")
    assert r["tier"] == "none"
    assert r["target_tag"] == "9.9-9"
    assert "not found" in r["note"]


# --- runtime clamping ------------------------------------------------------ #
def test_major_jump_clamped_to_minor():
    # On AF2, asking for 'latest' wants AF3 (major); max-scope=minor must hold
    # it to the newest AF2 runtime.
    r = rt.resolve_runtime("2.10-12", target="latest", max_scope="minor")
    assert r["clamped"] is True
    assert r["target_tag"] == "2.11-1"
    assert r["tier"] == "minor"


def test_major_jump_clamped_to_patch():
    r = rt.resolve_runtime("2.10-12", target="latest", max_scope="patch")
    # No newer patch on the 2.10 line in the fixture -> stays put.
    assert r["target_tag"] == "2.10-12"
    assert r["tier"] == "none"


# --- providers ------------------------------------------------------------- #
def test_provider_minor_clamp_excludes_yanked_and_prerelease():
    p = rt._provider_latest("apache-airflow-providers-amazon", "9.0.0", "minor")
    # 9.31.0 is yanked, 10.1.0rc1 is prerelease, 10.0.0 is a major -> clamp to 9.30.0.
    assert p["target"] == "9.30.0"
    assert p["tier"] == "minor"
    assert p["clamped"] is True


def test_provider_major_allowed_when_scope_major():
    p = rt._provider_latest("apache-airflow-providers-amazon", "9.0.0", "major")
    assert p["target"] == "10.0.0"
    assert p["tier"] == "major"


def test_provider_no_downgrade():
    p = rt._provider_latest("apache-airflow-providers-amazon", "99.0.0", "major")
    assert p["tier"] == "none"
    assert p["target"] == "99.0.0"


# --- full plan: majors are advisory-only ----------------------------------- #
def _run_plan(tmp_path: Path, monkeypatch, current: dict, **env):
    cur_file = tmp_path / "current.json"
    cur_file.write_text(json.dumps(current))
    monkeypatch.setenv("CURRENT_FILE", str(cur_file))
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rt.main()
    return json.loads(buf.getvalue())


def test_major_plan_is_advisory_only(tmp_path, monkeypatch):
    plan = _run_plan(
        tmp_path, monkeypatch,
        {"runtime": {"tag": "2.10-12", "image_repo": "x/runtime"}, "providers": []},
        TARGET="latest", MAX_SCOPE="major", INCLUDE_PROVIDERS="false",
    )
    assert plan["overall_tier"] == "major"
    assert plan["author_changes"] is False  # never auto-author a major
    assert plan["advisory"]


def test_minor_plan_authors_changes(tmp_path, monkeypatch):
    plan = _run_plan(
        tmp_path, monkeypatch,
        {"runtime": {"tag": "3.1-5", "image_repo": "x/runtime"},
         "providers": [{"package": "apache-airflow-providers-amazon", "pinned_version": "9.0.0"}]},
        TARGET="latest-minor", MAX_SCOPE="minor",
    )
    assert plan["overall_tier"] == "minor"
    assert plan["author_changes"] is True
    assert plan["needs_migration"] is True
    assert plan["no_update"] is False


def test_no_update_when_current_is_latest(tmp_path, monkeypatch):
    plan = _run_plan(
        tmp_path, monkeypatch,
        {"runtime": {"tag": "3.2-3", "image_repo": "x/runtime"}, "providers": []},
        TARGET="latest", MAX_SCOPE="major", INCLUDE_PROVIDERS="false",
    )
    assert plan["no_update"] is True
    assert plan["author_changes"] is False
