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
    # Replace the tag only on FROM lines that carry the current tag, anchored at
    # the end of the tag token (not `\b`, which — since `-` is a non-word char —
    # would let a current tag of `3.2` match the `3.2` prefix of `3.2-3`).
    pattern = re.compile(
        r"(?P<head>^\s*FROM\s+\S*runtime\s*:\s*)" + re.escape(current_tag) + r"(?=[\s@\"']|$)",
        re.IGNORECASE | re.MULTILINE,
    )
    new_text, n = pattern.subn(lambda m: m.group("head") + target_tag, text)
    if n == 0:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    return True


def _pkg_pattern(package: str) -> str:
    """Regex matching any spelling of ``package`` that PEP 503-normalizes to it:
    each ``-`` in the normalized name matches a run of ``-``/``_``/``.``, so a
    typo'd `common.sql` line still gets its pin bumped. The trailing ``==``
    anchor in the caller prevents prefix false-matches (`common-sqlx`)."""
    return r"[-_.]+".join(re.escape(part) for part in package.split("-"))


def bump_requirements(project_path: str, providers: list[dict]) -> list[dict]:
    path = os.path.join(project_path, "requirements.txt")
    changed: list[dict] = []
    if not os.path.isfile(path):
        return changed
    targets = {
        p["package"]: (p["current"], p["target"])
        for p in providers
        if p.get("current") and p.get("target") and p["current"] != p["target"]
    }
    if not targets:
        return changed
    # The `(?=[\s;]|$)` boundary mirrors detect_versions: a wildcard or
    # local-segment pin never matches, so a partially-captured version can't be
    # spliced into an invalid requirement.
    patterns = {
        pkg: re.compile(
            rf"^(?P<pre>\s*{_pkg_pattern(pkg)}(?:\s*\[[^\]]*\])?\s*==\s*)"
            r"(?P<ver>[\w.\-]+)(?=[\s;]|$)(?P<post>.*)$",
            re.IGNORECASE,
        )
        for pkg in targets
    }
    # newline="" preserves the file's own line endings — universal-newline mode
    # would rewrite a CRLF requirements.txt entirely to LF, turning a one-pin
    # bump into a whole-file diff (this module's contract is byte-for-byte).
    with open(path, encoding="utf-8", newline="") as fh:
        lines = fh.readlines()
    for i, raw in enumerate(lines):
        code = raw.split("#", 1)[0]
        for pkg, (current, target) in targets.items():
            # Match `pkg[extras]==x.y.z` (any PEP 503-equivalent spelling) and
            # swap only the version, preserving the user's spelling, extras,
            # markers, trailing comment, and newline. Only lines pinned at the
            # plan's CURRENT version change: a package listed twice with
            # per-marker versions (foo==1 for py<3.12, foo==2 otherwise) must
            # not have its unrelated variant dragged along.
            m = patterns[pkg].match(code)
            if not m or m.group("ver") != current:
                continue
            comment = raw[len(code):]
            lines[i] = f"{m.group('pre')}{target}{m.group('post')}{comment}"
            if not lines[i].endswith("\n"):
                lines[i] += "\n"
            changed.append({"package": pkg, "from": m.group("ver"), "to": target})
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as fh:
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
