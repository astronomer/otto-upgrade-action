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


def test_cross_line_report_is_flagged_lower_bound():
    assert sf.collect(PAGE, "3.2-3", "3.3-2")["lower_bound"] is True
    assert sf.collect(PAGE, "3.3-1", "3.3-2")["lower_bound"] is False


@pytest.mark.parametrize("mutation", [
    ("### Security fixes", "#### Security fixes"),      # level change
    ("### Security fixes", "### Security updates"),      # rename, keeps the word
    ("### Security fixes", "### Security fixes (CVEs)"),  # parenthetical
])
def test_unrecognized_security_heading_in_crossed_build_fails_closed(mutation):
    page = PAGE.replace(*mutation)
    report = sf.collect(page, "3.3-1", "3.3-2")
    assert report["status"] == "shape-mismatch"
    assert "unrecognized format" in report["reason"]


def test_sitewide_heading_rename_without_the_word_security_trips_canary():
    # A rename the heading probe can't see ("Vulnerability patches") makes
    # every section parse as absent — the page-wide canary must refuse to
    # report a confident zero.
    page = PAGE.replace("### Security fixes", "### Vulnerability patches")
    report = sf.collect(page, "3.3-1", "3.3-2")
    assert report["status"] == "shape-mismatch"
    assert "anywhere on the page" in report["reason"]


def test_numbered_list_entries_fail_closed():
    page = PAGE.replace(
        "* Fixed [CVE-2026-49298](https://avd.aquasec.com/nvd/cve-2026-49298)\n"
        "* Fixed [GHSA-65pc-fj4g-8rjx](https://github.com/advisories/GHSA-65pc-fj4g-8rjx)",
        "1. Fixed [CVE-2026-49298](https://avd.aquasec.com/nvd/cve-2026-49298)",
    )
    report = sf.collect(page, "3.3-1", "3.3-2")
    assert report["status"] == "shape-mismatch"


def test_link_with_markdown_title_keeps_id_and_url():
    page = ('## Astro Runtime 3.3-2\n\n### Security fixes\n\n'
            '* Fixed [CVE-2026-1](https://example.com/cve-2026-1 "advisory")\n')
    report = sf.collect(page, "3.3-1", "3.3-2")
    assert report["fixes"] == [
        {"id": "CVE-2026-1", "url": "https://example.com/cve-2026-1",
         "builds": ["3.3-2"]}]


class TestFetchHardening:
    class _Resp:
        def __init__(self, body, headers):
            self._body = body
            self.headers = headers

        def read(self, n=None):
            return self._body if n is None else self._body[:n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _serve(self, monkeypatch, responses):
        """responses: list consumed per urlopen call — (body, headers) or Exception."""
        calls = iter(responses)
        seen = {}

        def fake_urlopen(req, timeout):
            seen["accept-encoding"] = req.headers.get("Accept-encoding")
            item = next(calls)
            if isinstance(item, Exception):
                raise item
            return self._Resp(*item)

        monkeypatch.setattr(sf.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(sf.time, "sleep", lambda _s: None)
        return seen

    def test_requests_identity_encoding(self, monkeypatch):
        seen = self._serve(monkeypatch, [(b"# ok", {"Content-Type": "text/markdown"})])
        assert sf._fetch_text("https://x") == "# ok"
        assert seen["accept-encoding"] == "identity"

    def test_gzip_body_is_decompressed(self, monkeypatch):
        import gzip as gz
        body = gz.compress(b"## Astro Runtime 3.3-2\n")
        self._serve(monkeypatch, [(body, {"Content-Encoding": "gzip",
                                          "Content-Type": "text/markdown"})])
        assert "3.3-2" in sf._fetch_text("https://x")

    def test_gzip_magic_without_header_is_decompressed(self, monkeypatch):
        import gzip as gz
        body = gz.compress(b"content")
        self._serve(monkeypatch, [(body, {"Content-Type": "text/markdown"})])
        assert sf._fetch_text("https://x") == "content"

    def test_garbage_bytes_retry_once_then_surface_forensics(self, monkeypatch):
        # The field failure: one bad edge response, identical request fine after.
        self._serve(monkeypatch, [
            (b"\xa5\x01\x02\x03junk", {"Content-Type": "text/markdown"}),
            (b"# recovered", {"Content-Type": "text/markdown"}),
        ])
        assert sf._fetch_text("https://x") == "# recovered"

    def test_persistent_garbage_raises_with_diagnostics(self, monkeypatch):
        bad = (b"\xa5\x01\x02\x03junk", {"Content-Type": "text/markdown",
                                         "Content-Encoding": "br"})
        self._serve(monkeypatch, [bad, bad])
        with pytest.raises(RuntimeError) as exc:
            sf._fetch_text("https://x")
        msg = str(exc.value)
        assert "content-encoding=br" in msg
        assert "0xa5010203" in msg

    def test_transient_network_error_retries_once(self, monkeypatch):
        self._serve(monkeypatch, [
            OSError("connection reset"),
            (b"# ok", {"Content-Type": "text/markdown"}),
        ])
        assert sf._fetch_text("https://x") == "# ok"

    def test_persistent_urllib_failure_falls_back_to_curl(self, monkeypatch):
        bad = (b"\xa5\x01junk", {"Content-Type": "text/markdown"})
        self._serve(monkeypatch, [bad, bad])

        def fake_curl(cmd, **_kw):
            assert "--compressed" in cmd

            class P:
                returncode = 0
                stdout = "# via curl"
                stderr = ""
            return P()

        monkeypatch.setattr(sf.subprocess, "run", fake_curl)
        assert sf._fetch_text("https://x") == "# via curl"

    def test_curl_failure_surfaces_the_urllib_error(self, monkeypatch):
        bad = (b"\xa5\x01junk", {"Content-Type": "text/markdown"})
        self._serve(monkeypatch, [bad, bad])

        def fake_curl(cmd, **_kw):
            class P:
                returncode = 127
                stdout = ""
                stderr = "curl: not found"
            return P()

        monkeypatch.setattr(sf.subprocess, "run", fake_curl)
        with pytest.raises(RuntimeError) as exc:
            sf._fetch_text("https://x")
        assert "not UTF-8 text" in str(exc.value)  # the primary diagnosis wins

    def test_compression_bomb_is_rejected_bounded(self, monkeypatch):
        import gzip as gz
        bomb = gz.compress(b"\x00" * (20 * 1024 * 1024))  # tiny body, 20MB payload
        self._serve(monkeypatch, [
            (bomb, {"Content-Encoding": "gzip", "Content-Type": "text/markdown"}),
            (bomb, {"Content-Encoding": "gzip", "Content-Type": "text/markdown"}),
        ])
        monkeypatch.setattr(
            sf.subprocess, "run",
            lambda *a, **k: pytest.fail("curl must not be reached with a bounded reject"))

        class NoCurl:
            returncode = 22
            stdout = ""
            stderr = "rejected"
        monkeypatch.setattr(sf.subprocess, "run", lambda *a, **k: NoCurl())
        with pytest.raises(RuntimeError) as exc:
            sf._fetch_text("https://x")
        assert "size cap" in str(exc.value)

    def test_oversized_raw_body_is_rejected(self, monkeypatch):
        big = b"a" * (sf._MAX_BODY + 10)
        self._serve(monkeypatch, [(big, {"Content-Type": "text/markdown"}),
                                  (big, {"Content-Type": "text/markdown"})])

        class NoCurl:
            returncode = 22
            stdout = ""
            stderr = "rejected"
        monkeypatch.setattr(sf.subprocess, "run", lambda *a, **k: NoCurl())
        with pytest.raises(RuntimeError) as exc:
            sf._fetch_text("https://x")
        assert "size cap" in str(exc.value)
