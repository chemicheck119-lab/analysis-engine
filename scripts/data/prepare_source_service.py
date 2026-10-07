"""Archived actual XML replay with explicit synthetic baseline; no approval."""

# ruff: noqa: E402
import argparse
import json
import shutil
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import pytest
from chemiguard119 import reference_batch as batch, reference_postgres as pg
from chemiguard119.kosha_client import KoshaMsdsClient, KOSHA_STAGING_COLUMNS
from chemiguard119.utils import sha256_file
from chemiguard119.reference_service import require_local_dsn
from test_reference_batch import setup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_local_dsn()
    if args.output.exists():
        raise RuntimeError("FRESH_PRIVATE_OUTPUT_REQUIRED")
    prior = batch.read(args.archive / "verification.json")
    originals = prior["responses"]
    files = sorted((args.archive / "responses").glob("*.xml"))
    if len(files) != len(originals):
        raise RuntimeError("ARCHIVE_RESPONSE_COUNT_MISMATCH")
    for path, metadata in zip(files, originals):
        if sha256_file(path) != metadata["sha256"]:
            raise RuntimeError("ARCHIVE_CHECKSUM_MISMATCH")
    index = 0

    def fetch(url, timeout):
        nonlocal index
        endpoint = Path(urllib.parse.urlsplit(url).path).name
        if index >= len(files) or endpoint != originals[index]["endpoint"]:
            raise RuntimeError("ARCHIVE_REQUEST_SEQUENCE_MISMATCH")
        value = files[index].read_bytes()
        index += 1
        return value

    client = KoshaMsdsClient(
        "archived-replay-placeholder", fetch_xml=fetch, request_interval_seconds=0
    )
    collected = client.collect_cas(prior["cas_number"])
    if collected["status"] != "COLLECTED" or index != len(files):
        raise RuntimeError("ARCHIVE_REPLAY_INCOMPLETE")
    args.output.mkdir(parents=True, mode=0o700)
    with pytest.MonkeyPatch.context() as patch:
        root, spec, incoming, _ = setup.__wrapped__(args.output, patch)
        batch.write_csv(incoming, KOSHA_STAGING_COLUMNS, collected["records"])
        spec.update(cas_numbers=[prior["cas_number"]], max_error_fraction=0.0)
        pipeline = pg.PostgresPipeline(root, "archived-source-001")
        pipeline.initialize(spec)
        for step in batch.STEPS[:5]:
            pipeline.execute(step)
        state = pipeline.state()
    provenance = {
        "scope": "LOCAL_ARCHIVED_REAL_KOSHA_WITH_SYNTHETIC_BASELINE",
        "source": prior["source"],
        "original_collected_at": prior["started_at"],
        "replayed_at": batch.now(),
        "new_external_collection_verified": False,
        "raw_responses": originals,
        "cas": prior["cas_number"],
        "stream": pg.stream_id(root),
        "root": str(root),
        "version": state["version"],
        "run_id": pipeline.run_id,
        "counts": state["counts"],
        "exclusion_reasons": state["exclusion_reasons"],
        "quality_summary": state["quality_summary"],
        "human_review_required": True,
        "service_activated": False,
    }
    shutil.copytree(args.archive / "responses", args.output / "responses")
    batch.atomic_json(args.output / "source_service.json", provenance)
    print(
        json.dumps(
            {k: v for k, v in provenance.items() if k not in {"raw_responses", "root"}},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
