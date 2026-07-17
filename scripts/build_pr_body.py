"""Render the upgrade PR / summary body as Markdown from the run artifacts.

Pulls together the resolved plan, the applied diff summary, the verification
result, and (when present) Otto's migration result into one deterministic body.
Used both for the real PR description and for the dry-run step summary.

Env in:
  PLAN_FILE         resolve_target.py output        (required)
  VERIFY_FILE       verify status text file         (optional)
  OTTO_FILE         extract_result.py output JSON   (optional)
  SECURITY_FILE     security_fixes.py output JSON   (optional)
  DEPRECATION_FILE  deprecation_cleanup.py JSON     (optional)
  ACTION_REF        action version, for the footer  (optional)
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
    # The authoritative outcome comes from verify.sh's status file — several
    # skipped-path reports don't start with ℹ️, so sniffing the report emoji
    # misses them. The sniff remains only as a fallback for callers that don't
    # wire the status file.
    status_path = os.environ.get("VERIFY_STATUS_FILE")
    verify_status = (
        open(status_path).read().strip()
        if status_path and os.path.isfile(status_path) else ""
    )
    if not verify_status and verify:
        verify_status = {"✅": "passed", "❌": "failed", "ℹ": "skipped"}.get(verify.lstrip()[:1], "")

    out: list[str] = [MARKER, "## Airflow upgrade", ""]

    # Lead with a clear banner whenever the PR is not verified-green: a failure
    # must not read as "ready", and a SKIP must not hide in the collapsed
    # details — un-run verification looked exactly like success in the field.
    if verify_status == "failed":
        out += [
            "> [!CAUTION]",
            "> **Verification failed: new import failures at the target version** — "
            "do not merge until the failures below are resolved. See the "
            "Verification section.",
            "",
        ]
    elif verify_status == "skipped":
        out += [
            "> [!WARNING]",
            "> **Verification did not run** — DAG imports at the target version were "
            "NOT checked. See the Verification section for why; review the changes "
            "manually, or fix the blocker and re-run.",
            "",
        ]

    overall = plan.get("overall_tier", "none")
    out.append(f"**Scope:** {TIER_BADGE.get(overall, overall)}")
    if plan.get("scope_exceeded"):
        if plan.get("held_airflow_major"):
            # The withheld jump is an Airflow major — never auto-authored by a
            # scheduled run, even at max-upgrade-scope: major. Don't tell the user
            # to raise the cap; the Heads up section points them to the guided
            # upgrade instead.
            out.append(
                "\n> A newer **Airflow major** is available but is never auto-authored "
                "by a scheduled run — see **Heads up** below for the guided upgrade."
            )
        else:
            out.append(
                "\n> A larger upgrade was available but held back by `max-upgrade-scope`. "
                "Raise the input to go further."
            )
    out.append("")

    # Version table.
    rt = plan.get("runtime")
    rows = []
    runtime_bumped = bool(
        rt and rt.get("target_tag") and rt.get("current_tag") != rt.get("target_tag"))
    if runtime_bumped:
        # A bumped runtime carries its note (python-pin exclusions, deprecated
        # channel) in ITS OWN row — routing it to "Not changed" while the
        # table shows a change reads as a contradiction.
        note = f"; {rt['note']}" if rt.get("note") else ""
        rows.append(
            f"| Runtime | `{rt['current_tag']}` | `{rt['target_tag']}` "
            f"| {TIER_BADGE.get(rt['tier'], rt['tier'])} | Airflow "
            f"{rt.get('current_airflow','?')} → {rt.get('target_airflow','?')}{note} |"
        )
    for p in plan.get("providers", []):
        if p.get("current") and p.get("target") and p["current"] != p["target"]:
            spec = p.get("spec_name") or ""
            spelled = f"pinned as `{spec}`" if spec and spec != p["package"] else ""
            # A provider held at an intermediate version carries its
            # explanation (and the raise-this-pin advice) in `note` — it must
            # surface here, since the Not-changed section excludes bumped rows.
            notes = "; ".join(x for x in (spelled, p.get("note") or "") if x)
            rows.append(
                f"| `{p['package'].replace('apache-airflow-providers-', '')}` "
                f"| `{p['current']}` | `{p['target']}` "
                f"| {TIER_BADGE.get(p['tier'], p['tier'])} | {notes} |"
            )
    # User pins the run raised to unblock a provider (bump-blocking-pins).
    # These are USER-owned edits — they must be as visible as the bumps.
    for b in plan.get("user_pin_bumps", []):
        unb = b.get("unblocks") or {}
        taken = unb.get("package", "?").replace("apache-airflow-providers-", "")
        rows.append(
            f"| `{b.get('pin', '?')}` (your pin) | `{b.get('from', '?')}` "
            f"| `{b.get('to', '?')}` | — | raised to take `{taken}` "
            f"{unb.get('version', '?')} (`bump-blocking-pins`) |"
        )
    if rows:
        out += ["| Component | From | To | Tier | Notes |", "| --- | --- | --- | --- | --- |", *rows, ""]
    else:
        out += ["_No version changes applied._", ""]

    # Not changed / skipped — surface why something behind wasn't bumped
    # (digest-pinned runtime, unpinned provider, PyPI lookup failure, …) so the
    # PR doesn't silently look like it covered everything.
    skipped = []
    if rt and rt.get("note") and not runtime_bumped:
        skipped.append(f"- **Runtime** (`{rt.get('current_tag','?')}`): {rt['note']}")
    for p in plan.get("providers", []):
        if p.get("note") and not (p.get("current") and p.get("target") and p["current"] != p["target"]):
            name = p["package"].replace("apache-airflow-providers-", "")
            skipped.append(f"- **`{name}`**: {p['note']}")
    if skipped:
        out += ["### Not changed", "", *skipped, ""]

    # Security fixes the Runtime upgrade delivers. Scoped to the target's
    # release line (see security_fixes.py) — the wording must only assert
    # what that scope supports. A determination failure is said out loud so
    # a shape change on the notes page can't silently drop the section.
    sec = _load("SECURITY_FILE")
    if sec and sec.get("checked"):
        # Cross-line counts are a lower bound (fixes inherited at the new
        # line's fork point aren't enumerable from per-line notes) — say
        # "at least" and never present the number as exhaustive.
        lower = sec.get("lower_bound")
        if sec.get("status") == "ok" and sec.get("fixes"):
            # .get() throughout: this JSON crossed a process boundary, and a
            # missing key must degrade the section, never crash the render —
            # a crash here would abort open-pr.sh and ship NO PR at all.
            total = sec.get("total", len(sec["fixes"]))
            qty = f"at least {total}" if lower else f"{total}"
            out += [
                "### Security fixes included",
                "",
                f"The Runtime release notes list {qty} security "
                f"fix(es) in the `{sec.get('target', '?')}` line that this upgrade picks up:",
                "",
            ]
            for fix in sec["fixes"]:
                label = (f"[{fix['id']}]({fix['url']})"
                         if fix.get("id") and fix.get("url") else fix.get("id", "?"))
                via = ", ".join(f"`{b}`" for b in fix.get("builds", []))
                out.append(f"- {label}" + (f" (fixed in {via})" if via else ""))
            if lower:
                out += ["",
                        "_The new release line may also include fixes inherited "
                        "from earlier lines; the release notes only enumerate "
                        "fixes per line, so this list is a lower bound._"]
            out.append("")
        elif sec.get("status") == "ok" and lower:
            out += [
                "### Security fixes included",
                "",
                "_The Runtime release notes list no security fixes in the "
                "target's release line yet. The upgrade may still include "
                "fixes inherited from earlier lines — the notes don't "
                "enumerate those._",
                "",
            ]
        elif sec.get("status") == "ok":
            out += [
                "### Security fixes included",
                "",
                "_The Runtime release notes list no security fixes for the "
                "builds this upgrade picks up._",
                "",
            ]
        else:
            out += [
                "### Security fixes included",
                "",
                f"> ⚠️ Could not determine the security fixes this upgrade "
                f"ships: {sec.get('reason', 'unknown')}",
                "",
            ]

    # Otto migration result.
    if otto:
        out += ["### Code migration (Otto)", ""]
        if otto.get("summary"):
            out += [otto["summary"], ""]
        addressed = otto.get("changes_made") or otto.get("breaking_changes_addressed") or []
        if addressed:
            out.append("**Changes and decisions:**")
            out += [f"- {c}" for c in addressed] + [""]
        followups = otto.get("manual_followups") or []
        if followups:
            out.append("**Manual follow-ups required before merge:**")
            out += [f"- [ ] {c}" for c in followups] + [""]
    elif plan.get("needs_migration"):
        out += [
            "### Code migration",
            "",
            "> This is a minor/major jump that may carry breaking changes, but no Otto "
            "migration summary is attached (a `dry-run` preview, or Otto returned no "
            "structured result). **Review breaking changes manually before merging.**",
            "",
        ]

    # Deprecation sweep — what got mechanically rewritten and what debt
    # remains. A tooling miss renders loudly, mirroring the security section.
    dep = _load("DEPRECATION_FILE")
    if dep:
        fixed, remaining = dep.get("fixed", 0), dep.get("remaining") or []
        if dep.get("status") != "ok":
            out += [
                "### Deprecation sweep",
                "",
                f"> ⚠️ The deprecated-usage sweep could not run: "
                f"{dep.get('reason', 'unknown')}",
                "",
            ]
        elif fixed or remaining:
            out += ["### Deprecation sweep", ""]
            if dep.get("demoted"):
                out += [f"> {dep['demoted']}.", ""]
            if fixed:
                files = ", ".join(f"`{f}`" for f in dep.get("files_changed", []))
                # The "verified" claim must track the actual outcome — a PR
                # whose verification failed or never ran must not describe
                # its riskiest edits as checked.
                gate = (" (covered by this PR's verification)"
                        if verify_status == "passed" else "")
                out += [
                    f"Rewrote {fixed} deprecated usage(s)"
                    + (f" in {files}" if files else "") + f"{gate}.",
                    "",
                ]
                if verify_status != "passed":
                    out += [
                        "> ⚠️ This PR's verification did not pass (see the "
                        "Verification section), so these rewrites are "
                        "**unverified** — review them before merging.",
                        "",
                    ]
            if remaining:
                out.append("**Remaining deprecation debt** (no mechanical fix; "
                           "works today but will break in a future Airflow):")
                for g in remaining:
                    locs = ", ".join(f"`{loc}`" for loc in g.get("locations", []))
                    more = (f", +{g['count'] - len(g.get('locations', []))} more"
                            if g.get("count", 0) > len(g.get("locations", [])) else "")
                    out.append(
                        f"- **{g.get('rule', 'AIR?')}** ×{g.get('count', '?')}: "
                        f"{g.get('message', '')} — {locs}{more}")
                out.append("")

    # Verification — collapsible, with the outcome in the summary line so the
    # detail (and any Airflow import-time noise) stays tucked away. Failure and
    # skip are already surfaced loudly by the banners at the top of the body.
    if verify:
        summary = f"Verification — {verify_status}" if verify_status else "Verification"
        out += [
            "<details>",
            f"<summary><b>{summary}</b></summary>",
            "",
            verify,
            "",
            "</details>",
            "",
        ]

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
