"""Manual local CDC projection; isolated worker Python avoids Airflow dependency drift."""

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

from airflow.sdk import DAG, task
from airflow.exceptions import AirflowFailException


def worker(command, module="chemiguard119.reference_lakehouse"):
    result = subprocess.run(
        [
            os.environ["CHEMICHECK_LAKEHOUSE_PYTHON"],
            "-m",
            module,
            command,
        ],
        capture_output=True,
        text=True,
        timeout=540,
        check=False,
    )
    if result.returncode:
        # Driver errors may contain connection details. No stderr/payload dumping.
        raise AirflowFailException("LAKEHOUSE_STEP_FAILED_SEE_LOCAL_WORKER_STATUS")
    return json.loads(result.stdout)


with DAG(
    dag_id="chemicheck119_reference_kafka",
    start_date=datetime(2026, 10, 7, tzinfo=timezone.utc),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0, "execution_timeout": timedelta(minutes=10)},
    tags=["local-only", "approved-reference", "cdc"],
) as dag:

    @task
    def consume_kafka_releases():
        return worker("consume", "chemiguard119.reference_kafka")

    @task
    def compare_trino_with_postgres():
        return worker("verify")

    consume_kafka_releases() >> compare_trino_with_postgres()
