"""공개 검증 CAMEO 교차표의 모든 물질쌍을 오프라인 회귀 검사한다."""

from __future__ import annotations

import csv
from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

from chemiguard119.rules import (
    PUBLIC_SOURCE_PILOT_POLICY,
    review_pair,
    validate_review_output,
)
from chemiguard119.utils import sha256_file


METRICS_VERSION = "verified-pair-regression-v1"
PUBLIC_VERIFIED = "PUBLIC_SOURCE_VERIFIED"


class PairEvaluationError(ValueError):
    """공개 검증 물질쌍 평가 계약이 깨졌을 때 발생한다."""


def _verified_crosswalk(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise PairEvaluationError(f"CAMEO crosswalk를 찾을 수 없습니다: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    verified = [
        row for row in rows if row.get("verification_status") == PUBLIC_VERIFIED
    ]
    if len(verified) < 2:
        raise PairEvaluationError(
            "공개 검증 물질쌍 평가에는 PUBLIC_SOURCE_VERIFIED CAS가 2개 이상 필요합니다."
        )
    return sorted(verified, key=lambda row: row["cas_number"])


def evaluate_verified_pairs(
    db_path: Path,
    config_dir: Path,
) -> dict[str, Any]:
    """검증된 CAS의 모든 고유 조합을 Rule Engine으로 실행한다.

    이 함수는 현장 존재 확인을 대신하지 않는다. 배포 전에 crosswalk와 CAMEO 원자료가
    실제로 연결되는지 검사하는 오프라인 회귀 평가다.
    """

    crosswalk_path = config_dir / "cameo_crosswalk.csv"
    verified = _verified_crosswalk(crosswalk_path)
    results: list[dict[str, Any]] = []

    for incident, facility in combinations(verified, 2):
        review = review_pair(
            incident["cas_number"],
            facility["cas_number"],
            db_path,
            config_dir=config_dir,
            policy_mode=PUBLIC_SOURCE_PILOT_POLICY,
        )
        validation_errors = validate_review_output(review)
        if validation_errors:
            raise PairEvaluationError(
                "물질쌍 출력 검증 실패: "
                f"{incident['cas_number']} + {facility['cas_number']}: "
                + "; ".join(validation_errors)
            )
        risk_scale = review.get("risk_scale") or {}
        results.append(
            {
                "cas_a": incident["cas_number"],
                "form_a": incident["selected_form"],
                "cas_b": facility["cas_number"],
                "form_b": facility["selected_form"],
                "status": review.get("status"),
                "risk_level": review.get("risk_level"),
                "risk_level_ko": review.get("risk_level_ko"),
                "raw_class_id": risk_scale.get("raw_class_id"),
                "hazard_codes": review.get("hazard_codes") or [],
                "gas_products": review.get("gas_products") or [],
                "screened_group_pair_count": len(
                    review.get("cameo_group_screening") or []
                ),
                "evidence_urls": review.get("evidence_urls") or [],
                "expert_reviewed": review.get("expert_reviewed"),
                "is_probability": risk_scale.get("is_probability"),
            }
        )

    status_counts = Counter(str(row["status"]) for row in results)
    risk_counts = Counter(str(row["risk_level"]) for row in results)
    return {
        "metrics_version": METRICS_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "policy_mode": PUBLIC_SOURCE_PILOT_POLICY,
        "database": {
            "file": db_path.name,
            "sha256": sha256_file(db_path),
        },
        "crosswalk": {
            "file": crosswalk_path.name,
            "sha256": sha256_file(crosswalk_path),
            "public_verified_substance_count": len(verified),
        },
        "expected_unique_pair_count": len(verified) * (len(verified) - 1) // 2,
        "evaluated_pair_count": len(results),
        "status_counts": dict(sorted(status_counts.items())),
        "risk_level_counts": dict(sorted(risk_counts.items())),
        "pairs": results,
        "offline_regression_only": True,
        "does_not_confirm_on_site_presence": True,
        "is_probability": False,
        "interpretation": (
            "공개 검증 CAS와 CAMEO 원자료의 연결 회귀 검사이며 현장 존재 확인, "
            "사고 확률 또는 전문가 승인을 의미하지 않습니다."
        ),
    }


__all__ = [
    "METRICS_VERSION",
    "PairEvaluationError",
    "evaluate_verified_pairs",
]
