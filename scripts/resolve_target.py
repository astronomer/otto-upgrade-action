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
tags like ``12.12.0`` vs AF3-era ``3.2-5``). One exception: a newer Runtime
*build* on the **same** Airflow version (e.g. ``3.1-5`` -> ``3.1-7``, both
Airflow 3.1.2 — a base-image CVE or provider-bundle fix) is a real `patch`-tier
upgrade, not a no-op.

Design choice — Airflow majors are advisory-only. A scheduled bot must never
author an Airflow 2->3 (or any Airflow-major) migration PR unattended; that is
the guided-upgrade path. When the runtime jump resolves to a major, the plan
sets ``author_changes=false`` and carries an advisory instead of a diff. Provider
majors *are* authored (far lower risk than an Airflow major).

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
VALID_TARGETS = {"patch", "latest-minor", "latest"}
# PEP 440 prerelease markers, preceded by a digit or dot so they match both
# `1.0.0rc1` and `1.0.0.dev3`. `post` is intentionally excluded — a post-release
# is a final release that supersedes its base, not a prerelease.
_PRERELEASE = re.compile(r"[.\d](rc|alpha|beta|a|b|c|dev|pre)\d*", re.IGNORECASE)


def _http_json(url: str) -> Any:
    # Trusted hosts only (the Runtime feed and PyPI); tests stub this out.
    req = urllib.request.Request(url, headers={"User-Agent": "otto-upgrade-action"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def version_tuple(v: str) -> tuple[int, ...]:
    """Best-effort numeric tuple for a version string.

    ``3.2.1`` -> (3, 2, 1); ``3.1-17`` -> (3, 1, 17). The PEP 440 epoch and
    local segment are dropped so they don't inflate the comparison
    (``1!2.0.0`` compares as ``2.0.0``); trailing prerelease/post markers are
    ignored — callers gate prereleases separately.
    """
    v = v.split("+", 1)[0]          # drop local segment
    if "!" in v:                    # drop epoch
        v = v.split("!", 1)[1]
    nums = re.findall(r"\d+", v)
    return tuple(int(n) for n in nums) if nums else (0,)


def is_prerelease(v: str) -> bool:
    return bool(_PRERELEASE.search(v.split("+", 1)[0]))


def stable_release_versions(releases: dict) -> list[str]:
    """Installable stable versions from a PyPI ``releases`` mapping.

    The single definition of "installable": not a prerelease, and at least one
    non-yanked artifact. Used for target selection here and for the
    conflict walk in co_resolve.py — the two must never disagree about which
    versions exist.
    """
    return [
        ver for ver, files in releases.items()
        if not is_prerelease(ver) and files and not all(f.get("yanked") for f in files)
    ]


def tier_between(cur: str, tgt: str) -> str:
    """patch / minor / major between two Airflow (or semver) versions."""
    c = (version_tuple(cur) + (0, 0, 0))[:3]
    t = (version_tuple(tgt) + (0, 0, 0))[:3]
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
# Release channels. Upgrade *targets* are stable-only — we never bump a project
# onto a deprecated runtime. But a project may legitimately *run* a deprecated
# (often LTS, sometimes past end-of-support) tag, and that's exactly who should
# upgrade: we must still resolve its Airflow version so we can move it off, not
# give up. Only the deprecated channel joins stable for that "where are we now"
# lookup — prerelease/alpha channels stay off-limits everywhere.
_STABLE: frozenset[str] = frozenset({"stable"})
_STABLE_AND_DEPRECATED: frozenset[str] = frozenset({"stable", "deprecated"})

# Astronomer publishes variant image tags the Runtime feed does NOT list —
# the feed carries base tags plus a `pythonVersions` support list per build.
# Documented variant chains stack: `-python-3.13`, `-python-3.11-base`,
# `-ubi9-python-3.11`, `-slim-base` (see runtime-image-architecture docs).
# Field case (Tamara, 2026-07-15): a variant-pinned project got "tag not
# found in the Runtime feed" and lost runtime bumps entirely.
_VARIANT_START = re.compile(r"-(?:python-\d|ubi\d*\b|slim\b|base\b)")
_PY_IN_SUFFIX = re.compile(r"-python-(\d+\.\d+(?:\.\d+)?)")


def split_python_variant(tag: str) -> tuple[str, str | None, str]:
    """Peel the variant suffix chain off a Runtime tag.

    ``3.2-5-python-3.13``        -> (``3.2-5``,  ``3.13``, ``-python-3.13``)
    ``3.1-14-ubi9-python-3.11``  -> (``3.1-14``, ``3.11``, ``-ubi9-python-3.11``)
    ``13.2.0-python-3.11-slim-base`` -> (``13.2.0``, ``3.11``, suffix)
    ``3.2-5-slim-base``          -> (``3.2-5``,  None,     ``-slim-base``)
    Plain tags pass through as (tag, None, "").

    The FULL suffix is preserved verbatim onto upgrade targets; the Python
    version (when present) additionally filters candidates by feed support.
    """
    m = _VARIANT_START.search(tag)
    if not m or m.start() == 0:
        return tag, None, ""
    base, suffix = tag[:m.start()], tag[m.start():]
    py = _PY_IN_SUFFIX.search(suffix)
    return base, (py.group(1) if py else None), suffix


def _runtime_candidates(channels: frozenset[str] = _STABLE) -> list[dict[str, Any]]:
    """Flatten the runtime feed into candidates with airflow versions.

    ``channels`` selects which release channels to include: stable-only for
    upgrade targets, stable+deprecated when locating where the project is now.
    V3 entries take precedence over legacy ones on a tag-string collision.
    """
    feed = _http_json(RUNTIME_FEED_URL)
    out: list[dict[str, Any]] = []
    for key in ("runtimeVersionsV3", "runtimeVersions"):  # V3 first => precedence
        for tag, entry in (feed.get(key) or {}).items():
            meta = entry.get("metadata", {})
            if meta.get("channel") not in channels:
                continue
            af = meta.get("airflowVersion")
            if not af or is_prerelease(af):  # never target a prerelease Airflow
                continue
            out.append(
                {
                    "tag": tag,
                    "airflow": af,
                    "release_date": meta.get("releaseDate", ""),
                    "channel": meta.get("channel", ""),
                    "scheme": "v3" if key == "runtimeVersionsV3" else "legacy",
                    "python_versions": meta.get("pythonVersions") or [],
                }
            )
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for c in out:
        if c["tag"] in seen:
            continue
        seen.add(c["tag"])
        deduped.append(c)
    return deduped


def _newest(cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Newest candidate, ordered by (airflow version, release date, tag).

    The tag tuple is the tiebreak: two builds of the same Airflow can ship the
    same day (field case: 3.3-1 and 3.3-2 both stable, both 2026-07-09), and
    without it the winner is feed order — which handed out the older build.
    """
    if not cands:
        return None
    return max(cands, key=lambda c: (
        version_tuple(c["airflow"]), c["release_date"], version_tuple(c["tag"])))


def _runtime_tier(current_tag: str, cur_af: str, pick: dict[str, Any]) -> str:
    """Tier of a runtime move. A newer build on the same Airflow is `patch`."""
    tier = tier_between(cur_af, pick["airflow"])
    if tier == "none" and pick["tag"] != current_tag:
        return "patch"
    return tier


def airflow_for_tag(tag: str) -> str | None:
    """Airflow version behind a Runtime tag, or None if the tag isn't in the feed.

    Resolves against stable *and* deprecated channels so a project on an EOL
    runtime still gets a real current Airflow version. Python-variant tags
    resolve via their base tag (the feed lists base tags only).
    """
    base, _py, _suffix = split_python_variant(tag)
    try:
        for c in _runtime_candidates(_STABLE_AND_DEPRECATED):
            if c["tag"] == base:
                return c["airflow"]
    except Exception:  # noqa: BLE001 — feed unreachable; caller treats None as "unknown"
        return None
    return None


def resolve_runtime(current_tag: str, target: str, max_scope: str) -> dict[str, Any]:
    cands = _runtime_candidates()                              # stable-only: upgrade targets
    cur_cands = _runtime_candidates(_STABLE_AND_DEPRECATED)    # +deprecated: locate "now"
    by_tag: dict[str, Any] = {}
    for c in cur_cands:
        by_tag.setdefault(c["tag"], c)

    # A variant suffix (`-python-X.Y`, `-ubi9-...`, `-slim-base`) resolves
    # via its BASE tag and is restored verbatim onto whatever target gets
    # picked, so the user keeps their Python/OS/flavor.
    base_tag, py_pin, variant_suffix = split_python_variant(current_tag)
    cur = by_tag.get(base_tag)

    def _with_variant(tag: str) -> str:
        return f"{tag}{variant_suffix}"

    if cur is None:
        return {
            "current_tag": current_tag,
            "current_airflow": None,
            "target_tag": current_tag,
            "tier": "none",
            "clamped": False,
            "held_major": False,
            "available_latest_tag": (_newest(cands) or {}).get("tag"),
            "note": f"Runtime tag '{current_tag}' not found in the Runtime feed; "
            "skipping the runtime bump. Pin a published Runtime tag to enable it.",
        }

    def _order_key(c: dict[str, Any]) -> tuple:
        return (version_tuple(c["airflow"]), c["release_date"], version_tuple(c["tag"]))

    py_note = ""
    if py_pin and not cur.get("python_versions"):
        # The feed doesn't track Python support for this line (live feed:
        # every legacy 13.x entry omits pythonVersions). Filtering would
        # silently hold ALL upgrades — resolve on the base instead and say
        # the pin wasn't validated.
        py_note = (f"the Runtime feed doesn't list Python support for this "
                   f"line; your Python {py_pin} pin was kept on the target "
                   "but couldn't be validated against it")
    elif py_pin:
        # Only builds that publish the pinned Python are upgrade candidates.
        # A build without pythonVersions metadata is excluded (fail closed:
        # proposing a variant tag that may not exist beats guessing).
        unfiltered_newest = _newest(cands)
        cands = [c for c in cands if py_pin in c["python_versions"]]
        newest = _newest(cands)
        # Full ordering, not Airflow versions: a newer BUILD of the same
        # Airflow (a patch/security refresh) blocked by the pin must be
        # said out loud too, not read as a clean no-update.
        if unfiltered_newest and (
                newest is None or _order_key(newest) < _order_key(unfiltered_newest)):
            if unfiltered_newest["tag"] == base_tag:
                py_note = (f"your current build {base_tag} doesn't list Python "
                           f"{py_pin} support in the feed; no in-scope upgrade "
                           "is possible for this pin")
            else:
                py_note = (f"newer Runtime {unfiltered_newest['tag']} doesn't "
                           f"list Python {py_pin} support, so it isn't a "
                           "candidate for this python-pinned image")

    cur_af = cur["airflow"]
    cm = version_tuple(cur_af)
    cur_major, cur_minor = (cm + (0, 0))[:2]

    # Target pools are stable-only — even when the current runtime is deprecated,
    # we upgrade onto a supported release, never another deprecated one.
    same_minor = [c for c in cands if (version_tuple(c["airflow"]) + (0, 0))[:2] == (cur_major, cur_minor)]
    same_major = [c for c in cands if version_tuple(c["airflow"])[0] == cur_major]

    if target == "patch":
        pick = _newest(same_minor)
    elif target == "latest":
        pick = _newest(cands)
    else:  # latest-minor: newest within the current Airflow major
        pick = _newest(same_major)

    pick = pick or cur
    tier = _runtime_tier(base_tag, cur_af, pick)

    # Clamp to max-upgrade-scope. If the natural pick is too big a jump, fall
    # back to the newest candidate that stays within scope. Track whether the
    # jump we held back was an Airflow *major*: a scheduled run never authors
    # that (advisory-only, regardless of max-upgrade-scope), so the PR must point
    # at the guided upgrade rather than tell the user to raise the cap.
    clamped = False
    held_major = False
    uncapped_target_airflow = pick.get("airflow")
    if TIER_ORDER[tier] > TIER_ORDER[max_scope]:
        clamped = True
        held_major = tier == "major"
        if max_scope == "patch":
            pick = _newest(same_minor) or cur
        elif max_scope == "minor":
            pick = _newest(same_major) or cur
        tier = _runtime_tier(base_tag, cur_af, pick)

    # NEVER author a downgrade. The python filter can remove the CURRENT
    # build from the pool (pin not listed for it), which breaks the implicit
    # pick >= current invariant every other path relies on.
    if _order_key(pick) < _order_key(cur):
        pick = cur
        tier = _runtime_tier(base_tag, cur_af, pick)
        clamped = False
        if py_pin and not py_note:
            py_note = (f"held at {current_tag}: no in-scope build newer than "
                       f"the current one lists Python {py_pin} support")

    out: dict[str, Any] = {
        "current_tag": current_tag,
        "current_airflow": cur_af,
        "target_tag": _with_variant(pick["tag"]) if pick["tag"] != base_tag else current_tag,
        "target_airflow": pick["airflow"],
        "tier": tier,
        "clamped": clamped,
        "held_major": held_major,
        "available_latest_tag": (_newest(cands) or {}).get("tag"),
        "python_pin": py_pin,
        "note": "",
    }
    if held_major:
        out["uncapped_target_airflow"] = uncapped_target_airflow
    if cur.get("channel") == "deprecated":
        # Surface the EOL status as a reason to upgrade, not an error.
        out["current_channel"] = "deprecated"
        out["note"] = (
            f"current Runtime '{current_tag}' (Airflow {cur_af}) is on the "
            "deprecated channel — upgrading off it is recommended."
        )
    if py_note:
        out["note"] = f"{out['note']}; {py_note}" if out["note"] else py_note
    return out


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
# Cap on per-provider compatibility lookups. The compat walk does one PyPI
# metadata call per candidate version it has to rule out; without a cap a
# provider with a long release history could fan a single resolve step into
# dozens of serial HTTP calls. Six covers the realistic "newest few are too new"
# case; if nothing within the newest few fits the landing Airflow, we hold the
# pin rather than walk (and bump to) a much older release.
_COMPAT_WALK_LIMIT = 6


def _airflow_satisfies(spec: str, af: tuple[int, ...]) -> bool:
    """Whether Airflow tuple ``af`` satisfies a PEP 440 specifier string (which
    may be comma-separated), for the operators apache-airflow pins use:
    ``>= > <= < == != ~=``. Clauses we can't parse are treated as satisfied, so
    we never wrongly *block* a bump on a spec we don't understand.
    """
    a = (af + (0, 0, 0))[:3]
    for clause in spec.split(","):
        m = re.match(r"\s*(>=|<=|==|!=|~=|>|<)\s*([0-9][0-9.]*)", clause)
        if not m:
            continue
        op, raw = m.group(1), m.group(2)
        v = (version_tuple(raw) + (0, 0, 0))[:3]
        if (op == ">=" and a < v) or (op == ">" and a <= v) \
           or (op == "<=" and a > v) or (op == "<" and a >= v) \
           or (op == "==" and a != v) or (op == "!=" and a == v):
            return False
        if op == "~=":  # compatible release: >= v AND < next-up of v's prefix
            if a < v:
                return False
            vt = version_tuple(raw)
            if len(vt) >= 2:
                hi = list(vt[:-1])
                hi[-1] += 1
                if a >= (tuple(hi) + (0, 0, 0))[:3]:
                    return False
    return True


def _airflow_specifiers(package: str, version: str) -> list[str] | None:
    """Unconditional core ``apache-airflow`` version specifiers for a provider
    release, parsed from its per-version PyPI ``requires_dist``.

    Returns None on a metadata *lookup failure* (caller treats the release as
    ineligible — fail closed, so a transient PyPI blip can't wave through an
    incompatible pin). Returns ``[]`` when the release declares no Airflow
    constraint (compatible with anything). Extras-gated deps (``; extra == ...``)
    are skipped — they're optional, not an unconditional core requirement.
    """
    try:
        data = _http_json(f"{PYPI_BASE_URL}/{package}/{version}/json")
    except Exception:  # noqa: BLE001 — network/404; caller fails closed on None
        return None
    specs: list[str] = []
    for req in data.get("info", {}).get("requires_dist") or []:
        base, _, marker = req.strip().partition(";")
        if "extra" in marker:
            continue
        # CORE apache-airflow only (optionally with extras), never a provider:
        # the operator must follow immediately, so "apache-airflow-providers-foo"
        # (next char '-') and bare "apache-airflow" (no operator) don't match.
        m = re.match(r"^apache-airflow(?:\[[^\]]*\])?\s*([<>=!~].*)$", base.strip(), re.IGNORECASE)
        if m:
            specs.append(m.group(1).strip())
    return specs


def _provider_compatible(package: str, version: str, af: tuple[int, ...]) -> bool | None:
    """True/False if the release is/ isn't Airflow-compatible; None if unknown
    (metadata lookup failed). Checks the *full* specifier (lower and upper
    bounds), not just a minimum."""
    specs = _airflow_specifiers(package, version)
    if specs is None:
        return None
    return all(_airflow_satisfies(s, af) for s in specs)


def _provider_latest(package: str, cur: str, max_scope: str,
                     target_airflow: str | None = None) -> dict[str, Any]:
    try:
        data = _http_json(f"{PYPI_BASE_URL}/{package}/json")
    except Exception as exc:  # noqa: BLE001 — network / 404; report, don't crash the plan
        return {"package": package, "current": cur, "target": cur, "tier": "none",
                "clamped": False, "note": f"PyPI lookup failed: {exc}"}

    stable = stable_release_versions(data.get("releases", {}))
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

    # Never propose a downgrade (pad both sides so 9.0 vs 9.0.0 isn't a "downgrade").
    if (version_tuple(target) + (0, 0, 0))[:3] < (cm + (0, 0, 0))[:3]:
        target, tier, clamped = cur, "none", False

    note = ""
    # Airflow-compatibility clamp. A newer provider can require a different
    # Airflow than this project runs (e.g. common-sql 1.36 requires Airflow 2.11
    # on a 2.10 project). When we know the Airflow we're landing on, walk down the
    # in-scope candidates to the newest release whose full specifier is satisfied.
    # Skipped when target_airflow is unknown (digest-pinned / unresolved runtime).
    if target_airflow and version_tuple(target) > cm:
        af_t = (version_tuple(target_airflow) + (0, 0, 0))[:3]
        if max_scope == "patch":
            pool = [v for v in stable if (version_tuple(v) + (0, 0))[:2] == (cur_major, cur_minor)]
        elif max_scope == "minor":
            pool = [v for v in stable if version_tuple(v)[0] == cur_major]
        else:
            pool = list(stable)
        # In-scope, above current, at or below the already-chosen target, newest
        # first, capped so a long release history can't fan out unbounded lookups.
        candidates = sorted(
            (v for v in pool if cm < version_tuple(v) <= version_tuple(target)),
            key=version_tuple, reverse=True,
        )[:_COMPAT_WALK_LIMIT]
        # Pick the newest release we can *confirm* compatible. A release whose
        # metadata we couldn't fetch (None) is skipped, not assumed compatible.
        chosen = next((v for v in candidates if _provider_compatible(package, v, af_t) is True), None)
        if chosen is None:
            # Nothing confirmed compatible within the lookup budget. Hold the pin
            # rather than risk an incompatible bump. This is a *compatibility*
            # hold, not a scope clamp — clear `clamped` so it doesn't read as
            # "raise max-upgrade-scope to go further" (raising it wouldn't help).
            note = (f"left at {cur}: no in-scope release confirmed compatible with "
                    f"Airflow {target_airflow} (checked the newest {_COMPAT_WALK_LIMIT})")
            target, tier, clamped = cur, "none", False
        elif chosen != target:
            note = (f"held at {chosen} — newest release compatible with Airflow "
                    f"{target_airflow}; later versions require a different Airflow")
            target = chosen
            tier = tier_between(cur, target)
            clamped = True

    return {"package": package, "current": cur, "target": target, "tier": tier,
            "clamped": clamped, "available_latest": latest, "note": note}


def roll_up(plan: dict) -> None:
    """Recompute every derived aggregate from the runtime + provider entries.

    The single source of truth for plan finalization: called here after the
    plan is built, and again by co_resolve.py after reconciling pins. A partial
    re-roll (only some fields) leaves the others stale and contradictory —
    e.g. `author_changes` still true after every bump was walked back.
    """
    tiers = []
    if plan["runtime"]:
        tiers.append(plan["runtime"]["tier"])
    tiers += [p["tier"] for p in plan["providers"]]
    overall = "none"
    for t in tiers:
        if TIER_ORDER.get(t, -1) > TIER_ORDER[overall]:
            overall = t
    plan["overall_tier"] = overall

    runtime = plan["runtime"] or {}
    runtime_tier = runtime.get("tier", "none")
    held_airflow_major = bool(runtime.get("held_major"))
    # A held Airflow major is advisory-only, never authored — but it is NOT
    # "nothing to do": the action must still surface the guided-upgrade advisory.
    # Excluding it here keeps the run from collapsing into a silent no-op (the
    # advisory step is gated on no-update != true). When the clamp held a major
    # but nothing in-scope was authorable, overall is "none" yet no_update stays
    # false so the advisory is shown.
    plan["no_update"] = overall == "none" and not held_airflow_major
    # Never auto-author an *Airflow* major (runtime jump). Provider majors are
    # authored. Everything patch/minor is authored.
    plan["author_changes"] = overall != "none" and runtime_tier != "major"
    plan["needs_migration"] = overall in ("minor", "major")

    held = [c for c in ([plan["runtime"]] if plan["runtime"] else []) + plan["providers"]
            if c.get("clamped")]
    plan["scope_exceeded"] = bool(held)
    # True when the withheld jump is specifically an Airflow major — which a
    # scheduled run never authors even if max-upgrade-scope is raised. The PR
    # body uses this to point at the guided upgrade instead of "raise the cap".
    plan["held_airflow_major"] = held_airflow_major

    advisory = ""
    if runtime_tier == "major" or held_airflow_major:
        rt_af = runtime.get("current_airflow") or "your current version"
        # When the major was held back by the scope cap, the authored move is a
        # smaller jump; point at the major we declined (uncapped_target_airflow).
        rt_t = (runtime.get("uncapped_target_airflow") if held_airflow_major
                else runtime.get("target_airflow")) or "the next major"
        advisory = (
            f"A major Airflow upgrade is available ({rt_af} -> {rt_t}). Major "
            "migrations are not auto-authored by this action — run the guided "
            "upgrade (`astro otto`, Airflow upgrade workflow) and review the "
            "breaking changes interactively."
        )
    plan["advisory"] = advisory


def main() -> int:
    current = json.load(open(os.environ["CURRENT_FILE"]))
    target = os.environ.get("TARGET", "latest-minor")
    max_scope = os.environ.get("MAX_SCOPE", "minor")
    include_providers = os.environ.get("INCLUDE_PROVIDERS", "true").lower() == "true"

    if target not in VALID_TARGETS:
        print(f"::error::invalid TARGET '{target}' (expected one of {sorted(VALID_TARGETS)})",
              file=sys.stderr)
        return 2
    if max_scope not in TIER_ORDER:
        print(f"::error::invalid MAX_SCOPE '{max_scope}'", file=sys.stderr)
        return 2

    plan: dict[str, Any] = {"runtime": None, "providers": []}

    rt = current.get("runtime")
    if rt and rt.get("tag"):
        if rt.get("digest"):
            # A digest-pinned FROM resolves by digest and ignores the tag, so
            # bumping the tag wouldn't change the built image. We don't bump it —
            # but we still resolve the current Airflow version from the tag so
            # Otto gets a real currentVersion and verify can import against it
            # (the providers can still move; only the runtime is pinned).
            plan["runtime"] = {
                "current_tag": rt["tag"], "current_airflow": airflow_for_tag(rt["tag"]),
                "target_tag": rt["tag"], "tier": "none", "clamped": False,
                "image_repo": rt.get("image_repo", ""),
                "note": "FROM line is digest-pinned (@sha256:...); the Runtime tag is "
                "not auto-bumped. Remove the digest pin to let the action manage it.",
            }
        else:
            plan["runtime"] = resolve_runtime(rt["tag"], target, max_scope)
            plan["runtime"]["image_repo"] = rt.get("image_repo", "")

    # The Airflow providers must stay compatible with: the version we're landing
    # on if the runtime moves, else the current Airflow (provider-only bumps).
    af_for_providers = None
    if plan["runtime"]:
        af_for_providers = (plan["runtime"].get("target_airflow")
                            or plan["runtime"].get("current_airflow"))

    if include_providers:
        for p in current.get("providers", []):
            if not p.get("pinned_version"):
                # Detection may already carry a reason (e.g. duplicate entries
                # with conflicting pins); default to the plain unpinned note.
                entry = {"package": p["package"], "current": None, "target": None,
                         "tier": "none", "clamped": False,
                         "note": p.get("note")
                         or "unpinned; skipped (can only bump exact pins safely)"}
            else:
                entry = _provider_latest(p["package"], p["pinned_version"], max_scope,
                                         af_for_providers)
            if p.get("spec_name"):
                # Original requirements.txt spelling — the PR body shows it when
                # it differs from the normalized package name.
                entry["spec_name"] = p["spec_name"]
            plan["providers"].append(entry)

    roll_up(plan)

    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
