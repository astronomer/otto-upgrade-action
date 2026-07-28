"""parse_check.py turns `astro dev parse` output into the verifier's JSON.

Fixtures are trimmed from real `astro dev parse` runs (Runtime 3.2-3/3.3-1).
Exit contract: 0 clean, 3 DAG import failures, 4 image build failure,
2 unrecognized output (infra — never a code verdict).
"""

import json

import parse_check

CLEAN_RUN = """\
Checking your DAGs for errors…
Astro Runtime Version: 3.2-3
============================= test session starts ==============================
collected 21 items

.astro/test_dag_integrity_default.py .....................               [100%]

======================== 21 passed, 1 warning in 18.37s ========================
✔ No errors detected in your DAGs
"""

FAILING_RUN = """\
Checking your DAGs for errors…
Astro Runtime Version: 3.2-3
============================= test session starts ==============================
collected 22 items

.astro/test_dag_integrity_default.py .........F............              [100%]

=================================== FAILURES ===================================
>           raise Exception(f"{rel_path} failed to import with message \\n {rv}")
E           Exception: dags/format_probe.py failed to import with message
E            Traceback (most recent call last):
E             File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
E             File "/usr/local/airflow/dags/format_probe.py", line 2, in <module>
E               from airflow.thismoduledoesnotexist import Nope
E           ModuleNotFoundError: No module named 'airflow.thismoduledoesnotexist'

.astro/test_dag_integrity_default.py:138: Exception
=========================== short test summary info ============================
FAILED .astro/test_dag_integrity_default.py::test_file_imports[dags/format_probe.py]
================== 1 failed, 21 passed, 2 warnings in 17.71s ===================
"""

BUILD_FAILURE_RUN = """\
Checking your DAGs for errors…
#9 1.057       depends on pydantic-ai-slim>=2.0.0 and you require
#9 1.057       requirements and pydantic-ai-slim[openai]==1.107.0 are incompatible.
#9 1.057       And because you require pydantic-ai-slim[openai]==1.107.0, we can
#9 1.057       conclude that your requirements are unsatisfiable.
ERROR: failed to build: failed to solve: process \
"/usr/local/bin/install-python-dependencies" did not complete successfully: exit code: 1

Error: something went wrong while parsing your DAGs: an error was encountered \
while building the image, see the build logs for details
"""


def _run(tmp_path, text, monkeypatch=None):
    log = tmp_path / "parse.log"
    log.write_text(text)
    out = tmp_path / "failures.json"
    rc, result = parse_check.parse_output(text)
    return rc, result, log, out


def test_clean_run(tmp_path):
    rc, result, *_ = _run(tmp_path, CLEAN_RUN)
    assert rc == 0
    assert result == {"checked": 21, "failures": []}


def test_failing_run_extracts_path_class_and_terse_message(tmp_path):
    rc, result, *_ = _run(tmp_path, FAILING_RUN)
    assert rc == 3
    assert result["checked"] == 22
    (failure,) = result["failures"]
    assert failure["path"] == "dags/format_probe.py"
    assert failure["exc_class"] == "ModuleNotFoundError"
    assert failure["msg"] == "ModuleNotFoundError: No module named 'airflow.thismoduledoesnotexist'"


def test_build_failure_detected(tmp_path):
    rc, result, *_ = _run(tmp_path, BUILD_FAILURE_RUN)
    assert rc == 4
    assert result["failures"] == []


COLLECTION_ERROR_RUN = """\
Checking your DAGs for errors…
============================= test session starts ==============================
collected 0 items / 1 error
==================================== ERRORS ====================================
.astro/test_dag_integrity_default.py:106: in get_import_errors
=========================== short test summary info ============================
ERROR .astro/test_dag_integrity_default.py - TypeError: DagBag.__init__() got an unexpected keyword argument
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
========================= 2 warnings, 1 error in 1.83s =========================
Error: something went wrong while parsing your DAGs: failed to execute cmd: exit status 2
"""


def test_collection_error_detected_with_cause(tmp_path):
    # The repo's committed integrity test can be incompatible with the target
    # Airflow (real case: DagBag signature change in 3.3). The DAGs were never
    # tested — this must not read as pass, fail, OR unrecognized.
    rc, result, *_ = _run(tmp_path, COLLECTION_ERROR_RUN)
    assert rc == 5
    assert "DagBag.__init__()" in result["collection_error"]


def test_unrecognized_output_is_infra(tmp_path):
    rc, result, *_ = _run(tmp_path, "docker daemon not running\n")
    assert rc == 2
    # Nothing to quote: the caller falls back to its own catch-all wording.
    assert "cli_error" not in result


STANDALONE_NO_VENV_RUN = """\
Checking your DAGs for errors…
Error: something went wrong while parsing your DAGs: no virtual environment found \
— run 'astro dev start' first
"""

STANDALONE_NO_PYTEST_RUN = """\
Checking your DAGs for errors…
Error: something went wrong while parsing your DAGs: exec: "pytest": \
executable file not found in $PATH
"""


def test_cli_refusal_carries_its_own_reason(tmp_path):
    # A project committing `dev.mode: standalone` sends the CLI to a gitignored
    # local .venv no CI checkout has. Still no verdict (rc 2), but the reason
    # must reach the caller — reporting only "produced no recognizable result"
    # is what made this take a log dig to diagnose in the field.
    rc, result, *_ = _run(tmp_path, STANDALONE_NO_VENV_RUN)
    assert rc == 2
    assert result["cli_error"] == "no virtual environment found — run 'astro dev start' first"


def test_cli_refusal_half_provisioned_venv(tmp_path):
    # The nastier standalone shape: the venv exists (so the CLI's own venv check
    # passes) but nothing installed pytest into it.
    rc, result, *_ = _run(tmp_path, STANDALONE_NO_PYTEST_RUN)
    assert rc == 2
    assert result["cli_error"] == 'exec: "pytest": executable file not found in $PATH'


def test_build_failure_wrapper_is_not_demoted_to_cli_error(tmp_path):
    # The image-build failure wears the same "something went wrong while parsing
    # your DAGs" wrapper. It is a real verdict (rc 4) and must not be reclassified
    # as a missing one just because the wrapper matches.
    rc, result, *_ = _run(tmp_path, BUILD_FAILURE_RUN)
    assert rc == 4
    assert "cli_error" not in result


def test_failing_run_with_wrapper_still_reports_the_failures(tmp_path):
    # A wrapper alongside real failures means the run was cut short after finding
    # them (pytest exit 1 gets its own message, not the wrapper). Reporting the
    # failures is fail-closed; degrading to the fallback would discard them.
    text = FAILING_RUN + "Error: something went wrong while parsing your DAGs: noise\n"
    rc, result, *_ = _run(tmp_path, text)
    assert rc == 3
    assert result["failures"][0]["path"] == "dags/format_probe.py"
    assert "cli_error" not in result


# A Dockerfile `RUN pytest` lands in the same captured stream as the integrity
# run. Buildkit prefixes those lines ("#12 1.204 ") so they miss the anchored
# patterns, but the legacy builder emits RUN stdout verbatim — and an unprefixed
# summary was enough to manufacture a pass. Found by adversarial review.
BUILD_PYTEST_NOISE = """\
Checking your DAGs for errors…
Step 6/7 : RUN pytest tests/unit -q
 ---> Running in a1b2c3
collected 4 items
=========================== 4 passed in 0.21s ===========================
 ---> d4e5f6
Successfully built d4e5f6
"""


def test_build_pytest_summary_cannot_pass_a_refused_parse(tmp_path):
    # The worst shape: the build's summary satisfies "completed" while the CLI is
    # saying it never ran the parse at all.
    text = BUILD_PYTEST_NOISE + (
        "Error: something went wrong while parsing your DAGs: no virtual "
        "environment found — run 'astro dev start' first\n"
    )
    rc, result, *_ = _run(tmp_path, text)
    assert rc == 2, "a build's pytest summary must not stand in for the integrity run"
    assert result["checked"] == 0, "the build's collected count must not be reported"
    assert "virtual environment" in result["cli_error"]


def test_build_pytest_summary_cannot_pass_a_truncated_integrity_run(tmp_path):
    truncated = (
        "============================= test session starts ==============================\n"
        "collected 37 items\n\n"
        ".astro/test_dag_integrity_default.py ......\n"
    )
    rc, result, *_ = _run(tmp_path, BUILD_PYTEST_NOISE + truncated)
    assert rc == 2
    assert result["checked"] == 0


def test_no_tests_ran_is_not_a_pass(tmp_path):
    # pytest exits 5 when it collects nothing, which the CLI wraps. Reporting
    # "all 0 DAG file(s) import cleanly" over an untested project is the exact
    # false green this level exists to prevent.
    text = (
        "Checking your DAGs for errors…\n"
        "============================= test session starts ==============================\n"
        "collected 0 items\n\n"
        "==================== no tests ran in 0.12s ====================\n"
        "Error: something went wrong while parsing your DAGs: "
        "something went wrong while Pytesting your DAGs\n"
    )
    rc, result, *_ = _run(tmp_path, text)
    assert rc == 2
    assert "Pytesting" in result["cli_error"]


def test_build_noise_does_not_hide_a_real_collection_error(tmp_path):
    # The integrity session's own collection error must still win rc 5 even with a
    # clean build-stage pytest run ahead of it in the stream.
    rc, result, *_ = _run(tmp_path, BUILD_PYTEST_NOISE + COLLECTION_ERROR_RUN)
    assert rc == 5
    assert "DagBag.__init__()" in result["collection_error"]


def test_counted_failures_we_cannot_name_are_not_a_pass(tmp_path):
    # pytest counted a failure that no test_file_imports[...] entry accounts for
    # (an unexpected harness shape). We can't name what broke, so the only honest
    # answers are "no verdict" or a fabricated one — take the former.
    text = (
        "Checking your DAGs for errors…\n"
        "============================= test session starts ==============================\n"
        "collected 1 item\n\n"
        "tests/test_something_else.py F                                           [100%]\n"
        "=========================== 1 failed in 1.93s ===========================\n"
    )
    rc, result, *_ = _run(tmp_path, text)
    assert rc == 2
    assert result["checked"] == 0


def test_build_noise_does_not_inflate_a_real_pass(tmp_path):
    # The complement: with a genuine integrity run after the build noise, the
    # count must come from the integrity session (21), not the build's (4).
    rc, result, *_ = _run(tmp_path, BUILD_PYTEST_NOISE + CLEAN_RUN)
    assert rc == 0
    assert result["checked"] == 21


def test_long_run_summary_with_hms_suffix_is_recognized(tmp_path):
    # pytest switches the summary duration format at 60s: "in 62.50s (0:01:02)".
    # Missing that form silently disabled parse-level verdicts for any project
    # whose in-image suite runs over a minute.
    text = FAILING_RUN.replace("in 17.71s", "in 62.50s (0:01:02)")
    rc, result, *_ = _run(tmp_path, text)
    assert rc == 3
    assert result["failures"][0]["path"] == "dags/format_probe.py"


def test_timeout_truncated_run_is_not_a_verdict(tmp_path):
    # A `timeout` kill mid-pytest leaves "collected N items" and progress dots
    # but no closing summary. That must never read as a pass — it's what a
    # green "all N import cleanly" over untested DAGs looks like.
    truncated = (
        "Checking your DAGs for errors…\n"
        "============================= test session starts ==============================\n"
        "collected 37 items\n\n"
        ".astro/test_dag_integrity_default.py ...........\n"
    )
    rc, result, *_ = _run(tmp_path, truncated)
    assert rc == 2


def test_failed_line_without_e_block_still_reported(tmp_path):
    # If pytest output formatting changes and the E-block regex misses, the
    # short-summary FAILED line must still produce an entry (fail-closed).
    text = (
        "collected 3 items\n"
        "FAILED .astro/test_dag_integrity_default.py::test_file_imports[dags/x.py]\n"
        "=== 1 failed, 2 passed in 1.00s ===\n"
    )
    rc, result, *_ = _run(tmp_path, text)
    assert rc == 3
    (failure,) = result["failures"]
    assert failure["path"] == "dags/x.py"
    assert "see the CI log" in failure["msg"]


def test_main_writes_import_json(tmp_path, monkeypatch):
    log = tmp_path / "parse.log"
    log.write_text(FAILING_RUN)
    out = tmp_path / "failures.json"
    monkeypatch.setenv("IMPORT_JSON", str(out))
    monkeypatch.setattr("sys.argv", ["parse_check.py", str(log)])
    assert parse_check.main() == 3
    data = json.loads(out.read_text())
    assert data["failures"][0]["path"] == "dags/format_probe.py"
