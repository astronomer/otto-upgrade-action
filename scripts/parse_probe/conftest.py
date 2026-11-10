# --- DAG-integrity result reporter, installed by otto-upgrade-action ---------
#
# verify.sh appends this to `.astro/conftest.py` in a throwaway copy of the
# project (creating the file if the project has none), next to the project's
# own `.astro/test_dag_integrity_default.py`. It records each test's outcome
# from pytest's report objects and writes ONE line to the real stdout when the
# session ends:
#
#     OTTO_PARSE_RESULT {"version": 1, "nonce": "...", "exitstatus": 0, ...}
#
# parse_check.py reads that line instead of pytest's or the Astro CLI's text,
# so the verdict doesn't depend on how the run was formatted: a project's pytest
# `addopts` (`--color=yes`, `-q`, `--tb=...`, `--no-header`) and CLI wording
# changes have both broken text scraping before. No line means no verdict.
#
# Appended rather than installed beside a project conftest so that conftest
# keeps loading exactly as it would without us. Every name here is prefixed and
# every hook is registered under a unique `pytest_otto_*` name (pluggy's
# `specname`; pytest only scans names starting `pytest_`), so
# nothing here replaces the project's own hooks, fixtures or options.

import json as _otto_json
import os as _otto_os
import sys as _otto_sys

import pytest as _otto_pytest

_OTTO_SENTINEL = "OTTO_PARSE_RESULT "
# The tail is what's read (a traceback ends with its "SomeError: ..." line);
# keeping only that bounds the result line however many files fail.
_OTTO_MAX_MSG = 600
_otto_results: dict = {}
_otto_collect_errors: list = []
_otto_ran: set = set()


def _otto_message(report):
    """The failure text, preferring the exception message over the formatted
    traceback so the project's --tb setting can't change what we read."""
    text = getattr(report, "_otto_msg", None)
    if text is None:
        # Collection errors carry no exception message; the crash summary is
        # the "SomeError: ..." line itself, free of --tb formatting.
        crash = getattr(report.longrepr, "reprcrash", None)
        text = getattr(crash, "message", "") or str(report.longrepr or "")
    return text.strip()[-_OTTO_MAX_MSG:]


def _otto_test_id(item):
    """The DAG file a parametrized integrity test checks: its callspec id
    (the template parametrizes with ids=<relative file path>), else the nodeid."""
    callspec = getattr(item, "callspec", None)
    if callspec is not None:
        params = getattr(callspec, "params", {}) or {}
        if "rel_path" in params:
            # None marks older templates' always-passing placeholder
            # ([(None, None)] + import errors), which checks no file.
            return params["rel_path"] if isinstance(params["rel_path"], str) else None
        if getattr(callspec, "id", None):
            return callspec.id
    return item.nodeid


@_otto_pytest.hookimpl(specname="pytest_configure", trylast=True)
def pytest_otto_configure(config):
    # A project's -x / --maxfail would stop at the first failing file; files
    # after it would then be missing, not verified. Check every file.
    config.option.maxfail = 0


@_otto_pytest.hookimpl(specname="pytest_collectreport")
def pytest_otto_collectreport(report):
    if report.failed:
        _otto_collect_errors.append({"nodeid": report.nodeid, "msg": _otto_message(report)})


@_otto_pytest.hookimpl(specname="pytest_runtest_makereport", hookwrapper=True)
def pytest_otto_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if call.excinfo is not None:
        # The class separately: a custom test raising ModuleNotFoundError
        # itself must still read as an import failure, however long or
        # multi-line its message is.
        report._otto_msg = str(call.excinfo.value)
        report._otto_exc_class = type(call.excinfo.value).__name__
    report._otto_id = _otto_test_id(item)


@_otto_pytest.hookimpl(specname="pytest_runtest_logreport")
def pytest_otto_logreport(report):
    # A test counts as run once its teardown reports: one interrupted mid-call
    # must leave the session short of complete.
    if report.when == "teardown":
        _otto_ran.add(report.nodeid)
    test_id = getattr(report, "_otto_id", report.nodeid)
    if test_id is None:
        return
    entry = _otto_results.setdefault(test_id, {"id": test_id, "outcome": "passed", "msg": ""})
    # setup/teardown failures count too: the file was never verified clean.
    if report.failed and entry["outcome"] != "failed":
        entry["outcome"] = "failed"
        entry["msg"] = _otto_message(report)
        entry["exc_class"] = getattr(report, "_otto_exc_class", "")
    elif report.skipped and report.when in ("setup", "call") and entry["outcome"] == "passed":
        entry["outcome"] = "skipped"


@_otto_pytest.hookimpl(specname="pytest_sessionfinish")
def pytest_otto_sessionfinish(session, exitstatus):
    payload = {
        "version": 1,
        # Set by verify.sh in the copy's .env, which only the integrity
        # container receives: proves the line came from this run, not from a
        # pytest step in the image build.
        "nonce": _otto_os.environ.get("OTTO_PARSE_NONCE", ""),
        "exitstatus": int(exitstatus),
        # Compared to tell a full session from one that stopped early. Counted
        # per test, since results are per file and a template may test a file
        # more than once.
        "collected": int(getattr(session, "testscollected", 0) or 0),
        "ran": len(_otto_ran),
        "results": list(_otto_results.values()),
        "collect_errors": _otto_collect_errors,
    }
    # The real stdout: pytest's capture and the project's output options don't
    # apply to it. Leading newline so the sentinel always starts a line.
    _otto_sys.__stdout__.write("\n" + _OTTO_SENTINEL + _otto_json.dumps(payload, sort_keys=True) + "\n")
    _otto_sys.__stdout__.flush()
