"""security_fixes.py scrapes the Runtime release notes for shipped CVE fixes.

The page fetch is stubbed with a fixture mirroring the real page shape
(## Astro Runtime <tag> headings, ### Security fixes bullets with
CVE/GHSA/PYSEC links, legacy X.Y.Z tags further down).
"""

import contextlib
import io
import json

import pytest
import security_fixes as sf

PAGE = """\
Some intro prose and a subscribe link.

## Astro Runtime 3.3-2

Released July 9, 2026.

### Security fixes

* Fixed [CVE-2026-49298](https://avd.aquasec.com/nvd/cve-2026-49298)
* Fixed [GHSA-65pc-fj4g-8rjx](https://github.com/advisories/GHSA-65pc-fj4g-8rjx)

### Additional improvements

* Something unrelated with a [link](https://example.com) that must not count.

## Astro Runtime 3.3-1

### Security fixes

* Fixed [CVE-2026-49298](https://avd.aquasec.com/nvd/cve-2026-49298)
* Fixed [PYSEC-2026-24](https://osv.dev/vulnerability/PYSEC-2026-24)

## Astro Runtime 3.2-5

### Security fixes

* Fixed [CVE-2026-11111](https://avd.aquasec.com/nvd/cve-2026-11111)

## Astro Runtime 3.2-3

No security section in this build.

## Astro Runtime 13.8.0

### Security fixes

* Fixed [CVE-2024-00001](https://avd.aquasec.com/nvd/cve-2024-00001)
"""


def test_same_line_upgrade_counts_only_builds_above_current():
    report = sf.collect(PAGE, "3.3-1", "3.3-2")
    assert report["status"] == "ok"
    assert report["crossed"] == ["3.3-2"]
    ids = {f["id"] for f in report["fixes"]}
    assert ids == {"CVE-2026-49298", "GHSA-65pc-fj4g-8rjx"}


def test_cross_line_upgrade_scopes_to_target_line_and_dedupes():
    # 3.2-3 -> 3.3-2 must NOT claim 3.2-5's fix (that build can postdate the
    # target image), and the CVE listed in both 3.3 builds counts once.
    report = sf.collect(PAGE, "3.2-3", "3.3-2")
    assert report["crossed"] == ["3.3-1", "3.3-2"]
    by_id = {f["id"]: f for f in report["fixes"]}
    assert set(by_id) == {"CVE-2026-49298", "PYSEC-2026-24", "GHSA-65pc-fj4g-8rjx"}
    assert by_id["CVE-2026-49298"]["builds"] == ["3.3-1", "3.3-2"]
    assert report["total"] == 3
    assert "CVE-2026-11111" not in by_id
    assert "CVE-2024-00001" not in by_id


def test_non_security_sections_do_not_leak_links():
    report = sf.collect(PAGE, "3.3-1", "3.3-2")
    assert all(f["url"] != "https://example.com" for f in report["fixes"])


def test_target_missing_from_page_is_loud_shape_mismatch():
    report = sf.collect(PAGE, "3.2-3", "3.3-9")
    assert report["status"] == "shape-mismatch"
    assert "3.3-9" in report["reason"]


def test_headingless_page_is_loud_shape_mismatch():
    report = sf.collect("totally different page now", "3.2-3", "3.3-2")
    assert report["status"] == "shape-mismatch"
    assert "format may have changed" in report["reason"]


def test_build_with_no_security_section_yields_zero_fixes():
    report = sf.collect(PAGE, "3.2-1", "3.2-3")
    assert report["status"] == "ok"
    assert report["crossed"] == ["3.2-3"]
    assert report["fixes"] == [] and report["total"] == 0


def test_linkless_bullet_keeps_text_without_url():
    page = "## Astro Runtime 3.3-2\n\n### Security fixes\n\n* Fixed CVE-2026-7 in the base image\n"
    report = sf.collect(page, "3.3-1", "3.3-2")
    assert report["fixes"] == [
        {"id": "CVE-2026-7 in the base image", "url": None, "builds": ["3.3-2"]}]


def _main(tmp_path, monkeypatch, plan, page=PAGE, fetch_error=None):
    plan_f = tmp_path / "plan.json"
    plan_f.write_text(json.dumps(plan))
    monkeypatch.setenv("PLAN_FILE", str(plan_f))
    if fetch_error is not None:
        def boom(_url):
            raise fetch_error
        monkeypatch.setattr(sf, "_fetch_text", boom)
    else:
        monkeypatch.setattr(sf, "_fetch_text", lambda _url: page)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert sf.main() == 0
    return json.loads(buf.getvalue())


@pytest.mark.parametrize("runtime", [
    None,
    {"current_tag": "3.3-2", "target_tag": "3.3-2"},
    {"current_tag": None, "target_tag": "3.3-2"},
])
def test_main_skips_when_runtime_unchanged(tmp_path, monkeypatch, runtime):
    report = _main(tmp_path, monkeypatch, {"runtime": runtime})
    assert report == {"checked": False, "reason": "runtime unchanged"}


def test_main_reports_fetch_failure_loudly_and_exits_zero(tmp_path, monkeypatch):
    report = _main(
        tmp_path, monkeypatch,
        {"runtime": {"current_tag": "3.2-3", "target_tag": "3.3-2"}},
        fetch_error=OSError("offline"),
    )
    assert report["status"] == "unavailable"
    assert "offline" in report["reason"]


def test_main_end_to_end_ok(tmp_path, monkeypatch):
    report = _main(
        tmp_path, monkeypatch,
        {"runtime": {"current_tag": "3.2-3", "target_tag": "3.3-2"}},
    )
    assert report["status"] == "ok" and report["total"] == 3
