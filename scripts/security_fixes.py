"""Collect the security fixes shipped by the Runtime upgrade being proposed.

The public Runtime release notes list per-build "### Security fixes" entries
(CVE/GHSA/PYSEC links). This step reads the markdown variant of that page and
gathers the fixes the upgrade delivers, so the PR body can show the security
payoff of merging — not just version arithmetic.

Scope is deliberately conservative — never claim a fix the target may lack.
Runtime lines interleave in time (a 3.2 build can ship AFTER a 3.3 build with
fixes the 3.3 image doesn't have yet), so only builds in the TARGET's line
count: same Airflow minor as the target, at or below the target build, and —
when the current tag is on the same line — strictly above it. A cross-minor
upgrade therefore under-claims (fixes the new line inherited from its fork
point go uncounted); the PR wording matches what is actually asserted.

Best-effort on DATA, loud on infrastructure: any data miss — page
unreachable, heading shape changed, target build not listed yet — is
recorded in the output JSON (exit 0) so the PR says "could not be
determined" instead of silently omitting the section. A broken invocation
(PLAN_FILE unset/unreadable) still raises: that's an action bug and must
red the run.

Env in:
  PLAN_FILE           resolve_target.py output JSON (required)
  RELEASE_NOTES_URL   override the source page (tests / air-gapped runners)
Writes a JSON report to stdout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zlib

from resolve_target import split_python_variant, version_tuple

RELEASE_NOTES_URL = os.environ.get(
    "RELEASE_NOTES_URL",
    "https://www.astronomer.io/docs/runtime/runtime-release-notes.md",
)

_BUILD_HEADING = re.compile(r"^##\s+Astro Runtime\s+(?P<tag>[A-Za-z0-9.\-]+)\s*$", re.M)
_SECURITY_HEADING = re.compile(r"^###\s+Security fixes\s*$", re.M | re.I)
_HEADING_LINE = re.compile(r"^#{1,6}\s.*$", re.M)
_BULLET = re.compile(r"^\s*[*+-]\s+(?P<text>\S.*)$")
_LINK = re.compile(r"\[(?P<id>[^\]]+)\]\((?P<url>[^)\s]+)(?:\s+\"[^\"]*\")?\)")


# Bomb protection, not a growth budget: the caps exist so a hostile/broken
# response (compression bomb, runaway body) can't OOM the step before main()
# emits its loud-skip JSON. Headroom is ~72x today's page (~110 KB for two
# years of builds, ~1.3 KB per entry — decades of growth), and the docs site
# already archives old Runtime lines to a separate page, so the page prunes
# rather than growing monotonically. If the cap is ever hit anyway, the
# outcome is the loud "could not determine" skip, never a wrong count.
_MAX_BODY = 8 * 1024 * 1024


def _decompress_capped(data: bytes) -> bytes:
    # wbits=47 auto-detects gzip and zlib containers.
    d = zlib.decompressobj(47)
    out = d.decompress(data, _MAX_BODY + 1)
    if len(out) > _MAX_BODY:
        raise RuntimeError("decompressed release-notes body exceeds the size cap")
    return out


def _fetch_once(url: str) -> str:
    req = urllib.request.Request(url, headers={  # noqa: S310
        "User-Agent": "otto-upgrade-action",
        "Accept": "text/markdown, text/plain;q=0.9, */*;q=0.1",
        # Field case (Tamara, 2026-07-15): a CDN edge intermittently served a
        # compressed body even though urllib never asked for one, and the
        # blind .decode("utf-8") died on byte 0xa5. Ask for identity
        # explicitly, and decompress defensively below when an edge insists.
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        data = resp.read(_MAX_BODY + 1)
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        ctype = resp.headers.get("Content-Type") or "unknown"
    if len(data) > _MAX_BODY:
        raise RuntimeError("release-notes body exceeds the size cap")
    if encoding in ("gzip", "deflate") or data[:2] == b"\x1f\x8b":
        data = _decompress_capped(data)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Brotli/zstd or plain garbage: stdlib can't decompress those, so
        # surface a reason with enough forensics to diagnose from a PR body.
        raise RuntimeError(
            f"response was not UTF-8 text (content-type={ctype}, "
            f"content-encoding={encoding or 'none'}, "
            f"first-bytes=0x{data[:4].hex()}): {exc}") from None


def _fetch_via_curl(url: str) -> str:
    """Last-resort fetch through curl, which negotiates and decodes brotli —
    stdlib can't. The field failure recurred across runs from the same
    runner (its CDN edge kept serving br), so a urllib retry alone isn't
    enough there. curl ships on every GitHub runner; a machine without it
    just surfaces the original error."""
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["curl", "-fsSL", "--compressed", "--max-time", "30",  # noqa: S607
         "--max-filesize", str(_MAX_BODY),
         "-A", "otto-upgrade-action", url],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"curl fallback failed (rc={proc.returncode}): "
                           f"{proc.stderr.strip()[:200]}")
    return proc.stdout


def _fetch_text(url: str) -> str:
    """Fetch with one urllib retry, then a curl fallback. Field history: one
    runner's CDN edge served brotli persistently (two runs in a row) while
    other runners fetched the same URL cleanly in between."""
    try:
        return _fetch_once(url)
    except Exception:  # noqa: BLE001 — retry once before escalating
        time.sleep(2)
        try:
            return _fetch_once(url)
        except Exception as urllib_exc:  # noqa: BLE001 — brotli edge: curl can decode it
            try:
                return _fetch_via_curl(url)
            except Exception:  # noqa: BLE001 — keep the primary error as the reason
                raise urllib_exc from None


def _parse_builds(page: str) -> list[tuple[str, str]]:
    """(tag, section body) per '## Astro Runtime <tag>' heading, page order."""
    matches = list(_BUILD_HEADING.finditer(page))
    return [
        (m.group("tag"), page[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(page)])
        for i, m in enumerate(matches)
    ]


def _security_entries(body: str) -> list[dict] | None:
    """Fix entries under a build's security heading.

    [] means the build has no security section — a legitimate zero. None
    means the build HAS security content in a shape this parser doesn't
    recognize (heading at another depth/name, entries that aren't bullets):
    fail closed — callers must report a shape mismatch loudly, never let a
    format change on the page masquerade as "zero fixes".
    """
    for h in _HEADING_LINE.finditer(body):
        line = h.group(0)
        if "security" in line.lower() and not _SECURITY_HEADING.match(line):
            return None
    m = _SECURITY_HEADING.search(body)
    if not m:
        return []
    section = body[m.end():]
    nxt = re.search(r"^#{2,3}\s", section, re.M)
    if nxt:
        section = section[:nxt.start()]
    entries = []
    for line in section.splitlines():
        bullet = _BULLET.match(line)
        if not bullet:
            continue
        text = bullet.group("text")
        link = _LINK.search(text)
        if link:
            entries.append({"id": link.group("id"), "url": link.group("url")})
        else:
            entries.append({"id": re.sub(r"^Fixed\s+", "", text).strip(), "url": None})
    if not entries and section.strip():
        return None
    return entries


def _line(tag: str) -> tuple[int, ...]:
    """The release line a build belongs to (Airflow minor): 3.3-2 -> (3, 3)."""
    return version_tuple(tag)[:2]


def collect(page: str, current: str, target: str) -> dict:
    report: dict = {"checked": True, "current": current, "target": target}
    builds = _parse_builds(page)
    if not builds:
        report.update(status="shape-mismatch",
                      reason="no '## Astro Runtime <tag>' headings found; "
                             "the release-notes page format may have changed")
        return report
    if target not in {tag for tag, _ in builds}:
        report.update(status="shape-mismatch",
                      reason=f"target build {target} is not listed on the "
                             "release-notes page (yet)")
        return report

    same_line = _line(current) == _line(target)
    crossed = sorted(
        (tag for tag, _ in builds
         if _line(tag) == _line(target)
         and version_tuple(tag) <= version_tuple(target)
         and (not same_line or version_tuple(tag) > version_tuple(current))),
        key=version_tuple,
    )
    # Parse every build, not just crossed ones: if NOTHING on the whole page
    # yields a recognizable security entry, the format changed globally and a
    # "zero fixes" answer would be a lie with confidence.
    parsed = {tag: _security_entries(body) for tag, body in builds}
    for tag in crossed:
        if parsed[tag] is None:
            report.update(status="shape-mismatch",
                          reason=f"build {tag} has security content in an "
                                 "unrecognized format; refusing to report a count")
            return report
    if not any(parsed.values()):
        report.update(status="shape-mismatch",
                      reason="no security-fix entries recognized anywhere on "
                             "the page; the format may have changed")
        return report
    fixes: dict[str, dict] = {}
    for tag in crossed:
        for entry in parsed[tag]:
            fix = fixes.setdefault(
                entry["id"], {"id": entry["id"], "url": entry["url"], "builds": []})
            fix["builds"].append(tag)
    # Cross-line upgrades under-claim by design (fixes the target line
    # inherited at its fork point are not enumerable from per-line notes) —
    # the count is a lower bound and consumers must present it as one.
    report.update(status="ok", crossed=crossed, lower_bound=not same_line,
                  fixes=list(fixes.values()), total=len(fixes))
    return report


def main() -> int:
    with open(os.environ["PLAN_FILE"], encoding="utf-8") as fh:
        plan = json.load(fh)
    runtime = plan.get("runtime") or {}
    current, target = runtime.get("current_tag"), runtime.get("target_tag")
    # Release notes list base tags only; a python-variant pin
    # (3.3-2-python-3.13) must not read as an unknown build.
    current = split_python_variant(current)[0] if current else current
    target = split_python_variant(target)[0] if target else target
    if not current or not target or current == target:
        json.dump({"checked": False, "reason": "runtime unchanged"}, sys.stdout, indent=2)
        return 0
    try:
        page = _fetch_text(RELEASE_NOTES_URL)
    except Exception as exc:  # noqa: BLE001 — a fetch miss must not fail the run
        json.dump({"checked": True, "status": "unavailable",
                   "current": current, "target": target,
                   "reason": f"release notes fetch failed: {exc}"},
                  sys.stdout, indent=2)
        return 0
    json.dump(collect(page, current, target), sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
