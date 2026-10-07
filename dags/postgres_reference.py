"""Airflow 3 local reference-data batch. No incident request-path dependency."""

import os
from datetime import datetime, timedelta, timezone

from airflow.sdk import DAG, task, get_current_context
from airflow.exceptions import AirflowFailException

from chemiguard119.reference_batch import BatchError, read
from chemiguard119.reference_postgres import PostgresPipeline as Pipeline


def pipeline():
    context = get_current_context()
    return Pipeline(os.environ["CHEMICHECK_PG_BATCH_ROOT"], context["run_id"])


with DAG(
    dag_id="chemicheck119_postgres_reference",
    start_date=datetime(2026, 10, 1, tzinfo=timezone.utc),
    schedule=os.getenv("CHEMICHECK_BATCH_SCHEDULE") or None,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0, "execution_timeout": timedelta(hours=2)},
    tags=["local-only", "reference", "kosha"],
) as dag:

    @task
    def collect():
        p = pipeline()
        context = get_current_context()
        spec_path = (
            context["dag_run"].conf.get("spec_path")
            or os.environ["CHEMICHECK_PG_BATCH_SPEC"]
        )
        p.initialize(read(spec_path))
        try:
            # Existing KOSHA client owns bounded network retries (2); Airflow does
            # not restart validation failures or multiply the API retry budget.
            p.execute("collect")
        except BatchError as error:
            raise AirflowFailException(str(error)) from None

    def step_task(name):
        @task(task_id=name)
        def execute():
            try:
                pipeline().execute(name)
            except BatchError as error:
                raise AirflowFailException(str(error)) from None

        return execute()

    archive = step_task("archive")
    normalize = step_task("normalize")
    validate = step_task("validate")
    stage = step_task("stage")
    activate = step_task("activate")

    @task
    def verify_usage():
        pipeline().verify_usage()

    usage = verify_usage()

    @task(trigger_rule="all_done")
    def record_result():
        p = pipeline()
        result = p.finish()
        # all_done must not turn an upstream failure into a successful DagRun.
        if result["status"] != "SUCCESS":
            raise AirflowFailException("BATCH_FAILED_SEE_LOCAL_RESULT_AND_TASK_LOGS")

    result = record_result()
    collection = collect()
    collection >> archive >> normalize >> validate >> stage >> activate
    activate >> usage
    [collection, archive, normalize, validate, stage, activate, usage] >> result
