#!/usr/bin/env python3
"""공식근거 registry의 URL·문서 위치 drift 보고서를 생성한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.reference_drift import check_reference_sources


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "공식근거 URL의 도달성, 최종 host, MIME과 HTML locator probe를 검사합니다. "
            "온라인 API 요청 경로에서는 실행하지 않습니다."
        )
    )
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    report = check_reference_sources(
        args.config_dir,
        timeout_seconds=args.timeout,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"공식근거 {report['summary']['source_count']}건 검사: "
        f"{report['status']} (실패 {report['summary']['failed_source_count']}건, "
        f"PDF locator 메타데이터 한정 {report['summary']['locator_metadata_only_count']}건)"
    )
    print(f"결과: {args.output}")
    if report["status"] == "FAIL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
