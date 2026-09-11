#!/usr/bin/env python3
"""공식 ZIP과 고정 Resolver를 검증하고 원문 없는 재평가 집계만 출력한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.incident_replay import replay_official_source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--reference-report", type=Path, required=True)
    parser.add_argument("--private-output-dir", type=Path, required=True)
    parser.add_argument("--expected-archive-sha256", required=True)
    parser.add_argument("--expected-model-sha256", required=True)
    args = parser.parse_args()
    result = replay_official_source(
        args.archive,
        args.model,
        args.reference_report,
        args.private_output_dir,
        expected_archive_sha256=args.expected_archive_sha256,
        expected_model_sha256=args.expected_model_sha256,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
