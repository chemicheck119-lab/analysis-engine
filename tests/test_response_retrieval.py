from copy import deepcopy

from chemiguard119.response_retrieval import response_chapters, select_response_evidence


def row(key, cas, chapter, url="https://www.data.go.kr/data/15157612/openapi.do"):
    return {
        "evidence_id": key,
        "source": "KOSHA",
        "cas_number": cas,
        "title": f"시험 물질 MSDS {chapter:02d}장 항목",
        "body": "합성 검증용 본문",
        "source_url": url,
        "cas_link_status": "SOURCE_EXACT",
    }


def test_selection_is_same_cas_situation_bounded_and_keeps_protection_section():
    artifact = {
        "rows": [
            row("unrelated", "64-17-5", 6),
            row("storage", "7681-52-9", 7),
            row("leak-a", "7681-52-9", 6),
            row("leak-b", "7681-52-9", 6),
            row("ppe", "7681-52-9", 8),
        ]
    }
    before = deepcopy(artifact)
    retrieval = {"status": "COMPLETED", "results": [{"evidence_id": "storage"}]}
    parsed = {"incident_types": ["LEAK"], "source_text": "누출 신고"}
    result = select_response_evidence(retrieval, artifact, parsed, "7681-52-9", 2)
    assert [r["evidence_id"] for r in result["results"]] == ["leak-a", "ppe"]
    assert result["missing_response_chapters"] == []
    assert artifact == before
    assert retrieval["results"] == [{"evidence_id": "storage"}]


def test_missing_or_invalid_source_never_becomes_other_cas_or_generic_fallback():
    artifact = {
        "rows": [
            row("wrong-cas", "64-17-5", 5),
            row("bad-url", "7681-52-9", 5, "KOSHA source label"),
            row("generic", "7681-52-9", 7),
        ]
    }
    result = select_response_evidence(
        {"status": "COMPLETED", "results": artifact["rows"]},
        artifact,
        {"incident_types": ["FIRE"]},
        "7681-52-9",
        5,
    )
    assert result["results"] == []
    assert result["status"] == "NO_EVIDENCE_FOUND"
    assert result["missing_response_chapters"] == [5, 8]


def test_negated_exposure_conflict_and_retrieval_failure_do_not_trigger_selection():
    assert (
        response_chapters(
            {"incident_types": ["UNKNOWN"], "source_text": "노출은 없습니다"}
        )
        == []
    )
    assert (
        response_chapters(
            {"incident_types": ["FIRE"], "requires_statement_clarification": True}
        )
        == []
    )
    assert response_chapters(
        {"incident_types": ["LEAK"], "source_text": "작업자가 흡입했습니다"}
    ) == [4, 6, 8]
    unavailable = {"status": "RETRIEVAL_UNAVAILABLE", "results": []}
    assert (
        select_response_evidence(
            unavailable,
            {"rows": [row("fire", "7681-52-9", 5)]},
            {"incident_types": ["FIRE"]},
            "7681-52-9",
            3,
        )
        == unavailable
    )


def test_unknown_exposure_route_does_not_select_eye_or_skin_instructions():
    eye = row("eye", "7681-52-9", 4)
    eye["title"] = "시험 물질 MSDS 04장 눈에 들어갔을 때"
    skin = row("skin", "7681-52-9", 4)
    skin["title"] = "시험 물질 MSDS 04장 피부에 접촉했을 때"
    artifact = {"rows": [eye, skin, row("ppe", "7681-52-9", 8)]}
    baseline = {"status": "COMPLETED", "results": []}
    result = select_response_evidence(
        baseline,
        artifact,
        {"source_text": "작업자가 노출되었습니다", "incident_types": ["UNKNOWN"]},
        "7681-52-9",
        5,
    )
    assert [r["evidence_id"] for r in result["results"]] == ["ppe"]
    assert result["exposure_route_missing"] is True
    assert result["missing_response_chapters"] == [4]
    skin_result = select_response_evidence(
        baseline,
        artifact,
        {"source_text": "피부에 노출되었습니다", "incident_types": ["UNKNOWN"]},
        "7681-52-9",
        5,
    )
    assert [r["evidence_id"] for r in skin_result["results"]] == ["skin", "ppe"]
