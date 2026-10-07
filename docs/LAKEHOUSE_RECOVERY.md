# Local current-head reconstruction

2026-10-07: User manually triggered the Airflow DAG and inspected comparison failure.
Separate worker diagnosis found ICEBERG_MISSING_METADATA after the memory-only Moto
fixture restarted. User then queried the selected PG version and confirmed 18 rows
for the previously selected stream. No application UI continuity claim follows.

The new `recover` command reconstructs only current approved PG heads in a unique
`recovery_<uuid>` Iceberg namespace. Original catalog entries, snapshots/receipts,
PG evidence, service heads, and activation history are retained. Lost historic files
are not restored. New receipts are keyed by namespace and activation. Full key and
field comparison through Trino must succeed before a PG transaction publishes the
new projection namespace. Consumer and stream advisory locks protect reconciliation
against concurrent release/recovery. Subsequent consume/verify uses this projection;
service_evidence continues using the original PG service heads.

## Local commands

From /Users/hywznn/Documents/chemicheck119-lab/analysis-engine:

```sh
set -a
source local/lakehouse/env.example
set +a
.venv/bin/python -m chemiguard119.reference_lakehouse recover
.venv/bin/python -m chemiguard119.reference_lakehouse verify
```

The local fixture bucket `reference` must exist. Prepare a missing empty fixture
bucket with `scripts/data/prepare_lakehouse_fixture.py` before recovery. This does
not contact AWS. Do not delete the original PG receipts or catalog entries.
In Airflow, trigger chemicheck119_reference_lakehouse after recovery and inspect
both task logs and comparison XCom. Recovery itself was executed by the AI, not the
user. The user's next trigger is a separate direct execution record.

## Verification

Initial real local PG/Trino recovery: 24 current heads, 100 service rows, full content
MATCHED. Before/after service content checksum:
0b140bbfa12f1a14b3465f7dae0341b03bc8e9f625cbe2eed8f56abbc60a79d8.
Selected heads and original receipt checksum unchanged. Injected failure before
projection publication left the old namespace selected. Retry succeeded in a fresh
namespace, and subsequent consume/verify succeeded. Measurement JSON saved locally
at /tmp/chemicheck119-recovery-before.json and /tmp/chemicheck119-recovery-result.json.

Integration regression command (creates additional isolated fixture streams):

```sh
.venv/bin/python -m pytest tests/test_reference_lakehouse.py -q
```

## Limits

Moto is still memory-only: this change supplies reconstruction, not durable storage
or backup. Failed reconstruction may leave unselected namespace files; preserve for
inspection and clean separately after review. Reconstruction does not recover old
Iceberg snapshot IDs, guarantee operational safety, or demonstrate production use.
The active recovery namespace is resolved by consume/verify; manual Trino queries
hard-coded to `reference.approved` still address the original failed namespace.

Both integration scenarios passed (pytest exit 0). They added two separate fixture
streams, so the final comparison checked 26 heads and returned MATCHED. This increase
is test scope, not a production dataset increase. Ruff and diff whitespace checks pass.
