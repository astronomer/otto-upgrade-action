"""Needs a value from the gitignored .env at parse time (os.environ, which the
integrity test does not mock). FIXTURE_SIMULATE_REGRESSION=1 makes it also break
on any Runtime other than the fixture's pinned one, standing in for an upgrade
that breaks this file."""

import os

from airflow.sdk import dag, task

BUCKET = os.environ["FIXTURE_DATA_BUCKET"]

if (
    os.environ.get("FIXTURE_SIMULATE_REGRESSION") == "1"
    and os.environ.get("ASTRONOMER_RUNTIME_VERSION") != "3.3-10"
):
    raise TypeError("simulated upgrade regression in env_dependent_dag")


@dag(tags=["e2e"])
def env_dependent_dag():
    @task
    def read_bucket():
        return BUCKET

    read_bucket()


env_dependent_dag()
