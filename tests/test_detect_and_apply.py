"""Detection parses real Dockerfiles/requirements; the patcher is correct + idempotent."""


import apply_bump
import detect_versions as dv


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
        "apache-airflow-providers-cncf-kubernetes\n"  # unpinned
        "pandas==2.0.0\n"  # not a provider
    )
    p = _project(tmp_path, "FROM astrocrpublic.azurecr.io/runtime:3.1-12\n", reqs)
    provs = {x["package"]: x["pinned_version"] for x in dv.detect_providers(p)}
    assert provs["apache-airflow-providers-amazon"] == "9.0.0"
    assert provs["apache-airflow-providers-http"] == "5.0.0"
    assert provs["apache-airflow-providers-google"] == "10.0.0"
    assert provs["apache-airflow-providers-cncf-kubernetes"] is None
    assert "pandas" not in " ".join(provs)


# --- patching -------------------------------------------------------------- #
def test_bump_dockerfile_swaps_only_the_tag(tmp_path):
    p = _project(tmp_path, "FROM astrocrpublic.azurecr.io/runtime:3.1-12\n", "")
    assert apply_bump.bump_dockerfile(p, "3.1-12", "3.2-3") is True
    assert (tmp_path / "Dockerfile").read_text() == "FROM astrocrpublic.azurecr.io/runtime:3.2-3\n"


def test_bump_dockerfile_idempotent(tmp_path):
    p = _project(tmp_path, "FROM astrocrpublic.azurecr.io/runtime:3.2-3\n", "")
    assert apply_bump.bump_dockerfile(p, "3.1-12", "3.2-3") is False  # current tag absent


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
