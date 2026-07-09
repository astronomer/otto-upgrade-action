"""Detect the current Airflow Runtime tag and provider pins in an Astro project.

Parses the project's Dockerfile for the Astro Runtime base image tag and
requirements.txt for ``apache-airflow-providers-*`` pins. Emits a JSON document
that ``resolve_target.py`` consumes.

Env in:
  PROJECT_PATH    project root (default ".")
Writes the detected-versions JSON to stdout.

Recognized runtime base images (the tag after the colon is what we bump):
  quay.io/astronomer/astro-runtime:<tag>
  astrocrpublic.azurecr.io/runtime:<tag>
  <any>/astro-runtime:<tag>  /  <any>/runtime:<tag>
"""

from __future__ import annotations

import json
import os
import re
import sys

# A FROM line whose image path ends in astro-runtime or runtime, capturing the
# repo (without tag), the tag, and an optional `@sha256:` digest. Tolerates an
# `AS stage` trailer and registry prefixes.
_FROM = re.compile(
    r"^\s*FROM\s+(?P<repo>\S*?(?:astro-runtime|/runtime|^runtime))\s*:\s*"
    r"(?P<tag>[\w.\-]+)(?:@(?P<digest>sha256:[a-fA-F0-9]+))?",
    re.IGNORECASE,
)
# Looser fallback: any FROM that mentions runtime and has a :tag.
_FROM_LOOSE = re.compile(
    r"^\s*FROM\s+(?P<repo>\S*runtime)\s*:\s*"
    r"(?P<tag>[\w.\-]+)(?:@(?P<digest>sha256:[a-fA-F0-9]+))?",
    re.IGNORECASE,
)

# Requirement name token (PEP 508 name grammar), with optional extras and an
# optional exact pin. The provider check happens on the PEP 503-normalized
# form, so `common.sql`, `common_sql`, and `Common-SQL` — which pip all treat
# as the same package — resolve to the provider they actually install.
_REQ_NAME = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[[^\]]*\])?"
    r"\s*(?:==\s*(?P<ver>[\w.\-]+))?"
)


def normalize_name(name: str) -> str:
    """PEP 503 normalization: runs of ``-``/``_``/``.`` collapse to ``-``, lowercased."""
    return re.sub(r"[-_.]+", "-", name).lower()


def detect_runtime(project_path: str) -> dict | None:
    dockerfile = os.path.join(project_path, "Dockerfile")
    if not os.path.isfile(dockerfile):
        return None
    found = None
    with open(dockerfile, encoding="utf-8") as fh:
        for line in fh:
            m = _FROM.match(line) or _FROM_LOOSE.match(line)
            if m:
                found = {  # last FROM wins (multi-stage: the final runtime stage)
                    "image_repo": m.group("repo"),
                    "tag": m.group("tag"),
                    "digest": m.groupdict().get("digest"),
                    "dockerfile": "Dockerfile",
                }
    return found


def detect_providers(project_path: str) -> list[dict]:
    req = os.path.join(project_path, "requirements.txt")
    out: list[dict] = []
    by_pkg: dict[str, dict] = {}
    if not os.path.isfile(req):
        return out
    with open(req, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            m = _REQ_NAME.match(line)
            if not m:
                continue
            pkg = normalize_name(m.group("name"))
            if not pkg.startswith("apache-airflow-providers-"):
                continue
            entry = {
                "package": pkg,
                "spec_name": m.group("name"),  # exact spelling in the file
                "pinned_version": m.group("ver"),  # None when unpinned
                "spec_file": "requirements.txt",
            }
            prev = by_pkg.get(pkg)
            if prev is None:
                by_pkg[pkg] = entry
                out.append(entry)
            elif prev["pinned_version"] != entry["pinned_version"]:
                # Two spellings of the same package with different pins. pip
                # resolves this by accident (last wins); never pick a side —
                # drop the pin so the resolver skips it and the PR surfaces it.
                prev["pinned_version"] = None
                prev["note"] = (
                    "duplicate entries with conflicting pins "
                    f"(`{prev['spec_name']}`, `{entry['spec_name']}`); skipped"
                )
    return out


def main() -> int:
    project_path = os.environ.get("PROJECT_PATH", ".")
    result = {
        "project_path": project_path,
        "runtime": detect_runtime(project_path),
        "providers": detect_providers(project_path),
    }
    if result["runtime"] is None and not result["providers"]:
        print(
            "::warning::No Astro Runtime Dockerfile FROM line and no pinned "
            f"providers found under '{project_path}'. Nothing to upgrade.",
            file=sys.stderr,
        )
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
