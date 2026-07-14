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

Best-effort by design: always exits 0. Any miss — page unreachable, heading
shape changed, target build not listed yet — is recorded loudly in the output
JSON so the PR says "could not be determined" instead of silently omitting
the section.

Env in:
  PLAN_FILE           resolve_target.py output JSON (required)
  RELEASE_NOTES_URL   override the source page (tests / air-gapped runners)
Writes a JSON report to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

from resolve_target import version_tuple

RELEASE_NOTES_URL = os.environ.get(
    "RELEASE_NOTES_URL",
    "https://www.astronomer.io/docs/runtime/runtime-release-notes.md",
)

_BUILD_HEADING = re.compile(r"^##\s+Astro Runtime\s+(?P<tag>[A-Za-z0-9.\-]+)\s*$", re.M)
_SECURITY_HEADING = re.compile(r"^###\s+Security fixes\s*$", re.M | re.I)
_BULLET = re.compile(r"^\s*[*+-]\s+(?P<text>\S.*)$")
_LINK = re.compile(r"\[(?P<id>[^\]]+)\]\((?P<url>[^)\s]+)\)")


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "otto-upgrade-action"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read().decode("utf-8")


def _parse_builds(page: str) -> list[tuple[str, str]]:
    """(tag, section body) per '## Astro Runtime <tag>' heading, page order."""
    matches = list(_BUILD_HEADING.finditer(page))
    return [
        (m.group("tag"), page[m.end():matches[i + 1].start() if i + 1 < len(matches) else len(page)])
        for i, m in enumerate(matches)
    ]


def _security_entries(body: str) -> list[dict]:
    """Fix entries under a build's 'Security fixes' heading; [] when absent."""
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
    bodies = dict(builds)
    fixes: dict[str, dict] = {}
    for tag in crossed:
        for entry in _security_entries(bodies[tag]):
            fix = fixes.setdefault(
                entry["id"], {"id": entry["id"], "url": entry["url"], "builds": []})
            fix["builds"].append(tag)
    report.update(status="ok", crossed=crossed,
                  fixes=list(fixes.values()), total=len(fixes))
    return report


def main() -> int:
    with open(os.environ["PLAN_FILE"], encoding="utf-8") as fh:
        plan = json.load(fh)
    runtime = plan.get("runtime") or {}
    current, target = runtime.get("current_tag"), runtime.get("target_tag")
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
