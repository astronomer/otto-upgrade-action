"""parse_check.py reads the probe's result line, never pytest's or the CLI's text.

Most tests run a REAL pytest session with parse_probe/conftest.py installed next
to a stand-in integrity test shaped like the Astro CLI template (parametrized
test_file_imports, raising Exception("<path> failed to import with message
<traceback>")), under project pytest options that reshape the text output. The
verdict must not change with those options: that is the regression class this
design exists to close (a consumer's `addopts = --color=yes --no-header
--tb=short` once turned a passing 181-DAG parse into "no recognizable result").
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import parse_check
import pytest

PROBE = Path(__file__).resolve().parent.parent / "scripts" / "parse_probe" / "conftest.py"

INTEGRITY_TEST = '''\
import pytest

RESULTS = {results!r}


@pytest.mark.parametrize("rel_path, rv", list(RESULTS.items()), ids=list(RESULTS))
def test_file_imports(rel_path, rv):
    if rv != "No import errors":
        raise Exception(f"{{rel_path}} failed to import with message \\n {{rv}}")
'''

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/usr/local/airflow/dags/broken.py", line 2, in <module>\n'
    "    from airflow.thismoduledoesnotexist import Nope\n"
    "ModuleNotFoundError: No module named 'airflow.thismoduledoesnotexist'"
)

ADDOPTS = [
    pytest.param("", id="no-addopts"),
    pytest.param('addopts = ["--tb=short", "--no-header", "--color=yes"]', id="sirius-addopts"),
    pytest.param('addopts = ["-q", "--tb=no", "-p", "no:warnings"]', id="quiet-no-tb"),
    pytest.param('addopts = ["-rA", "--tb=line", "-v"]', id="verbose-line-tb"),
]


def _pytest(cwd):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", ".astro/test_dag_integrity_default.py",
         "-p", "no:cacheprovider"],
        cwd=cwd, capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "FORCE_COLOR": "1"},
    )
    return proc.stdout + proc.stderr


def run_probe_session(tmp_path, results, addopts="", project_conftest=None):
    """Lay out an Astro-like project with the probe installed the way verify.sh
    does, and run pytest the way the CLI does (`pytest
    .astro/test_dag_integrity_default.py` from the project root)."""
    layout_project(tmp_path, results, addopts, project_conftest)
    return _pytest(tmp_path)


def layout_project(tmp_path, results, addopts="", project_conftest=None):
    astro = tmp_path / ".astro"
    astro.mkdir()
    (astro / "test_dag_integrity_default.py").write_text(INTEGRITY_TEST.format(results=results))
    # What verify.sh does: append the reporter to the project's conftest.
    (astro / "conftest.py").write_text((project_conftest or "") + "\n" + PROBE.read_text())
    (tmp_path / "pyproject.toml").write_text(f"[tool.pytest.ini_options]\n{addopts}\n")


@pytest.mark.parametrize("addopts", ADDOPTS)
def test_clean_run_is_a_pass_whatever_the_output_format(tmp_path, addopts):
    out = run_probe_session(tmp_path, {"dags/a.py": "No import errors",
                                       "dags/b.py": "No import errors"}, addopts)
    assert parse_check.parse_output(out) == (0, {"checked": 2, "failures": []})


@pytest.mark.parametrize("addopts", ADDOPTS)
def test_failure_names_the_file_and_terse_error_whatever_the_output_format(tmp_path, addopts):
    out = run_probe_session(tmp_path, {"dags/a.py": "No import errors",
                                       "dags/broken.py": TRACEBACK}, addopts)
    rc, result = parse_check.parse_output(out)
    assert rc == 3
    assert result["checked"] == 2
    assert result["failures"] == [{
        "path": "dags/broken.py",
        "exc_class": "ModuleNotFoundError",
        "msg": "ModuleNotFoundError: No module named 'airflow.thismoduledoesnotexist'",
    }]


def test_collection_error_reports_its_cause(tmp_path):
    # The shape of an old template calling DagBag(include_examples=False) on
    # Airflow >= 3.3: the test module dies at import, so no DAG is tested.
    layout_project(tmp_path, {"dags/a.py": "No import errors"})
    (tmp_path / ".astro" / "test_dag_integrity_default.py").write_text(
        "raise TypeError(\"DagBag.__init__() got an unexpected keyword argument "
        "'include_examples'\")\n")
    rc, result = parse_check.parse_output(_pytest(tmp_path))
    assert rc == 5
    assert result["collection_error"].startswith("TypeError: DagBag.__init__()")


def test_session_that_tests_nothing_is_not_a_pass(tmp_path):
    rc, result = parse_check.parse_output(run_probe_session(tmp_path, {}))
    assert rc == 2
    assert "tested no DAG files" in result["reason"]


def test_project_conftest_keeps_its_hooks_fixtures_and_options(tmp_path):
    # The reporter is appended to the project's .astro/conftest.py, so the
    # project's own hooks (same names as the reporter's), fixtures and options
    # must all still work.
    project_conftest = textwrap.dedent("""\
        import pytest

        SEEN = []

        def pytest_addoption(parser):
            parser.addoption("--project-flag", action="store_true")

        def pytest_runtest_logreport(report):
            SEEN.append(report.when)

        def pytest_sessionfinish(session, exitstatus):
            print("PROJECT_HOOK_RAN", len(SEEN))

        @pytest.fixture
        def project_fixture(request):
            return request.config.getoption("--project-flag")
    """)
    astro = tmp_path / ".astro"
    layout_project(tmp_path, {}, addopts='addopts = ["--project-flag", "-s"]',
                   project_conftest=project_conftest)
    (astro / "test_dag_integrity_default.py").write_text(textwrap.dedent("""\
        def test_file_imports(project_fixture):
            assert project_fixture is True
    """))
    out = _pytest(tmp_path)
    rc, result = parse_check.parse_output(out)
    assert (rc, result["failures"]) == (0, [])
    assert "PROJECT_HOOK_RAN 3" in out


def test_project_exitfirst_does_not_leave_files_untested(tmp_path):
    out = run_probe_session(tmp_path, {"dags/a.py": TRACEBACK, "dags/b.py": TRACEBACK},
                            'addopts = ["-x"]')
    rc, result = parse_check.parse_output(out)
    assert rc == 3
    assert [f["path"] for f in result["failures"]] == ["dags/a.py", "dags/b.py"]


def test_result_line_survives_cli_and_build_noise_around_it(tmp_path):
    session = run_probe_session(tmp_path, {"dags/a.py": "No import errors"})
    noisy = (
        "Checking your Dags for errors…\n"
        "#12 1.204 ============ 4 failed in 0.21s ============\n"  # a Dockerfile RUN pytest
        + session
        + "\x1b[32m✔\x1b[0m No errors detected in your Dags \n"
    )
    assert parse_check.parse_output(noisy) == (0, {"checked": 1, "failures": []})


BUILD_FAILURE_RUN = """\
Checking your Dags for errors…
#9 1.057       And because you require pydantic-ai-slim[openai]==1.107.0, we can
#9 1.057       conclude that your requirements are unsatisfiable.
ERROR: failed to build: failed to solve: process \
"/usr/local/bin/install-python-dependencies" did not complete successfully: exit code: 1

Error: something went wrong while parsing your Dags: an error was encountered \
while building the image, see the build logs for details
"""


@pytest.mark.parametrize("spelling", ["Dags", "DAGs"])
def test_build_failure_is_a_build_verdict(spelling):
    assert parse_check.parse_output(BUILD_FAILURE_RUN.replace("Dags", spelling))[0] == 4


@pytest.mark.parametrize("spelling", ["Dags", "DAGs"])
def test_cli_refusal_is_no_verdict_with_its_own_reason(spelling):
    text = (f"Error: something went wrong while parsing your {spelling}: no virtual "
            "environment found — run 'astro dev start' first\n")
    rc, result = parse_check.parse_output(text)
    assert rc == 2
    assert result["cli_error"].startswith("no virtual environment found")


def test_pytest_text_without_a_result_line_is_never_a_pass():
    # A clean-looking run whose reporter never loaded: no result line, no verdict.
    text = ("============================= test session starts ==============================\n"
            "collected 3 items\n\n"
            ".astro/test_dag_integrity_default.py ...                                  [100%]\n\n"
            "============================== 3 passed in 1.02s ===============================\n"
            "✔ No errors detected in your Dags\n")
    assert parse_check.parse_output(text)[0] == 2


def _payload(**overrides):
    payload = {"version": 1, "nonce": "n1", "exitstatus": 0, "collected": 1,
               "collect_errors": [],
               "results": [{"id": "dags/a.py", "outcome": "passed", "msg": ""}]}
    payload.update(overrides)
    return parse_check.SENTINEL + json.dumps(payload) + "\n"


def test_truncated_result_line_is_no_verdict():
    text = parse_check.SENTINEL + '{"version": 1, "exitstatus": 0, "collec'
    assert parse_check.parse_output(text)[0] == 2


def test_interrupted_session_without_failures_is_no_verdict():
    rc, result = parse_check.parse_output(_payload(exitstatus=2))
    assert rc == 2
    assert "pytest exit 2" in result["reason"]


def test_failures_from_an_interrupted_session_are_reported_as_incomplete():
    text = _payload(exitstatus=2, collected=1, results=[
        {"id": "dags/b.py", "outcome": "failed",
         "msg": "dags/b.py failed to import with message \n " + TRACEBACK}])
    rc, result = parse_check.parse_output(text)
    assert rc == 3
    assert result["failures"][0]["path"] == "dags/b.py"
    assert "pytest exit 2" in result["incomplete"]


def test_main_writes_import_json(tmp_path, monkeypatch):
    log = tmp_path / "parse.log"
    log.write_text(_payload())
    out = tmp_path / "out.json"
    monkeypatch.setenv("IMPORT_JSON", str(out))
    monkeypatch.setattr(sys, "argv", ["parse_check.py", str(log)])
    assert parse_check.main() == 0
    assert json.loads(out.read_text()) == {"checked": 1, "failures": []}


def test_result_line_inside_a_build_step_is_not_the_verdict():
    text = "#12 1.204 " + _payload()
    assert parse_check.parse_output(text)[0] == 2


def test_failed_build_outranks_a_result_line():
    # A Dockerfile step that ran pytest printed a result, then the build failed:
    # no integrity run happened inside a built image.
    text = _payload() + BUILD_FAILURE_RUN
    assert parse_check.parse_output(text)[0] == 4


def test_session_shorter_than_its_collection_is_no_verdict():
    rc, result = parse_check.parse_output(_payload(collected=3, ran=1))
    assert rc == 2
    assert "stopped after 1 of 3" in result["reason"]


def test_collection_error_next_to_passing_results_is_not_a_pass():
    text = _payload(collect_errors=[{"nodeid": "x", "msg": "ImportError: boom"}], exitstatus=1)
    rc, result = parse_check.parse_output(text)
    assert rc == 2
    assert result["reason"].startswith("part of the DAG integrity test failed to collect")


def test_only_the_runs_own_nonce_counts():
    # A line from a pytest step in the image build carries no (or another) nonce.
    assert parse_check.parse_output(_payload(nonce=""), nonce="n1")[0] == 2
    stale_then_real = _payload(nonce="n1") + _payload(nonce="other")
    assert parse_check.parse_output(stale_then_real, nonce="n1")[0] == 0


def test_session_stopped_early_keeps_its_failures_but_is_marked_incomplete():
    # Its failures stand, but they must not be compared with the baseline:
    # a shared failure could clear while unreached files go unchecked.
    text = _payload(exitstatus=1, collected=3, ran=1, results=[
        {"id": "dags/a.py", "outcome": "failed", "msg": "KeyError: 'x'"}])
    rc, result = parse_check.parse_output(text)
    assert rc == 3
    assert result["incomplete"] == "the DAG integrity run stopped after 1 of 3 tests"


def test_session_stopped_early_without_failures_is_no_verdict():
    rc, result = parse_check.parse_output(_payload(collected=3, ran=1))
    assert rc == 2
    assert "stopped after 1 of 3" in result["reason"]


def test_unparseable_last_line_does_not_hide_the_real_result():
    assert parse_check.parse_output(_payload() + parse_check.SENTINEL + '{"trunc')[0] == 0


OLD_TEMPLATE_TEST = """\
import pytest

@pytest.mark.parametrize("rel_path, rv", [(None, None)] + {errors!r}, ids=["placeholder"] + {ids!r})
def test_file_imports(rel_path, rv):
    if rel_path and rv:
        raise Exception(f"{{rel_path}} failed to import with message \\n {{rv}}")
"""


def test_older_templates_placeholder_is_not_a_checked_file(tmp_path):
    layout_project(tmp_path, {})
    (tmp_path / ".astro" / "test_dag_integrity_default.py").write_text(
        OLD_TEMPLATE_TEST.format(errors=[], ids=[]))
    rc, result = parse_check.parse_output(_pytest(tmp_path))
    assert rc == 2
    assert "tested no DAG files" in result["reason"]


def test_several_tests_per_file_is_a_complete_session(tmp_path):
    # A customised template testing each file twice: results are per file,
    # the completeness check is per test.
    layout_project(tmp_path, {})
    (tmp_path / ".astro" / "test_dag_integrity_default.py").write_text(textwrap.dedent("""\
        import pytest

        FILES = ["dags/a.py", "dags/b.py"]

        @pytest.mark.parametrize("rel_path", FILES, ids=FILES)
        def test_file_imports(rel_path):
            assert rel_path != "dags/b.py", "b is broken"

        @pytest.mark.parametrize("rel_path", FILES, ids=FILES)
        def test_dag_tags(rel_path):
            pass
    """))
    rc, result = parse_check.parse_output(_pytest(tmp_path))
    assert rc == 3
    assert [f["path"] for f in result["failures"]] == ["dags/b.py"]


def test_collection_error_keeps_its_cause_when_nothing_ran():
    text = _payload(results=[], collected=2, ran=0,
                    collect_errors=[{"nodeid": "x", "msg": "TypeError: DagBag.__init__() boom"}])
    rc, result = parse_check.parse_output(text)
    assert rc == 5
    assert result["collection_error"].startswith("TypeError")


def test_collection_error_beside_failures_keeps_them_and_is_incomplete():
    text = _payload(exitstatus=1, collected=1, ran=1,
                    collect_errors=[{"nodeid": "x", "msg": "ImportError: boom"}],
                    results=[{"id": "dags/a.py", "outcome": "failed", "msg": "KeyError: 'x'"}])
    rc, result = parse_check.parse_output(text)
    assert rc == 3
    assert [f["path"] for f in result["failures"]] == ["dags/a.py"]
    assert result["incomplete"].startswith("part of the DAG integrity test failed to collect")


@pytest.mark.parametrize("message", ["No module named 'gone'",
                                     "No module named 'gone'\\nhint: install the extra"])
def test_directly_raised_import_error_keeps_its_class(tmp_path, message):
    layout_project(tmp_path, {})
    (tmp_path / ".astro" / "test_dag_integrity_default.py").write_text(textwrap.dedent(f"""\
        import pytest

        @pytest.mark.parametrize("rel_path", ["dags/a.py"], ids=["dags/a.py"])
        def test_file_imports(rel_path):
            raise ModuleNotFoundError("{message}")
    """))
    rc, result = parse_check.parse_output(_pytest(tmp_path))
    assert rc == 3
    assert result["failures"][0]["exc_class"] == "ModuleNotFoundError"


def test_test_interrupted_mid_call_leaves_the_session_incomplete(tmp_path):
    layout_project(tmp_path, {})
    (tmp_path / ".astro" / "test_dag_integrity_default.py").write_text(textwrap.dedent("""\
        import pytest

        FILES = ["dags/a.py", "dags/b.py"]

        @pytest.mark.parametrize("rel_path", FILES, ids=FILES)
        def test_file_imports(rel_path):
            if rel_path == "dags/a.py":
                raise Exception("dags/a.py failed to import with message \\n KeyError: 'x'")
            raise KeyboardInterrupt
    """))
    rc, result = parse_check.parse_output(_pytest(tmp_path))
    assert rc == 3
    assert result["incomplete"] == "the DAG integrity run stopped after 1 of 2 tests"
