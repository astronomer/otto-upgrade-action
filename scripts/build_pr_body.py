"""Render the upgrade PR / summary body as Markdown from the run artifacts.

Pulls together the resolved plan, the applied diff summary, the verification
result, and (when present) Otto's migration result into one deterministic body.
Used both for the real PR description and for the dry-run step summary.

Env in:
  PLAN_FILE       resolve_target.py output      (required)
  VERIFY_FILE     verify status text file       (optional)
  OTTO_FILE       extract_result.py output JSON (optional)
  ACTION_REF      action version, for the footer (optional)
Writes Markdown to stdout.
"""

from __future__ import annotations

import json
import os
import sys

MARKER = "<!-- otto-upgrade-action -->"
TIER_BADGE = {"patch": "🟢 patch", "minor": "🟡 minor", "major": "🔴 major", "none": "—"}


def _load(env_key: str):
    path = os.environ.get(env_key)
    if path and os.path.isfile(path):
        try:
            return json.load(open(path))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def main() -> int:
    plan = json.load(open(os.environ["PLAN_FILE"]))
    otto = _load("OTTO_FILE")
    verify_path = os.environ.get("VERIFY_FILE")
    verify = open(verify_path).read().strip() if verify_path and os.path.isfile(verify_path) else ""

    out: list[str] = [MARKER, "## Airflow upgrade", ""]

    overall = plan.get("overall_tier", "none")
    out.append(f"**Scope:** {TIER_BADGE.get(overall, overall)}")
    if plan.get("scope_exceeded"):
        out.append(
            "\n> A larger upgrade was available but held back by `max-upgrade-scope`. "
            "Raise the input to go further."
        )
    out.append("")

    # Version table.
    rt = plan.get("runtime")
    rows = []
    if rt and rt.get("target_tag") and rt.get("current_tag") != rt.get("target_tag"):
        rows.append(
            f"| Runtime | `{rt['current_tag']}` | `{rt['target_tag']}` "
            f"| {TIER_BADGE.get(rt['tier'], rt['tier'])} | Airflow "
            f"{rt.get('current_airflow','?')} → {rt.get('target_airflow','?')} |"
        )
    for p in plan.get("providers", []):
        if p.get("current") and p.get("target") and p["current"] != p["target"]:
            rows.append(
                f"| `{p['package'].replace('apache-airflow-providers-', '')}` "
                f"| `{p['current']}` | `{p['target']}` "
                f"| {TIER_BADGE.get(p['tier'], p['tier'])} | |"
            )
    if rows:
        out += ["| Component | From | To | Tier | Notes |", "| --- | --- | --- | --- | --- |", *rows, ""]
    else:
        out += ["_No version changes applied._", ""]

    # Otto migration result.
    if otto:
        out += ["### Code migration (Otto)", ""]
        if otto.get("summary"):
            out += [otto["summary"], ""]
        addressed = otto.get("changes_made") or otto.get("breaking_changes_addressed") or []
        if addressed:
            out.append("**Breaking changes handled:**")
            out += [f"- {c}" for c in addressed] + [""]
        followups = otto.get("manual_followups") or []
        if followups:
            out.append("**Manual follow-ups required before merge:**")
            out += [f"- [ ] {c}" for c in followups] + [""]
    elif plan.get("needs_migration"):
        out += [
            "### Code migration",
            "",
            "> This is a minor/major jump that may carry breaking changes, but the "
            "Otto migration step did not run (no token, `run-otto: false`, or the "
            "`upgrader` persona was unavailable). **Review breaking changes manually "
            "before merging.**",
            "",
        ]

    # Verification.
    if verify:
        out += ["### Verification", "", verify, ""]

    # Advisory (majors).
    if plan.get("advisory"):
        out += ["### Heads up", "", f"> {plan['advisory']}", ""]

    out += [
        "---",
        f"<sub>Opened by [otto-upgrade-action]"
        f"(https://github.com/astronomer/otto-upgrade-action) "
        f"{os.environ.get('ACTION_REF', '')}. Re-runs update this PR in place.</sub>",
    ]
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
