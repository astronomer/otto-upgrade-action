"""Convert an `astro dev parse` run into the verifier's failure JSON.

`astro dev parse` builds the project's Docker image (the real target Runtime +
the project's requirements) and runs the project's DAG-integrity pytest file
inside it — one `test_file_imports[<relative path>]` per discovered DAG file.
verify.sh installs parse_probe/conftest.py next to that file, which writes the
run's outcome as one line:

    OTTO_PARSE_RESULT {"version": 1, "exitstatus": ..., "results": [...], ...}

This script reads that line (see the probe for why not pytest's text) and emits
the same JSON shape import_check.py does, so compare_failures.py can diff a
target run against a baseline run unchanged. No result line means no verdict.

The one exception is an image that fails to BUILD: the probe never runs, so the
CLI's build-failure message is the only evidence, and it outranks any result
line. Its wording is matched case-insensitively; if it drifts, the run reads as
"no verdict" (rc 2), which verify.sh reports as a fallback, never as a pass.

Argv: path to the captured `astro dev parse` output (stdout+stderr combined).
Env in:
  OTTO_PARSE_NONCE  when set, only a result line carrying this nonce counts
                    (verify.sh hands the same value to the integrity container
                    via the copy's .env)
Env out:
  IMPORT_JSON  when set, {"checked": N, "failures": [{"path", "exc_class", "msg"}]}
               plus "collection_error" (rc 5) or "reason"/"cli_error" (rc 2)

Exit codes (verify.sh keys on these):
  0  image built, every collected DAG file imports
  3  image built, at least one DAG file failed to import. "incomplete" carries
     a reason when the session wasn't whole (a collection error, a stop before
     every test ran, an abnormal pytest exit): the failures stand, but they
     must not be compared with a baseline, since files that never ran could
     hide behind failures the baseline shares
  4  the image itself failed to build (dependency resolution, Dockerfile error)
  5  pytest died during COLLECTION and nothing ran, so no DAG was tested. Field case: the
     repo's committed .astro/test_dag_integrity_default.py (from an older CLI)
     calls DagBag with an argument the target Airflow removed.
  2  no verdict: no result line (the run never completed, the CLI refused to
     start it, or the reporter wasn't loaded), an incomplete session with no
     failures, or a session that tested no DAG file
"""

from __future__ import annotations

import json
import os
import re
import sys

from report_fmt import failure

SENTINEL = "OTTO_PARSE_RESULT "
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BUILD_FAILED = re.compile(r"an error was encountered while building the image", re.I)
# The CLI's own reason for not producing a result, e.g. a project configured
# for standalone dev mode or a venv without pytest. Only enriches the rc 2
# message.
_CLI_ERROR = re.compile(
    r"^Error: something went wrong while parsing your DAGs: (?P<cause>.+)$", re.M | re.I)
_EXC_CLASS = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Warning|Exit))\b")
# The default template raises Exception(f"{rel_path} failed to import with
# message \n {rv}"); rv is DagBag's import-error text, a traceback whose last
# line is the terse `SomeError: ...`.
_TEMPLATE_PREFIX = re.compile(r"^\S+ failed to import with message\s*", re.I)


def _result_line(text: str, nonce: str) -> dict | None:
    # Anchored at the line start, where the probe writes it, and carrying this
    # run's nonce: output from a pytest step in the image build is never the
    # verdict, even when an old builder prints it unprefixed.
    for line in reversed(text.splitlines()):
        if not line.startswith(SENTINEL):
            continue
        try:
            payload = json.loads(line[len(SENTINEL):])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and (not nonce or payload.get("nonce") == nonce):
            return payload
    return None


def _terse(msg: str) -> str:
    """Last non-empty line of the failure text, the convention import_check uses."""
    lines = [ln.strip() for ln in _TEMPLATE_PREFIX.sub("", msg.strip()).splitlines()]
    lines = [ln for ln in lines if ln]
    return lines[-1] if lines else "failed to import (see the CI log for the traceback)"


def _no_verdict(text: str, reason: str) -> tuple[int, dict]:
    result: dict = {"checked": 0, "failures": [], "reason": reason}
    cli_err = _CLI_ERROR.search(text)
    if cli_err is not None:
        result["cli_error"] = cli_err.group("cause").strip()
    return 2, result


def parse_output(text: str, nonce: str = "") -> tuple[int, dict]:
    text = _ANSI.sub("", text)
    # The CLI runs the integrity test only after the image builds, so a failed
    # build means any result line came from somewhere else (a Dockerfile step).
    if _BUILD_FAILED.search(text):
        return 4, {"checked": 0, "failures": []}
    payload = _result_line(text, nonce)
    if payload is None:
        return _no_verdict(text, "the DAG integrity run reported no result")

    results = payload.get("results") or []
    collect_errors = payload.get("collect_errors") or []

    failures = []
    for r in results:
        if r.get("outcome") != "failed":
            continue
        msg = _terse(r.get("msg", ""))
        cm = _EXC_CLASS.match(msg)
        exc_class = cm.group(1).rsplit(".", 1)[-1] if cm else ""
        # The template wraps DagBag's error in a plain Exception, so the class
        # comes from the message's last line; a test that raised the error
        # itself has it in exc_class.
        if not exc_class and r.get("exc_class") not in (None, "", "Exception"):
            exc_class = r["exc_class"]
        failures.append(failure(r.get("id", "?"), exc_class, msg))
    failures.sort(key=lambda f: f["path"])
    # A skipped test checked nothing (an empty parametrize yields one skipped
    # placeholder when DagBag found no files).
    checked = sum(1 for r in results if r.get("outcome") != "skipped")

    if collect_errors and not results:
        return 5, {"checked": 0, "failures": [],
                   "collection_error": _terse(collect_errors[0].get("msg", ""))}

    # Whether every collected test ran to the end. Some DAG file may have gone
    # untested otherwise, even if others ran.
    ran, collected = payload.get("ran"), payload.get("collected") or 0
    exitstatus = payload.get("exitstatus")
    incomplete = ""
    if collect_errors:
        incomplete = "part of the DAG integrity test failed to collect: " + \
            _terse(collect_errors[0].get("msg", ""))
    elif ran is not None and ran < collected:
        incomplete = f"the DAG integrity run stopped after {ran} of {collected} tests"
    elif exitstatus not in (0, 1):
        incomplete = f"the DAG integrity run ended abnormally (pytest exit {exitstatus})"

    if failures:
        result: dict = {"checked": checked, "failures": failures}
        if incomplete:
            result["incomplete"] = incomplete
        return 3, result
    if incomplete:
        return _no_verdict(text, incomplete)
    if checked == 0:
        return _no_verdict(text, "the DAG integrity run tested no DAG files")
    return 0, {"checked": checked, "failures": []}


def main() -> int:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    rc, result = parse_output(text, os.environ.get("OTTO_PARSE_NONCE", ""))
    json_path = os.environ.get("IMPORT_JSON")
    if json_path:
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    return rc


if __name__ == "__main__":
    sys.exit(main())
