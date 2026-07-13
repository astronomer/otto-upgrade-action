"""Classify target-version import failures against a baseline (current-version) run.

Field-verified rationale: projects accumulate import errors that are
environment-dependent (a dbt profile path from an env var, a connection read at
parse time) and fail identically before and after the upgrade. Reporting those
as upgrade breakage buries the signal — only failures ABSENT at the current
version are the upgrade's fault.

Argv: target.json baseline.json  (both produced by import_check.py IMPORT_JSON)
Stdout: the Markdown verification report.
Exit 3 when at least one NEW failure exists; exit 0 otherwise
(pre-existing-only is a pass). Any internal error also exits 3 — the caller
only invokes this when the target run found real failures, so a crash here
must fail closed, never read as a pass.

Classification is by project-root-relative path — both runs emit those, so the
two checkouts compare directly. A path failing on both sides with a different
exception class stays pre-existing (annotated, not failing) — the root cause
almost always predates the upgrade — EXCEPT when the target error is
import-family (ImportError/ModuleNotFoundError/AttributeError) and the
baseline's wasn't: a moved or removed import surfacing behind an unrelated
pre-existing failure is exactly the upgrade signal this tool exists to catch.
"""

from __future__ import annotations

import json
import sys

_IMPORT_FAMILY = {"ImportError", "ModuleNotFoundError", "AttributeError"}


def _code(msg: str) -> str:
    """Render an error message as a code span so `__init__`/`__future__` don't
    turn into bold on GitHub. Backticks inside the message would break the
    span, so they are downgraded to quotes first."""
    return f"`{msg.replace('`', chr(39))}`"


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as fh:
        target = json.load(fh)
    with open(sys.argv[2], encoding="utf-8") as fh:
        baseline = json.load(fh)

    target_by_path = {f["path"]: f for f in target["failures"]}
    baseline_by_path = {f["path"]: f for f in baseline["failures"]}

    new, pre = [], []
    for path, f in target_by_path.items():
        b = baseline_by_path.get(path)
        if b is None:
            new.append(f)
        elif (f.get("exc_class") != b.get("exc_class")
              and f.get("exc_class") in _IMPORT_FAMILY):
            # Escalate: an import-family error that wasn't there before is
            # upgrade breakage even if the file already failed differently.
            f = dict(f, escalated=True)
            new.append(f)
        else:
            pre.append(f)
    fixed = [p for p in baseline_by_path if p not in target_by_path]

    lines: list[str] = []
    if new:
        lines += [
            f"❌ {len(new)} NEW import failure(s) at the target version "
            "(these do not occur at your current versions):",
            "",
        ]
        for f in new:
            note = ""
            if f.get("escalated"):
                note = (" _(this file also fails this check at your current "
                        "versions, but with a non-import error — the import "
                        "failure is new)_")
            lines.append(f"  - `{f['path']}`: {_code(f['msg'])}{note}")
        lines.append("")
    else:
        lines += [
            f"✅ No new import failures at the target version "
            f"({target.get('checked', 0)} file(s) checked).",
            "",
        ]
    if pre:
        lines += [
            f"⚠️ {len(pre)} pre-existing import issue(s) — these fail **in this "
            "check's environment** at your current versions too, so they are not "
            "caused by this upgrade. This is usually an environment-dependent DAG "
            "(one that needs runtime variables, connections, or a metadata DB to "
            "parse), not a broken DAG:",
            "",
        ]
        for f in pre:
            note = ""
            if baseline_by_path[f["path"]].get("exc_class") != f.get("exc_class"):
                note = " _(error changed at the target version)_"
            lines.append(f"  - `{f['path']}`: {_code(f['msg'])}{note}")
        lines.append("")
    if fixed:
        lines += [
            f"✅ {len(fixed)} file(s) that fail at the current version import "
            "cleanly at the target.",
            "",
        ]

    sys.stdout.write("\n".join(lines).rstrip() + "\n")
    return 3 if new else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail closed, never crash to a pass
        print(f"❌ Baseline comparison error: {type(exc).__name__}: {exc}")
        sys.exit(3)
