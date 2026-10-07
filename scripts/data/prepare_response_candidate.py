"""Repair a known legacy source label in a private, unapproved local copy."""

import argparse
import shutil
import sqlite3
from pathlib import Path

import joblib

from chemiguard119.preprocessing import KOSHA_OFFICIAL_SOURCE_URL
from chemiguard119.rag import _valid_official_url
from chemiguard119.release import create_runtime_manifest
from chemiguard119.utils import sha256_file, write_json

ROOT = Path(__file__).resolve().parents[2]
PINNED = {
    "chemiguard119.sqlite": "ed81fe9aac45a38880920d967ce8f3954acabfedf7b2f6ea59464552e7958b91",
    "resolver.joblib": "2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff",
    "retriever.joblib": "2e93e066648890400b26f56b532d6ed608b828203f8668e4af3cf63de1bc544b",
}


def tree_hashes(path):
    return {
        str(p.relative_to(path)): sha256_file(p)
        for p in sorted(path.rglob("*"))
        if p.is_file()
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Fresh private candidate directory required")
    before = tree_hashes(args.runtime)
    for name, checksum in PINNED.items():
        if sha256_file(args.runtime / name) != checksum:
            raise RuntimeError("PINNED_RUNTIME_MISMATCH")
    args.output.mkdir(parents=True)
    for name in PINNED:
        shutil.copyfile(args.runtime / name, args.output / name)
    legacy = "KOSHA MSDS OpenAPI via data.go.kr"
    with sqlite3.connect(args.output / "chemiguard119.sqlite") as connection:
        changed = connection.execute(
            "SELECT evidence_id FROM evidence WHERE source='KOSHA' AND source_url=? ORDER BY evidence_id",
            (legacy,),
        ).fetchall()
        connection.execute(
            "UPDATE evidence SET source_url=? WHERE source='KOSHA' AND source_url=?",
            (KOSHA_OFFICIAL_SOURCE_URL, legacy),
        )
        connection.row_factory = sqlite3.Row
        database_rows = {
            row["evidence_id"]: dict(row)
            for row in connection.execute("SELECT * FROM evidence WHERE source='KOSHA'")
        }
    artifact = joblib.load(args.output / "retriever.joblib")
    changed_ids = {row[0] for row in changed}
    for row in artifact["rows"]:
        if row.get("source") == "KOSHA" and row.get("source_url") == legacy:
            database_row = database_rows.get(row["evidence_id"])
            if not database_row or any(
                row.get(key) != database_row.get(key)
                for key in (
                    "source_record_id",
                    "cas_number",
                    "title",
                    "body",
                    "document_version",
                    "cas_link_status",
                )
            ):
                raise RuntimeError("DB_INDEX_SOURCE_MISMATCH")
            if not _valid_official_url(database_row["source_url"], "KOSHA"):
                raise RuntimeError("DB_SOURCE_URL_UNVERIFIED")
            row["source_url"] = database_row["source_url"]
            changed_ids.add(row["evidence_id"])
    if changed_ids != {
        row["evidence_id"]
        for row in artifact["rows"]
        if row["evidence_id"] in changed_ids
    }:
        raise RuntimeError("DB_INDEX_RECORD_MISMATCH")
    artifact.pop("_runtime_index", None)
    joblib.dump(artifact, args.output / "retriever.joblib", compress=3)
    create_runtime_manifest(
        db_path=args.output / "chemiguard119.sqlite",
        resolver_model_path=args.output / "resolver.joblib",
        retriever_model_path=args.output / "retriever.joblib",
        config_dir=ROOT / "config",
        output_path=args.output / "runtime_manifest.json",
    )
    write_json(
        args.output / "candidate_metadata.json",
        {
            "status": "UNAPPROVED_LOCAL_CANDIDATE",
            "baseline_hashes": PINNED,
            "runtime_hashes": {
                name: sha256_file(args.output / name) for name in PINNED
            },
            "changed_records": len(changed_ids),
            "database_changed_records": len(changed),
            "previous_source_label": legacy,
            "source_url": KOSHA_OFFICIAL_SOURCE_URL,
            "source_url_scope": "SOURCE_SERVICE_PORTAL_NOT_ITEM_DEEP_LINK",
            "publisher_dates_changed": False,
            "raw_source_recollected": False,
            "service_activated": False,
            "script_sha256": sha256_file(Path(__file__)),
        },
    )
    assert before == tree_hashes(args.runtime)
    print(
        f"Prepared unapproved local candidate; {len(changed_ids)} source labels repaired; original unchanged"
    )


if __name__ == "__main__":
    main()
