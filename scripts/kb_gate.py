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
stepping. Tokenless runs (dry-run) skip the gate: the plan is a preview and
notes say coverage wasn't checked. Core unreachable also degrades to
unchecked-with-note rather than holding everything — if Core is down the
Otto step will fail loudly anyway, and a transient blip shouldn't no-op a
scheduled run.

Env in:
  PLAN_FILE           resolve_target.py output JSON (required; updated in place)
  ASTRO_API_TOKEN     bearer token (ASTRO_TOKEN fallback)
  ASTRO_ORGANIZATION  organization id (required for a real probe)
  ASTRO_DOMAIN        astronomer.io | astronomer-stage.io | ... (default astronomer.io)
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

import resolve_target as rt

_MAX_PROBES = 8
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


def _bumped(plan: dict) -> list[dict]:
    return [p for p in plan.get("providers", [])
            if p.get("current") and p.get("target") and p["current"] != p["target"]]


def _note(entry: dict, text: str) -> None:
    entry["note"] = f"{entry['note']}; {text}" if entry.get("note") else text


def main() -> int:
    plan_file = os.environ["PLAN_FILE"]
    with open(plan_file, encoding="utf-8") as fh:
        plan = json.load(fh)
    summary: dict = {"checked": False, "adjustments": []}
    runtime = plan.get("runtime") or {}
    current_af = runtime.get("current_airflow")

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
    for _ in range(_MAX_PROBES):
        target_af = runtime.get("target_airflow") or current_af
        status, message = probe(url, token, current_af, target_af, _bumped(plan))
        summary["checked"] = True
        if status in (200, 204):
            summary["status"] = "covered"
            break
        if status == 0 or status >= 500:
            # Transport/Core trouble: proceed unenforced but say so where
            # the user will look. Otto fails loudly anyway if Core is down.
            summary.update(checked=False, status="unchecked",
                           reason=f"Core unreachable at plan time ({message})")
            if runtime.get("target_tag") != runtime.get("current_tag"):
                _note(runtime, "KB coverage was NOT verified (Core unreachable at plan time)")
                changed = True
            break
        if status != 400:
            summary.update(status="unchecked",
                           reason=f"unexpected HTTP {status}: {message}")
            break

        pkg_match = _PROVIDER_IN_400.search(message)
        if pkg_match:
            pkg = pkg_match.group(1)
            entry = next((p for p in _bumped(plan) if p["package"] == pkg), None)
            if entry is None:
                summary.update(status="unenforced",
                               reason=f"could not map Core's rejection to the plan: {message}")
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
        break

    if changed:
        rt.roll_up(plan)
        with open(plan_file, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    json.dump(summary, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
