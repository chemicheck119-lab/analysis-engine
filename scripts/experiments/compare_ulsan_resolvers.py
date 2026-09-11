#!/usr/bin/env python3
"""사전 고정 공식 입력으로 Sparse/Dense/RRF를 비교하고 집계만 출력한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.ulsan_resolver_comparison import run_comparison


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in (
        "source-dir",
        "model",
        "embedding-model",
        "regression",
        "safety",
        "private-output-dir",
    ):
        parser.add_argument(f"--{field}", type=Path, required=True)
    args = parser.parse_args()
    report = run_comparison(
        args.source_dir,
        args.model,
        args.embedding_model,
        args.regression,
        args.safety,
        args.private_output_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
