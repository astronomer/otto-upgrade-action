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
import tempfile

import apply_bump
import resolve_target as rt
from detect_versions import normalize_name

# Iteration cap on the reconcile loop. An iteration costs one `uv pip compile`
# (~seconds, cached) — up to four when a pin-raise attempt runs (resolve the
# choice, verify it, revert it, re-verify). Bounds pathological chains, not
# the common one-conflict case.
_MAX_COMPILES = 10

# "And because you require pydantic-ai-slim[openai]==1.107.0, we can ..."
_BLOCKING_PIN = re.compile(
    r"you require (?P<pin>[A-Za-z0-9_.\-]+(?:\[[^\]]*\])?==[0-9][^\s,]*)"
)


def _dependency_spec_for(package: str, version: str, dep_name: str) -> str | None:
    """The full constraint ``package==version`` puts on ``dep_name``, from its
    per-version PyPI ``requires_dist`` — structured metadata, scoped to the
    exact offender and version by construction. uv's error prose can
    interleave clauses from unrelated conflicts and truncate compound
    specifiers, so it is never used for the bound. Names compare under PEP
    503 normalization; extras-gated requirements are skipped (optional, not
    the blocking constraint). None on any miss — the advice then degrades to
    direction-only rather than guessing."""
    try:
        data = rt._http_json(f"{rt.PYPI_BASE_URL}/{package}/{version}/json")  # noqa: SLF001
    except Exception:  # noqa: BLE001 — metadata miss just means no concrete bound
        return None
    want = normalize_name(dep_name)
    for req in data.get("info", {}).get("requires_dist") or []:
        base, _, marker = req.strip().partition(";")
        if "extra" in marker:
            continue
        m = re.match(
            r"^(?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]*\])?\s*(?P<spec>[<>=!~(].*)?$",
            base.strip(),
        )
        if m and normalize_name(m.group("name")) == want:
            spec = (m.group("spec") or "").strip().strip("()")
            return spec or None
    return None


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
    try:
        # No -o: uv writes output ATOMICALLY via a temp file in the output
        # file's directory, so `-o /dev/null` exits 2 on every SUCCESSFUL
        # resolve for non-root users (/dev isn't writable) — which made the
        # rc==0 keep-gate below unpassable on GitHub runners (field case:
        # Tamara's bump-blocking-pins raise silently reverting). Discarding
        # captured stdout is the only safe /dev/null here.
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["uv", "pip", "compile", req, "--no-header"],  # noqa: S607 — uv from PATH by design
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # This step is best-effort under set -euo pipefail: a stalled or
        # vanished uv must degrade to "not attributable" (verification is the
        # backstop), never abort the action.
        return -1, f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stderr


def resolve_pin_choice(project_path: str, pin_base: str,
                       override_spec: str | None = None) -> str | None:
    """The version uv picks for ``pin_base`` when its user pin is lifted.

    Compiles the requirements with an override that relaxes JUST this pin,
    then reads uv's choice from the lockfile it writes. All PEP 440/extras/
    marker semantics stay uv's problem — no homegrown specifier evaluation.
    None when uv still can't resolve or the choice can't be read; callers
    fall back to walking the provider.

    ``override_spec`` must carry the pin's EXTRAS (``pydantic-ai-slim[openai]``):
    an override REPLACES the declared requirement wholesale, so a bare name
    would resolve an extras-stripped graph — a version can pass there and
    still not co-resolve once written back next to the real requirement.
    """
    req = os.path.join(project_path, "requirements.txt")
    with tempfile.TemporaryDirectory() as tmp:
        override = os.path.join(tmp, "override.txt")
        with open(override, "w", encoding="utf-8") as fh:
            fh.write((override_spec or pin_base) + "\n")
        out_file = os.path.join(tmp, "resolved.txt")
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                ["uv", "pip", "compile", req, "-o", out_file, "--no-header",  # noqa: S607
                 "--override", override],
                capture_output=True, text=True, timeout=300, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0 or not os.path.isfile(out_file):
            return None
        want = normalize_name(pin_base)
        with open(out_file, encoding="utf-8") as fh:
            for line in fh:
                code = line.split("#", 1)[0].strip()
                m = re.match(r"([A-Za-z0-9._\-]+)(?:\[[^\]]*\])?==(\S+)", code)
                if m and normalize_name(m.group(1)) == want:
                    return m.group(2)
    return None


def in_scope_versions(package: str, above: str, below: str) -> list[str]:
    """Installable stable versions strictly between two pins, newest first."""
    try:
        data = rt._http_json(f"{rt.PYPI_BASE_URL}/{package}/json")  # noqa: SLF001
    except Exception:  # noqa: BLE001 — no candidates just means "hold at current"
        return []
    pool = [
        ver for ver in rt.stable_release_versions(data.get("releases", {}))
        if rt.version_tuple(above) < rt.version_tuple(ver) < rt.version_tuple(below)
    ]
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
    # Opt-in (Tamara's field ask): raise the USER's blocking pin instead of
    # holding the provider back. Off by default — it edits user-owned
    # dependencies; every raise is reported in the plan and gated by
    # verification like any other change.
    bump_pins = os.environ.get("BUMP_BLOCKING_PINS", "").lower() in ("true", "1", "yes")
    pin_raises: list[dict] = []
    # pkg -> (pin name, from, tried) for raises that were APPLIED and failed
    # the keep-gate: the hold note must say the raise was tried rather than
    # advise the user to make the exact edit that just didn't resolve.
    raise_attempts: dict[str, tuple[str, str, str]] = {}
    # (pin base, chosen version) pairs that already failed: retry a pin only
    # when the graph shift produces a NEW answer — re-verifying the same
    # failed choice on every walk step is pure compile churn.
    failed_raises: set[tuple[str, str]] = set()

    rc, err = compile_requirements(project_path)
    for _ in range(_MAX_COMPILES):
        if rc == 0:
            break
        offender = next((p for p in bumped
                         if p["package"] not in exhausted
                         and f"{p['package']}=={live[p['package']]}" in err), None)
        if offender is None:
            # Word-boundary match: a bare substring would let `...common-ai`
            # claim an error that actually names `...common-ai-foo`.
            offender = next((p for p in bumped
                             if p["package"] not in exhausted
                             and re.search(rf"{re.escape(p['package'])}(?![\w.-])", err)
                             and live[p["package"]] != p["current"]), None)
        if offender is None:
            break  # not attributable to a bump we made — verification's problem
        pkg = offender["package"]
        pin = _blocking_pin_for(err, pkg)
        if pin:
            blocking.setdefault(pkg, pin)
            blk_name, _, cur_ver = pin.partition("==")
            base = blk_name.split("[")[0]
            if bump_pins and cur_ver:
                choice = resolve_pin_choice(project_path, base, blk_name)
                # Upward only: an upper-bound conflict makes uv pick a LOWER
                # version, and silently downgrading a user pin under a flag
                # named bump-* would be a lie — that case stays a hold+advice.
                if (choice and (base, choice) not in failed_raises
                        and rt.version_tuple(choice) > rt.version_tuple(cur_ver)):
                    spec = {"package": normalize_name(base),
                            "current": cur_ver, "target": choice}
                    changed = apply_bump.bump_requirements(project_path, [spec])
                    revert = {**spec, "current": choice, "target": cur_ver}
                    if len(changed) == 1:
                        rc, err = compile_requirements(project_path)
                        if rc == 0:
                            # Keep-gate is deliberately rc==0, nothing weaker:
                            # "offender gone from the error" is NOT success —
                            # the override that picked `choice` also silenced
                            # every OTHER pin's cap on this package, so the
                            # written-back == pin can break a neighbor (review
                            # proved it live with requests/urllib3). A raise
                            # survives only when the WHOLE set resolves.
                            pin_raises.append({
                                "pin": blk_name, "from": cur_ver, "to": choice,
                                "unblocks": {"package": pkg, "version": live[pkg]}})
                            continue
                        # Anything else: undo and let the walk handle this
                        # offender. A later iteration may retry this pin, but
                        # only if the shifting graph yields a DIFFERENT choice.
                        failed_raises.add((base, choice))
                        raise_attempts[pkg] = (blk_name, cur_ver, choice)
                        apply_bump.bump_requirements(project_path, [revert])
                        rc, err = compile_requirements(project_path)
                    elif changed:
                        # >1 line changed: the file pins this package more than
                        # once (per-marker variants) — too ambiguous to edit
                        # blind; restore and hold the provider instead.
                        failed_raises.add((base, choice))
                        apply_bump.bump_requirements(project_path, [revert])
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

    # A raise is only worth keeping while the provider it bought stayed
    # bumped: a later, unrelated conflict can walk that provider after the
    # raise. The plan must describe the FINAL state — revert raises whose
    # benefit evaporated, and re-point the rest at the final target.
    current_of = {p["package"]: p["current"] for p in bumped}
    for raised in list(pin_raises):
        pkg = raised["unblocks"]["package"]
        pin_pkg = normalize_name(raised["pin"].split("[")[0])
        if live[pkg] == current_of[pkg]:
            if apply_bump.bump_requirements(
                    project_path, [{"package": pin_pkg,
                                    "current": raised["to"],
                                    "target": raised["from"]}]):
                rc2, _ = compile_requirements(project_path)
                if rc2 == 0:
                    pin_raises.remove(raised)
                    continue
                # Reverting re-broke the final set — restore and keep it.
                apply_bump.bump_requirements(
                    project_path, [{"package": pin_pkg,
                                    "current": raised["from"],
                                    "target": raised["to"]}])
        raised["unblocks"]["version"] = live[pkg]

    if not adjusted and not pin_raises:
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
        advice = ""
        if pkg in raise_attempts:
            blk_name, from_ver, tried = raise_attempts[pkg]
            advice = (f" (raising your `{blk_name}` pin {from_ver} → {tried} "
                      f"was tried under `bump-blocking-pins`, but the full "
                      f"pin set still didn't resolve)")
        elif blk:
            # The concrete requirement comes from the ORIGINAL target's own
            # metadata at note-build time — atomically scoped to this offender
            # and version, never scraped from the (multi-conflict) error text.
            blk_name = blk.split("==")[0]
            dep_base = blk_name.split("[")[0]
            spec = _dependency_spec_for(pkg, orig, dep_base)
            if spec:
                # "raise" is only provably the right direction for a pure
                # lower bound; caps/exclusions (a provider capping a dep BELOW
                # the user's pin is a real shape) get the neutral verb.
                ops = set(re.findall(r">=|<=|==|!=|~=|>|<", spec))
                verb = "raise" if ops <= {">=", ">"} else "adjust"
                advice = (f" (to take {orig}, {verb} your `{blk_name}` pin "
                          f"to satisfy `{dep_base}{spec}`)")
            else:
                advice = f" (to take {orig}, adjust your `{blk_name}` pin)"
        if final == p["current"]:
            p["note"] = f"left at {p['current']}: {why}{advice}"
        else:
            p["note"] = (f"held at {final} — newest version that resolves together "
                         f"with your pins; {why}{advice}")
        summary["adjustments"].append(
            {"package": pkg, "from": orig, "to": final, "blocking_pin": blk})

    if pin_raises:
        plan["user_pin_bumps"] = plan.get("user_pin_bumps", []) + pin_raises
        summary["pin_raises"] = pin_raises
        by_pkg = {p.get("package"): p for p in plan.get("providers", [])}
        for raised in pin_raises:
            p = by_pkg.get(raised["unblocks"]["package"])
            # A provider that was ALSO walked back keeps its hold note (the
            # raise alone didn't clear its conflict); the raise itself still
            # surfaces via user_pin_bumps either way.
            if p is not None and p.get("package") not in adjusted:
                p["note"] = (
                    f"takes {raised['unblocks']['version']} — your "
                    f"`{raised['pin']}` pin was raised {raised['from']} → "
                    f"{raised['to']} to resolve the conflict (`bump-blocking-pins`)")

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
