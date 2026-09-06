#!/usr/bin/env python3
"""울산 사고–CAS 원본을 Resolver 평가용 최소 컬럼으로 축약한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.incident_source_intake import prepare_ulsan_resolver_source


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    result = prepare_ulsan_resolver_source(
        args.source,
        args.output,
        args.manifest,
        overwrite=args.overwrite,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
