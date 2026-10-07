"""Bounded local KOSHA XML collection check; never approves or activates data."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from chemiguard119.kosha_client import (
    KOSHA_API_BASE_URL,
    KOSHA_SOURCE_PAGE,
    KoshaApiError,
    KoshaMsdsClient,
    KOSHA_STAGING_COLUMNS,
)
from chemiguard119.reference_batch import Pipeline, atomic_json, digest, now, write_csv
from chemiguard119.utils import sha256_file, valid_cas_checksum


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cas", default="67-56-1")
    args = parser.parse_args()
    if not valid_cas_checksum(args.cas):
        parser.error("invalid CAS")
    if args.output.exists():
        parser.error("use a fresh private local output directory")
    key = os.getenv("KOSHA_API_SERVICE_KEY") or getpass.getpass("KOSHA key (hidden): ")
    args.output.mkdir(parents=True, mode=0o700)
    response_dir = args.output / "responses"
    response_dir.mkdir(mode=0o700)
    calls = []

    def fetch(url, timeout):
        payload = KoshaMsdsClient._default_fetch_xml(url, timeout)
        if key.encode() in payload or urllib.parse.quote(key).encode() in payload:
            raise KoshaApiError("SECRET_ECHO_IN_RESPONSE", "response not retained")
        endpoint = Path(urllib.parse.urlsplit(url).path).name
        path = response_dir / f"{len(calls):03d}-{endpoint}.xml"
        path.write_bytes(payload)
        path.chmod(0o600)
        try:
            root = ET.fromstring(payload)
            tags = sorted({node.tag.rsplit("}", 1)[-1] for node in root.iter()})
        except ET.ParseError:
            tags = []
        calls.append(
            {
                "endpoint": endpoint,
                "bytes": len(payload),
                "sha256": sha256_file(path),
                "xml_tags": tags,
            }
        )
        return payload

    client = KoshaMsdsClient(
        key,
        fetch_xml=fetch,
        timeout_seconds=20,
        request_interval_seconds=1,
        max_retries=1,
    )
    report = {
        "scope": "LIVE_COLLECTION_LOCAL_STAGING_ONLY",
        "source": KOSHA_SOURCE_PAGE,
        "endpoint": KOSHA_API_BASE_URL,
        "cas_number": args.cas,
        "started_at": now(),
        "human_reviewed": False,
        "activated": False,
    }
    started = time.monotonic()
    try:
        result = client.collect_cas(args.cas)
        rows = result.pop("records")
        csv_path = args.output / "incoming.csv"
        write_csv(csv_path, KOSHA_STAGING_COLUMNS, rows)
        csv_path.chmod(0o600)
        report.update(
            {
                "result": result,
                "input_count": len(rows),
                "source_sha256": sha256_file(csv_path),
            }
        )
        if result["status"] == "COLLECTED":
            # Reuse exact batch CAS/field/duplicate normalization, without baseline
            # fabrication or running stage/activation on unreviewed real documents.
            p = Pipeline(args.output / "quality", "live-kosha-" + digest(args.cas))
            p.work.mkdir(parents=True)
            state = {"spec": {"fixture_csv": str(csv_path)}}
            import shutil

            shutil.copyfile(csv_path, p.work / "incoming.csv")
            state["raw_sha256"] = sha256_file(p.work / "incoming.csv")
            p._normalize(state)
            report.update(
                {
                    "counts": state["counts"],
                    "exclusion_reasons": state["exclusion_reasons"],
                    "normalization_verified": True,
                    "quality_summary": state["quality_summary"],
                    "default_quality_gate": {
                        "max_error_fraction": 0.0,
                        "passed": state["quality_summary"]["error_fraction"] == 0.0,
                        "expected_exclusions_review_required": state["quality_summary"][
                            "expected_fraction"
                        ]
                        > 0.25,
                    },
                }
            )
    except KoshaApiError as error:
        report.update({"error_code": error.code, "retryable": error.retryable})
    except Exception as error:
        report["error_code"] = type(error).__name__
    report.update(
        {
            "finished_at": now(),
            "seconds": round(time.monotonic() - started, 3),
            "request_count": client.request_count,
            "responses": calls,
        }
    )
    atomic_json(args.output / "verification.json", report)
    # Aggregate only; never print URL query, key, response text, document bodies.
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "responses"}, ensure_ascii=False
        )
    )
    return 0 if report.get("result", {}).get("status") == "COLLECTED" else 2


if __name__ == "__main__":
    sys.exit(main())
