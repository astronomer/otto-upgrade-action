"""Validate the plan's targets against the Upgrade KB, at plan time.

Core's airflow-upgrade archive endpoint already enforces the KB universe:
an Airflow target outside the KB's known versions, or a provider target the
KB doesn't cover (including curated yanks), is rejected with a 400 naming
the offender. Without this gate that rejection fires in the middle of the
Otto step — after pins are bumped and the PR is framed — and kills the
migration. This step probes the same endpoint with the resolved plan BEFORE
Otto runs and converts rejections into plan adjustments with honest notes:

  - Airflow target not covered  -> step down through the resolver's ranked
    runtime candidates (newest first) until one is covered, else hold.
  - Provider target not covered -> hold that provider at its current pin,
    quoting Core's reason (yank reasons are curated in the KB).

The gate is validate-only by design (the endpoint can't enumerate coverage);
Linear AI-994 upgrades this step to a JSON plan mode with newest-covered
stepping. Tokenless runs never reach this script (the action's step is
gated on resolved auth); the in-script token check is a second line of
defense only. Core unreachable — and any non-400 surprise, including a bad
token's 401 — degrades to unchecked, with a NOT-verified note in the plan:
if Core is down the Otto step fails loudly anyway, and a transient blip
shouldn't no-op a scheduled run.

The gate runs twice: once BEFORE apply_bump (plan-only — held targets never
reach the tree), and again after pin reconciliation IF co_resolve walked any
provider (its walk-backs pick versions the first probe never saw). The
re-gate pass sets PROJECT_PATH: the tree already carries the reconciled
pins, so any target the gate changes is synced back into the files too.

Env in:
  PLAN_FILE           resolve_target.py output JSON (required; updated in place)
  ASTRO_API_TOKEN     bearer token (ASTRO_TOKEN fallback)
  ASTRO_ORGANIZATION  organization id (required for a real probe)
  ASTRO_DOMAIN        astronomer.io | astronomer-stage.io | ... (default astronomer.io)
  MAX_SCOPE           patch | minor | major (provider re-clamp after step-down)
  PROJECT_PATH        set ONLY on the re-gate pass: sync plan changes to files
Writes a JSON summary to stdout. Always exits 0 unless PLAN_FILE is broken.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import apply_bump
import resolve_target as rt

_MAX_PROBES = 8
# Core rejects requests naming more than 50 providers (controller cap);
# anything past it is held without probing rather than poisoning the probe.
_MAX_PROVIDERS_PER_PROBE = 50
_PROVIDER_IN_400 = re.compile(r"provider (apache-airflow-providers-[a-z0-9.\-_]+)", re.IGNORECASE)


def _endpoint() -> tuple[str, str] | None:
    token = os.environ.get("ASTRO_API_TOKEN") or os.environ.get("ASTRO_TOKEN")
    org = os.environ.get("ASTRO_ORGANIZATION")
    if not token or not org:
        return None
    domain = os.environ.get("ASTRO_DOMAIN", "astronomer.io")
    return (f"https://api.{domain}/v1alpha1/organizations/{org}"
            "/agent/skills/airflow-upgrade/archive", token)


def probe(url: str, token: str, current_af: str, target_af: str,
          providers: list[dict]) -> tuple[int, str]:
    """One validation probe. Returns (status, message). Status 0 = transport
    error (Core unreachable / unexpected), message carries the reason."""
    params = {"currentVersion": current_af}
    if target_af and target_af != current_af:
        params["targetVersion"] = target_af
    entries = [f"{p['package']}:{p['current']}:{p['target']}"
               for p in providers]
    if entries:
        params["providers"] = ",".join(entries)
    req = urllib.request.Request(  # noqa: S310 — https URL built from the Astro domain
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": "otto-upgrade-action"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — https to the caller's Astro domain
            resp.read(1)  # the archive body is irrelevant; probe is the point
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(body).get("message", body)
        except (json.JSONDecodeError, AttributeError):
            message = body
        return exc.code, message[:500]
    except Exception as exc:  # noqa: BLE001 — DNS, TLS, timeout: transport-level
        return 0, f"{type(exc).__name__}: {exc}"


def _probe_budget(n_bumped: int, n_candidates: int) -> int:
    """Probe budget scales with how much there is to reject: every provider
    can cost one probe, every runtime candidate one more, plus the covered
    probe. Every attributable 400 strictly shrinks remaining work, so under
    normal Core semantics the budget can't exhaust — the post-loop fail-closed
    hold is a backstop against adversarial/looping rejections only."""
    return max(_MAX_PROBES, n_bumped + n_candidates + 2)


def _bumped(plan: dict) -> list[dict]:
    return [p for p in plan.get("providers", [])
            if p.get("current") and p.get("target") and p["current"] != p["target"]]


def _reclamp_providers(plan: dict, new_airflow: str, summary: dict) -> None:
    """Re-run the resolver's Airflow-compat clamp for every bumped provider
    against ``new_airflow``. Used after a runtime step-down: the original
    targets were compat-checked against the higher, rejected Airflow."""
    max_scope = os.environ.get("MAX_SCOPE", "minor")
    for entry in _bumped(plan):
        refreshed = rt._provider_latest(  # noqa: SLF001 — same resolver, same rules
            entry["package"], entry["current"], max_scope,
            target_airflow=new_airflow)
        if refreshed["target"] == entry["target"]:
            continue
        old = entry["target"]
        entry["target"] = refreshed["target"]
        entry["tier"] = refreshed["tier"]
        if refreshed.get("note"):
            _note(entry, refreshed["note"])
        _note(entry, f"re-resolved from {old} for Airflow {new_airflow} "
                     "after the runtime stepped down")
        summary["adjustments"].append(
            {"kind": "provider-reclamped", "package": entry["package"],
             "from": old, "to": refreshed["target"],
             "for_airflow": new_airflow})


def _note(entry: dict, text: str) -> None:
    entry["note"] = f"{entry['note']}; {text}" if entry.get("note") else text


def _sync_files(project_path: str, before: dict, plan: dict) -> None:
    """Re-gate mode: the tree already carries the pre-gate targets. Any
    target the gate changed must land in the files too, or the plan and the
    diff diverge."""
    rt_after = plan.get("runtime") or {}
    if (before["runtime_tag"] and rt_after.get("target_tag")
            and before["runtime_tag"] != rt_after["target_tag"]):
        apply_bump.bump_dockerfile(
            project_path, before["runtime_tag"], rt_after["target_tag"])
    moves = []
    for p in plan.get("providers", []):
        old = before["providers"].get(p.get("package"))
        if old and p.get("target") and p["target"] != old:
            moves.append({"package": p["package"], "current": old,
                          "target": p["target"]})
    if moves:
        apply_bump.bump_requirements(project_path, moves)


def main() -> int:
    plan_file = os.environ["PLAN_FILE"]
    with open(plan_file, encoding="utf-8") as fh:
        plan = json.load(fh)
    summary: dict = {"checked": False, "adjustments": []}
    runtime = plan.get("runtime") or {}
    current_af = runtime.get("current_airflow")
    pre_gate = {
        "runtime_tag": runtime.get("target_tag"),
        "providers": {p.get("package"): p.get("target")
                      for p in plan.get("providers", [])},
    }

    endpoint = _endpoint()
    if endpoint is None:
        summary["reason"] = "no Astro token/organization (dry-run?); KB coverage not checked"
        json.dump(summary, sys.stdout, indent=2)
        return 0
    if not current_af:
        summary["reason"] = "current Airflow unknown; KB coverage not checked"
        json.dump(summary, sys.stdout, indent=2)
        return 0
    url, token = endpoint

    # Ranked step-down candidates the resolver exported (newest first,
    # distinct Airflow versions, all within scope). Fall back to hold-only
    # when a plan predates the field.
    candidates = list(runtime.get("kb_step_candidates") or [])
    changed = False

    # Core caps providers at 50 per request; anything past the cap can't be
    # probed and holds conservatively rather than poisoning every probe with
    # a "too many providers" 400 the gate can't attribute.
    overflow = _bumped(plan)[_MAX_PROVIDERS_PER_PROBE:]
    for entry in overflow:
        held_target = entry["target"]
        entry["target"] = entry["current"]
        entry["tier"] = "none"
        _note(entry, f"left at {entry['current']}: beyond the KB gate's "
                     f"{_MAX_PROVIDERS_PER_PROBE}-provider probe limit; held")
        summary["adjustments"].append(
            {"kind": "provider-held", "package": entry["package"],
             "rejected": held_target, "reason": "probe limit"})
        changed = True

    resolved = False
    for _ in range(_probe_budget(len(_bumped(plan)), len(candidates))):
        target_af = runtime.get("target_airflow") or current_af
        status, message = probe(url, token, current_af, target_af, _bumped(plan))
        summary["checked"] = True
        if status in (200, 204):
            summary["status"] = "covered"
            resolved = True
            break
        if status == 0 or status >= 500:
            # Transport/Core trouble: proceed unenforced but say so where
            # the user will look — a plan-level flag the PR body renders, so
            # provider-only plans disclose it too. Otto fails loudly anyway
            # if Core is down.
            reason = f"Core unreachable at plan time ({message})"
            summary.update(checked=False, status="unchecked", reason=reason)
            plan["kb_gate_unchecked"] = reason
            changed = True
            resolved = True
            break
        if status != 400:
            # 401/403 (bad token) and other surprises: the run will fail at
            # Otto with the same credential, but the durable artifact must
            # carry the warning too, not just a log group nobody reopens.
            reason = f"unexpected HTTP {status} from Core at plan time: {message}"
            summary.update(checked=False, status="unchecked", reason=reason)
            plan["kb_gate_unchecked"] = reason
            changed = True
            resolved = True
            break

        pkg_match = _PROVIDER_IN_400.search(message)
        if pkg_match:
            pkg = pkg_match.group(1)
            entry = next((p for p in _bumped(plan) if p["package"] == pkg), None)
            if entry is None:
                summary.update(status="unenforced",
                               reason=f"could not map Core's rejection to the plan: {message}")
                resolved = True
                break
            held_target = entry["target"]
            entry["target"] = entry["current"]
            entry["tier"] = "none"
            _note(entry, f"left at {entry['current']}: {held_target} rejected by the "
                         f"upgrade KB — {message}")
            summary["adjustments"].append(
                {"kind": "provider-held", "package": pkg, "rejected": held_target,
                 "reason": message})
            changed = True
            continue

        if "targetversion" in message.lower():
            rejected_af = runtime.get("target_airflow")
            nxt = next((c for c in candidates
                        if rt.version_tuple(c["airflow"]) < rt.version_tuple(rejected_af)), None)
            candidates = [c for c in candidates
                          if rt.version_tuple(c["airflow"]) < rt.version_tuple(rejected_af)]
            if nxt is None:
                runtime["target_tag"] = runtime.get("current_tag")
                runtime["target_airflow"] = current_af
                runtime["tier"] = "none"
                _note(runtime, f"runtime held: Airflow {rejected_af} isn't covered by "
                               "the upgrade KB yet and no covered candidate remains")
                summary["adjustments"].append(
                    {"kind": "runtime-held", "rejected": rejected_af, "reason": message})
            else:
                runtime["target_tag"] = nxt["tag"]
                runtime["target_airflow"] = nxt["airflow"]
                runtime["tier"] = rt.tier_between(current_af, nxt["airflow"]) \
                    if nxt["airflow"] != current_af else (
                        "patch" if nxt["tag"] != runtime.get("current_tag") else "none")
                _note(runtime, f"stepped down to {nxt['tag']}: Airflow {rejected_af} "
                               "isn't covered by the upgrade KB yet")
                summary["adjustments"].append(
                    {"kind": "runtime-stepped", "rejected": rejected_af,
                     "to": nxt["tag"], "reason": message})
                # The provider targets were compat-clamped against the
                # REJECTED Airflow; a provider needing the newer one is now
                # incompatible with where we're actually landing. Re-clamp
                # against the stepped-down version before re-probing.
                _reclamp_providers(plan, nxt["airflow"], summary)
            changed = True
            continue

        # A 400 we can't attribute: fail closed on everything we'd author.
        for entry in _bumped(plan):
            entry["target"] = entry["current"]
            entry["tier"] = "none"
            _note(entry, "held: the KB coverage gate couldn't interpret Core's "
                         f"rejection ({message})")
        if runtime.get("target_tag") != runtime.get("current_tag"):
            runtime["target_tag"] = runtime.get("current_tag")
            runtime["target_airflow"] = current_af
            runtime["tier"] = "none"
            _note(runtime, f"runtime held: uninterpretable KB rejection ({message})")
        summary.update(status="held-all", reason=message)
        changed = True
        resolved = True
        break

    if not resolved:
        # Budget exhausted with a rejection still standing: leaking it into
        # the Otto step (which re-runs the same validation as a hard 400 mid-
        # migration) defeats the gate. Fail closed on whatever is left.
        for entry in _bumped(plan):
            entry["target"] = entry["current"]
            entry["tier"] = "none"
            _note(entry, "held: KB gate probe budget exhausted before this "
                         "target was confirmed covered")
        if runtime.get("target_tag") != runtime.get("current_tag"):
            runtime["target_tag"] = runtime.get("current_tag")
            runtime["target_airflow"] = current_af
            runtime["tier"] = "none"
            _note(runtime, "runtime held: KB gate probe budget exhausted")
        summary.update(status="held-all",
                       reason="probe budget exhausted with rejections remaining")
        changed = True

    if changed:
        rt.roll_up(plan)
        project_path = os.environ.get("PROJECT_PATH")
        if project_path:
            _sync_files(project_path, pre_gate, plan)
        with open(plan_file, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    json.dump(summary, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
