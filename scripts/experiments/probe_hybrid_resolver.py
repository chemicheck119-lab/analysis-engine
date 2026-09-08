#!/usr/bin/env python3
"""기존 Sparse Resolver와 로컬 Dense 후보를 작은 관측 probe에서 비교한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.hybrid_resolver_probe import (
    evaluate_hybrid_probe,
    make_transformer_cls_encoder,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolver-model", type=Path, required=True)
    parser.add_argument("--temporal-snapshot", type=Path, required=True)
    parser.add_argument("--regression", type=Path, required=True)
    parser.add_argument("--safety-evaluation", type=Path, required=True)
    parser.add_argument("--embedding-model", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-length", type=int, default=32)
    parser.add_argument(
        "--device", choices=("auto", "cpu", "mps", "cuda"), default="auto"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    embed, metadata = make_transformer_cls_encoder(
        args.embedding_model,
        batch_size=args.batch_size,
        max_length=args.max_length,
        device_name=args.device,
    )
    report = evaluate_hybrid_probe(
        resolver_model_path=args.resolver_model,
        temporal_snapshot_path=args.temporal_snapshot,
        regression_path=args.regression,
        safety_evaluation_path=args.safety_evaluation,
        embed=embed,
        embedding_model=metadata,
        report_path=args.report,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
