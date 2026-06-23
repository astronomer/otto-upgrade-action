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


# --- version helpers ------------------------------------------------------- #
@pytest.mark.parametrize(
    "ver,expected",
    [("9.30.0", False), ("10.1.0rc1", True), ("9.0.0.post1", False),
     ("2.0.0.dev3", True), ("1.2.3b2", True), ("3.3.0rc1", True), ("21.0.0", False)],
)
def test_is_prerelease(ver, expected):
    assert rt.is_prerelease(ver) is expected


@pytest.mark.parametrize(
    "ver,expected",
    [("3.2.1", (3, 2, 1)), ("3.1-17", (3, 1, 17)), ("1!2.0.0", (2, 0, 0)),
     ("2.0.0.post1", (2, 0, 0, 1)), ("9.0", (9, 0))],
)
def test_version_tuple(ver, expected):
    assert rt.version_tuple(ver) == expected


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


def test_runtime_build_patch_same_airflow():
    # 3.0-9 and 3.0-10 are both Airflow 3.0.5 — a newer Runtime *build* on the
    # same Airflow (CVE/provider-bundle fix). Must be a patch bump, not a no-op.
    r = rt.resolve_runtime("3.0-9", target="patch", max_scope="patch")
    assert r["target_tag"] == "3.0-10"
    assert r["tier"] == "patch"


def test_non_stable_channel_is_ignored():
    # 3.3-rc (alpha channel) and 3.3-1 (stable channel but Airflow 3.3.0rc1, a
    # prerelease Airflow) must never be picked even with target=latest.
    r = rt.resolve_runtime("3.2-1", target="latest", max_scope="major")
    assert r["target_tag"] == "3.2-3"
    assert "rc" not in (r["target_airflow"] or "")


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


# --- deprecated / EOL current runtime -------------------------------------- #
def test_deprecated_current_runtime_resolves_and_targets_stable():
    # 2.9-5 is on the *deprecated* channel (Airflow 2.9.3). The project still
    # runs it, so we must resolve its Airflow and bump it onto the newest STABLE
    # runtime within the Airflow-2 major (2.11-1) — never another deprecated tag.
    r = rt.resolve_runtime("2.9-5", target="latest-minor", max_scope="minor")
    assert r["current_airflow"] == "2.9.3"
    assert r["target_tag"] == "2.11-1"
    assert r["target_airflow"] == "2.11.0"
    assert r["tier"] == "minor"
    assert r["current_channel"] == "deprecated"
    assert "deprecated" in r["note"]


def test_deprecated_runtime_is_never_an_upgrade_target():
    # Coming from an even older deprecated tag, the target is still the newest
    # stable (2.11-1), not the deprecated 2.9-5/2.8-3 entries in the feed.
    r = rt.resolve_runtime("2.8-3", target="latest-minor", max_scope="minor")
    assert r["target_tag"] == "2.11-1"


def test_airflow_for_tag_resolves_deprecated():
    # Current-version lookup spans stable + deprecated so EOL runtimes aren't
    # treated as "unknown".
    assert rt.airflow_for_tag("2.9-5") == "2.9.3"
    assert rt.airflow_for_tag("3.1-5") == "3.1.0"   # stable still works
    assert rt.airflow_for_tag("9.9-9") is None      # genuinely unknown


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


def test_provider_pypi_failure_is_reported_not_crashed(monkeypatch):
    monkeypatch.setattr(rt, "_http_json", lambda url: (_ for _ in ()).throw(OSError("boom")))
    p = rt._provider_latest("apache-airflow-providers-amazon", "9.0.0", "minor")
    assert p["tier"] == "none"
    assert p["target"] == "9.0.0"          # unchanged
    assert "PyPI lookup failed" in p["note"]


def test_provider_airflow_compat_clamp(monkeypatch):
    # A provider can raise its minimum Airflow above what the project runs.
    # 1.30/1.36 need Airflow 2.11; 1.16/1.20 need 2.9. Landing on Airflow 2.10.3,
    # the bump must be held at the newest release that still fits (1.20.0).
    pkg = "apache-airflow-providers-common-sql"
    listing = {"releases": {v: [{"yanked": False}] for v in ("1.16.0", "1.20.0", "1.30.0", "1.36.0")}}
    floors = {"1.16.0": "2.9.0", "1.20.0": "2.9.0", "1.30.0": "2.11.0", "1.36.0": "2.11.0"}

    def fake(url: str):
        if url.endswith(f"{pkg}/json"):
            return listing
        for v, af in floors.items():
            if url.endswith(f"{pkg}/{v}/json"):
                return {"info": {"requires_dist": [f"apache-airflow>={af}"]}}
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(rt, "_http_json", fake)

    held = rt._provider_latest(pkg, "1.16.0", "minor", target_airflow="2.10.3")
    assert held["target"] == "1.20.0"
    assert held["tier"] == "minor"
    assert held["clamped"] is True
    assert "2.10.3" in held["note"]

    # Landing on 2.11.2, the newest (1.36.0) fits -> no compat clamp.
    fits = rt._provider_latest(pkg, "1.16.0", "minor", target_airflow="2.11.2")
    assert fits["target"] == "1.36.0"
    assert fits["note"] == ""

    # Unknown landing Airflow (e.g. digest-pinned runtime) -> no compat clamp.
    unknown = rt._provider_latest(pkg, "1.16.0", "minor")
    assert unknown["target"] == "1.36.0"


def test_provider_compat_clamp_no_compatible_release(monkeypatch):
    # Every candidate above current needs a newer Airflow than we're landing on
    # -> leave the pin untouched rather than ship an incompatible bump.
    pkg = "apache-airflow-providers-common-sql"
    listing = {"releases": {v: [{"yanked": False}] for v in ("1.16.0", "1.20.0", "1.30.0")}}
    floors = {"1.16.0": "2.9.0", "1.20.0": "2.9.0", "1.30.0": "2.11.0"}

    def fake(url: str):
        if url.endswith(f"{pkg}/json"):
            return listing
        for v, af in floors.items():
            if url.endswith(f"{pkg}/{v}/json"):
                return {"info": {"requires_dist": [f"apache-airflow>={af}"]}}
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(rt, "_http_json", fake)
    # Landing on Airflow 2.8.0: 1.20 (needs 2.9) and 1.30 (needs 2.11) both exceed it.
    p = rt._provider_latest(pkg, "1.16.0", "minor", target_airflow="2.8.0")
    assert p["target"] == "1.16.0"   # unchanged
    assert p["tier"] == "none"
    assert "compatible" in p["note"]


def test_provider_no_stable_releases(monkeypatch):
    # Only prereleases + a fully-yanked release -> nothing installable -> no bump.
    monkeypatch.setattr(rt, "_http_json", lambda url: {
        "releases": {"9.1.0rc1": [{"yanked": False}], "9.0.0": [{"yanked": True}]}
    })
    p = rt._provider_latest("apache-airflow-providers-amazon", "8.0.0", "major")
    assert p["tier"] == "none"
    assert "no stable releases" in p["note"]


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


def test_held_airflow_major_advises_not_raise_scope(tmp_path, monkeypatch):
    # On Airflow 2 (2.10-12) asking for 'latest' with max-scope=minor: the minor
    # bump to newest AF2 (2.11-1) IS authored, but the withheld jump is the AF3
    # major. That must set the guided-upgrade advisory + held flag — raising the
    # scope cap would NOT make a scheduled run author the major.
    plan = _run_plan(
        tmp_path, monkeypatch,
        {"runtime": {"tag": "2.10-12", "image_repo": "x/runtime"}, "providers": []},
        TARGET="latest", MAX_SCOPE="minor", INCLUDE_PROVIDERS="false",
    )
    assert plan["overall_tier"] == "minor"
    assert plan["author_changes"] is True
    assert plan["runtime"]["target_tag"] == "2.11-1"
    assert plan["held_airflow_major"] is True
    assert plan["advisory"]
    assert "3.2.2" in plan["advisory"]      # points at the withheld AF3 target
    assert plan["scope_exceeded"] is True


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


def test_provider_only_major_is_authored(tmp_path, monkeypatch):
    # Runtime already at latest (no Airflow move), but a provider major is
    # available with max-scope=major. Provider majors ARE authored — only
    # *Airflow* majors are advisory-only.
    plan = _run_plan(
        tmp_path, monkeypatch,
        {"runtime": {"tag": "3.2-3", "image_repo": "x/runtime"},
         "providers": [{"package": "apache-airflow-providers-amazon", "pinned_version": "9.0.0"}]},
        TARGET="latest", MAX_SCOPE="major",
    )
    assert plan["overall_tier"] == "major"
    assert plan["author_changes"] is True
    assert plan["advisory"] == ""


def test_digest_pinned_runtime_is_refused(tmp_path, monkeypatch):
    plan = _run_plan(
        tmp_path, monkeypatch,
        {"runtime": {"tag": "3.1-5", "image_repo": "x/runtime", "digest": "sha256:deadbeef"},
         "providers": []},
        TARGET="latest", MAX_SCOPE="major", INCLUDE_PROVIDERS="false",
    )
    assert plan["runtime"]["tier"] == "none"           # not bumped
    assert "digest-pinned" in plan["runtime"]["note"]
    assert plan["runtime"]["target_tag"] == "3.1-5"    # tag unchanged
    # …but the current Airflow is still resolved from the tag, so Otto/verify
    # have a real version to work against (3.1-5 -> Airflow 3.1.0 in the fixture).
    assert plan["runtime"]["current_airflow"] == "3.1.0"
    assert plan["no_update"] is True

    # An unknown digest-pinned tag leaves current_airflow None (graceful).
    plan2 = _run_plan(
        tmp_path, monkeypatch,
        {"runtime": {"tag": "9.9-9", "image_repo": "x/runtime", "digest": "sha256:dead"},
         "providers": []},
        TARGET="latest", MAX_SCOPE="major", INCLUDE_PROVIDERS="false",
    )
    assert plan2["runtime"]["current_airflow"] is None
