"""Run manual local projection DAG and write only non-sensitive task status."""

# ruff: noqa: E402
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from airflow.models.dagbag import DagBag, sync_bag_to_db
from airflow.dag_processing.bundles.manager import DagBundlesManager

root = Path(__file__).resolve().parents[2]
bag = DagBag(str(root / "dags" / "lakehouse_reference.py"), include_examples=False)
assert not bag.import_errors, "DAG_IMPORT_FAILED"
DagBundlesManager().sync_bundles_to_db()
sync_bag_to_db(bag, bundle_name="dags-folder", bundle_version=None)
dag = bag.dags["chemicheck119_reference_lakehouse"]
run = dag.test(logical_date=datetime(2026, 10, 7, 4, 0, tzinfo=timezone.utc))
assert str(run.state) == "success", "LOCAL_PROJECTION_DAG_FAILED"
Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "scope": "LOCAL_SYNTHETIC_WAL_ICEBERG_TRINO",
            "state": str(run.state),
            "tasks": dag.task_ids,
            "schedule": None,
            "production_changed": False,
            "existing_incident_runtime_changed": False,
            "executed_by": "AI_AGENT",
            "user_direct_execution_verified": False,
        },
        indent=2,
    )
    + "\n"
)
