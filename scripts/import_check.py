"""Import DAG and plugin files the way Airflow discovers them; report failures.

Run *inside* an environment that has the target Airflow + providers installed
(verify.sh sets that up via `uv run --with ...`). Importing the module is enough
to surface the failure mode upgrades actually cause — a moved or removed import,
a renamed operator, a dropped parameter at call time — without needing an
Airflow metadata DB.

Fidelity rules (mirrors the Astro Runtime / Airflow parse surface, so the check
can't fail on files Airflow itself would never import):

  - Only files under the dags root are DAG-imported, and only when they pass
    Airflow's safe-mode heuristic: content mentions "airflow" AND ("dag" OR
    "asset"), case-insensitive. Airflow 2 keyed on dag+airflow; Airflow 3 added
    asset — the wider form is used unconditionally (cost on AF2: one extra
    import attempt; the alternative silently exempts asset-only DAG files).
    Known blind spot: DAG_DISCOVERY_SAFE_MODE=False / custom discovery
    callables widen what Airflow parses beyond this heuristic.
  - `.airflowignore` files are honored per subtree (glob syntax by default,
    regexp for Airflow 2 targets via --ignore-syntax), patterns matching paths
    relative to the ignore file's directory. Close approximation of Airflow's
    matcher, not a reimplementation.
  - The plugins root is imported recursively WITHOUT the heuristic — Airflow's
    plugin manager imports every module there at startup, so an import failure
    there is a genuine runtime failure.
  - include/ is never imported directly (Airflow doesn't either); verify.sh
    byte-compiles it instead.
  - sys.path gets the project root, dags root, plugins root, and config dir —
    matching the Runtime image (PYTHONPATH=/usr/local/airflow) plus Airflow's
    own sys.path preparation — so `include.foo` and dag-folder-local imports
    resolve exactly as they do in production.

Failure paths are reported relative to --project-root so a target run and a
baseline run (different checkouts of the same project) are directly comparable.

Exit 0 = all imported; exit 3 = at least one import raised. The human-readable
report is printed to stdout either way. Exit code 3 (not 1) so the caller can
tell a genuine DAG import failure apart from `uv` failing to build the env
(which exits 1/2) — the two must map to different verify statuses.

Env out:
  IMPORT_REPORT  when set, the clean human-readable summary is written here
  IMPORT_JSON    when set, machine-readable results are written here:
                 {"checked": N, "failures": [{"path", "exc_class", "msg"}]}
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import os
import re
import sys
import traceback

from report_fmt import code_span, failure


def might_contain_dag(path: str) -> bool:
    """Airflow's safe-mode discovery heuristic (Airflow 3 form)."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            content = fh.read().lower()
    except OSError:
        return False
    return "airflow" in content and ("dag" in content or "asset" in content)


def _ignored(path: str, rules: list[tuple[str, str]], syntax: str) -> bool:
    for base, pattern in rules:
        rel = os.path.relpath(path, base)
        if syntax == "regexp":
            try:
                if re.search(pattern, rel):
                    return True
            except re.error:
                continue
        elif fnmatch.fnmatch(rel, pattern):
            return True
    return False


def _walk_py(root: str, syntax: str):
    """Yield importable .py files under root, honoring per-subtree .airflowignore."""
    rules_by_dir: dict[str, list[tuple[str, str]]] = {}
    for dirpath, dirs, names in os.walk(root):
        rules = list(rules_by_dir.get(os.path.dirname(dirpath), [])) if dirpath != root else []
        if ".airflowignore" in names:
            with open(os.path.join(dirpath, ".airflowignore"), encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        rules.append((dirpath, line))
        rules_by_dir[dirpath] = rules
        dirs[:] = [
            d for d in sorted(dirs)
            if not d.startswith(".") and d != "__pycache__"
            and not _ignored(os.path.join(dirpath, d), rules, syntax)
        ]
        for f in sorted(names):
            if not f.endswith(".py") or f.startswith("."):
                continue
            path = os.path.join(dirpath, f)
            if not _ignored(path, rules, syntax):
                yield path


def _import_one(project_root: str, path: str) -> dict | None:
    rel = os.path.relpath(path, project_root)
    mod_name = "dagcheck_" + re.sub(r"[^0-9A-Za-z_]", "_", rel)
    snapshot = set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # Register before exec so relative imports and self-references resolve;
        # the previous unregistered exec was itself a source of false
        # "No module named 'dagcheck_...'" failures on package __init__ files.
        sys.modules[mod_name] = module
        spec.loader.exec_module(module)
        return None
    except Exception as exc:  # noqa: BLE001 — any import-time error is the signal
        msg = traceback.format_exc().strip().splitlines()[-1]
        return failure(rel, type(exc).__name__, msg)
    finally:
        # Drop project-local modules this file pulled in, so one file's import
        # state can't mask or cause another file's failure. Third-party modules
        # stay cached — evicting airflow would re-run its heavy init per file.
        for name in set(sys.modules) - snapshot:
            mod = sys.modules.get(name)
            file = getattr(mod, "__file__", None) or ""
            if name == mod_name or file.startswith(project_root + os.sep):
                del sys.modules[name]
        sys.modules.pop(mod_name, None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project-root", required=True)
    ap.add_argument("--dags-root")
    ap.add_argument("--plugins-root")
    ap.add_argument("--ignore-syntax", choices=("glob", "regexp"), default="glob")
    args = ap.parse_args(argv)

    project_root = os.path.abspath(args.project_root)
    dags_root = os.path.abspath(args.dags_root) if args.dags_root else None
    plugins_root = os.path.abspath(args.plugins_root) if args.plugins_root else None

    for p in (os.path.join(project_root, "config"), plugins_root, dags_root, project_root):
        if p and os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

    # Import Airflow once up front (when the env ships it) so the per-file
    # module cleanup never evicts it.
    try:
        import airflow  # noqa: F401
    except Exception:  # noqa: BLE001, S110 — unit-test envs run without airflow
        pass

    failures: list[dict] = []
    count = 0
    for root, apply_heuristic in ((dags_root, True), (plugins_root, False)):
        if not root or not os.path.isdir(root):
            continue
        for path in _walk_py(root, args.ignore_syntax):
            if apply_heuristic and not might_contain_dag(path):
                continue
            count += 1
            failure = _import_one(project_root, path)
            if failure:
                failures.append(failure)

    if failures:
        lines = [f"❌ {len(failures)} of {count} DAG file(s) failed to import at the target version:", ""]
        lines += [f"  - `{f['path']}`: {code_span(f['msg'])}" for f in failures]
        rc = 3
    else:
        lines = [f"✅ All {count} DAG file(s) import cleanly at the target version."]
        rc = 0

    report = "\n".join(lines)
    # The clean summary goes to IMPORT_REPORT (consumed by verify.sh for the PR
    # body) so Airflow's import-time logging — which spews to stdout/stderr when
    # the DAGs are imported — can't pollute it. stdout still gets the summary for
    # the CI log.
    print(report)
    report_path = os.environ.get("IMPORT_REPORT")
    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(report + "\n")
        except OSError:
            pass
    json_path = os.environ.get("IMPORT_JSON")
    if json_path:
        try:
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump({"checked": count, "failures": failures}, fh, indent=2)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
