"""Loopback local BFF adapter for approved reference data, never accident advice."""

import os
from pathlib import Path
from urllib.parse import urlsplit
import re

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
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

    async def unavailable(_request, _exception):
        return JSONResponse(
            status_code=503, content={"detail": "APPROVED_REFERENCE_UNAVAILABLE"}
        )

    for exception_type in (batch.BatchError, OSError, KeyError, psycopg.Error):
        app.add_exception_handler(exception_type, unavailable)

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

    @app.get("/reference/guidance")
    def guidance(query: str):
        """Select original MSDS sections; never infer a safe operation or mixture."""
        if not query.strip() or len(query) > 200:
            raise HTTPException(422, "INVALID_MATERIAL_QUERY")
        candidate = batch.verify_bundle(root, info["version"])
        records = batch.read(candidate / "normalized.json")
        names = {
            str(row.get("화학물질명_국문", "")).strip()
            for row in records
            if row.get("CAS번호") == info["cas"]
        } - {""}

        def compact(text):
            return re.sub(r"\s+", "", text).casefold()

        matched = compact(query) == compact(info["cas"]) or any(
            compact(query) == compact(name) for name in names
        )
        if not matched:
            return {
                "status": "NO_EVIDENCE",
                "cas": None,
                "name": None,
                "sections": [],
                "version": None,
                "collected_at": None,
                "source_url": info["source"],
                "document_versions": [],
            }
        result = evidence(info["cas"])
        labels = [
            (4, "응급조치"),
            (5, "화재 시 참고사항"),
            (6, "누출 시 참고사항"),
            (8, "노출방지·보호구"),
            (10, "안정성·반응성"),
        ]
        sections = []
        if result["status"] == "AVAILABLE":
            by_id = {r["레코드ID"]: r for r in records}
            for number, label in labels:
                items = []
                for row in result["rows"]:
                    original = by_id.get(row["source_record_id"], {})
                    if original.get("MSDS_장번호") == str(number):
                        items.append(
                            {
                                "evidence_id": row["evidence_id"],
                                "title": original.get("MSDS_항목명_국문", row["title"]),
                                "text": row["body"],
                            }
                        )
                sections.append(
                    {
                        "number": number,
                        "label": label,
                        "items": items,
                        "status": "AVAILABLE" if items else "NO_INFORMATION",
                    }
                )
        return {
            "status": result["status"],
            "cas": info["cas"],
            "name": sorted(names)[0] if names else info["cas"],
            "sections": sections,
            "version": result["version"],
            "collected_at": result["collected_at"],
            "source_url": info["source"],
            "document_versions": sorted(
                {
                    r.get("최종개정일", "")
                    for r in records
                    if r.get("CAS번호") == info["cas"]
                }
                - {""}
            ),
        }

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
