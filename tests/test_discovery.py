from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from chemiguard119 import discovery


def _profile_database(path: Path, *, include_profile: bool = True) -> Path:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE substance(
                cas_number TEXT PRIMARY KEY,
                canonical_name_ko TEXT,
                canonical_name_en TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO substance VALUES (?, ?, ?)",
            ("78-93-3", "메틸 에틸 케톤", "Methyl ethyl ketone"),
        )
        if include_profile:
            connection.execute(
                """
                CREATE TABLE substance_profile(
                    cas_number TEXT PRIMARY KEY,
                    canonical_name_ko TEXT,
                    canonical_name_en TEXT,
                    physical_state TEXT,
                    color TEXT,
                    odor TEXT,
                    use_description TEXT,
                    source_url TEXT,
                    document_version TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE VIRTUAL TABLE substance_profile_fts USING fts5(
                    cas_number UNINDEXED,
                    canonical_name_ko,
                    canonical_name_en,
                    physical_state,
                    color,
                    odor,
                    use_description,
                    tokenize = 'unicode61'
                )
                """
            )
            row = (
                "78-93-3",
                "메틸 에틸 케톤",
                "Methyl ethyl ketone",
                "액체(휘발성)",
                "무색 투명",
                "박하 및 달콤한 냄새 | 아세톤 냄새",
                "용제",
            )
            connection.execute(
                """
                INSERT INTO substance_profile(
                    cas_number, canonical_name_ko, canonical_name_en,
                    physical_state, color, odor, use_description,
                    source_url, document_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *row,
                    discovery.PROPERTY_SOURCE_URL,
                    "2021-01-15 기준",
                ),
            )
            connection.execute(
                "INSERT INTO substance_profile_fts VALUES (?, ?, ?, ?, ?, ?, ?)",
                row,
            )
    return path


def _stub_resolution(
    query: str, _artifact: dict[str, Any], top_k: int
) -> dict[str, Any]:
    return {
        "query": query,
        "status": "NO_RELIABLE_MATCH",
        "candidates": [],
        "top_k": top_k,
    }


def test_property_description_returns_candidate_and_same_cas_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _profile_database(tmp_path / "model.sqlite")
    evidence_calls: list[str | None] = []
    monkeypatch.setattr(discovery, "resolve_substance", _stub_resolution)

    def fake_search(
        _query: str,
        _db_path: Path,
        _artifact: dict[str, Any],
        *,
        cas_hint: str | None,
        top_k: int,
    ) -> dict[str, Any]:
        evidence_calls.append(cas_hint)
        return {
            "status": "COMPLETED",
            "warning": "검색 순위는 위험등급이 아닙니다.",
            "notice": "원문을 확인하세요.",
            "cas_link_warning": "테스트 연결 경고",
            "results": [
                {
                    "evidence_id": "KOSHA:MEK-1",
                    "cas_number": cas_hint,
                    "source": "KOSHA",
                    "title": "메틸 에틸 케톤 MSDS",
                    "body_preview": "공식 문서 발췌",
                    "source_url": "https://example.test/kosha/mek",
                    "document_version": "2026-01-01",
                    "cas_link_status": "SOURCE_EXACT",
                }
            ][:top_k],
        }

    monkeypatch.setattr(discovery, "search_evidence", fake_search)

    result = discovery.discover_substances(
        "무색 투명하고 박하 냄새가 나는 휘발성 액체",
        db_path=db_path,
        resolver_artifact={},
        retriever_artifact={},
    )

    assert result["status"] == "CANDIDATES_FOUND"
    assert result["search_mode"] == "PROPERTY_PROFILE_RETRIEVAL"
    assert evidence_calls == ["78-93-3"]
    candidate = result["candidates"][0]
    assert candidate["cas_number"] == "78-93-3"
    assert candidate["display_name"] == "메틸 에틸 케톤"
    assert candidate["match_basis"] == "PUBLIC_PROPERTY_PROFILE"
    assert {item["field"] for item in candidate["matched_properties"]} >= {
        "physical_state",
        "color",
        "odor",
    }
    assert candidate["evidence"][0]["cas_number"] == "78-93-3"
    assert candidate["evidence"][0]["cas_link_status"] == "SOURCE_EXACT"
    assert candidate["evidence_warning"] == "검색 순위는 위험등급이 아닙니다."
    assert candidate["evidence_notice"] == "원문을 확인하세요."
    assert candidate["cas_link_warning"] == "테스트 연결 경고"
    assert 0 <= candidate["ranking_score"] <= 1
    assert candidate["ranking_score_is_probability"] is False
    assert len(candidate["ranking_features"]) == 6
    assert candidate["requires_responder_confirmation"] is True
    assert candidate["rule_eligible"] is False
    assert candidate["risk_determination_allowed"] is False


def test_discovery_abstains_when_no_identity_or_property_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _profile_database(tmp_path / "model.sqlite")
    monkeypatch.setattr(discovery, "resolve_substance", _stub_resolution)

    result = discovery.discover_substances(
        "전혀 등록되지 않은 관찰 표현",
        db_path=db_path,
        resolver_artifact={},
        retriever_artifact={},
    )

    assert result["status"] == "NO_RELIABLE_CANDIDATE"
    assert result["search_mode"] == "ABSTAINED"
    assert result["candidates"] == []
    assert result["next_best_checks"][0]["check_id"] == (
        "COLLECT_AUTHORITATIVE_IDENTITY_SOURCE"
    )
    assert "없거나 안전하다는 뜻이 아니" in result["notice"]


@pytest.mark.parametrize("query", ["또는", "그리고", "냄새가 나는 액체"])
def test_discovery_abstains_for_generic_property_words(
    query: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _profile_database(tmp_path / "model.sqlite")
    monkeypatch.setattr(discovery, "resolve_substance", _stub_resolution)

    result = discovery.discover_substances(
        query,
        db_path=db_path,
        resolver_artifact={},
        retriever_artifact={},
    )

    assert result["status"] == "NO_RELIABLE_CANDIDATE"
    assert result["candidates"] == []


def test_discovery_reports_legacy_database_without_profile_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _profile_database(tmp_path / "legacy.sqlite", include_profile=False)
    monkeypatch.setattr(discovery, "resolve_substance", _stub_resolution)

    result = discovery.discover_substances(
        "무색 휘발성 액체",
        db_path=db_path,
        resolver_artifact={},
        retriever_artifact={},
    )

    assert result["status"] == "PROFILE_INDEX_NOT_AVAILABLE"
    assert result["profile_index_available"] is False
    assert result["candidates"] == []


def test_fuzzy_resolver_result_is_not_promoted_to_discovery_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _profile_database(tmp_path / "model.sqlite")
    monkeypatch.setattr(
        discovery,
        "resolve_substance",
        lambda query, _artifact, top_k: {
            "query": query,
            "status": "FUZZY_CANDIDATES",
            "candidates": [{"cas_number": "78-93-3", "score": 0.99}],
            "top_k": top_k,
        },
    )
    result = discovery.discover_substances(
        "등록되지 않은 이름",
        db_path=db_path,
        resolver_artifact={},
        retriever_artifact={},
    )

    assert result["status"] == "NO_RELIABLE_CANDIDATE"
    assert result["candidates"] == []


def test_substance_name_used_as_odor_description_is_not_auto_promoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _profile_database(tmp_path / "model.sqlite")
    monkeypatch.setattr(
        discovery,
        "resolve_substance",
        lambda query, _artifact, top_k: {
            "query": query,
            "status": "FUZZY_CANDIDATES",
            "candidates": [{"cas_number": "67-64-1", "score": 0.99}],
            "top_k": top_k,
        },
    )
    monkeypatch.setattr(
        discovery,
        "search_evidence",
        lambda *_args, **_kwargs: {
            "status": "CAS_EVIDENCE_NOT_LOADED",
            "results": [],
        },
    )

    result = discovery.discover_substances(
        "아세톤 냄새가 나는 무색 휘발성 액체",
        db_path=db_path,
        resolver_artifact={},
        retriever_artifact={},
    )

    assert result["status"] == "CANDIDATES_FOUND"
    assert [item["cas_number"] for item in result["candidates"]] == ["78-93-3"]
    assert result["candidates"][0]["match_basis"] == "PUBLIC_PROPERTY_PROFILE"


def test_exact_identity_candidate_is_enriched_with_public_property_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = _profile_database(tmp_path / "model.sqlite")
    monkeypatch.setattr(
        discovery,
        "resolve_substance",
        lambda query, _artifact, top_k: {
            "query": query,
            "status": "EXACT_ALIAS_CANDIDATE",
            "candidates": [
                {
                    "cas_number": "78-93-3",
                    "matched_alias": "메틸 에틸 케톤",
                }
            ][:top_k],
        },
    )
    monkeypatch.setattr(
        discovery,
        "search_evidence",
        lambda *_args, **_kwargs: {
            "status": "CAS_EVIDENCE_NOT_LOADED",
            "results": [],
        },
    )

    result = discovery.discover_substances(
        "메틸 에틸 케톤",
        db_path=db_path,
        resolver_artifact={},
        retriever_artifact={},
    )

    candidate = result["candidates"][0]
    assert result["search_mode"] == "IDENTITY_RETRIEVAL"
    assert candidate["match_basis"] == "IDENTITY_AND_PUBLIC_PROPERTY_PROFILE"
    assert candidate["property_profile"]["color"] == "무색 투명"
    assert result["ranking_model"]["model_version"] == ("material-evidence-ranker-v1")
    assert result["next_best_checks"][0]["check_id"] == "VERIFY_CONTAINER_LABEL_CAS"
