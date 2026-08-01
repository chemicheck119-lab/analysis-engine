from __future__ import annotations

from chemiguard119.material_ranker import (
    FEATURE_WEIGHTS,
    next_best_checks,
    rank_material_candidates,
    ranking_model_metadata,
)


def _candidate(
    cas_number: str,
    name: str,
    *,
    matched_fields: tuple[str, ...] = (),
    physical_state: str = "",
    color: str = "",
    odor: str = "",
    use_description: str = "",
    evidence: bool = False,
) -> dict:
    values = {
        "physical_state": physical_state,
        "color": color,
        "odor": odor,
        "use_description": use_description,
    }
    return {
        "rank": 1,
        "cas_number": cas_number,
        "display_name": name,
        "matched_properties": [
            {"field": field, "label": field, "value": values[field]}
            for field in matched_fields
        ],
        "property_profile": values if any(values.values()) else None,
        "evidence": ([{"evidence_id": "TEST"}] if evidence else []),
    }


def test_explainable_ranker_keeps_exact_identity_ahead_of_property_only_match() -> None:
    exact = _candidate("7647-01-0", "염산", matched_fields=("physical_state",))
    property_only = _candidate(
        "78-93-3",
        "메틸 에틸 케톤",
        matched_fields=("physical_state", "color", "odor"),
        evidence=True,
    )

    ranked = rank_material_candidates(
        [property_only, exact],
        direct_by_cas={
            "7647-01-0": {
                "score": 1.0,
                "match_type": "UNIQUE_ALIAS_EXACT",
                "authority_level": "PUBLIC_AUTHORITY_SOURCE",
            }
        },
        resolution_status="EXACT_ALIAS_CANDIDATE",
    )

    assert [item["cas_number"] for item in ranked] == ["7647-01-0", "78-93-3"]
    assert [item["rank"] for item in ranked] == [1, 2]
    assert all(item["ranking_score_is_probability"] is False for item in ranked)
    assert len(ranked[0]["ranking_features"]) == len(FEATURE_WEIGHTS)
    assert (
        sum(item["contribution"] for item in ranked[0]["ranking_features"])
        == ranked[0]["ranking_score"]
    )


def test_ranker_explains_property_coverage_without_regressing_bm25_prior() -> None:
    one_field = _candidate(
        "64-17-5",
        "에탄올",
        matched_fields=("color",),
    )
    three_fields = _candidate(
        "67-56-1",
        "메탄올",
        matched_fields=("physical_state", "color", "odor"),
    )

    ranked = rank_material_candidates(
        [one_field, three_fields],
        direct_by_cas={},
        resolution_status="UNRESOLVED",
    )

    assert [item["cas_number"] for item in ranked] == ["64-17-5", "67-56-1"]
    coverage = {
        item["cas_number"]: next(
            feature["value"]
            for feature in item["ranking_features"]
            if feature["name"] == "property_coverage"
        )
        for item in ranked
    }
    assert coverage == {"64-17-5": 0.25, "67-56-1": 0.75}
    assert "rule_eligible" not in ranked[0]


def test_next_best_check_selects_highest_discriminating_unobserved_field() -> None:
    candidates = [
        _candidate(
            "64-17-5",
            "에탄올",
            matched_fields=("physical_state",),
            physical_state="액체",
            color="무색",
            odor="알코올 냄새",
            use_description="용제",
        ),
        _candidate(
            "67-56-1",
            "메탄올",
            matched_fields=("physical_state",),
            physical_state="액체",
            color="투명",
            odor="자극성 냄새",
            use_description="연료",
        ),
    ]

    checks = next_best_checks(candidates)

    assert [item["priority"] for item in checks] == [1, 2, 3]
    assert checks[0]["check_id"] == "VERIFY_CONTAINER_LABEL_CAS"
    assert checks[1]["field"] == "color"
    assert checks[1]["candidate_values"][0]["cas_number"] == "64-17-5"
    assert checks[1]["score_is_probability"] is False
    assert checks[2]["check_id"] == "VERIFY_ON_SITE_MSDS"


def test_odor_check_never_tells_responder_to_smell_material() -> None:
    candidates = [
        _candidate(
            "64-17-5",
            "에탄올",
            matched_fields=("physical_state", "color", "use_description"),
            physical_state="액체",
            color="무색",
            odor="알코올 냄새",
            use_description="용제",
        ),
        _candidate(
            "67-56-1",
            "메탄올",
            matched_fields=("physical_state", "color", "use_description"),
            physical_state="액체",
            color="무색",
            odor="자극성 냄새",
            use_description="용제",
        ),
    ]

    odor_check = next(
        item for item in next_best_checks(candidates) if item["field"] == "odor"
    )

    assert "의도적으로 냄새를 맡지 말고" in odor_check["prompt"]
    assert "계측기" in odor_check["prompt"]


def test_no_candidate_returns_authoritative_identity_collection_action() -> None:
    checks = next_best_checks([])

    assert checks == [
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


def test_ranking_metadata_discloses_non_supervised_non_probability_semantics() -> None:
    metadata = ranking_model_metadata()

    assert metadata["training_status"] == "NOT_SUPERVISED_INSUFFICIENT_REVIEWED_LABELS"
    assert metadata["score_is_probability"] is False
    assert metadata["score_semantics"] == "CANDIDATE_ORDERING_NOT_PROBABILITY"
    assert sum(metadata["feature_weights"].values()) == 1.0
