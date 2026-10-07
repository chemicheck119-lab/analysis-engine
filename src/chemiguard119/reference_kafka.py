"""Local Debezium release consumer; payloads never emitted to logs."""

import json
import re

from chemiguard119 import reference_batch as batch
from chemiguard119 import reference_lakehouse as lake
from chemiguard119 import reference_postgres as pg

TOPIC = "chemicheck.reference_lab.activations"
GROUP = "chemicheck-iceberg-local"


def process(value, fault=None):
    if isinstance(value, dict) and "schema" in value and "payload" in value:
        value = value["payload"]
    if not isinstance(value, dict) or value.get("op") not in {"c", "r"}:
        raise batch.BatchError("KAFKA_UNSUPPORTED_RELEASE_EVENT")
    source = value.get("source") or {}
    if source.get("schema") != "reference_lab" or source.get("table") != "activations":
        raise batch.BatchError("KAFKA_UNEXPECTED_SOURCE")
    after = value.get("after") or {}
    activation_id = after.get("activation_id", "")
    if not isinstance(activation_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", activation_id
    ):
        raise batch.BatchError("KAFKA_INVALID_ACTIVATION")
    with pg.connection() as db:
        pg.lock(db, "lakehouse-consumer")
        event = db.execute(
            "SELECT stream,version FROM reference_lab.activations WHERE activation_id=%s",
            (activation_id,),
        ).fetchone()
        if not event or tuple(event) != (after.get("stream"), after.get("version")):
            raise batch.BatchError("KAFKA_RELEASE_DB_MISMATCH")
        pg.lock(db, event[0])
        head = pg.head(db, event[0])
        # Initial snapshots and delayed events must not replace a newer selected head.
        if not head or head["activation_id"] != activation_id:
            return {"status": "HISTORICAL_EVENT_SKIPPED"}
        namespace = lake.active_namespace(db)
        existed = lake.receipt(db, activation_id, namespace) is not None
        lake.project(db, lake.catalog(), activation_id, fault=fault)
    return {"status": "ALREADY_PROJECTED" if existed else "PROJECTED"}


def consume(group=GROUP, max_records=1000, fault=None):
    from kafka import KafkaConsumer
    from kafka.structs import OffsetAndMetadata, TopicPartition

    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers="127.0.0.1:59092",
        group_id=group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        consumer_timeout_ms=8000,
        max_poll_records=1,
        value_deserializer=lambda data: json.loads(data),
    )
    counts = {"records": 0, "projected": 0, "duplicates": 0, "historical": 0}
    try:
        for message in consumer:
            result = process(message.value, fault=fault)
            if fault == "before_offset_commit":
                raise batch.BatchError("INJECTED_BEFORE_KAFKA_OFFSET_COMMIT")
            consumer.commit(
                {
                    TopicPartition(message.topic, message.partition): OffsetAndMetadata(
                        message.offset + 1, "", -1
                    )
                }
            )
            counts["records"] += 1
            key = {
                "PROJECTED": "projected",
                "ALREADY_PROJECTED": "duplicates",
                "HISTORICAL_EVENT_SKIPPED": "historical",
            }[result["status"]]
            counts[key] += 1
            if counts["records"] >= max_records:
                break
    finally:
        consumer.close()
    return counts


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["consume"])
    parser.add_argument("--group", default=GROUP)
    args = parser.parse_args()
    print(json.dumps(consume(group=args.group)))


if __name__ == "__main__":
    main()
