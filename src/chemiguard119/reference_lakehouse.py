"""Local approved-release WAL projection. Never used in the incident request path."""

import os
import re

from chemiguard119 import reference_batch as batch
from chemiguard119 import reference_postgres as pg

SLOT = "chemicheck_reference_local"
EVENT = re.compile(
    r"^table reference_lab\.activations: INSERT: .*?activation_id\[text\]:'([0-9a-f]{32})'"
)
FIELDS = (
    "version",
    "evidence_id",
    "source_record_id",
    "cas_number",
    "title",
    "body",
    "document_version",
)


def catalog():
    from pyiceberg.catalog import load_catalog

    return load_catalog(
        "reference",
        type="sql",
        uri=os.environ["CHEMICHECK_ICEBERG_CATALOG_URI"],
        warehouse="s3://reference/",
        **{
            "s3.endpoint": os.environ["CHEMICHECK_ICEBERG_S3_ENDPOINT"],
            "s3.access-key-id": os.environ["CHEMICHECK_ICEBERG_S3_KEY"],
            "s3.secret-access-key": os.environ["CHEMICHECK_ICEBERG_S3_SECRET"],
            "s3.region": "us-east-1",
        },
    )


def initialize():
    pg.migrate()
    with pg.connection() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS reference_lab.lake_receipts (
          activation_id text PRIMARY KEY REFERENCES reference_lab.activations(activation_id),
          snapshot_id bigint NOT NULL, row_count integer NOT NULL, checksum text NOT NULL,
          at timestamptz NOT NULL DEFAULT now())""")
    with pg.connection() as db:
        if not db.execute(
            "SELECT 1 FROM pg_replication_slots WHERE slot_name=%s", (SLOT,)
        ).fetchone():
            db.execute(
                "SELECT pg_create_logical_replication_slot(%s,'test_decoding')", (SLOT,)
            )
    catalog().create_namespace_if_not_exists("approved")


def table_for(cat, stream):
    from pyiceberg.schema import Schema
    from pyiceberg.types import NestedField, StringType

    identifier = ("approved", "s_" + stream)
    return cat.create_table_if_not_exists(
        identifier,
        Schema(
            *[
                NestedField(i + 1, name, StringType(), required=False)
                for i, name in enumerate(FIELDS)
            ]
        ),
    )


def arrow(rows):
    import pyarrow as pa

    return pa.Table.from_pylist(
        [dict(zip(FIELDS, row)) for row in rows],
        schema=pa.schema([(name, pa.string()) for name in FIELDS]),
    )


def fingerprint(rows):
    return batch.digest(sorted([list(row) for row in rows], key=lambda row: row[1]))


def project(db, cat, activation_id, fault=None):
    event = db.execute(
        "SELECT stream,version FROM reference_lab.activations WHERE activation_id=%s",
        (activation_id,),
    ).fetchone()
    if not event:
        raise batch.BatchError("CDC_ACTIVATION_NOT_FOUND")
    stream, version = event
    if db.execute(
        "SELECT 1 FROM reference_lab.lake_receipts WHERE activation_id=%s",
        (activation_id,),
    ).fetchone():
        return
    rows = db.execute(
        "SELECT version,evidence_id,source_record_id,cas_number,title,body,document_version "
        "FROM reference_lab.evidence WHERE stream=%s AND version=%s ORDER BY evidence_id",
        (stream, version),
    ).fetchall()
    if not rows or len(rows) != len({row[1] for row in rows}):
        raise batch.BatchError("CDC_INVALID_RELEASE")
    checksum = fingerprint(rows)
    table = table_for(cat, stream)
    # Recover the commit/receipt gap without another overwrite or duplicate append.
    snapshot = next(
        (
            s
            for s in reversed(table.snapshots())
            if s.summary.get("activation_id") == activation_id
        ),
        None,
    )
    if snapshot is None:
        table.overwrite(
            arrow(rows),
            snapshot_properties={
                "activation_id": activation_id,
                "version": version,
                "checksum": checksum,
            },
        )
        snapshot = table.current_snapshot()
    actual = table.scan(snapshot_id=snapshot.snapshot_id).to_arrow().to_pylist()
    actual_rows = [tuple(row.get(name) for name in FIELDS) for row in actual]
    if fingerprint(actual_rows) != checksum:
        raise batch.BatchError("ICEBERG_CONTENT_MISMATCH")
    if fault == "after_iceberg_commit":
        raise batch.BatchError("INJECTED_AFTER_ICEBERG_COMMIT")
    db.execute(
        "INSERT INTO reference_lab.lake_receipts VALUES(%s,%s,%s,%s,now())",
        (activation_id, snapshot.snapshot_id, len(rows), checksum),
    )


def consume(fault=None):
    """Peek committed WAL, persist verified receipts, then acknowledge WAL.

    test_decoding is deliberately confined to a fixture-only DB. Raw decoded rows
    may include staging data; never log or persist them. Production needs a scoped
    publication/pgoutput or Debezium connector and restricted replication role.
    """
    cat = catalog()
    with pg.connection() as db:
        pg.lock(db, "lakehouse-consumer")
        changes = db.execute(
            "SELECT lsn::text,data FROM pg_logical_slot_peek_changes(%s,NULL,NULL,'include-xids','0')",
            (SLOT,),
        ).fetchall()
        ids = []
        for _, data in changes:
            if data.startswith("table reference_lab.activations:"):
                match = EVENT.match(data)
                if not match:
                    raise batch.BatchError("CDC_RELEASE_HISTORY_MUTATED_OR_UNSUPPORTED")
                ids.append(match.group(1))
        for activation_id in ids:
            project(db, cat, activation_id, fault)
    # Separate receipt commit from acknowledgement; replay is idempotent.
    if changes:
        with pg.connection() as db:
            pg.lock(db, "lakehouse-consumer")
            db.execute(
                "SELECT count(*) FROM pg_logical_slot_get_changes(%s,%s::pg_lsn,NULL,'include-xids','0')",
                (SLOT, changes[-1][0]),
            )
    return {"release_events": len(ids), "wal_records": len(changes)}


def bootstrap():
    """Project existing current heads after creating the slot; no historical replay claim."""
    with pg.connection() as db:
        pg.lock(db, "lakehouse-consumer")
        streams = db.execute(
            "SELECT stream FROM reference_lab.heads ORDER BY stream"
        ).fetchall()
        for (stream,) in streams:
            pg.lock(db, stream)
        pending = db.execute(
            "SELECT data FROM pg_logical_slot_peek_changes(%s,NULL,NULL,'include-xids','0')",
            (SLOT,),
        ).fetchall()
        if any(
            data.startswith("table reference_lab.activations:") for (data,) in pending
        ):
            raise batch.BatchError("BOOTSTRAP_REQUIRES_DRAINED_WAL")
        ids = db.execute(
            "SELECT activation_id FROM reference_lab.heads ORDER BY stream"
        ).fetchall()
        for (activation_id,) in ids:
            project(db, catalog(), activation_id)
    return len(ids)


def verify_trino():
    """Read receipt-selected snapshots, compare complete keys and content with PG."""
    from trino.dbapi import connect

    checked = 0
    with pg.connection() as db:
        pg.lock(db, "lakehouse-consumer")
        releases = db.execute(
            "SELECT h.stream,h.version,r.snapshot_id,r.checksum FROM reference_lab.heads h "
            "LEFT JOIN reference_lab.lake_receipts r USING(activation_id) ORDER BY h.stream"
        ).fetchall()
        cursor = connect(host="127.0.0.1", port=58080, user="local-fixture").cursor()
        for stream, version, snapshot_id, checksum in releases:
            if snapshot_id is None:
                raise batch.BatchError("LAKEHOUSE_RELEASE_PENDING")
            if not re.fullmatch(r"[0-9a-f]{64}", stream):
                raise batch.BatchError("INVALID_RELEASE_STREAM")
            cursor.execute(
                f"SELECT {','.join(FIELDS)} FROM reference.approved.s_{stream} "
                f"FOR VERSION AS OF {int(snapshot_id)} ORDER BY evidence_id"
            )
            actual = [tuple(row) for row in cursor.fetchall()]
            expected = db.execute(
                "SELECT version,evidence_id,source_record_id,cas_number,title,body,document_version "
                "FROM reference_lab.evidence WHERE stream=%s AND version=%s ORDER BY evidence_id",
                (stream, version),
            ).fetchall()
            if (
                sorted(actual, key=lambda row: row[1])
                != sorted(expected, key=lambda row: row[1])
                or fingerprint(actual) != checksum
            ):
                raise batch.BatchError("TRINO_RELEASE_CONTENT_MISMATCH")
            checked += 1
    return {"checked_heads": checked, "status": "MATCHED"}


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["init", "bootstrap", "consume", "verify", "status"]
    )
    args = parser.parse_args()
    if args.command == "init":
        initialize()
        result = {"initialized": True}
    elif args.command == "bootstrap":
        result = {"bootstrapped_heads": bootstrap()}
    elif args.command == "consume":
        result = consume()
    elif args.command == "verify":
        result = verify_trino()
    else:
        with pg.connection() as db:
            result = {
                "slot": db.execute(
                    "SELECT slot_name,confirmed_flush_lsn::text,pg_wal_lsn_diff(pg_current_wal_lsn(),confirmed_flush_lsn)::bigint "
                    "FROM pg_replication_slots WHERE slot_name=%s",
                    (SLOT,),
                ).fetchall(),
                "verified_releases": db.execute(
                    "SELECT count(*) FROM reference_lab.lake_receipts"
                ).fetchone()[0],
            }
    print(json.dumps(result))


if __name__ == "__main__":
    main()
