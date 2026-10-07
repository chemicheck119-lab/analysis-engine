"""Loopback local BFF adapter for approved reference data, never accident advice."""

import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
import psycopg
from chemiguard119 import reference_batch as batch, reference_postgres as pg


def require_local_dsn():
    uri = urlsplit(os.getenv("CHEMICHECK_REFERENCE_PG_DSN", ""))
    if uri.hostname != "127.0.0.1" or uri.port != 55433 or uri.path != "/reference_lab":
        raise batch.BatchError("LOCAL_REFERENCE_DSN_REQUIRED")


def create_app(directory):
    directory = Path(directory)
    info = batch.read(directory / "source_service.json")
    root = directory / "batch"
    app = FastAPI(title="Chemicheck119 local approved source adapter")

    @app.get("/reference/evidence")
    def evidence(cas: str = "67-56-1"):
        try:
            with pg.connection() as db:
                pg.lock(db, pg.stream_id(root))
                selected = pg.head(db, pg.stream_id(root))
                rows = (
                    pg.dataset(db, pg.stream_id(root), cas)
                    if selected and cas == info["cas"]
                    else []
                )
            if selected:
                if selected["version"] != info["version"]:
                    raise batch.BatchError("SOURCE_PROVENANCE_VERSION_MISMATCH")
                target = batch.verify_bundle(root, selected["version"])
                approval = batch.read(
                    root / "approvals" / (selected["version"] + ".json")
                )
                if approval.get("bundle_sha256") != batch.sha256_file(
                    target / "bundle.json"
                ):
                    raise batch.BatchError("SOURCE_APPROVAL_HASH_MISMATCH")
            return {
                "scope": info["scope"],
                "status": "PENDING_HUMAN_REVIEW"
                if selected is None
                else "AVAILABLE"
                if rows
                else "NO_EVIDENCE",
                "source": info["source"],
                "collected_at": info["original_collected_at"],
                "new_external_collection_verified": False,
                "version": selected["version"] if selected else None,
                "candidate_counts": info["counts"],
                "exclusion_reasons": info["exclusion_reasons"],
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
        except (batch.BatchError, OSError, KeyError, psycopg.Error):
            raise HTTPException(503, "APPROVED_REFERENCE_UNAVAILABLE") from None

    return app


def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, default=8004)
    args = parser.parse_args()
    require_local_dsn()
    uvicorn.run(
        create_app(args.directory), host="127.0.0.1", port=args.port, access_log=False
    )


if __name__ == "__main__":
    main()
