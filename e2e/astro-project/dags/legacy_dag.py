"""A DAG written against older Airflow conventions.

It is syntactically valid (so the syntax-level verifier passes), but uses the
pre-3.x ``schedule_interval`` kwarg and the legacy top-level ``airflow.DAG``
import path. This is the kind of file the Otto migration step rewrites and the
import-level verifier would flag at the target Airflow version.
"""

from datetime import datetime

from airflow import DAG
from airflow.operators.python import PythonOperator


def _say_hello():
    print("hello from a legacy dag")


with DAG(
    dag_id="legacy_dag",
    start_date=datetime(2026, 1, 1),
    schedule_interval="@daily",  # renamed to `schedule` in Airflow 3
    catchup=False,
    tags=["e2e", "otto-upgrade", "legacy"],
):
    PythonOperator(task_id="say_hello", python_callable=_say_hello)
