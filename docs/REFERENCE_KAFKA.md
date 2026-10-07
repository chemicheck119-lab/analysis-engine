# Local Debezium / Kafka approved-release pipeline

## Purpose and boundary

This extends the existing local PG / Iceberg / Trino exercise, not live incident
service traffic. Only `reference_lab.activations` is published. Staging rows,
chemical bodies, phone transcripts, and incident input are not Kafka payloads.
The DB is the existing synthetic reference lab; user approval gates are unchanged.
Debezium pgoutput is a separate replication slot from the original test_decoding
consumer. The old manual DAG remains available; the new DAG is
`chemicheck119_reference_kafka`. Manual schedule, no automatic retry on validation.
Do not enable both consumers as recurring competing delivery routes.

Flow: PG activation insert -> Debezium Connect -> Kafka topic
`chemicheck.reference_lab.activations` -> Python consumer -> existing Iceberg
projection -> Trino full-key/full-content reconciliation. Kafka is single-broker
local PLAINTEXT with loopback host ports, never an operational security template.
Debezium snapshot `r` events replay existing approval history; `c` events represent
new committed inserts. Only current selected activations are projected; delayed
historic events cannot move a table back to a no-longer-selected version. Delete
and update of immutable activation history fail without committing an offset.

## Start / inspect / consume

From /Users/hywznn/Documents/chemicheck119-lab/analysis-engine:

```sh
uv pip install --python .venv/bin/python -r local/cdc/requirements.txt
docker compose -f local/cdc/compose.yaml up -d
.venv/bin/python scripts/data/register_local_cdc.py
curl http://127.0.0.1:58083/connectors/chemicheck-approved-local/status
set -a
source local/lakehouse/env.example
set +a
.venv/bin/python -m chemiguard119.reference_kafka consume
.venv/bin/python -m chemiguard119.reference_lakehouse verify
```

Prerequisite: existing local lakehouse PostgreSQL and healthy Iceberg/Trino. Kafka
Connect config PUT is idempotent. The S3 fixture remains memory-only and requires
reconstruction after object loss; Kafka does not fix that. Starting this Compose
project does not restart the lakehouse containers. Kafka has a named local volume;
its persistence/restart durability is not implied by consumer replay tests.

## Delivery / failure semantics

Kafka auto-commit is disabled. One record is processed at a time, PG receipt is
committed only after verified Iceberg projection, then offset+1 is committed.
Fault after Iceberg commit reuses its matching snapshot; fault after receipt but
before Kafka offset commit replays as ALREADY_PROJECTED. At-least-once delivery
with idempotent projection, not exactly-once or cross-system atomicity. Validation
and storage failure leave the offset uncommitted. No raw event logging. Kafka
initial snapshot may still contain run IDs; only non-sensitive fixture DB permitted.

## Direct learning exercise

1. In Airflow open `chemicheck119_reference_kafka`, read the code and identify which
   task reads Kafka and which compares data. The AI implemented this code.
2. Trigger it manually. Examine logs and XCom counts: records, projected,
   duplicates, historical. Zero records means no unread events, not CDC failure.
3. Confirm comparison MATCHED. Trigger again to observe no additional records.
4. For new-event observation use a separate synthetic fixture run, stage and
   inspect it, explicitly approve it, then consume. Do not bypass the review gate
   by direct SQL insertion. Tests below automate synthetic approval; they are AI
   mechanics checks, not the user's own approval experience.

## Reproduce validation

```sh
export CHEMICHECK_KAFKA_TEST=1
.venv/bin/python -m pytest tests/test_reference_kafka.py -q
```

Tests inspect actual snapshots/service rows, not just return codes: staging emits
no release, offset-gap crash replays without new snapshots, Iceberg/receipt crash
recovery reuses a snapshot, source validation rejects unrelated tables and delete
operations. Real Debezium initial history and committed inserts are used.

Remaining: independently restricted DB replication role, schema changes, source
logical update/delete design, connector/broker outage, multi-broker failover, WAL
retention/slot loss, retention expiry, durable object store, live BFF integration,
user measurements. No commercial operation or field safety claim.

Official references:
- https://debezium.io/documentation/reference/stable/connectors/postgresql.html
- https://kafka.apache.org/40/getting-started/docker/
