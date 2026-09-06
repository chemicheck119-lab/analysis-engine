from __future__ import annotations

from pathlib import Path

import pytest

import chemiguard119.pipeline as pipeline_module
from chemiguard119.pipeline import analyze_incident, validate_pipeline_output


@pytest.fixture()
def resolver_artifact() -> dict:
    return {
        "rows": [
            {
                "cas_number": "7681-52-9",
                "alias_text": "차아염소산나트륨",
                "alias_type": "canonical_name_ko",
                "source": "KOSHA",
                "verification_status": "SOURCE_EXACT",
            },
            {
                "cas_number": "7647-01-0",
                "alias_text": "염산",
                "alias_type": "search_name",
                "source": "KOSHA",
                "verification_status": "SOURCE_EXACT",
            },
        ]
    }


@pytest.fixture(autouse=True)
def stub_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    def search(
        query: str, _db: Path, _artifact: dict, cas_hint: str | None, top_k: int
    ) -> dict:
        return {
            "status": "COMPLETED",
            "query": query,
            "cas_hint": cas_hint,
            "warning": "검색 순위는 위험등급이 아닙니다.",
            "notice": None,
            "results": [
                {
                    "evidence_id": f"E-{cas_hint or 'NONE'}",
                    "source": "KOSHA",
                    "cas_number": cas_hint,
                }
            ][:top_k],
        }

    monkeypatch.setattr(pipeline_module, "search_evidence", search)


def test_name_candidates_never_trigger_rule_engine(
    tmp_path: Path,
    resolver_artifact: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_review(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("이름 후보만으로 Rule Engine을 실행하면 안 됩니다.")

    monkeypatch.setattr(pipeline_module, "review_pair", forbidden_review)
    result = analyze_incident(
        "차아염소산나트륨 탱크에서 누출 중이며 옆 저장고에 염산이 있습니다.",
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=resolver_artifact,
        retriever_artifact={},
    )

    assert result["status"] == "NEEDS_SUBSTANCE_CONFIRMATION"
    assert result["rule_review"]["executed"] is False
    assert result["rule_review"]["status"] == "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"
    assert {item["cas_basis"] for item in result["evidence"]} == {"PARSER_CANDIDATE"}
    assert all(item["requires_responder_confirmation"] for item in result["evidence"])
    assert result["output_validation"] == {"status": "PASSED", "errors": []}


def test_only_one_confirmed_cas_still_skips_rule(
    tmp_path: Path,
    resolver_artifact: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "review_pair",
        lambda *_args, **_kwargs: pytest.fail(
            "두 번째 확인 CAS 없이 Rule을 실행했습니다."
        ),
    )

    result = analyze_incident(
        "차아염소산나트륨 누출, 시설에 염산 보관",
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=resolver_artifact,
        retriever_artifact={},
        confirmed_incident_cas="7681-52-9",
    )

    assert result["status"] == "NEEDS_FACILITY_SUBSTANCE_CONFIRMATION"
    assert result["rule_review"]["executed"] is False
    assert (
        result["confirmed_substances"]["incident"]["confirmation"]
        == "RESPONDER_CONFIRMED"
    )


def test_pipeline_surfaces_missing_same_cas_evidence_without_substitution(
    tmp_path: Path,
    resolver_artifact: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_search(
        query: str,
        _db: Path,
        _artifact: dict,
        cas_hint: str | None,
        top_k: int,
    ) -> dict:
        assert query
        assert cas_hint == "7681-52-9"
        assert top_k == 5
        return {
            "status": "CAS_EVIDENCE_NOT_LOADED",
            "query": query,
            "cas_hint": cas_hint,
            "warning": "해당 CAS의 상세 근거가 시스템에 적재되어 있지 않습니다.",
            "notice": "외부 공식 MSDS 확인이 필요합니다.",
            "results": [],
        }

    monkeypatch.setattr(pipeline_module, "search_evidence", missing_search)
    result = analyze_incident(
        "차아염소산나트륨 누출",
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=resolver_artifact,
        retriever_artifact={},
        confirmed_incident_cas="7681-52-9",
    )

    retrieval = result["evidence"][0]["retrieval"]
    assert retrieval["status"] == "CAS_EVIDENCE_NOT_LOADED"
    assert retrieval["results"] == []
    assert "외부 공식 MSDS" in retrieval["notice"]
    retrieval_trace = next(
        item for item in result["trace"] if item["stage"] == "EVIDENCE_RETRIEVAL"
    )
    assert retrieval_trace["status"] == "COMPLETED_WITH_WARNINGS"
    assert retrieval_trace["missing_cas_evidence_count"] == 1
    assert result["output_validation"] == {"status": "PASSED", "errors": []}


def test_retriever_timeout_fails_closed_without_leaking_error(
    tmp_path: Path,
    resolver_artifact: dict,
) -> None:
    def timeout_search(*_args: object, **_kwargs: object) -> dict:
        raise TimeoutError("PRIVATE_RETRIEVER_ENDPOINT_AND_QUERY")

    result = analyze_incident(
        "PX-119-Z 미등록 제품이 누출됐다는 신고",
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=resolver_artifact,
        retriever_artifact={},
        evidence_searcher=timeout_search,
    )

    retrieval = result["evidence"][0]["retrieval"]
    assert retrieval["status"] == "RETRIEVAL_UNAVAILABLE"
    assert retrieval["results"] == []
    assert retrieval["cas_hint"] is None
    assert "PRIVATE_RETRIEVER_ENDPOINT_AND_QUERY" not in str(result)
    assert result["rule_review"]["executed"] is False
    assert result["rule_review"]["status"] == "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"
    trace = next(
        item for item in result["trace"] if item["stage"] == "EVIDENCE_RETRIEVAL"
    )
    assert trace["status"] == "COMPLETED_WITH_WARNINGS"
    assert trace["retrieval_unavailable_count"] == 1
    assert result["output_validation"] == {"status": "PASSED", "errors": []}

    result["evidence"][0]["retrieval"]["results"] = [
        {"evidence_id": "FORGED", "cas_number": "7647-01-0"}
    ]
    assert (
        "근거 검색 장애 상태에 결과 문서를 포함할 수 없습니다."
        in validate_pipeline_output(result)
    )


def test_retriever_timeout_with_one_confirmation_still_skips_rule(
    tmp_path: Path,
    resolver_artifact: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pipeline_module,
        "review_pair",
        lambda *_args, **_kwargs: pytest.fail(
            "Retriever timeout 뒤 한쪽 CAS만으로 Rule을 실행했습니다."
        ),
    )

    result = analyze_incident(
        "차아염소산나트륨 누출",
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=resolver_artifact,
        retriever_artifact={},
        confirmed_incident_cas="7681-52-9",
        evidence_searcher=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("private timeout")
        ),
    )

    assert result["status"] == "NEEDS_FACILITY_SUBSTANCE_CONFIRMATION"
    assert result["rule_review"]["executed"] is False
    assert result["evidence"][0]["retrieval"]["status"] == "RETRIEVAL_UNAVAILABLE"
    assert result["output_validation"] == {"status": "PASSED", "errors": []}


def test_ambiguous_name_never_uses_first_candidate_as_evidence_cas_hint(
    tmp_path: Path,
) -> None:
    ambiguous_resolver = {
        "rows": [
            {
                "cas_number": "64-17-5",
                "alias_text": "알코올",
                "alias_type": "common_name",
            },
            {
                "cas_number": "67-56-1",
                "alias_text": "알코올",
                "alias_type": "common_name",
            },
        ]
    }

    result = analyze_incident(
        "알코올 탱크에서 누출 중입니다.",
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=ambiguous_resolver,
        retriever_artifact={},
    )

    mention = result["substance_candidates"][0]
    assert mention["resolver_status"] == "AMBIGUOUS_ALIAS"
    assert mention["evidence_cas_hint"] is None
    assert result["evidence"][0]["cas_hint"] is None
    assert result["evidence"][0]["cas_basis"] == "NO_CAS_HINT"


@pytest.mark.parametrize(
    "source",
    [
        "염산염 누출 신고",
        "염산성 세척제 누출",
    ],
)
def test_embedded_alias_never_restricts_pipeline_evidence_by_cas(
    tmp_path: Path,
    resolver_artifact: dict,
    source: str,
) -> None:
    result = analyze_incident(
        source,
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=resolver_artifact,
        retriever_artifact={},
    )

    assert result["substance_candidates"] == []
    assert result["evidence"][0]["cas_hint"] is None
    assert result["evidence"][0]["cas_basis"] == "NO_CAS_HINT"
    assert result["rule_review"]["executed"] is False
    assert result["output_validation"] == {"status": "PASSED", "errors": []}


def test_two_confirmed_cas_execute_rule_and_preserve_trace(
    tmp_path: Path,
    resolver_artifact: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, list[str]]] = []

    def review(
        incident_cas: str,
        facility_cas: str,
        _db: Path,
        planned_actions: list[str],
        **_kwargs: object,
    ) -> dict:
        calls.append((incident_cas, facility_cas, planned_actions))
        return {
            "status": "COMPLETED",
            "incident_cas": incident_cas,
            "facility_cas": facility_cas,
            "severity": "HIGH_RISK",
            "risk_level": "HIGH",
            "risk_level_ko": "높음",
            "risk_scale": {
                "type": "ORDINAL_RULE_CLASSIFICATION",
                "is_probability": False,
                "probability_percent": None,
            },
            "rule_id": "TEST-RULE-001",
            "evidence_urls": ["https://example.test/evidence"],
            "final_decision": "현장 지휘관 판단",
            "human_confirmation_required": True,
        }

    monkeypatch.setattr(pipeline_module, "review_pair", review)
    result = analyze_incident(
        "차아염소산나트륨 누출, 시설에 염산 보관",
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=resolver_artifact,
        retriever_artifact={},
        confirmed_incident_cas="7681-52-9",
        confirmed_facility_cas="7647-01-0",
        planned_actions=["환기 검토"],
    )

    assert calls == [("7681-52-9", "7647-01-0", ["환기 검토"])]
    assert result["status"] == "COMPLETED_WITH_WARNINGS"
    assert result["rule_review"]["executed"] is True
    assert result["rule_review"]["result"]["rule_id"] == "TEST-RULE-001"
    assert any(
        item["stage"] == "RULE_REVIEW" and item["status"] == "EXECUTED"
        for item in result["trace"]
    )
    assert validate_pipeline_output(result) == []


def test_validator_rejects_rule_output_without_two_confirmations() -> None:
    forged = {
        "schema_version": "incident-analysis-v1",
        "status": "COMPLETED_WITH_WARNINGS",
        "input": {"raw_text": "염산 누출"},
        "confirmed_substances": {"incident": None, "facility": None},
        "parsed_report": None,
        "evidence": [],
        "rule_review": {
            "executed": True,
            "result": {
                "status": "COMPLETED",
                "severity": "HIGH_RISK",
                "rule_id": "FORGED",
                "evidence_urls": ["https://example.test"],
                "final_decision": "현장 지휘관 판단",
            },
        },
        "safety": pipeline_module._safety_fields(),
    }

    assert (
        "두 CAS의 대원 확인 없이 Rule Engine이 실행되었습니다."
        in validate_pipeline_output(forged)
    )


def test_invalid_cas_is_blocked_before_parsing(
    tmp_path: Path, resolver_artifact: dict
) -> None:
    result = analyze_incident(
        "차아염소산나트륨 누출",
        db_path=tmp_path / "unused.sqlite",
        resolver_artifact=resolver_artifact,
        retriever_artifact={},
        confirmed_incident_cas="7681-52-0",
    )

    assert result["status"] == "INVALID_INPUT"
    assert result["rule_review"]["executed"] is False
    assert result["output_validation"]["status"] == "PASSED"
