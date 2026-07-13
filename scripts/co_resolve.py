"""Reconcile bumped provider pins with the project's own requirements.

A provider bump the plan chose can be unsatisfiable NEXT TO the user's other
pins (field case: common-ai 0.6.0 requires pydantic-ai-slim>=2.0.0 while the
project pins 1.107.0). The action never edits user-owned dependencies, and
Otto is forbidden from touching pins — so the resolver has to be the smart
one: walk the conflicting provider back to the newest in-scope version that
co-resolves, and say why in the plan note, including which pin to raise to
take the newer provider.

Runs AFTER apply_bump (requirements.txt already carries the bumped pins) and
BEFORE the Otto prompt is built, so the migration and the PR both see the
final pin set. Only ever moves providers the plan bumped, only downward, never
below the current pin, and never touches the runtime tag — conflicts with the
image's bundled constraints remain verification's job.

Best-effort by design: exits 0 even when a conflict can't be attributed or
resolved. Verification is the backstop; this step just prevents avoidable
failures.

Env in:
  PROJECT_PATH   project root (default ".")
  PLAN_FILE      resolve_target.py output JSON (required; updated in place)
Writes a JSON summary of adjustments to stdout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import apply_bump
import resolve_target as rt

# Each attributed conflict costs one `uv pip compile` (~seconds, cached); the
# cap bounds pathological chains, not the common one-conflict case.
_MAX_COMPILES = 10

# "And because you require pydantic-ai-slim[openai]==1.107.0, we can ..."
_BLOCKING_PIN = re.compile(
    r"you require (?P<pin>[A-Za-z0-9_.\-]+(?:\[[^\]]*\])?==[0-9][^\s,]*)"
)


def _blocking_pin_for(err: str, pkg: str) -> str | None:
    """The user pin gating ``pkg``, from uv's error text.

    A multi-conflict error carries several "you require X==..." clauses; the
    first can belong to an unrelated conflict. In uv's derivations the gating
    pin FOLLOWS the offender's mention ("Because <offender>... and you require
    <pin>"), so take the first clause after the offender; fall back to the
    first clause anywhere when none follows.
    """
    anchor = err.find(pkg)
    fallback = None
    for m in _BLOCKING_PIN.finditer(err):
        pin = m.group("pin")
        if pin.startswith(pkg):
            continue
        if anchor >= 0 and m.start() >= anchor:
            return pin
        if fallback is None:
            fallback = pin
    return fallback


def compile_requirements(project_path: str) -> tuple[int, str]:
    req = os.path.join(project_path, "requirements.txt")
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["uv", "pip", "compile", req, "-o", os.devnull, "--no-header"],  # noqa: S607 — uv from PATH by design

        capture_output=True, text=True, timeout=300, check=False,
    )
    return proc.returncode, proc.stderr


def in_scope_versions(package: str, above: str, below: str) -> list[str]:
    """Installable stable versions strictly between two pins, newest first."""
    try:
        data = rt._http_json(f"{rt.PYPI_BASE_URL}/{package}/json")  # noqa: SLF001
    except Exception:  # noqa: BLE001 — no candidates just means "hold at current"
        return []
    pool = []
    for ver, files in data.get("releases", {}).items():
        if rt.is_prerelease(ver):
            continue
        if not files or all(f.get("yanked") for f in files):
            continue
        if rt.version_tuple(above) < rt.version_tuple(ver) < rt.version_tuple(below):
            pool.append(ver)
    return sorted(pool, key=rt.version_tuple, reverse=True)


def main() -> int:
    project_path = os.environ.get("PROJECT_PATH", ".")
    plan_file = os.environ["PLAN_FILE"]
    with open(plan_file, encoding="utf-8") as fh:
        plan = json.load(fh)

    bumped = [p for p in plan.get("providers", [])
              if p.get("current") and p.get("target") and p["current"] != p["target"]]
    req_path = os.path.join(project_path, "requirements.txt")
    summary: dict = {"checked": bool(bumped), "adjustments": []}
    if not bumped or not os.path.isfile(req_path):
        json.dump(summary, sys.stdout, indent=2)
        return 0

    live = {p["package"]: p["target"] for p in bumped}   # pin currently in the file
    original = {p["package"]: p["target"] for p in bumped}
    blocking: dict[str, str] = {}
    pools: dict[str, list[str]] = {}
    adjusted: set[str] = set()
    exhausted: set[str] = set()

    rc, err = compile_requirements(project_path)
    for _ in range(_MAX_COMPILES):
        if rc == 0:
            break
        offender = next((p for p in bumped
                         if p["package"] not in exhausted
                         and f"{p['package']}=={live[p['package']]}" in err), None)
        if offender is None:
            offender = next((p for p in bumped
                             if p["package"] not in exhausted
                             and p["package"] in err
                             and live[p["package"]] != p["current"]), None)
        if offender is None:
            break  # not attributable to a bump we made — verification's problem
        pkg = offender["package"]
        pin = _blocking_pin_for(err, pkg)
        if pin:
            blocking.setdefault(pkg, pin)
        if pkg not in pools:
            pools[pkg] = in_scope_versions(pkg, offender["current"], original[pkg])
        pool = pools[pkg]
        while pool and rt.version_tuple(pool[0]) >= rt.version_tuple(live[pkg]):
            pool.pop(0)
        nxt = pool.pop(0) if pool else offender["current"]
        if nxt == live[pkg]:
            # This offender is walked all the way back; a multi-conflict error
            # may still name another bumped provider — move on to it rather
            # than abandoning the whole reconciliation.
            exhausted.add(pkg)
            continue
        apply_bump.bump_requirements(
            project_path, [{"package": pkg, "current": live[pkg], "target": nxt}])
        live[pkg] = nxt
        adjusted.add(pkg)
        rc, err = compile_requirements(project_path)

    if not adjusted:
        json.dump(summary, sys.stdout, indent=2)
        return 0

    for p in plan.get("providers", []):
        pkg = p.get("package")
        if pkg not in adjusted:
            continue
        final, orig = live[pkg], original[pkg]
        p["target"] = final
        p["tier"] = rt.tier_between(p["current"], final)
        blk = blocking.get(pkg)
        why = (f"{orig} conflicts with your `{blk}` pin" if blk
               else f"{orig} does not resolve together with your other pins")
        advice = f" (raise `{blk.split('==')[0]}` to take {orig})" if blk else ""
        if final == p["current"]:
            p["note"] = f"left at {p['current']}: {why}{advice}"
        else:
            p["note"] = (f"held at {final} — newest version that resolves together "
                         f"with your pins; {why}{advice}")
        summary["adjustments"].append(
            {"package": pkg, "from": orig, "to": final, "blocking_pin": blk})

    # One source of truth for ALL derived aggregates (overall_tier, no_update,
    # author_changes, needs_migration, scope_exceeded, advisory) — a partial
    # re-roll here previously left the others stale after a walk-back.
    rt.roll_up(plan)

    with open(plan_file, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, indent=2)
        fh.write("\n")
    json.dump(summary, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
