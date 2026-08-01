#!/usr/bin/env python3
"""공개 물성 프로필의 자기검색 일관성으로 후보 ranker를 회귀 감사한다.

프로필 값에서 만든 질의로 같은 프로필 인덱스를 조회하므로 이 결과는 독립 현장
정확도가 아니다. 전처리·검색·재순위화 사이의 일관성과 회귀 여부만 측정한다.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chemiguard119.discovery import _matched_properties, _property_candidates
from chemiguard119.material_ranker import (
    MATERIAL_RANKER_VERSION,
    next_best_checks,
    rank_material_candidates,
)


FIELDS = ("physical_state", "color", "odor", "use_description")


def _short_value(value: object) -> str:
    text = str(value or "").strip()
    return text.split("|")[0].strip()[:120]


def _load_profiles(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT cas_number, canonical_name_ko, canonical_name_en,
                       physical_state, color, odor, use_description,
                       source_url, document_version
                FROM substance_profile
                ORDER BY cas_number
                """
            )
        ]


def _query_for_profile(profile: dict[str, Any]) -> str | None:
    values = [
        _short_value(profile.get(field))
        for field in FIELDS
        if _short_value(profile.get(field))
    ]
    if len(values) < 2:
        return None
    return " ".join(values[:3])


def _reciprocal_rank(expected: str, ranked: list[str]) -> float:
    try:
        return 1.0 / (ranked.index(expected) + 1)
    except ValueError:
        return 0.0


def _metrics(rows: list[dict[str, Any]], key: str) -> dict[str, float | int]:
    rankings = [list(row[key]) for row in rows]
    expected = [str(row["expected_cas"]) for row in rows]
    count = len(rows)
    return {
        "case_count": count,
        "top1_accuracy": round(
            sum(
                bool(items) and items[0] == target
                for items, target in zip(rankings, expected)
            )
            / count,
            6,
        ),
        "top3_recall": round(
            sum(target in items[:3] for items, target in zip(rankings, expected))
            / count,
            6,
        ),
        "mrr": round(
            sum(
                _reciprocal_rank(target, items)
                for items, target in zip(rankings, expected)
            )
            / count,
            6,
        ),
    }


def evaluate(db_path: Path, *, max_cases: int = 300) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    latencies: list[float] = []
    discriminating_check_count = 0
    for profile in _load_profiles(db_path):
        query = _query_for_profile(profile)
        if not query:
            continue
        started = time.perf_counter()
        property_rows, available = _property_candidates(query, db_path, limit=5)
        if not available or not property_rows:
            continue
        candidates = [
            {
                "rank": index,
                "cas_number": str(row["cas_number"]),
                "display_name": str(
                    row.get("canonical_name_ko")
                    or row.get("canonical_name_en")
                    or row["cas_number"]
                ),
                "matched_properties": _matched_properties(query, row),
                "property_profile": row,
                "evidence": [],
            }
            for index, row in enumerate(property_rows, 1)
        ]
        ranked = rank_material_candidates(
            candidates,
            direct_by_cas={},
            resolution_status="UNRESOLVED",
        )
        checks = next_best_checks(ranked)
        if any(item.get("field") for item in checks):
            discriminating_check_count += 1
        latencies.append((time.perf_counter() - started) * 1_000)
        cases.append(
            {
                "expected_cas": str(profile["cas_number"]),
                "query": query,
                "baseline_ranking": [str(row["cas_number"]) for row in property_rows],
                "ranker_ranking": [str(row["cas_number"]) for row in ranked],
            }
        )
        if len(cases) >= max_cases:
            break

    if not cases:
        raise RuntimeError("평가 가능한 공개 물성 프로필 자기검색 사례가 없습니다.")
    ordered_latencies = sorted(latencies)
    p95_index = min(len(ordered_latencies) - 1, int(len(ordered_latencies) * 0.95))
    baseline = _metrics(cases, "baseline_ranking")
    ranker = _metrics(cases, "ranker_ranking")
    return {
        "metrics_version": "material-ranker-self-retrieval-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_version": MATERIAL_RANKER_VERSION,
        "evaluation_scope": "PROFILE_SELF_RETRIEVAL_REGRESSION_ONLY",
        "is_independent_field_accuracy": False,
        "data_source": "substance_profile derived from NFA Ulsan chemical information",
        "case_generation": "up to three non-empty property fields from each distinct CAS profile",
        "case_count": len(cases),
        "baseline": baseline,
        "ranker": ranker,
        "top1_delta": round(
            float(ranker["top1_accuracy"]) - float(baseline["top1_accuracy"]), 6
        ),
        "top3_delta": round(
            float(ranker["top3_recall"]) - float(baseline["top3_recall"]), 6
        ),
        "discriminating_check_coverage": round(
            discriminating_check_count / len(cases), 6
        ),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 6),
            "p95": round(ordered_latencies[p95_index], 6),
            "max": round(max(latencies), 6),
        },
        "safety": {
            "candidate_score_is_probability": False,
            "auto_confirmation_allowed": False,
            "rule_execution_allowed": False,
        },
        "limitations": [
            "질의가 동일 프로필의 필드에서 생성되어 독립 보류셋이 아닙니다.",
            "현장 표현·제품명·오인 신고에 대한 정확도를 의미하지 않습니다.",
            "지도학습 모델 채택은 검수된 현장 질의-CAS 라벨 확보 후 재평가합니다.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--max-cases", type=int, default=300)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate(args.db, max_cases=args.max_cases)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
