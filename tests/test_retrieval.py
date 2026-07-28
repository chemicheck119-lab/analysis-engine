from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import chemiguard119.retrieval as retrieval_module
from chemiguard119.retrieval import (
    RUNTIME_INDEX_KEY,
    _append_missing_cas_source_representatives,
    _rrf,
    _select_source_diverse_ids,
    evaluate_retriever,
    load_retriever,
    search_evidence,
    train_retriever,
)


@pytest.fixture()
def retriever_fixture(tmp_path: Path) -> tuple[Path, dict]:
    db_path = tmp_path / "evidence.sqlite"
    evidence_rows = [
        (
            "E1",
            "CAMEO",
            "4503",
            "7681-52-9",
            "4503",
            "차아염소산나트륨",
            "산성 물질과 접촉하면 유독성 염소가스가 발생할 수 있음",
            "https://example.test/cameo/4503",
            "2026-01",
            "SOURCE_VERIFIED_NEEDS_EXPERT_APPROVAL",
        ),
        (
            "E2",
            "KOSHA",
            "MSDS-CL2",
            "7782-50-5",
            None,
            "염소",
            "염소가스 누출 시 호흡기 위험 및 환기 확인",
            "https://example.test/kosha/chlorine",
            "2026-01",
            "SOURCE_EXACT",
        ),
        (
            "E3",
            "KOSHA",
            "MSDS-ETHANOL",
            "64-17-5",
            None,
            "에탄올",
            "인화성 액체이며 화기와 거리를 두어야 함",
            "https://example.test/kosha/ethanol",
            "2026-01",
            "SOURCE_EXACT",
        ),
    ]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_record_id TEXT,
                cas_number TEXT,
                cameo_chemical_id TEXT,
                title TEXT,
                body TEXT,
                source_url TEXT,
                document_version TEXT,
                cas_link_status TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            evidence_rows,
        )
        connection.execute(
            "CREATE VIRTUAL TABLE evidence_fts USING fts5(evidence_id UNINDEXED, title, body)"
        )
        connection.executemany(
            "INSERT INTO evidence_fts (evidence_id, title, body) VALUES (?, ?, ?)",
            [(row[0], row[5], row[6]) for row in evidence_rows],
        )

    model_path = tmp_path / "retriever.joblib"
    summary = train_retriever(db_path, model_path, max_features_per_branch=100)
    assert summary["document_count"] == 3
    assert model_path.is_file()
    return db_path, load_retriever(model_path)


def test_rrf_gives_weighted_exact_ranking_more_influence() -> None:
    scores = _rrf(
        [
            (["exact-document", "semantic-document"], 2.0),
            (["semantic-document", "exact-document"], 1.0),
        ],
        k=60,
    )

    assert scores["exact-document"] == pytest.approx(2.0 / 61 + 1.0 / 62)
    assert scores["semantic-document"] == pytest.approx(2.0 / 62 + 1.0 / 61)
    assert scores["exact-document"] > scores["semantic-document"]


def test_hybrid_search_uses_exact_bm25_tfidf_and_rrf(
    retriever_fixture: tuple[Path, dict],
) -> None:
    db_path, artifact = retriever_fixture

    result = search_evidence(
        "차아염소산나트륨 산 접촉 염소가스",
        db_path,
        artifact,
        cas_hint="7681-52-9",
        top_k=3,
    )

    assert result["status"] == "COMPLETED"
    assert "RRF(k=60)" in result["method"]
    assert result["warning"].startswith("검색 순위는 위험등급이 아니며")
    assert result["notice"] is None
    assert {row["cas_number"] for row in result["results"]} == {"7681-52-9"}
    assert result["results"][0]["evidence_id"] == "E1"
    assert result["results"][0]["cas_number"] == "7681-52-9"
    assert result["results"][0]["rank_sources"]["exact"] == 1
    assert result["results"][0]["rank_sources"]["bm25"] is not None
    assert result["results"][0]["rank_sources"]["tfidf"] is not None
    assert result["results"][0]["rrf_score"] > 0
    assert (
        result["results"][0]["cas_link_status"]
        == "SOURCE_VERIFIED_NEEDS_EXPERT_APPROVAL"
    )
    assert "공개 근거 대조가 완료되지 않은" in result["cas_link_warning"]
    assert "body" not in result["results"][0]
    assert "유독성 염소가스" in result["results"][0]["body_preview"]


def test_loaded_retriever_reuses_document_lookup_index(
    retriever_fixture: tuple[Path, dict],
) -> None:
    db_path, artifact = retriever_fixture
    runtime_index = artifact[RUNTIME_INDEX_KEY]

    search_evidence(
        "차아염소산나트륨",
        db_path,
        artifact,
        cas_hint="7681-52-9",
    )

    assert artifact[RUNTIME_INDEX_KEY] is runtime_index
    assert runtime_index["row_by_id"]["E1"]["source"] == "CAMEO"
    assert runtime_index["official_ids_by_cas"]["7681-52-9"] == {"E1"}


def test_valid_cas_hint_with_no_loaded_detail_never_returns_another_substance(
    retriever_fixture: tuple[Path, dict],
) -> None:
    db_path, artifact = retriever_fixture

    result = search_evidence(
        "황산 누출",
        db_path,
        artifact,
        cas_hint="7664-93-9",
        top_k=5,
    )

    assert result["status"] == "CAS_EVIDENCE_NOT_LOADED"
    assert result["cas_hint"] == "7664-93-9"
    assert result["results"] == []
    assert "상세 근거" in result["warning"]
    assert "다른 물질의 근거로 대체하지 않습니다" in result["notice"]
    assert "외부 공식 MSDS" in result["notice"]
    assert result["cas_link_warning"] is None


def test_exact_candidates_append_missing_source_for_same_cas() -> None:
    rows = [
        {"evidence_id": "K1", "source": "KOSHA", "cas_number": "7681-52-9"},
        {"evidence_id": "K2", "source": "KOSHA", "cas_number": "7681-52-9"},
        {"evidence_id": "C1", "source": "CAMEO", "cas_number": "7681-52-9"},
        {"evidence_id": "N1", "source": "CAMEO", "cas_number": "7647-01-0"},
    ]

    augmented = _append_missing_cas_source_representatives(
        ["K1", "K2"],
        rows,
        cas_hint="7681-52-9",
    )

    assert augmented == ["K1", "K2", "C1"]


def test_source_diversity_reserves_same_cas_official_sources_without_rescoring() -> (
    None
):
    row_by_id = {
        "K1": {"source": "KOSHA", "cas_number": "7681-52-9"},
        "K2": {"source": "KOSHA", "cas_number": "7681-52-9"},
        "K3": {"source": "KOSHA", "cas_number": "7681-52-9"},
        "C1": {"source": "CAMEO", "cas_number": "7681-52-9"},
        "OTHER": {"source": "CAMEO", "cas_number": "7647-01-0"},
    }
    ranked_ids = ["K1", "K2", "K3", "OTHER", "C1"]

    selected = _select_source_diverse_ids(
        ranked_ids,
        row_by_id,
        cas_hint="7681-52-9",
        top_k=3,
    )

    # KOSHA 문서가 상위 순위를 차지해도 동일 CAS의 CAMEO 대표는 유지한다.
    assert selected == ["K1", "C1", "K2"]
    # 전역 1위는 유지하고 누락 출처 대표를 바로 다음 슬롯에 둔다.
    assert selected[0] == ranked_ids[0]


def test_source_diversity_is_not_applied_without_cas_hint() -> None:
    row_by_id = {
        "K1": {"source": "KOSHA", "cas_number": "7681-52-9"},
        "K2": {"source": "KOSHA", "cas_number": "7681-52-9"},
        "C1": {"source": "CAMEO", "cas_number": "7681-52-9"},
    }

    assert _select_source_diverse_ids(
        ["K1", "K2", "C1"],
        row_by_id,
        cas_hint=None,
        top_k=2,
    ) == ["K1", "K2"]


def test_search_keeps_each_same_cas_source_when_one_source_fills_candidate_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "evidence_id": "K1",
            "source": "KOSHA",
            "source_record_id": "K1",
            "cas_number": "7681-52-9",
            "cameo_chemical_id": None,
            "title": "KOSHA 1",
            "body": "누출 대응 문서 1",
            "source_url": "https://example.test/k1",
            "document_version": "2026-01",
            "cas_link_status": "SOURCE_EXACT",
        },
        {
            "evidence_id": "K2",
            "source": "KOSHA",
            "source_record_id": "K2",
            "cas_number": "7681-52-9",
            "cameo_chemical_id": None,
            "title": "KOSHA 2",
            "body": "누출 대응 문서 2",
            "source_url": "https://example.test/k2",
            "document_version": "2026-01",
            "cas_link_status": "SOURCE_EXACT",
        },
        {
            "evidence_id": "C1",
            "source": "CAMEO",
            "source_record_id": "4503",
            "cas_number": "7681-52-9",
            "cameo_chemical_id": "4503",
            "title": "CAMEO",
            "body": "공식 반응성 근거",
            "source_url": "https://example.test/c1",
            "document_version": "2026-01",
            "cas_link_status": "PUBLIC_SOURCE_VERIFIED",
        },
    ]
    monkeypatch.setattr(
        retrieval_module,
        "_bm25_ranks",
        lambda _db_path, _query, _limit: ["K1", "K2"],
    )
    monkeypatch.setattr(
        retrieval_module,
        "_tfidf_ranks",
        lambda _artifact, _query, _cas_hint, _limit: ["K1", "K2"],
    )

    result = search_evidence(
        "차아염소산나트륨 누출",
        tmp_path / "unused.sqlite",
        {"rows": rows},
        cas_hint="7681-52-9",
        top_k=2,
        candidate_limit=2,
    )

    assert [row["evidence_id"] for row in result["results"]] == ["K1", "C1"]
    assert {row["source"] for row in result["results"]} == {"KOSHA", "CAMEO"}
    assert result["results"][1]["rank_sources"]["exact"] == 3
    assert result["cas_link_warning"] is None


def test_evaluation_separates_automatic_hint_from_retriever_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_path = tmp_path / "retrieval.csv"
    evaluation_path.write_text(
        "query,expected_source,expected_cas,expected_cameo_id,review_status\n"
        "모호한 복합물질 질의,CAMEO,7681-52-9,4503,DRAFT_INTERNAL_REGRESSION\n"
        "명확한 물질 질의,KOSHA,7647-01-0,,DRAFT_INTERNAL_REGRESSION\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(retrieval_module, "load_retriever", lambda _path: {})
    monkeypatch.setattr(retrieval_module, "load_resolver", lambda _path: {"rows": []})
    monkeypatch.setattr(
        retrieval_module,
        "select_evidence_cas_hint_from_text",
        lambda query, _artifact: (None if query.startswith("모호한") else "7647-01-0"),
    )

    def fake_search(
        query: str,
        _db_path: Path,
        _artifact: dict,
        *,
        cas_hint: str | None,
        top_k: int,
    ) -> dict:
        assert top_k == 8
        if query.startswith("모호한") and cas_hint == "7681-52-9":
            results = [
                {
                    "source": "CAMEO",
                    "cas_number": "7681-52-9",
                    "cameo_chemical_id": "4503",
                }
            ]
        elif query.startswith("명확한") and cas_hint == "7647-01-0":
            results = [
                {
                    "source": "KOSHA",
                    "cas_number": "7647-01-0",
                    "cameo_chemical_id": None,
                }
            ]
        else:
            results = []
        return {"results": results}

    monkeypatch.setattr(retrieval_module, "search_evidence", fake_search)

    report = evaluate_retriever(
        tmp_path / "unused.sqlite",
        tmp_path / "retriever.joblib",
        tmp_path / "resolver.joblib",
        evaluation_path,
    )

    assert report["metrics_version"] == "retriever-evaluation-v2"
    assert report["end_to_end"]["recall_at_5"] == 0.5
    assert report["retriever_with_oracle_cas"]["recall_at_5"] == 1.0
    assert report["cas_hint"]["coverage"] == 0.5
    assert report["cas_hint"]["exact_match_rate"] == 0.5
    assert report["cas_hint"]["precision_when_present"] == 1.0
    assert report["cas_hint"]["missing_count"] == 1
    assert report["rows"][0]["cas_hint_status"] == "MISSING"
    assert report["rows"][0]["end_to_end_rank"] is None
    assert report["rows"][0]["oracle_cas_rank"] == 1
