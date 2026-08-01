#!/usr/bin/env python3
"""공개 검증 CAMEO 물질의 모든 고유 쌍을 오프라인 회귀 평가한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.pair_evaluation import evaluate_verified_pairs


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "PUBLIC_SOURCE_VERIFIED CAS의 모든 고유 조합을 CAMEO Rule Engine으로 "
            "검사합니다. 결과는 현장 존재 확인이나 사고 확률이 아닙니다."
        )
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report = evaluate_verified_pairs(args.db, args.config_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"공개 검증 {report['crosswalk']['public_verified_substance_count']}종의 "
        f"고유 조합 {report['evaluated_pair_count']}개를 평가했습니다."
    )
    print(f"상태 분포: {report['status_counts']}")
    print(f"서수 등급 분포: {report['risk_level_counts']}")
    print(f"공식근거 보증 분포: {report['reference_assurance_status_counts']}")
    print(f"결과: {args.output}")


if __name__ == "__main__":
    main()
