"""설명 가능한 물질 후보 재순위화와 다음 현장 확인 정책.

이 모듈은 화학 위험이나 물질 정답 확률을 예측하지 않는다. Resolver와 공개
물성 프로필이 만든 후보를 동일한 특징 공간에서 정렬하고, 후보를 안전하게
구분하기 위한 다음 확인 행동을 선택한다. 지도학습 라벨이 충분하지 않은 현재
단계에서는 버전이 고정된 선형 점수와 정보이득 정책을 사용한다.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


MATERIAL_RANKER_VERSION = "material-evidence-ranker-v1"
CHECK_POLICY_VERSION = "material-next-best-check-v1"
MODEL_TYPE = "EXPLAINABLE_EVIDENCE_WEIGHTED_RANKER"
TRAINING_STATUS = "NOT_SUPERVISED_INSUFFICIENT_REVIEWED_LABELS"

FEATURE_WEIGHTS = {
    "retrieval_prior": 0.35,
    "identity_similarity": 0.27,
    "exact_identity": 0.17,
    "source_authority": 0.08,
    "property_coverage": 0.10,
    "official_evidence_available": 0.03,
}

PROPERTY_LABELS = {
    "physical_state": "상온 상태",
    "color": "색상",
    "odor": "냄새 정보",
    "use_description": "사용 용도",
}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _authority_value(candidate: dict[str, Any] | None) -> float:
    if not candidate:
        return 0.0
    return {
        "PUBLIC_AUTHORITY_SOURCE": 1.0,
        "PROJECT_VERIFIED": 0.85,
        "PUBLIC_CATALOG_CANDIDATE": 0.70,
        "PROJECT_CONFIG_CANDIDATE": 0.35,
        "UNVERIFIED": 0.0,
    }.get(str(candidate.get("authority_level") or ""), 0.0)


def _identity_score(candidate: dict[str, Any] | None) -> float:
    if not candidate:
        return 0.0
    raw_score = candidate.get("score")
    if raw_score is None:
        # 기존 테스트 fixture와 구버전 artifact의 정확 검색 후보에는 score가 없을
        # 수 있다. discovery가 허용한 정확 상태에서만 이 fallback을 사용한다.
        return 1.0
    try:
        return _clamp(float(raw_score))
    except (TypeError, ValueError):
        return 0.0


def _exact_identity_value(
    candidate: dict[str, Any] | None,
    resolution_status: str,
) -> float:
    if not candidate:
        return 0.0
    match_type = str(candidate.get("match_type") or "")
    if match_type in {
        "CAS_EXACT",
        "UNIQUE_ALIAS_EXACT",
        "AMBIGUOUS_ALIAS_EXACT",
    }:
        return 1.0
    if resolution_status in {
        "EXACT_IDENTIFIER_MATCH",
        "EXACT_ALIAS_CANDIDATE",
        "AMBIGUOUS_ALIAS",
    }:
        return 1.0
    return 0.0


def _feature_evidence(
    feature_name: str,
    *,
    candidate: dict[str, Any],
    direct_candidate: dict[str, Any] | None,
) -> str:
    if feature_name == "identity_similarity":
        if direct_candidate:
            return "물질명·CAS 식별 후보의 문자열 일치도를 사용했습니다."
        return "이름 식별 근거가 없어 이 특징은 반영하지 않았습니다."
    if feature_name == "retrieval_prior":
        return "기존 정확검색·FTS5 BM25 순위를 강한 사전값으로 보존했습니다."
    if feature_name == "exact_identity":
        if direct_candidate:
            return "정확한 CAS 또는 독립된 별칭 일치 여부를 반영했습니다."
        return "정확 식별 표현이 없어 이 특징은 반영하지 않았습니다."
    if feature_name == "source_authority":
        return "별칭을 제공한 공개기관·검증 상태를 단계형 값으로 반영했습니다."
    if feature_name == "property_coverage":
        count = len({item.get("field") for item in candidate["matched_properties"]})
        return f"신고·관찰 표현과 일치한 공개 물성 필드 {count}개를 반영했습니다."
    return "동일 CAS의 KOSHA·CAMEO 근거 카드 적재 여부만 반영했습니다."


def rank_material_candidates(
    candidates: list[dict[str, Any]],
    *,
    direct_by_cas: dict[str, dict[str, Any]],
    resolution_status: str,
) -> list[dict[str, Any]]:
    """후보를 설명 가능한 선형 점수로 재정렬한다.

    점수는 후보 순위용이며 정답 확률·위험 확률·현장 확인을 뜻하지 않는다.
    기존 검색 순서는 마지막 tie-break로 유지해 fallback이 결정적이게 한다.
    """

    ranked: list[tuple[float, int, str, dict[str, Any]]] = []
    for original_rank, candidate in enumerate(candidates, 1):
        cas_number = str(candidate.get("cas_number") or "")
        direct = direct_by_cas.get(cas_number)
        values = {
            "retrieval_prior": 1.0 / original_rank,
            "identity_similarity": _identity_score(direct),
            "exact_identity": _exact_identity_value(direct, resolution_status),
            "source_authority": _authority_value(direct),
            "property_coverage": _clamp(
                len(
                    {
                        item.get("field")
                        for item in candidate.get("matched_properties", [])
                        if item.get("field")
                    }
                )
                / 4.0
            ),
            "official_evidence_available": (1.0 if candidate.get("evidence") else 0.0),
        }
        features = []
        for name, weight in FEATURE_WEIGHTS.items():
            value = _clamp(values[name])
            features.append(
                {
                    "name": name,
                    "value": round(value, 6),
                    "weight": weight,
                    "contribution": round(value * weight, 6),
                    "evidence": _feature_evidence(
                        name,
                        candidate=candidate,
                        direct_candidate=direct,
                    ),
                }
            )
        ranking_score = round(sum(item["contribution"] for item in features), 6)
        enriched = {
            **candidate,
            "ranking_score": ranking_score,
            "ranking_score_is_probability": False,
            "ranking_features": features,
        }
        ranked.append((ranking_score, original_rank, cas_number, enriched))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [{**item[3], "rank": rank} for rank, item in enumerate(ranked, 1)]


def ranking_model_metadata() -> dict[str, Any]:
    return {
        "model_version": MATERIAL_RANKER_VERSION,
        "model_type": MODEL_TYPE,
        "training_status": TRAINING_STATUS,
        "score_semantics": "CANDIDATE_ORDERING_NOT_PROBABILITY",
        "score_is_probability": False,
        "feature_weights": dict(FEATURE_WEIGHTS),
        "check_policy_version": CHECK_POLICY_VERSION,
        "fallback": "PRESERVE_RETRIEVAL_ORDER_ON_EQUAL_SCORE",
    }


def _profile_values(
    candidates: list[dict[str, Any]],
    field: str,
) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for candidate in candidates:
        profile = candidate.get("property_profile") or {}
        value = str(profile.get(field) or "").strip()
        if not value:
            continue
        values.append(
            {
                "cas_number": str(candidate.get("cas_number") or ""),
                "display_name": str(candidate.get("display_name") or ""),
                "value": value,
            }
        )
    return values


def _discrimination_score(
    candidates: list[dict[str, Any]],
    field: str,
) -> tuple[float, list[dict[str, str]]]:
    values = _profile_values(candidates, field)
    if len(candidates) < 2 or len(values) < 2:
        return 0.0, values
    normalized = ["".join(item["value"].lower().split()) for item in values]
    counts = Counter(normalized)
    if len(counts) < 2:
        return 0.0, values
    covered = len(values)
    gini = 1.0 - sum((count / covered) ** 2 for count in counts.values())
    coverage = covered / len(candidates)
    return round(gini * coverage, 6), values


def _already_observed_fields(candidates: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("field"))
        for candidate in candidates
        for item in candidate.get("matched_properties", [])
        if item.get("field")
    }


def _discriminating_check(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    observed = _already_observed_fields(candidates)
    ranked_fields: list[tuple[float, int, str, list[dict[str, str]]]] = []
    priority = {"physical_state": 0, "color": 1, "use_description": 2, "odor": 3}
    for field in PROPERTY_LABELS:
        if field in observed:
            continue
        score, values = _discrimination_score(candidates, field)
        if score > 0:
            ranked_fields.append((score, priority[field], field, values))
    if not ranked_fields:
        return None
    score, _priority, field, values = sorted(
        ranked_fields,
        key=lambda item: (-item[0], item[1], item[2]),
    )[0]
    if field == "odor":
        prompt = (
            "의도적으로 냄새를 맡지 말고 신고자의 기존 관찰 기록·계측기·현장 MSDS에서 "
            "냄새 기술을 확인하세요."
        )
    else:
        prompt = (
            f"안전거리와 보호구 전제에서 {PROPERTY_LABELS[field]} 정보를 확인하세요."
        )
    return {
        "check_id": f"VERIFY_{field.upper()}",
        "field": field,
        "prompt": prompt,
        "reason": (
            f"현재 후보들의 {PROPERTY_LABELS[field]} 값이 달라 후보 구분력이 가장 높습니다."
        ),
        "discrimination_score": score,
        "score_is_probability": False,
        "candidate_values": values,
    }


def next_best_checks(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """후보 확정이 아니라 다음 안전 확인 행동의 우선순위를 반환한다."""

    if not candidates:
        return [
            {
                "priority": 1,
                "check_id": "COLLECT_AUTHORITATIVE_IDENTITY_SOURCE",
                "field": None,
                "prompt": "용기 라벨·운송 문서·현장 MSDS에서 물질명 또는 CAS를 확보하세요.",
                "reason": "현재 검색 근거만으로 신뢰할 후보를 만들 수 없습니다.",
                "discrimination_score": 0.0,
                "score_is_probability": False,
                "candidate_values": [],
            }
        ]

    checks: list[dict[str, Any]] = [
        {
            "check_id": "VERIFY_CONTAINER_LABEL_CAS",
            "field": None,
            "prompt": "용기·배관 라벨에서 물질명과 CAS 번호를 확인하세요.",
            "reason": "이름·성상 후보는 물질 정체와 현장 존재를 확정하지 않습니다.",
            "discrimination_score": 1.0,
            "score_is_probability": False,
            "candidate_values": [],
        }
    ]
    if check := _discriminating_check(candidates):
        checks.append(check)
    checks.append(
        {
            "check_id": "VERIFY_ON_SITE_MSDS",
            "field": None,
            "prompt": "현장 MSDS 또는 운송 문서의 제품명·성분·CAS를 교차 확인하세요.",
            "reason": "제품명은 여러 성분을 포함하거나 통칭과 다를 수 있습니다.",
            "discrimination_score": 1.0,
            "score_is_probability": False,
            "candidate_values": [],
        }
    )
    return [{**check, "priority": index} for index, check in enumerate(checks, 1)]


__all__ = [
    "CHECK_POLICY_VERSION",
    "FEATURE_WEIGHTS",
    "MATERIAL_RANKER_VERSION",
    "next_best_checks",
    "rank_material_candidates",
    "ranking_model_metadata",
]
