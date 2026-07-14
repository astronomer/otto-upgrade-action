"""Clean up deprecated Airflow usage after the migration (ruff AIR3 rules).

Otto migrates what the hop breaks; this step goes further and sweeps usage
that still works but is already deprecated (removed symbols, operators moved
to providers, old-style imports) — the debt that otherwise accumulates until
it IS the breakage. Runs ruff's Airflow-3 rules (AIR301/302/311/312) over the
project via a pinned uvx invocation.

Modes (DEPRECATION_MODE):
  fix       rewrite what ruff can (--fix --unsafe-fixes, AIR3 only) and
            report the rest as debt. Ruff marks ALL AIR3 rewrites "unsafe"
            because they change imports to Airflow-3 forms — exactly the
            forms this pipeline verifies afterwards inside the real target
            image, which is the gate that makes applying them responsible.
            Fix therefore demotes itself to advisory (and says so) whenever
            that gate is absent: a non-3.x target Airflow (the rewrites
            would break the deployment) or a verify-level below
            parse/import (nothing would check the rewrites).
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
import subprocess
import sys
from collections import Counter

# The exact version the AIR3 behavior was probed against; uvx pins it so a
# ruff release can't silently change what gets rewritten between runs.
RUFF_VERSION = os.environ.get("RUFF_VERSION", "0.14.0")

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
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        cmd, capture_output=True, text=True, timeout=300, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _dirty_files(project_path: str) -> set[str] | None:
    """Files modified vs HEAD, or None when git can't answer (not a repo,
    no git on PATH). Snapshotted before/after the fix pass so changed files
    come from the filesystem truth, not diagnostic arithmetic."""
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", project_path, "diff", "--name-only"],  # noqa: S607
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


def main() -> int:
    with open(os.environ["PLAN_FILE"], encoding="utf-8") as fh:
        plan = json.load(fh)
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
        json.dump(summary, sys.stdout, indent=2)
        return 0

    rc, out, err = _ruff(paths, fix=False)
    if rc not in (0, 1) or not out.strip():
        summary.update(status="unavailable",
                       reason=f"ruff run failed (rc={rc}): {err.strip()[:300]}")
        json.dump(summary, sys.stdout, indent=2)
        return 0
    try:
        found = json.loads(out)
    except json.JSONDecodeError:
        summary.update(status="unavailable",
                       reason="ruff produced unparseable output")
        json.dump(summary, sys.stdout, indent=2)
        return 0

    remaining = found
    dirty_before = dirty_after = None
    if mode == "fix" and found:
        dirty_before = _dirty_files(project_path)
        rc, out, err = _ruff(paths, fix=True)
        dirty_after = _dirty_files(project_path)
        if rc not in (0, 1):
            summary.update(status="unavailable",
                           reason=f"ruff --fix run failed (rc={rc}) — rewrites may "
                                  f"already be in the diff: {err.strip()[:300]}")
            json.dump(summary, sys.stdout, indent=2)
            return 0
        try:
            remaining = json.loads(out)
        except json.JSONDecodeError:
            # The fix pass already mutated files; pretending nothing happened
            # (ok/0-fixed) would hide those edits from the PR summary.
            summary.update(status="unavailable",
                           reason="the fix pass ran (rewrites may be in the diff) "
                                  "but its report was unparseable; debt not itemized")
            json.dump(summary, sys.stdout, indent=2)
            return 0

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
    )
    json.dump(summary, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
