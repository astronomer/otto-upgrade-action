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


def test_unrecognized_output_is_infra(tmp_path):
    rc, result, *_ = _run(tmp_path, "docker daemon not running\n")
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
