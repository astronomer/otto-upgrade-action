"""Resolve safe upgrade targets for an Astro project and tier each jump.

Reads the detected current versions (runtime tag + provider pins) and produces
an upgrade *plan*: for the runtime and each pinned provider, the version to move
to, the tier of that move (patch / minor / major), and whether the move had to
be clamped to stay inside the caller's `max-upgrade-scope`.

Two public data sources, no Astronomer credentials needed (this step is meant to
run unauthenticated so it works in CI / `act` without secrets):

  - Astronomer Runtime:  https://updates.astronomer.io/astronomer-runtime
  - Providers:           https://pypi.org/pypi/<package>/json

Tiering is driven by the *Airflow* version behind each runtime tag, not the tag
string, so it is correct regardless of the runtime tag scheme (AF2-era semver
tags like ``12.12.0`` vs AF3-era ``3.2-5``).

Design choice — majors are advisory-only. A scheduled bot must never author an
Airflow 2->3 (or any major) migration PR unattended; that is the guided-upgrade
path. When the only available jump is a major, the plan sets
``author_changes=false`` and carries an advisory instead of a diff.

Env in:
  CURRENT_FILE    path to detect-versions JSON  (required)
  TARGET          patch | latest-minor | latest (default latest-minor)
  MAX_SCOPE       patch | minor | major         (default minor)
  INCLUDE_PROVIDERS  true | false               (default true)
  RUNTIME_FEED_URL / PYPI_BASE_URL  override endpoints (tests / air-gapped)

Writes the plan JSON to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from typing import Any

RUNTIME_FEED_URL = os.environ.get(
    "RUNTIME_FEED_URL", "https://updates.astronomer.io/astronomer-runtime"
)
PYPI_BASE_URL = os.environ.get("PYPI_BASE_URL", "https://pypi.org/pypi")

TIER_ORDER = {"patch": 0, "minor": 1, "major": 2, "none": -1}
_PRERELEASE = re.compile(r"(a|b|rc|dev|pre|post)", re.IGNORECASE)


def _http_json(url: str) -> Any:
    # Trusted hosts only (the Runtime feed and PyPI); tests stub this out.
    req = urllib.request.Request(url, headers={"User-Agent": "otto-upgrade-action"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def version_tuple(v: str) -> tuple[int, ...]:
    """Best-effort numeric tuple for a version string.

    ``3.2.1`` -> (3, 2, 1); ``3.1-17`` -> (3, 1, 17). Non-numeric trailers
    (rc1, dev0) are dropped — callers gate prereleases separately.
    """
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums) if nums else (0,)


def is_prerelease(v: str) -> bool:
    # A trailing rc/dev/b segment, e.g. 21.0.0rc1 or 9.1.0.dev0.
    return bool(_PRERELEASE.search(v.split("+", 1)[0]))


def tier_between(cur: str, tgt: str) -> str:
    """patch / minor / major between two Airflow (or semver) versions."""
    c, t = version_tuple(cur), version_tuple(tgt)
    c = (c + (0, 0, 0))[:3]
    t = (t + (0, 0, 0))[:3]
    if t == c:
        return "none"
    if t[0] != c[0]:
        return "major"
    if t[1] != c[1]:
        return "minor"
    return "patch"


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
def _runtime_candidates() -> list[dict[str, Any]]:
    """Flatten the runtime feed into stable candidates with airflow versions."""
    feed = _http_json(RUNTIME_FEED_URL)
    out: list[dict[str, Any]] = []
    for key in ("runtimeVersionsV3", "runtimeVersions"):
        for tag, entry in (feed.get(key) or {}).items():
            meta = entry.get("metadata", {})
            if meta.get("channel") != "stable":
                continue
            af = meta.get("airflowVersion")
            if not af:
                continue
            out.append(
                {
                    "tag": tag,
                    "airflow": af,
                    "release_date": meta.get("releaseDate", ""),
                    "scheme": "v3" if key == "runtimeVersionsV3" else "legacy",
                }
            )
    return out


def _newest(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Newest candidate, ordered by (airflow version, release date)."""
    if not cands:
        return None
    return max(cands, key=lambda c: (version_tuple(c["airflow"]), c["release_date"]))


def resolve_runtime(current_tag: str, target: str, max_scope: str) -> dict[str, Any]:
    cands = _runtime_candidates()
    by_tag = {c["tag"]: c for c in cands}
    cur = by_tag.get(current_tag)

    if cur is None:
        return {
            "current_tag": current_tag,
            "current_airflow": None,
            "target_tag": current_tag,
            "tier": "none",
            "clamped": False,
            "available_latest_tag": (_newest(cands) or {}).get("tag"),
            "note": f"Runtime tag '{current_tag}' not found in the stable feed; "
            "skipping the runtime bump. Pin a published Runtime tag to enable it.",
        }

    cur_af = cur["airflow"]
    cm = version_tuple(cur_af)
    cur_major, cur_minor = (cm + (0, 0))[:2]

    same_minor = [c for c in cands if (version_tuple(c["airflow"]) + (0, 0))[:2] == (cur_major, cur_minor)]
    same_major = [c for c in cands if version_tuple(c["airflow"])[0] == cur_major]

    if target == "patch":
        pick = _newest(same_minor)
    elif target == "latest":
        pick = _newest(cands)
    else:  # latest-minor: newest within the current Airflow major
        pick = _newest(same_major)

    pick = pick or cur
    tier = tier_between(cur_af, pick["airflow"])

    # Clamp to max-upgrade-scope. If the natural pick is too big a jump, fall
    # back to the newest candidate that stays within scope.
    clamped = False
    if TIER_ORDER[tier] > TIER_ORDER[max_scope]:
        clamped = True
        if max_scope == "patch":
            pick = _newest(same_minor) or cur
        elif max_scope == "minor":
            pick = _newest(same_major) or cur
        tier = tier_between(cur_af, pick["airflow"])

    return {
        "current_tag": current_tag,
        "current_airflow": cur_af,
        "target_tag": pick["tag"],
        "target_airflow": pick["airflow"],
        "tier": tier,
        "clamped": clamped,
        "available_latest_tag": (_newest(cands) or {}).get("tag"),
        "note": "",
    }


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
def _provider_latest(package: str, cur: str, max_scope: str) -> dict[str, Any]:
    try:
        data = _http_json(f"{PYPI_BASE_URL}/{package}/json")
    except Exception as exc:  # network / 404 — report, don't crash the plan
        return {"package": package, "current": cur, "target": cur, "tier": "none",
                "clamped": False, "note": f"PyPI lookup failed: {exc}"}

    releases = data.get("releases", {})
    stable = []
    for ver, files in releases.items():
        if is_prerelease(ver):
            continue
        if files and all(f.get("yanked") for f in files):
            continue
        stable.append(ver)
    if not stable:
        return {"package": package, "current": cur, "target": cur, "tier": "none",
                "clamped": False, "note": "no stable releases found"}

    cm = version_tuple(cur)
    cur_major, cur_minor = (cm + (0, 0))[:2]

    def best(pool: list[str]) -> str:
        return max(pool, key=version_tuple)

    latest = best(stable)
    target = latest
    tier = tier_between(cur, target)
    clamped = False
    if TIER_ORDER[tier] > TIER_ORDER[max_scope]:
        clamped = True
        if max_scope == "patch":
            pool = [v for v in stable if (version_tuple(v) + (0, 0))[:2] == (cur_major, cur_minor)]
        else:  # minor
            pool = [v for v in stable if version_tuple(v)[0] == cur_major]
        target = best(pool) if pool else cur
        tier = tier_between(cur, target)

    # Never propose a downgrade (current pin newer than anything on the index we'd pick).
    if version_tuple(target) < cm:
        target, tier, clamped = cur, "none", False

    return {"package": package, "current": cur, "target": target, "tier": tier,
            "clamped": clamped, "available_latest": latest, "note": ""}


def main() -> int:
    current = json.load(open(os.environ["CURRENT_FILE"]))
    target = os.environ.get("TARGET", "latest-minor")
    max_scope = os.environ.get("MAX_SCOPE", "minor")
    include_providers = os.environ.get("INCLUDE_PROVIDERS", "true").lower() == "true"

    if max_scope not in TIER_ORDER:
        print(f"::error::invalid MAX_SCOPE '{max_scope}'", file=sys.stderr)
        return 2

    plan: dict[str, Any] = {"runtime": None, "providers": []}

    rt = current.get("runtime")
    if rt and rt.get("tag"):
        plan["runtime"] = resolve_runtime(rt["tag"], target, max_scope)
        plan["runtime"]["image_repo"] = rt.get("image_repo", "")

    if include_providers:
        for p in current.get("providers", []):
            if not p.get("pinned_version"):
                plan["providers"].append(
                    {"package": p["package"], "current": None, "target": None,
                     "tier": "none", "clamped": False,
                     "note": "unpinned; skipped (can only bump exact pins safely)"}
                )
                continue
            plan["providers"].append(
                _provider_latest(p["package"], p["pinned_version"], max_scope)
            )

    # Roll up.
    tiers = []
    if plan["runtime"]:
        tiers.append(plan["runtime"]["tier"])
    tiers += [p["tier"] for p in plan["providers"]]
    overall = "none"
    for t in tiers:
        if TIER_ORDER.get(t, -1) > TIER_ORDER[overall]:
            overall = t
    plan["overall_tier"] = overall
    plan["no_update"] = overall == "none"

    # Majors are advisory-only: never auto-author a major migration.
    plan["author_changes"] = overall in ("patch", "minor")
    plan["needs_migration"] = overall in ("minor", "major")

    held = [c for c in ([plan["runtime"]] if plan["runtime"] else []) + plan["providers"]
            if c.get("clamped")]
    plan["scope_exceeded"] = bool(held)

    advisory = ""
    if overall == "major":
        rt_af = (plan.get("runtime") or {}).get("current_airflow") or "your current version"
        rt_t = (plan.get("runtime") or {}).get("target_airflow") or "the next major"
        advisory = (
            f"A major Airflow upgrade is available ({rt_af} -> {rt_t}). Major "
            "migrations are not auto-authored by this action — run the guided "
            "upgrade (`astro otto`, Airflow upgrade workflow) and review the "
            "breaking changes interactively."
        )
    plan["advisory"] = advisory

    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
