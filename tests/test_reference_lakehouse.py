"""Fixture-only real PG WAL -> Iceberg -> Trino; no operational incident data."""

import os

import pytest

pytest.importorskip("pyiceberg")
from chemiguard119 import reference_lakehouse as lake
from chemiguard119 import reference_postgres as pg
from chemiguard119 import reference_batch as batch
from test_reference_postgres import staged, activate, snapshot, new_input
from test_reference_batch import setup as base_setup

pytestmark = pytest.mark.skipif(
    os.getenv("CHEMICHECK_LAKEHOUSE_TEST") != "1",
    reason="Isolated lakehouse fixture environment required",
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return base_setup.__wrapped__(tmp_path, monkeypatch)


def trino_rows(stream, snapshot_id=None):
    from trino.dbapi import connect

    cursor = connect(host="127.0.0.1", port=58080, user="local-fixture").cursor()
    with pg.connection() as db:
        namespace = lake.active_namespace(db)
    travel = f" FOR VERSION AS OF {snapshot_id}" if snapshot_id else ""
    cursor.execute(
        f"SELECT {','.join(lake.FIELDS)} FROM reference.{namespace}.s_{stream}{travel} ORDER BY evidence_id"
    )
    return [tuple(row) for row in cursor.fetchall()]


def test_wal_projection_crash_replay_trino_and_rollback(setup):
    root, spec, incoming, rows = setup
    lake.initialize()
    stream = pg.stream_id(root)
    p = staged(root, spec, "lake-first")
    with pytest.raises(batch.BatchError, match="HUMAN_REVIEW_REQUIRED"):
        p.execute("activate")
    lake.consume()
    assert not lake.catalog().table_exists(("approved", "s_" + stream))
    old = activate(p)
    lake.consume()
    with pg.connection() as db:
        namespace = lake.active_namespace(db)
    table = lake.table_for(lake.catalog(), stream, namespace)
    first_snapshot = table.current_snapshot().snapshot_id
    original = trino_rows(stream)
    assert len(original) == 2
    assert {r[0] for r in original} == {old}
    assert [r[:6] for r in original] == snapshot(root)[1]
    activate(staged(root, spec, "lake-same-version"))
    lake.consume()
    assert len(table.refresh().snapshots()) == 1
    new_input(incoming, rows)
    new = activate(staged(root, spec, "lake-new"))
    service_before = snapshot(root)
    with pytest.raises(batch.BatchError, match="INJECTED_AFTER_ICEBERG_COMMIT"):
        lake.consume(fault="after_iceberg_commit")
    assert snapshot(root) == service_before
    assert len(table.refresh().snapshots()) == 3
    lake.consume()  # receipt recovery reuses committed snapshot
    assert len(table.refresh().snapshots()) == 3
    assert len(trino_rows(stream)) == 3
    assert {r[0] for r in trino_rows(stream)} == {new}
    assert trino_rows(stream, first_snapshot) == original
    pg.rollback(root, old)
    lake.consume()
    assert trino_rows(stream) == original
    assert [r[:6] for r in trino_rows(stream)] == snapshot(root)[1]
    with pg.connection() as db:
        receipts = db.execute(
            "SELECT count(*) FROM reference_lab.lake_receipts r JOIN reference_lab.activations a USING(activation_id) WHERE a.stream=%s"
            if namespace == "approved"
            else "SELECT count(*) FROM reference_lab.lake_recovery_receipts r JOIN reference_lab.activations a USING(activation_id) WHERE a.stream=%s AND namespace=%s",
            (stream,) if namespace == "approved" else (stream, namespace),
        ).fetchone()[0]
    assert lake.verify_trino()["status"] == "MATCHED"
    # Same row count, altered source content must not pass reconciliation.
    with pg.connection() as db:
        key, body = db.execute(
            "SELECT evidence_id,body FROM reference_lab.evidence WHERE stream=%s AND version=%s ORDER BY evidence_id LIMIT 1",
            (stream, old),
        ).fetchone()
        db.execute(
            "UPDATE reference_lab.evidence SET body=%s WHERE stream=%s AND version=%s AND evidence_id=%s",
            (body + " fixture corruption", stream, old, key),
        )
    try:
        with pytest.raises(batch.BatchError, match="TRINO_RELEASE_CONTENT_MISMATCH"):
            lake.verify_trino()
    finally:
        with pg.connection() as db:
            db.execute(
                "UPDATE reference_lab.evidence SET body=%s WHERE stream=%s AND version=%s AND evidence_id=%s",
                (body, stream, old, key),
            )
    assert lake.verify_trino()["status"] == "MATCHED"
    assert receipts == 3
    assert len(table.refresh().snapshots()) == 5


def test_recovery_failure_preserves_service_and_projection(setup):
    root, spec, _, _ = setup
    lake.initialize()
    activate(staged(root, spec, "recovery-input"))
    lake.consume()
    before = snapshot(root)
    with pg.connection() as db:
        namespace = lake.active_namespace(db)
    with pytest.raises(batch.BatchError, match="INJECTED_RECOVERY_BEFORE_PUBLISH"):
        lake.recover(fault="before_publish")
    with pg.connection() as db:
        assert lake.active_namespace(db) == namespace
    assert snapshot(root) == before
    assert lake.verify_trino()["status"] == "MATCHED"
    recovered = lake.recover()
    assert recovered["status"] == "MATCHED"
    assert recovered["namespace"] != namespace
    assert snapshot(root) == before
    lake.consume()
    assert lake.verify_trino()["status"] == "MATCHED"
