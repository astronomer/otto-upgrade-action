"""Apply the version bumps from a plan to the project files, in place.

Rewrites only the bits the plan changes: the Astro Runtime tag on the
Dockerfile ``FROM`` line, and the ``==`` pin on each provider whose target
differs from its current pin. Everything else in the files is left byte-for-byte
intact. Idempotent — running twice produces no second diff.

Env in:
  PROJECT_PATH   project root (default ".")
  PLAN_FILE      resolve_target.py output JSON (required)
Writes a summary JSON of what changed to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys


def bump_dockerfile(project_path: str, current_tag: str, target_tag: str) -> bool:
    path = os.path.join(project_path, "Dockerfile")
    if not os.path.isfile(path) or current_tag == target_tag:
        return False
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # Replace the tag only on FROM lines that carry the current tag, so we never
    # touch an unrelated `:current_tag` substring elsewhere in the file.
    pattern = re.compile(
        r"(?P<head>^\s*FROM\s+\S*runtime\s*:\s*)" + re.escape(current_tag) + r"\b",
        re.IGNORECASE | re.MULTILINE,
    )
    new_text, n = pattern.subn(lambda m: m.group("head") + target_tag, text)
    if n == 0:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return True


def bump_requirements(project_path: str, providers: list[dict]) -> list[dict]:
    path = os.path.join(project_path, "requirements.txt")
    changed: list[dict] = []
    if not os.path.isfile(path):
        return changed
    targets = {
        p["package"]: p["target"]
        for p in providers
        if p.get("current") and p.get("target") and p["current"] != p["target"]
    }
    if not targets:
        return changed
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    for i, raw in enumerate(lines):
        code = raw.split("#", 1)[0]
        for pkg, target in targets.items():
            # Match `pkg[extras]==x.y.z` and swap only the version, preserving
            # extras, markers, trailing comment, and newline.
            m = re.match(
                rf"^(?P<pre>\s*{re.escape(pkg)}(?:\[[^\]]*\])?\s*==\s*)"
                r"(?P<ver>[\w.\-]+)(?P<post>.*)$",
                code,
            )
            if not m:
                continue
            comment = raw[len(code):]
            lines[i] = f"{m.group('pre')}{target}{m.group('post')}{comment}"
            if not lines[i].endswith("\n"):
                lines[i] += "\n"
            changed.append({"package": pkg, "from": m.group("ver"), "to": target})
    if changed:
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
    return changed


def main() -> int:
    project_path = os.environ.get("PROJECT_PATH", ".")
    plan = json.load(open(os.environ["PLAN_FILE"]))

    summary: dict = {"dockerfile_changed": False, "providers_changed": [], "files": []}

    rt = plan.get("runtime")
    if rt and rt.get("target_tag") and rt.get("current_tag") != rt.get("target_tag"):
        if bump_dockerfile(project_path, rt["current_tag"], rt["target_tag"]):
            summary["dockerfile_changed"] = True
            summary["files"].append(os.path.join(project_path, "Dockerfile"))

    changed = bump_requirements(project_path, plan.get("providers", []))
    summary["providers_changed"] = changed
    if changed:
        summary["files"].append(os.path.join(project_path, "requirements.txt"))

    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
