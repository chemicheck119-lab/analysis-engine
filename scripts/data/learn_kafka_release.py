"""Explicit local synthetic learning input; stage first, approve separately."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from chemiguard119 import reference_batch as batch  # noqa: E402
from chemiguard119 import reference_postgres as pg  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "inspect", "publish"])
    parser.add_argument("--directory", required=True)
    parser.add_argument("--reviewer")
    parser.add_argument("--note")
    args = parser.parse_args()
    directory = Path(args.directory).resolve()
    root = directory / "batch"
    if args.command == "prepare":
        import pytest
        from test_reference_batch import setup
        from test_reference_postgres import staged

        if directory.exists():
            raise RuntimeError("FRESH_SYNTHETIC_DIRECTORY_REQUIRED")
        directory.mkdir(parents=True)
        with pytest.MonkeyPatch.context() as patch:
            root, spec, _, _ = setup.__wrapped__(directory, patch)
            spec["target_date"] = "2026-10-07"
            pipeline = staged(root, spec, "human-kafka-001")
            batch.atomic_json(
                directory / "learning.json",
                {"version": pipeline.state()["version"], "scope": "SYNTHETIC_ONLY"},
            )
    info = batch.read(directory / "learning.json")
    pipeline = pg.PostgresPipeline(root, "human-kafka-001")
    if args.command == "publish":
        if not args.reviewer or not args.note:
            raise RuntimeError("REVIEWER_AND_EXPLICIT_FIXTURE_REVIEW_NOTE_REQUIRED")
        batch.approve(root, info["version"], args.reviewer, args.note, True, True)
        pipeline.execute("activate")
        pipeline.verify_usage()
        pipeline.finish()
    with pg.connection() as db:
        report = pg.checks(db, pg.stream_id(root), info["version"])
        selected = pg.head(db, pg.stream_id(root))
    print(
        json.dumps(
            {
                "scope": "SYNTHETIC_ONLY_NOT_CHEMICAL_APPROVAL",
                "stream": pg.stream_id(root),
                "version": info["version"],
                "checks": report,
                "selected": selected,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
