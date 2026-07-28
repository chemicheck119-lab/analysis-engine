#!/usr/bin/env python3
"""소방·ICIS·PRTR·KOSHA·CAMEO 신호로 지원 물질 구축 순위를 생성한다."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from chemiguard119.support_priority import build_support_priority


RANKING_METHOD = "LEXICOGRAPHIC_NON_PROBABILITY_V1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "지원 물질의 시연 순위와 공식 데이터 확장 순위를 생성합니다. "
            "출력 순위는 위험 확률이나 현재 재고 확률이 아닙니다."
        )
    )
    parser.add_argument("--facility", type=Path, required=True)
    parser.add_argument("--kosha", type=Path, required=True)
    parser.add_argument("--crosswalk", type=Path, required=True)
    parser.add_argument(
        "--fire-incident",
        type=Path,
        action="append",
        default=[],
        help="CAS번호 열이 있는 소방 사고 CSV. 여러 번 지정할 수 있습니다.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=50)
    return parser


def _csv_value(value: Any) -> Any:
    if isinstance(value, list):
        return "|".join(str(item) for item in value)
    if isinstance(value, bool):
        return "Y" if value else "N"
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summary_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "demo_rank": row["demo_rank"],
        "expansion_rank": row["expansion_rank"],
        "cas_number": row["cas_number"],
        "chemical_names": row["chemical_names"],
        "coverage_tier": row["coverage_tier"],
        "fire_incident_rows": row["fire_incident_rows"],
        "facility_count": row["facility_count"],
        "prtr_material_exact_record_count": row["prtr_material_exact_record_count"],
        "kosha_msds_loaded": row["kosha_msds_loaded"],
        "cameo_public_verified": row["cameo_public_verified"],
        "missing_official_evidence": row["missing_official_evidence"],
    }


def main() -> None:
    args = _parser().parse_args()
    if not 1 <= args.top_k <= 500:
        raise SystemExit("--top-k는 1~500이어야 합니다.")

    diagnostics: dict[str, Any] = {}
    rows = build_support_priority(
        facility_path=args.facility,
        kosha_path=args.kosha,
        crosswalk_path=args.crosswalk,
        fire_incident_paths=args.fire_incident,
        diagnostics=diagnostics,
    )
    if not rows:
        raise SystemExit("유효한 CAS가 있는 입력 행이 없습니다.")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(
            {key: _csv_value(value) for key, value in row.items()} for row in rows
        )

    tier_counts = Counter(row["coverage_tier"] for row in rows)
    top_expansion = [_summary_item(row) for row in rows[: args.top_k]]
    top_demo = [
        _summary_item(row)
        for row in sorted(rows, key=lambda item: item["demo_rank"])[: args.top_k]
    ]
    input_paths = [
        ("facility", args.facility),
        ("kosha", args.kosha),
        ("crosswalk", args.crosswalk),
        *[
            (f"fire_incident_{index}", path)
            for index, path in enumerate(args.fire_incident, start=1)
        ],
    ]
    summary = {
        "schema_version": rows[0]["schema_version"],
        "ranking_method": RANKING_METHOD,
        "input_files": [
            {
                "role": role,
                "file": path.name,
                "sha256": _sha256(path),
            }
            for role, path in input_paths
        ],
        "substance_count": len(rows),
        "coverage_tier_counts": dict(sorted(tier_counts.items())),
        "data_quality": diagnostics,
        "top_k": args.top_k,
        "top_demo_candidates": top_demo,
        "top_expansion_candidates": top_expansion,
        "is_probability": False,
        "interpretation": (
            "공식 데이터 구축 순위이며 위험 확률이나 업체의 현재 재고 확률이 아님"
        ),
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"지원 물질 {len(rows)}종의 우선순위를 생성했습니다.")
    print(f"CSV: {args.output}")
    print(f"요약: {args.summary}")
    print("주의: 순위는 위험 확률이나 현재 재고 확률이 아닙니다.")


if __name__ == "__main__":
    main()
