"""A well-formed DAG used by the otto-upgrade-action e2e setup.

Imports are current, the schedule uses the modern parameter name, and the task
flow is explicit. The verifier should report this file as clean at the target
Airflow version.
"""

from datetime import datetime

from airflow.sdk import DAG, task

with DAG(
    dag_id="clean_etl_dag",
    start_date=datetime(2026, 1, 1),
    schedule="@daily",
    catchup=False,
    tags=["e2e", "otto-upgrade"],
    default_args={"owner": "astronomer", "retries": 2},
):

    @task
    def extract() -> list[int]:
        return [1, 2, 3]

    @task
    def load(rows: list[int]) -> int:
        return sum(rows)

    load(extract())
