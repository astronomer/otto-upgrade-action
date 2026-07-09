"""Detection parses real Dockerfiles/requirements; the patcher is correct + idempotent."""


import apply_bump
import detect_versions as dv
import pytest


def _project(tmp_path, dockerfile: str, requirements: str):
    (tmp_path / "Dockerfile").write_text(dockerfile)
    (tmp_path / "requirements.txt").write_text(requirements)
    return str(tmp_path)


# --- detection ------------------------------------------------------------- #
def test_detect_azurecr_runtime_tag(tmp_path):
    p = _project(tmp_path, "FROM astrocrpublic.azurecr.io/runtime:3.1-12\n", "")
    rt = dv.detect_runtime(p)
    assert rt["tag"] == "3.1-12"
    assert rt["image_repo"] == "astrocrpublic.azurecr.io/runtime"


def test_detect_quay_runtime_tag(tmp_path):
    p = _project(tmp_path, "FROM quay.io/astronomer/astro-runtime:9.5.0\n", "")
    rt = dv.detect_runtime(p)
    assert rt["tag"] == "9.5.0"
    assert rt["image_repo"].endswith("astro-runtime")


def test_detect_digest_pinned(tmp_path):
    df = "FROM astrocrpublic.azurecr.io/runtime:3.2-3@sha256:abc123def456\n"
    p = _project(tmp_path, df, "")
    rt = dv.detect_runtime(p)
    assert rt["tag"] == "3.2-3"
    assert rt["digest"] == "sha256:abc123def456"


def test_detect_last_from_wins_multistage(tmp_path):
    df = (
        "FROM quay.io/astronomer/astro-runtime:9.0.0 AS base\n"
        "RUN echo hi\n"
        "FROM astrocrpublic.azurecr.io/runtime:3.2-3\n"
    )
    p = _project(tmp_path, df, "")
    assert dv.detect_runtime(p)["tag"] == "3.2-3"


def test_detect_providers_pins_and_unpinned(tmp_path):
    reqs = (
        "apache-airflow-providers-amazon==9.0.0\n"
        "apache-airflow-providers-http==5.0.0  # comment\n"
        "apache-airflow-providers-google[common]==10.0.0\n"
        "apache-airflow-providers-snowflake==5.1.0 ; python_version < '3.12'\n"  # env marker
        "apache-airflow-providers-cncf-kubernetes\n"  # unpinned
        "pandas==2.0.0\n"  # not a provider
    )
    p = _project(tmp_path, "FROM astrocrpublic.azurecr.io/runtime:3.1-12\n", reqs)
    provs = {x["package"]: x["pinned_version"] for x in dv.detect_providers(p)}
    assert provs["apache-airflow-providers-amazon"] == "9.0.0"
    assert provs["apache-airflow-providers-http"] == "5.0.0"
    assert provs["apache-airflow-providers-google"] == "10.0.0"
    assert provs["apache-airflow-providers-snowflake"] == "5.1.0"  # version, not the marker
    assert provs["apache-airflow-providers-cncf-kubernetes"] is None
    assert "pandas" not in " ".join(provs)


@pytest.mark.parametrize(
    ("spelling", "expected_pkg"),
    [
        ("apache-airflow-providers-common.sql", "apache-airflow-providers-common-sql"),
        ("apache-airflow-providers-common_sql", "apache-airflow-providers-common-sql"),
        ("Apache-Airflow-Providers-Common.SQL", "apache-airflow-providers-common-sql"),
        ("apache_airflow_providers_amazon", "apache-airflow-providers-amazon"),
    ],
)
def test_detect_pep503_equivalent_spellings(tmp_path, spelling, expected_pkg):
    # pip treats `.`/`_`/`-` and case as identical in package names (PEP 503);
    # a typo'd spelling must be detected as the provider it actually installs.
    p = _project(tmp_path, "", f"{spelling}==1.30.2\n")
    (prov,) = dv.detect_providers(p)
    assert prov["package"] == expected_pkg
    assert prov["pinned_version"] == "1.30.2"
    assert prov["spec_name"] == spelling  # original spelling preserved


def test_detect_pep503_typo_with_extras(tmp_path):
    p = _project(tmp_path, "", "apache-airflow-providers-common.sql[pandas]==1.2.0\n")
    (prov,) = dv.detect_providers(p)
    assert prov["package"] == "apache-airflow-providers-common-sql"
    assert prov["pinned_version"] == "1.2.0"


def test_detect_duplicate_spellings_conflicting_pins_skipped(tmp_path):
    reqs = (
        "apache-airflow-providers-common.sql==1.30.2\n"
        "apache-airflow-providers-common-sql==1.32.0\n"
    )
    p = _project(tmp_path, "", reqs)
    (prov,) = dv.detect_providers(p)  # collapsed to one entry
    assert prov["pinned_version"] is None  # never pick a side
    assert "conflicting pins" in prov["note"]
    assert "common.sql" in prov["note"] and "common-sql" in prov["note"]


def test_detect_duplicate_spellings_same_pin_collapsed(tmp_path):
    reqs = (
        "apache-airflow-providers-common.sql==1.30.2\n"
        "apache-airflow-providers-common-sql==1.30.2\n"
    )
    p = _project(tmp_path, "", reqs)
    (prov,) = dv.detect_providers(p)
    assert prov["pinned_version"] == "1.30.2"
    assert "note" not in prov


# --- patching -------------------------------------------------------------- #
def test_bump_dockerfile_swaps_only_the_tag(tmp_path):
    p = _project(tmp_path, "FROM astrocrpublic.azurecr.io/runtime:3.1-12\n", "")
    assert apply_bump.bump_dockerfile(p, "3.1-12", "3.2-3") is True
    assert (tmp_path / "Dockerfile").read_text() == "FROM astrocrpublic.azurecr.io/runtime:3.2-3\n"


def test_bump_dockerfile_idempotent(tmp_path):
    p = _project(tmp_path, "FROM astrocrpublic.azurecr.io/runtime:3.2-3\n", "")
    assert apply_bump.bump_dockerfile(p, "3.1-12", "3.2-3") is False  # current tag absent


def test_bump_dockerfile_partial_tag_not_matched(tmp_path):
    # A current tag of "3.2" must NOT match the "3.2" prefix of "3.2-3".
    p = _project(tmp_path, "FROM astrocrpublic.azurecr.io/runtime:3.2-3\n", "")
    assert apply_bump.bump_dockerfile(p, "3.2", "9.9") is False
    assert (tmp_path / "Dockerfile").read_text() == "FROM astrocrpublic.azurecr.io/runtime:3.2-3\n"


def test_bump_requirements_preserves_extras_and_comments(tmp_path):
    reqs = "apache-airflow-providers-amazon[s3]==9.0.0  # keep me\npandas\n"
    p = _project(tmp_path, "", reqs)
    providers = [{"package": "apache-airflow-providers-amazon", "current": "9.0.0", "target": "9.30.0"}]
    changed = apply_bump.bump_requirements(p, providers)
    assert changed == [{"package": "apache-airflow-providers-amazon", "from": "9.0.0", "to": "9.30.0"}]
    text = (tmp_path / "requirements.txt").read_text()
    assert "apache-airflow-providers-amazon[s3]==9.30.0  # keep me" in text
    assert "pandas\n" in text


def test_bump_requirements_noop_when_target_equals_current(tmp_path):
    reqs = "apache-airflow-providers-amazon==9.0.0\n"
    p = _project(tmp_path, "", reqs)
    providers = [{"package": "apache-airflow-providers-amazon", "current": "9.0.0", "target": "9.0.0"}]
    assert apply_bump.bump_requirements(p, providers) == []


@pytest.mark.parametrize(
    "line",
    [
        "apache-airflow-providers-common.sql==1.30.2  # keep me",
        "apache-airflow-providers-common_sql==1.30.2  # keep me",
        "Apache-Airflow-Providers-Common.SQL==1.30.2  # keep me",
        "apache-airflow-providers-common.sql[pandas]==1.30.2  # keep me",
    ],
)
def test_bump_requirements_pep503_spelling_preserved(tmp_path, line):
    # The plan carries the normalized name; the file keeps the user's spelling —
    # only the version changes.
    p = _project(tmp_path, "", line + "\n")
    providers = [{"package": "apache-airflow-providers-common-sql",
                  "current": "1.30.2", "target": "1.36.0"}]
    changed = apply_bump.bump_requirements(p, providers)
    assert changed == [{"package": "apache-airflow-providers-common-sql",
                        "from": "1.30.2", "to": "1.36.0"}]
    assert (tmp_path / "requirements.txt").read_text() == line.replace("1.30.2", "1.36.0") + "\n"


def test_bump_requirements_second_run_reports_nothing(tmp_path):
    # File-level idempotency also means the summary is empty on a re-run — a
    # from==to rewrite must not be reported as a change.
    p = _project(tmp_path, "", "apache-airflow-providers-amazon==9.0.0\n")
    providers = [{"package": "apache-airflow-providers-amazon", "current": "9.0.0", "target": "9.30.0"}]
    assert apply_bump.bump_requirements(p, providers) != []
    assert apply_bump.bump_requirements(p, providers) == []


def test_bump_requirements_pep503_no_prefix_false_match(tmp_path):
    # `common-sql` must not match the `common-sql` prefix of another package,
    # and `common` must not match inside `common-sql`.
    reqs = "apache-airflow-providers-common-sql==1.30.2\n"
    p = _project(tmp_path, "", reqs)
    providers = [{"package": "apache-airflow-providers-common", "current": "1.0.0", "target": "2.0.0"}]
    assert apply_bump.bump_requirements(p, providers) == []
    assert (tmp_path / "requirements.txt").read_text() == reqs
