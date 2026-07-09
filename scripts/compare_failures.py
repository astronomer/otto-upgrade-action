"""Classify target-version import failures against a baseline (current-version) run.

Field-verified rationale: projects accumulate import errors that are
environment-dependent (a dbt profile path from an env var, a connection read at
parse time) and fail identically before and after the upgrade. Reporting those
as upgrade breakage buries the signal — only failures ABSENT at the current
version are the upgrade's fault.

Argv: target.json baseline.json  (both produced by import_check.py IMPORT_JSON)
Stdout: the Markdown verification report.
Exit 3 when at least one NEW failure exists (mirrors import_check's contract);
exit 0 otherwise (pre-existing-only is a pass).

Classification is by project-root-relative path — both runs emit those, so the
two checkouts compare directly. A path failing on both sides with a DIFFERENT
exception class stays pre-existing (annotated, not failing): the root cause
almost always predates the upgrade, and a false CAUTION is the exact failure
mode this comparison exists to remove.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as fh:
        target = json.load(fh)
    with open(sys.argv[2], encoding="utf-8") as fh:
        baseline = json.load(fh)

    target_by_path = {f["path"]: f for f in target["failures"]}
    baseline_by_path = {f["path"]: f for f in baseline["failures"]}

    new = [f for p, f in target_by_path.items() if p not in baseline_by_path]
    pre = [f for p, f in target_by_path.items() if p in baseline_by_path]
    fixed = [p for p in baseline_by_path if p not in target_by_path]

    lines: list[str] = []
    if new:
        lines += [
            f"❌ {len(new)} NEW import failure(s) at the target version "
            "(these do not occur at your current versions):",
            "",
        ]
        lines += [f"  - `{f['path']}`: {f['msg']}" for f in new]
        lines.append("")
    else:
        lines += [
            f"✅ No new import failures at the target version "
            f"({target.get('checked', 0)} file(s) checked).",
            "",
        ]
    if pre:
        lines += [
            f"⚠️ {len(pre)} pre-existing import issue(s) — these fail at your "
            "current versions too and are not caused by this upgrade:",
            "",
        ]
        for f in pre:
            note = ""
            if baseline_by_path[f["path"]]["exc_class"] != f["exc_class"]:
                note = " _(error changed at the target version)_"
            lines.append(f"  - `{f['path']}`: {f['msg']}{note}")
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
    sys.exit(main())
