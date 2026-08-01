#!/usr/bin/env python3
"""소방 사고–CAS 공개 기록으로 후보 Resolver를 파인튜닝하고 평가한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.incident_adaptation import run_training_and_evaluation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--incidents", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = run_training_and_evaluation(
        args.base_model,
        args.incidents,
        args.output_dir,
        args.report,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
