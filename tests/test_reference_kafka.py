"""Actual local Debezium -> Kafka -> Iceberg replay; synthetic evidence only."""

import os
import uuid

import pytest

pytest.importorskip("kafka")
from chemiguard119 import reference_kafka as cdc
from chemiguard119 import reference_lakehouse as lake
from chemiguard119 import reference_postgres as pg
from chemiguard119 import reference_batch as batch
from test_reference_postgres import staged, activate, snapshot, new_input
from test_reference_batch import setup as base_setup

pytestmark = pytest.mark.skipif(
    os.getenv("CHEMICHECK_KAFKA_TEST") != "1",
    reason="Local real Kafka fixture required",
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return base_setup.__wrapped__(tmp_path, monkeypatch)


def test_debezium_offset_replay_and_iceberg_crash(setup):
    root, spec, incoming, rows = setup
    group = "test-" + uuid.uuid4().hex
    cdc.consume(group=group)  # Initial snapshot/history; not a new release.
    pipeline = staged(root, spec, "kafka-first")
    assert cdc.consume(group=group)["records"] == 0  # Staging is not published.
    activate(pipeline)
    before = snapshot(root)
    with pytest.raises(batch.BatchError, match="INJECTED_BEFORE_KAFKA_OFFSET_COMMIT"):
        cdc.consume(group=group, fault="before_offset_commit")
    with pg.connection() as db:
        namespace = lake.active_namespace(db)
    table = lake.table_for(lake.catalog(), pg.stream_id(root), namespace)
    assert len(table.snapshots()) == 1
    replay = cdc.consume(group=group)
    assert replay["duplicates"] == 1
    assert len(table.refresh().snapshots()) == 1
    assert snapshot(root) == before
    assert lake.verify_trino()["status"] == "MATCHED"
    new_input(incoming, rows)
    activate(staged(root, spec, "kafka-second"))
    before = snapshot(root)
    with pytest.raises(batch.BatchError, match="INJECTED_AFTER_ICEBERG_COMMIT"):
        cdc.consume(group=group, fault="after_iceberg_commit")
    count = len(table.refresh().snapshots())
    assert snapshot(root) == before
    assert cdc.consume(group=group)["projected"] == 1
    assert len(table.refresh().snapshots()) == count
    assert lake.verify_trino()["status"] == "MATCHED"
    assert cdc.consume(group=group)["records"] == 0


def test_unsupported_event_rejected():
    with pytest.raises(batch.BatchError, match="KAFKA_UNSUPPORTED_RELEASE_EVENT"):
        cdc.process({"op": "d"})
    with pytest.raises(batch.BatchError, match="KAFKA_UNEXPECTED_SOURCE"):
        cdc.process({"op": "c", "source": {"table": "input_lines"}})
