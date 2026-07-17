"""Clean up deprecated Airflow usage after the migration (ruff AIR3 rules).

Otto migrates what the hop breaks; this step goes further and sweeps usage
that still works but is already deprecated (removed symbols, operators moved
to providers, old-style imports) — the debt that otherwise accumulates until
it IS the breakage. Runs ruff's Airflow-3 rules (AIR301/302/311/312) over the
project via a pinned uvx invocation.

Modes (DEPRECATION_MODE):
  fix       rewrite what ruff can (--fix --unsafe-fixes, AIR3 only) and
            report the rest as debt. Both safe and unsafe AIR3 fixes are
            applied: most rewrites (import moves to Airflow-3 forms) are
            marked unsafe by ruff, a few (the schedule_interval->schedule
            kwarg rename) are safe. Parse-level verification in the real
            target image is the backstop — it proves the DAGs still parse,
            NOT that task logic behaves identically, which is why fix
            demotes itself to advisory (and says so) whenever that gate is
            absent: a non-3.x target Airflow (the rewrites would break the
            deployment) or a verify-level below parse/import (nothing
            would check the rewrites).
  advisory  report everything, rewrite nothing.
  off       the action skips this step entirely (no summary file).

Scope: only dags/, plugins/, and include/ are scanned and rewritten — the
same roots verification covers. Mutating anything verification can't see
would ship unreviewed edits. The user's own ruff config is honored (their
excludes, per-file-ignores, and noqa comments are safety boundaries, not
noise); only the rule selection is forced to AIR3 on the CLI.

Best-effort on tooling: uvx/ruff unavailable or ruff crashing is recorded in
the summary JSON (exit 0) so the PR shows a loud skip instead of silently
omitting the section. A broken invocation (PLAN_FILE unset) still raises.

Env in:
  PROJECT_PATH       project root (default ".")
  PLAN_FILE          resolve_target.py output JSON (required)
  DEPRECATION_MODE   fix | advisory (off never reaches this script)
  RUFF_VERSION       pin override (default: the version this was tested with)
Writes a JSON summary to stdout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

# The exact version the AIR3 behavior was probed against; uvx pins it so a
# ruff release can't silently change what gets rewritten between runs.
RUFF_VERSION = os.environ.get("RUFF_VERSION", "0.15.20")

# AIR3xx only: Airflow-3 removals and provider moves. AIR0xx are style/DAG
# authoring checks, not deprecation debt.
_SELECT = "AIR3"

# Mutation scope == verification scope. Rewrites outside what the verify
# step covers would land in the PR unchecked.
_SCAN_ROOTS = ("dags", "plugins", "include")


def _scan_paths(project_path: str) -> list[str]:
    return [p for d in _SCAN_ROOTS
            if os.path.isdir(p := os.path.join(project_path, d))]


def _ruff(paths: list[str], *, fix: bool) -> tuple[int, str, str]:
    cmd = [
        "uvx", f"ruff@{RUFF_VERSION}", "check", *paths,
        "--select", _SELECT, "--preview",
        "--output-format", "json",
    ]
    if fix:
        cmd += ["--fix", "--unsafe-fixes"]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=300, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        # A raise here would abort the composite action mid-pipeline (the
        # step runs under set -euo pipefail) and no PR would open. Feed the
        # existing rc-based failure handling instead.
        return -1, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout, proc.stderr


# One diagnostic this must always produce (usage site, not the bare import —
# AIR3 flags usages). If a RUFF_VERSION bump renames/retires the AIR3 rules,
# a scan would silently report "clean"; the canary makes that loud instead.
_CANARY_SNIPPET = "from airflow.utils.dates import days_ago\nx = days_ago(1)\n"


def _rules_alive() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        probe = os.path.join(tmp, "otto_air3_canary.py")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write(_CANARY_SNIPPET)
        rc, out, _err = _ruff([probe], fix=False)
        if rc != 1:
            return False
        try:
            return bool(json.loads(out))
        except json.JSONDecodeError:
            return False


# F401's message carries the unused binding's qualified name — including
# the bare-module form: "`airflow` imported but unused".
_F401_AIRFLOW = re.compile(r"^`(?P<q>airflow(?:\.[\w.]+)?)` imported but unused")


def _f401_report(paths: list[str], project_path: str) -> dict:
    """Unused airflow.* imports still in the tree — REPORT ONLY.

    Removal is the skill's F401 step (scoped to dags/ and include/; plain
    --fix); this discloses what remains — plugins/ (excluded there because
    Airflow plugins register by being imported), noqa'd lines, re-exports,
    or anything the migration missed. The AIR rules can't see these (they
    flag usage sites), so without this note the PR reads clean while dead
    deprecated imports ride along.

    Returns {status, items, reason?}: a tooling miss is status=unavailable,
    never an empty ok — silence must not read as clean (the invariant this
    feature exists for). It still never blocks the sweep."""
    cmd = [
        "uvx", f"ruff@{RUFF_VERSION}", "check", *paths,
        "--select", "F401", "--output-format", "json",
    ]
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=300, check=False)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "unavailable", "items": [],
                "reason": f"{type(exc).__name__}: {exc}"}
    if proc.returncode not in (0, 1):
        return {"status": "unavailable", "items": [],
                "reason": f"ruff F401 run failed (rc={proc.returncode}): "
                          f"{proc.stderr.strip()[:200]}"}
    try:
        diags = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"status": "unavailable", "items": [],
                "reason": "ruff F401 produced unparseable output"}
    items = []
    for d in diags:
        m = _F401_AIRFLOW.match(d.get("message", ""))
        if m:
            row = (d.get("location") or {}).get("row", "?")
            loc = _relative(d.get("filename", "?"), project_path)
            items.append({"name": m.group("q"), "location": f"{loc}:{row}"})
    items.sort(key=lambda i: (i["location"], i["name"]))
    return {"status": "ok", "items": items}


_FROM_IMPORT = re.compile(
    r"^(?P<indent>[ \t]*)from[ \t]+(?P<mod>[\w.]+)[ \t]+import[ \t]+"
    r"(?P<names>[\w][\w \t.,]*)$")
_IMPORT_NAME = re.compile(r"[\w.]+([ \t]+as[ \t]+\w+)?$")


def _mergeable(m: re.Match | None) -> bool:
    return bool(m) and all(
        _IMPORT_NAME.fullmatch(n.strip()) for n in m.group("names").split(","))


def _merge_adjacent_from_imports(path: str, before: str) -> bool:
    """Ruff's fixer inserts one `from M import x` line per applied fix, so two
    rewrites into the same module read as two lines (`from airflow.sdk import
    dag` / `... import task`) instead of one. Merge ADJACENT same-module plain
    from-imports — but only pairs the fix pass created (at least one line
    absent from the pre-fix text); user-authored style is never rewritten.
    Lines with comments, parens, continuations, or star imports never match.
    """
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            lines = fh.readlines()
    except OSError:
        return False
    pre_lines = {line.strip() for line in before.splitlines()}
    changed = False
    i = 0
    while i < len(lines) - 1:
        a = _FROM_IMPORT.match(lines[i].rstrip("\r\n"))
        b = _FROM_IMPORT.match(lines[i + 1].rstrip("\r\n"))
        if (_mergeable(a) and _mergeable(b)
                and a.group("mod") == b.group("mod")
                and a.group("indent") == b.group("indent")
                and (lines[i].strip() not in pre_lines
                     or lines[i + 1].strip() not in pre_lines)):
            ending = "\r\n" if lines[i].endswith("\r\n") else "\n"
            lines[i] = (f"{a.group('indent')}from {a.group('mod')} import "
                        f"{a.group('names').strip()}, {b.group('names').strip()}{ending}")
            del lines[i + 1]
            changed = True
            continue  # the merged line may merge again with the next one
        i += 1
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.writelines(lines)
    return changed


def _dirty_files(project_path: str) -> set[str] | None:
    """Files modified vs HEAD, or None when git can't answer (not a repo,
    no git on PATH). Snapshotted before/after the fix pass so changed files
    come from the filesystem truth, not diagnostic arithmetic."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", project_path, "diff", "--name-only", "--relative"],  # noqa: S607
            capture_output=True, text=True, timeout=60, check=False)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def _relative(filename: str, project_path: str) -> str:
    root = os.path.abspath(project_path)
    path = os.path.abspath(filename)
    return os.path.relpath(path, root) if path.startswith(root + os.sep) else filename


def _group(diagnostics: list[dict], project_path: str) -> list[dict]:
    """Diagnostics grouped per rule with counts and sample locations."""
    by_rule: dict[str, dict] = {}
    for d in diagnostics:
        code = d.get("code") or "AIR?"
        g = by_rule.setdefault(code, {"rule": code, "count": 0,
                                      "message": d.get("message", ""),
                                      "locations": []})
        g["count"] += 1
        loc = f"{_relative(d.get('filename', '?'), project_path)}:{(d.get('location') or {}).get('row', '?')}"
        if len(g["locations"]) < 5:
            g["locations"].append(loc)
    return sorted(by_rule.values(), key=lambda g: (-g["count"], g["rule"]))


def _target_airflow_major(plan: dict) -> int | None:
    runtime = plan.get("runtime") or {}
    version = runtime.get("target_airflow") or runtime.get("current_airflow") or ""
    head = str(version).split(".", 1)[0]
    return int(head) if head.isdigit() else None


def _sweep(plan: dict) -> dict:
    project_path = os.environ.get("PROJECT_PATH", ".")
    mode = os.environ.get("DEPRECATION_MODE", "advisory")
    summary: dict = {"mode": mode}

    major = _target_airflow_major(plan)
    verify_level = os.environ.get("VERIFY_LEVEL", "")
    if mode == "fix" and (major is None or major < 3):
        # AIR3 rewrites produce Airflow-3 import forms; applying them against
        # a 2.x (or unknown) target would break the deployment.
        summary["mode"] = "advisory"
        summary["demoted"] = (
            f"fix requested but the target Airflow is {major or 'unknown'}.x; "
            "AIR3 rewrites produce Airflow 3 forms, so this run only reports the debt")
        mode = "advisory"
    elif mode == "fix" and verify_level not in ("parse", "import"):
        # Unsafe rewrites are only responsible when something downstream
        # actually checks them; syntax/none verification checks nothing that
        # a rewrite could break.
        summary["mode"] = "advisory"
        summary["demoted"] = (
            f"fix requested but verify-level is '{verify_level or 'unset'}'; "
            "rewrites are only applied when a parse- or import-level "
            "verification gates them, so this run only reports the debt")
        mode = "advisory"

    paths = _scan_paths(project_path)
    summary["scanned"] = [os.path.relpath(p, project_path) for p in paths]
    if not paths:
        summary.update(status="ok", found=0, fixed=0,
                       files_changed=[], remaining=[])
        return summary

    rc, out, err = _ruff(paths, fix=False)
    if rc not in (0, 1) or not out.strip():
        summary.update(status="unavailable",
                       reason=f"ruff run failed (rc={rc}): {err.strip()[:300]}")
        return summary
    try:
        found = json.loads(out)
    except json.JSONDecodeError:
        summary.update(status="unavailable",
                       reason="ruff produced unparseable output")
        return summary

    if not found and not _rules_alive():
        # Zero findings is only trustworthy while the AIR3 rules demonstrably
        # fire; a version/rule drift must not read as "clean project".
        summary.update(status="unavailable",
                       reason="the AIR3 canary produced no findings — ruff "
                              "version or rule drift; refusing to report a "
                              "clean sweep")
        return summary

    remaining = found
    dirty_before = dirty_after = None
    if mode == "fix" and found:
        # Snapshot the files ruff may edit: the import-merge below must only
        # touch lines the fix pass itself introduced.
        pre_texts: dict[str, str] = {}
        for f in {d.get("filename") for d in found if d.get("filename")}:
            try:
                with open(f, encoding="utf-8") as fh:
                    pre_texts[f] = fh.read()
            except OSError:
                pass
        dirty_before = _dirty_files(project_path)
        rc, out, err = _ruff(paths, fix=True)
        for f, before in pre_texts.items():
            _merge_adjacent_from_imports(f, before)
        dirty_after = _dirty_files(project_path)
        if rc not in (0, 1):
            summary.update(status="unavailable",
                           reason=f"ruff --fix run failed (rc={rc}) — rewrites may "
                                  f"already be in the diff: {err.strip()[:300]}")
            return summary
        try:
            remaining = json.loads(out)
        except json.JSONDecodeError:
            # The fix pass already mutated files; pretending nothing happened
            # (ok/0-fixed) would hide those edits from the PR summary.
            summary.update(status="unavailable",
                           reason="the fix pass ran (rewrites may be in the diff) "
                                  "but its report was unparseable; debt not itemized")
            return summary

    # Fixed = per-(file, rule) count drop between the scan and the fix pass.
    # Rows can't key the match — applied fixes shift the surviving
    # diagnostics' line numbers, which would double-count a finding as both
    # fixed and remaining. (Net counts can under-report when a fix exposes a
    # new same-rule finding in the same file; the file list below doesn't
    # rely on them.)
    def _fc(d: dict) -> tuple:
        return (d.get("filename", "?"), d.get("code") or "AIR?")

    delta = Counter(_fc(d) for d in found) - Counter(_fc(d) for d in remaining)
    fixed = sum(delta.values())
    if dirty_before is not None and dirty_after is not None:
        # Filesystem truth beats diagnostic arithmetic for "what changed".
        files_changed = sorted(dirty_after - dirty_before)
    else:
        files_changed = sorted(
            {_relative(f, project_path) for (f, _), n in delta.items() if n})
    summary.update(
        status="ok",
        found=len(found),
        fixed=fixed,
        files_changed=files_changed,
        remaining=_group(remaining, project_path),
        # Post-pass state: report-only in BOTH modes; it never edits.
        unused_airflow_imports=_f401_report(paths, project_path),
    )
    return summary


def main() -> int:
    # A missing/corrupt PLAN_FILE raises: broken invocation, red the run.
    with open(os.environ["PLAN_FILE"], encoding="utf-8") as fh:
        plan = json.load(fh)
    try:
        summary = _sweep(plan)
    except Exception as exc:  # noqa: BLE001 — the step must never block the PR
        summary = {"mode": os.environ.get("DEPRECATION_MODE", "advisory"),
                   "status": "unavailable",
                   "reason": f"unexpected error: {type(exc).__name__}: {exc}"}
    json.dump(summary, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
