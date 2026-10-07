"""Explicit human review and local activation of the prepared source candidate."""

import argparse
from pathlib import Path

from chemiguard119 import reference_batch as batch, reference_postgres as pg
from chemiguard119.reference_service import require_local_dsn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--note", required=True)
    parser.add_argument(
        "--accept-expected-exclusions", action="store_true", required=True
    )
    args = parser.parse_args()
    require_local_dsn()
    info = batch.read(args.directory / "source_service.json")
    root = args.directory / "batch"
    pipeline = pg.PostgresPipeline(root, info["run_id"])
    if pipeline.state()["version"] != info["version"]:
        raise batch.BatchError("SOURCE_CANDIDATE_VERSION_MISMATCH")
    batch.approve(root, info["version"], args.reviewer, args.note, True, True)
    pipeline.execute("activate")
    pipeline.verify_usage()
    print(pipeline.finish())


if __name__ == "__main__":
    main()
