"""Local PostgreSQL extension; existing incident API and file head are untouched."""

import argparse
import json
import os
import sqlite3
import uuid
import time
from pathlib import Path
from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb
from fastapi import FastAPI, HTTPException

from chemiguard119 import reference_batch as batch
from chemiguard119.utils import sha256_file

SQL = Path(__file__).with_name("sql")


@contextmanager
def connection():
    # Credentials stay in the environment; never report driver exception text.
    try:
        with psycopg.connect(os.environ["CHEMICHECK_REFERENCE_PG_DSN"]) as db:
            db.execute("SET LOCAL statement_timeout = '30s'")
            yield db
    except batch.BatchError:
        raise
    except (psycopg.Error, KeyError):
        raise batch.BatchError("POSTGRES_OPERATION_FAILED") from None


def migrate():
    with connection() as db:
        db.execute(SQL.joinpath("reference_schema.sql").read_text())


def stream_id(root):
    return batch.digest(str(Path(root).resolve()))


def head(db, stream):
    row = db.execute(
        "SELECT version,activation_id FROM reference_lab.heads WHERE stream=%s",
        (stream,),
    ).fetchone()
    return dict(zip(("version", "activation_id"), row)) if row else None


def lock(db, stream):
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (stream,))


def checks(db, stream, version):
    report = {}
    for part in SQL.joinpath("reference_checks.sql").read_text().split("-- name: ")[1:]:
        name, query = part.split("\n", 1)
        report[name] = db.execute(
            query, {"stream": stream, "version": version}
        ).fetchone()[0]
    return report


def validate_report(report, counts):
    defects = (
        "duplicate_keys",
        "required_nulls",
        "missing_accepted",
        "extra_accepted",
        "missing_service_evidence",
    )
    if (
        any(report[k] for k in defects)
        or any(
            report[k + "_count"] != counts[v]
            for k, v in (
                ("input", "input"),
                ("normal", "normal"),
                ("excluded", "excluded"),
                ("stored", "normal"),
            )
        )
        or report["input_count"] != report["normal_count"] + report["excluded_count"]
    ):
        raise batch.BatchError("POSTGRES_RECONCILIATION_FAILED")


def dataset(db, stream, cas=None):
    return db.execute(
        SQL.joinpath("reference_service.sql").read_text(),
        {"stream": stream, "cas": cas},
    ).fetchall()


class PostgresPipeline(batch.Pipeline):
    def initialize(self, spec):
        # A date is an operator label, not a provider-supported historical query.
        period = spec.get("target_date")
        clean = {k: v for k, v in spec.items() if k != "target_date"}
        if period:
            from datetime import date

            try:
                date.fromisoformat(period)
            except (TypeError, ValueError):
                raise batch.BatchError("INVALID_TARGET_DATE") from None
        super().initialize(clean)
        with batch.locked(self.root), connection() as db:
            state = self.state()
            if "postgres_expected_head" not in state:
                state["postgres_expected_head"] = head(db, stream_id(self.root))
                state["expected_current"] = state["postgres_expected_head"]
                state["target_date"] = period
                state["postgres_sql_hashes"] = batch.tree_hashes(SQL)
                batch.atomic_json(self.state_path, state)
            elif state.get("target_date") != period:
                raise batch.BatchError("RUN_ID_SPEC_CONFLICT")

    def _validate(self, state, **kwargs):
        super()._validate(state, **kwargs)
        state["version"] = batch.digest(
            {"bundle": state["version"], "postgres_sql": state["postgres_sql_hashes"]}
        )

    def verify_storage(self, db, state):
        stream, version = stream_id(self.root), state["version"]
        target = batch.verify_bundle(self.root, version)
        saved = db.execute(
            "SELECT bundle_sha256 FROM reference_lab.versions WHERE stream=%s AND version=%s",
            (stream, version),
        ).fetchone()
        if not saved or saved[0] != sha256_file(target / "bundle.json"):
            raise batch.BatchError("POSTGRES_BUNDLE_HASH_MISMATCH")
        expected = {r["레코드ID"]: r for r in batch.read(target / "normalized.json")}
        actual = dict(
            db.execute(
                "SELECT record_key,payload FROM reference_lab.accepted WHERE stream=%s AND version=%s",
                (stream, version),
            ).fetchall()
        )
        exclusions = {
            r["line"]: r["reasons"] for r in batch.read(target / "exclusions.json")
        }
        expected_input = [
            (
                line,
                row.get("레코드ID"),
                "excluded" if line in exclusions else "normal",
                exclusions.get(line, []),
                row,
            )
            for line, row in enumerate(batch.rows_from(target / "incoming.csv"), 2)
        ]
        actual_input = db.execute(
            "SELECT line,record_key,disposition,reasons,raw_row FROM reference_lab.input_lines WHERE stream=%s AND version=%s ORDER BY line",
            (stream, version),
        ).fetchall()
        if expected_input != actual_input:
            raise batch.BatchError("POSTGRES_INPUT_LEDGER_MISMATCH")
        with sqlite3.connect(target / batch.preprocessing.DEFAULT_DB_FILE) as source:
            expected_evidence = set(
                source.execute(
                    "SELECT evidence_id,source_record_id,cas_number,title,body,document_version FROM evidence WHERE source='KOSHA'"
                )
            )
        actual_evidence = set(
            db.execute(
                "SELECT evidence_id,source_record_id,cas_number,title,body,document_version FROM reference_lab.evidence WHERE stream=%s AND version=%s",
                (stream, version),
            ).fetchall()
        )
        if expected != actual or expected_evidence != actual_evidence:
            raise batch.BatchError("POSTGRES_CONTENT_MISMATCH")
        report = checks(db, stream, version)
        validate_report(report, state["counts"])
        if report["indexed_count"] != len(expected_evidence):
            raise batch.BatchError("POSTGRES_INDEX_COUNT_MISMATCH")
        return report

    def _stage(self, state, *, fault, **kwargs):
        if batch.tree_hashes(SQL) != state["postgres_sql_hashes"]:
            raise batch.BatchError("POSTGRES_SQL_CHANGED_RESTART_WITH_NEW_RUN")
        super()._stage(state, fault=fault, **kwargs)
        stream, version = stream_id(self.root), state["version"]
        target = batch.verify_bundle(self.root, version)
        with connection() as db:
            lock(db, stream)
            existing = db.execute(
                "SELECT bundle_sha256 FROM reference_lab.versions WHERE stream=%s AND version=%s",
                (stream, version),
            ).fetchone()
            if existing:
                if existing[0] != state["bundle_sha256"]:
                    raise batch.BatchError("POSTGRES_BUNDLE_HASH_MISMATCH")
            else:
                metadata = {
                    k: state.get(k)
                    for k in (
                        "run_id",
                        "target_date",
                        "counts",
                        "collection",
                        "raw_sha256",
                        "quality_summary",
                    )
                }
                db.execute(
                    "INSERT INTO reference_lab.versions(stream,version,bundle_sha256,metadata) VALUES(%s,%s,%s,%s)",
                    (stream, version, state["bundle_sha256"], Jsonb(metadata)),
                )
                exclusions = {
                    r["line"]: r["reasons"]
                    for r in batch.read(self.work / "exclusions.json")
                }
                with db.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO reference_lab.input_lines VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        [
                            (
                                stream,
                                version,
                                line,
                                row.get("레코드ID"),
                                "excluded" if line in exclusions else "normal",
                                Jsonb(exclusions.get(line, [])),
                                Jsonb(row),
                            )
                            for line, row in enumerate(
                                batch.rows_from(self.work / "incoming.csv"), 2
                            )
                        ],
                    )
                    cur.executemany(
                        "INSERT INTO reference_lab.accepted VALUES(%s,%s,%s,%s,%s)",
                        [
                            (stream, version, r["레코드ID"], r["CAS번호"], Jsonb(r))
                            for r in batch.read(self.work / "normalized.json")
                        ],
                    )
                    with sqlite3.connect(
                        target / batch.preprocessing.DEFAULT_DB_FILE
                    ) as source:
                        rows = source.execute(
                            "SELECT evidence_id,source_record_id,cas_number,title,body,document_version FROM evidence WHERE source='KOSHA'"
                        ).fetchall()
                    cur.executemany(
                        "INSERT INTO reference_lab.evidence(stream,version,evidence_id,source_record_id,cas_number,title,body,document_version) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        [(stream, version, *r) for r in rows],
                    )
            report = self.verify_storage(db, state)
            if report["indexed_count"] != state["counts"]["stored"]:
                raise batch.BatchError("POSTGRES_INDEX_COUNT_MISMATCH")
            if fault == "postgres_stage":
                raise batch.BatchError("INJECTED_POSTGRES_STAGE_FAILURE")
        state["postgres_counts"] = report
        batch.atomic_json(self.work / "sql_checks.json", report)

    def _activate(self, state, *, fault, **_):
        self.check_activation_review(state)
        stream, version = stream_id(self.root), state["version"]
        with connection() as db:
            lock(db, stream)
            active = head(db, stream)
            self.verify_storage(db, state)
            if active and active["version"] == version:
                return
            if active != state["postgres_expected_head"]:
                raise batch.BatchError("CONCURRENT_ACTIVATION_BASELINE_CHANGED")
            if (
                active
                and db.execute(
                    "SELECT count(*) FROM reference_lab.evidence old WHERE old.stream=%s AND old.version=%s "
                    "AND NOT EXISTS(SELECT 1 FROM reference_lab.evidence new WHERE new.stream=old.stream AND new.version=%s "
                    "AND new.evidence_id=old.evidence_id AND new.body=old.body)",
                    (stream, active["version"], version),
                ).fetchone()[0]
            ):
                raise batch.BatchError("BASELINE_WOULD_LOSE_ACTIVE_EVIDENCE")
            activation = uuid.uuid4().hex
            db.execute(
                "INSERT INTO reference_lab.heads VALUES(%s,%s,%s) ON CONFLICT(stream) DO UPDATE SET version=excluded.version,activation_id=excluded.activation_id",
                (stream, version, activation),
            )
            db.execute(
                "INSERT INTO reference_lab.activations(stream,activation_id,version,run_id,operation) VALUES(%s,%s,%s,%s,'activate')",
                (stream, activation, version, self.run_id),
            )
            if fault in {"activate", "postgres_activate"}:
                raise batch.BatchError("INJECTED_POSTGRES_ACTIVATION_FAILURE")

    def verify_usage(self):
        started = (batch.now(), time.monotonic())
        with batch.locked(self.root):
            state = self.state()
            try:
                with connection() as db:
                    lock(db, stream_id(self.root))
                    self.verify_storage(db, state)
                    active = head(db, stream_id(self.root))
                    if not active or active["version"] != state["version"]:
                        raise batch.BatchError("SERVICE_VERSION_MISMATCH")
                    rows = dataset(db, stream_id(self.root))
                    if len(rows) != state["counts"]["stored"]:
                        raise batch.BatchError("SERVICE_COUNT_MISMATCH")
                result = {
                    "version": active["version"],
                    "rows": len(rows),
                    "path": "PostgreSQL service_evidence",
                }
                batch.atomic_json(self.work / "service_check.json", result)
                state["service_verified"] = True
                state.pop("failure", None)
                batch.event(
                    self.root, self.run_id, "verify_usage", "SUCCESS", started, **result
                )
                return result
            except batch.BatchError as error:
                state["failure"] = {
                    "step": "verify_usage",
                    "code": str(error),
                    "at": batch.now(),
                }
                batch.event(
                    self.root,
                    self.run_id,
                    "verify_usage",
                    "FAILED",
                    started,
                    code=str(error),
                )
                raise
            finally:
                batch.atomic_json(self.state_path, state)

    def finish(self):
        if self.state_path.exists():
            with batch.locked(self.root):
                state = self.state()
                if (
                    "activate" in state["done"]
                    and not state.get("service_verified")
                    and not state.get("failure")
                ):
                    state["failure"] = {
                        "step": "verify_usage",
                        "code": "SERVICE_CHECK_NOT_COMPLETED",
                    }
                    batch.atomic_json(self.state_path, state)
        result = super().finish()
        state = self.state() if self.state_path.exists() else {}
        if state.get("failure"):
            result["status"] = "FAILED"
        result["target_date"] = state.get("target_date")
        result["postgres_counts"] = state.get("postgres_counts")
        try:
            with connection() as db:
                result["service_head"] = head(db, stream_id(self.root))
        except batch.BatchError:
            result["service_head"] = None
            result["status"] = "FAILED"
            result["failure"] = {
                "step": "record_result",
                "code": "POSTGRES_STATUS_UNAVAILABLE",
            }
        batch.atomic_json(self.work / "result.json", result)
        return result


def rollback(root, version):
    target = batch.verify_bundle(root, version)
    approval = batch.read(Path(root) / "approvals" / f"{version}.json")
    if approval.get("bundle_sha256") != sha256_file(target / "bundle.json"):
        raise batch.BatchError("ROLLBACK_APPROVAL_INVALID")
    stream = stream_id(root)
    with connection() as db:
        lock(db, stream)
        if not db.execute(
            "SELECT 1 FROM reference_lab.activations WHERE stream=%s AND version=%s AND operation='activate'",
            (stream, version),
        ).fetchone():
            raise batch.BatchError("ROLLBACK_REQUIRES_PREVIOUS_SUCCESS")
        pg = PostgresPipeline(root, "rollback-check")
        manifest = batch.read(target / "batch_manifest.json")
        pg.check_activation_review(manifest)
        pg.verify_storage(db, manifest)
        activation = uuid.uuid4().hex
        db.execute(
            "UPDATE reference_lab.heads SET version=%s,activation_id=%s WHERE stream=%s",
            (version, activation, stream),
        )
        db.execute(
            "INSERT INTO reference_lab.activations(stream,activation_id,version,run_id,operation) VALUES(%s,%s,%s,%s,'rollback')",
            (stream, activation, version, "rollback-" + activation),
        )


def create_app(root):
    app = FastAPI(title="Chemicheck119 local PostgreSQL reference reader")

    @app.get("/reference/evidence")
    def evidence(cas: str | None = None):
        try:
            with connection() as db:
                # One statement joins the head: no mixed-version read across statements.
                rows = dataset(db, stream_id(root), cas)
            if rows:
                batch.verify_bundle(root, rows[0][0])
            return {
                "scope": "LOCAL_DEVELOPMENT_ONLY",
                "rows": [
                    dict(
                        zip(
                            (
                                "version",
                                "evidence_id",
                                "source_record_id",
                                "cas_number",
                                "title",
                                "body",
                            ),
                            row,
                        )
                    )
                    for row in rows
                ],
            }
        except batch.BatchError:
            raise HTTPException(503, "REFERENCE_DATA_UNAVAILABLE") from None

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument(
        "action", choices=["migrate", "run", "status", "rollback", "serve", "sql"]
    )
    parser.add_argument("--spec")
    parser.add_argument("--run-id")
    parser.add_argument("--version")
    parser.add_argument("--previous")
    parser.add_argument("--until", choices=["stage", "activate"], default="activate")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()
    if args.action == "migrate":
        migrate()
    elif args.action == "run":
        p = PostgresPipeline(args.root, args.run_id)
        try:
            p.initialize(batch.read(args.spec))
            for step in batch.STEPS:
                p.execute(step)
                if step == args.until:
                    break
            if args.until == "activate":
                p.verify_usage()
        finally:
            print(json.dumps(p.finish(), ensure_ascii=False))
    elif args.action == "rollback":
        rollback(args.root, args.version)
    elif args.action == "sql":
        with connection() as db:
            result = checks(db, stream_id(args.root), args.version)
            if args.previous:
                result["version_diff"] = db.execute(
                    SQL.joinpath("reference_diff.sql").read_text(),
                    {
                        "stream": stream_id(args.root),
                        "old": args.previous,
                        "new": args.version,
                    },
                ).fetchall()
            print(json.dumps(result, ensure_ascii=False))
    elif args.action == "status":
        with connection() as db:
            print(json.dumps(head(db, stream_id(args.root))))
    else:
        import uvicorn

        uvicorn.run(create_app(args.root), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
