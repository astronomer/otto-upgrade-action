from airflow.sdk import dag, task


@dag(tags=["e2e"])
def plain_dag():
    @task
    def hello():
        return "hello"

    hello()


plain_dag()
