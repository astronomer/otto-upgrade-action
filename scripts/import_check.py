"""Import every DAG file under the given roots and report failures.

Run *inside* an environment that has the target Airflow + providers installed
(verify.sh sets that up via `uv run --with ...`). Importing the module is enough
to surface the failure mode upgrades actually cause — a moved or removed import,
a renamed operator, a dropped parameter at call time — without needing an
Airflow metadata DB.

Argv: one or more directories to scan for *.py.
Exit 0 = all imported; exit 1 = at least one import raised. The human-readable
report is printed to stdout either way.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import traceback


def iter_py(roots: list[str]):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.endswith(".py") and not f.startswith("."):
                    yield os.path.join(dirpath, f)


def main() -> int:
    roots = sys.argv[1:] or ["dags"]
    failures: list[tuple[str, str]] = []
    count = 0
    for path in iter_py(roots):
        count += 1
        mod_name = "dagcheck_" + path.replace(os.sep, "_").replace(".", "_")
        try:
            spec = importlib.util.spec_from_file_location(mod_name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 — any import-time error is a real signal
            failures.append((path, traceback.format_exc().strip().splitlines()[-1]))

    if failures:
        print(f"❌ {len(failures)} of {count} DAG file(s) failed to import at the target version:")
        for path, err in failures:
            print(f"  - `{path}`: {err}")
        return 1
    print(f"✅ All {count} DAG file(s) import cleanly at the target version.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
