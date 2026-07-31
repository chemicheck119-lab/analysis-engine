from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import chemiguard119.retrieval_evaluation as evaluation_module
from chemiguard119.retrieval_evaluation import evaluate_retriever_sections


def _database(path: Path) -> None:
    rows = [
        (
            "E-WRONG-SECTION",
            "7647-01-0",
            "KOSHA",
            "https://example.test/product",
            "2026-01",
        ),
        (
            "E-RELEVANT",
            "7647-01-0",
            "KOSHA",
            "https://example.test/ppe",
            "2026-01",
        ),
        (
            "E-WRONG-CAS",
            "64-17-5",
            "KOSHA",
            "",
            "2026-01",
        ),
        (
            "E-HIGH-2",
            "7647-01-0",
            "KOSHA",
            "https://example.test/respirator",
            "2026-01",
        ),
        (
            "E-SUPPORT",
            "7647-01-0",
            "KOSHA",
            "https://example.test/support",
            "2026-01",
        ),
    ]
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY,
                cas_number TEXT,
                source TEXT,
                source_url TEXT,
                document_version TEXT
            )
            """
        )
        connection.executemany("INSERT INTO evidence VALUES (?, ?, ?, ?, ?)", rows)


def _case(**overrides: object) -> dict:
    row: dict[str, object] = {
        "case_id": "RET-1",
        "query": "염화수소 누출 보호구",
        "intent": "PPE",
        "answerable": True,
        "gold_cas_numbers": ["7647-01-0"],
        "qrels": [
            {
                "evidence_id": "E-WRONG-SECTION",
                "relevance_grade": 0,
                "required_fact_ids": [],
            },
            {
                "evidence_id": "E-RELEVANT",
                "relevance_grade": 3,
                "required_fact_ids": ["PPE"],
            },
        ],
        "review_status": "DRAFT_INTERNAL_REGRESSION",
        "source_type": "TEST_FIXTURE",
        "source_reference": "https://example.test/source",
        "labeler_id": "labeler",
        "reviewer_id": "",
        "expert_reviewed": False,
        "split": "locked_safety_regression",
        "duplicate_group": "ret-1",
    }
    row.update(overrides)
    return row


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_section_metrics_do_not_count_same_cas_wrong_section_as_correct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    _write(qrels_path, [_case()])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})
    monkeypatch.setattr(
        evaluation_module,
        "search_evidence",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "evidence_id": "E-WRONG-SECTION",
                    "cas_number": "7647-01-0",
                    "source_url": "https://example.test/product",
                },
                {
                    "evidence_id": "E-RELEVANT",
                    "cas_number": "7647-01-0",
                    "source_url": "https://example.test/ppe",
                },
                {
                    "evidence_id": "E-WRONG-CAS",
                    "cas_number": "64-17-5",
                    "source_url": "",
                },
            ]
        },
    )

    report = evaluate_retriever_sections(
        db_path,
        model_path,
        qrels_path,
        top_k=3,
    )

    assert report["claim_scope"] == "INTERNAL_REGRESSION_ONLY"
    assert report["metrics"]["mrr_at_k"] == 0.5
    assert report["metrics"]["recall_at_k"] == 1.0
    assert report["metrics"]["high_relevance_recall_at_k"] == 1.0
    assert report["metrics"]["graded_gain_recall_at_k"] == 1.0
    assert report["metrics"]["supporting_recall_at_k"] is None
    assert report["metrics"]["high_relevance_complete_case_rate_at_k"] == 1.0
    assert report["metrics"]["required_fact_coverage_at_k"] == 1.0
    assert report["metrics"]["high_relevance_fact_coverage_at_k"] == 1.0
    assert report["metrics"]["high_relevance_fact_complete_case_rate_at_k"] == 1.0
    uncertainty = report["uncertainty"]["high_relevance_complete_case_rate_at_k"]
    assert uncertainty["successes"] == 1
    assert uncertainty["total"] == 1
    assert uncertainty["lower"] < 0.5
    assert uncertainty["upper"] == 1.0
    assert report["metrics"]["precision_at_k"] == pytest.approx(1 / 3)
    assert report["metrics"][
        "same_cas_judged_wrong_section_rate_at_k"
    ] == pytest.approx(1 / 2)
    assert report["metrics"]["judged_coverage_at_k"] == pytest.approx(2 / 3)
    assert report["metrics"]["judged_relevant_rate_at_k"] == pytest.approx(1 / 2)
    assert report["metrics"]["unjudged_rate_at_k"] == pytest.approx(1 / 3)
    assert report["metrics"]["wrong_cas_rate_at_k"] == pytest.approx(1 / 3)
    assert report["metrics"]["valid_source_url_coverage_at_k"] == pytest.approx(2 / 3)
    assert report["metrics"]["ndcg_at_k"] < 1.0


def test_section_metrics_separate_high_relevance_and_supporting_recall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    row = _case()
    row["qrels"] = [
        {
            "evidence_id": "E-RELEVANT",
            "relevance_grade": 3,
            "required_fact_ids": ["PRIMARY_PPE"],
        },
        {
            "evidence_id": "E-HIGH-2",
            "relevance_grade": 2,
            "required_fact_ids": ["RESPIRATORY_PROTECTION"],
        },
        {
            "evidence_id": "E-SUPPORT",
            "relevance_grade": 1,
            "required_fact_ids": ["SUPPORTING_CONTEXT"],
        },
    ]
    _write(qrels_path, [row])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})
    monkeypatch.setattr(
        evaluation_module,
        "search_evidence",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "evidence_id": "E-RELEVANT",
                    "cas_number": "7647-01-0",
                    "source_url": "https://example.test/ppe",
                },
                {
                    "evidence_id": "E-HIGH-2",
                    "cas_number": "7647-01-0",
                    "source_url": "https://example.test/respirator",
                },
            ]
        },
    )

    report = evaluate_retriever_sections(
        db_path,
        model_path,
        qrels_path,
        top_k=5,
    )

    result = report["rows"][0]
    assert result["recall_at_k"] == pytest.approx(2 / 3)
    assert result["high_relevance_recall_at_k"] == 1.0
    assert result["supporting_recall_at_k"] == 0.0
    assert result["graded_gain_recall_at_k"] == pytest.approx(10 / 11)
    assert result["high_relevance_complete_at_k"] is True
    assert result["required_fact_coverage_at_k"] == pytest.approx(2 / 3)
    assert result["high_relevance_fact_coverage_at_k"] == 1.0
    assert result["supporting_fact_coverage_at_k"] == 0.0
    assert result["high_relevance_fact_complete_at_k"] is True
    assert result["missed_evidence_ids"] == {
        "high_relevance": [],
        "supporting": ["E-SUPPORT"],
    }
    assert result["missed_fact_ids"] == {
        "high_relevance": [],
        "supporting": ["SUPPORTING_CONTEXT"],
    }
    assert result["relevance_gain"] == {"returned": 10, "total": 11}
    assert report["metrics"]["required_fact_coverage_at_k"] == pytest.approx(2 / 3)
    assert report["metrics"]["high_relevance_fact_coverage_at_k"] == 1.0
    assert report["metrics"]["supporting_fact_coverage_at_k"] == 0.0
    assert report["metrics"]["high_relevance_fact_complete_case_rate_at_k"] == 1.0
    assert report["qrel_summary"] == {
        "relevance_grade_counts": {"0": 0, "1": 1, "2": 1, "3": 1},
        "positive_qrel_count": 3,
        "high_relevance_qrel_count": 2,
        "supporting_qrel_count": 1,
        "high_relevance_min_grade": 2,
        "required_fact_assignment_count": 3,
    }
    assert report["metric_definitions"]["high_relevance_recall_at_k"] == {
        "relevance_grades": [2, 3],
        "weighting": "BINARY_EQUAL",
        "aggregation": "MACRO_OVER_ANSWERABLE_CASES",
        "meaning": "핵심 답변 근거의 회수율",
    }


@pytest.mark.parametrize(
    "gold_cas_numbers",
    [[], ["7647-01-0", "64-17-5"], ["7647-01-0", "7647-01-0"]],
)
def test_oracle_section_evaluation_requires_exactly_one_gold_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gold_cas_numbers: list[str],
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    _write(qrels_path, [_case(gold_cas_numbers=gold_cas_numbers)])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})

    with pytest.raises(ValueError, match="정확히 1개의 gold CAS"):
        evaluate_retriever_sections(db_path, model_path, qrels_path)


def test_unanswerable_case_measures_abstention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    row = _case(
        answerable=False,
        qrels=[
            {
                "evidence_id": "E-WRONG-SECTION",
                "relevance_grade": 0,
                "required_fact_ids": [],
            }
        ],
    )
    _write(qrels_path, [row])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})
    monkeypatch.setattr(
        evaluation_module,
        "search_evidence",
        lambda *_args, **_kwargs: {"results": []},
    )

    report = evaluate_retriever_sections(db_path, model_path, qrels_path)

    assert report["answerable_case_count"] == 0
    assert report["unanswerable_case_count"] == 1
    assert report["metrics"]["unanswerable_abstention_rate"] == 1.0
    assert report["metrics"]["high_relevance_recall_at_k"] is None


def test_qrel_validation_rejects_missing_evidence_and_invalid_grade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    row = _case()
    row["qrels"] = [
        {
            "evidence_id": "DOES-NOT-EXIST",
            "relevance_grade": 4,
            "required_fact_ids": [],
        }
    ]
    _write(qrels_path, [row])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})

    with pytest.raises(ValueError, match="DB에 없는 evidence_id"):
        evaluate_retriever_sections(db_path, model_path, qrels_path)


def test_qrel_validation_rejects_invalid_grade_for_existing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    row = _case()
    row["qrels"] = [
        {
            "evidence_id": "E-RELEVANT",
            "relevance_grade": 4,
            "required_fact_ids": [],
        }
    ]
    _write(qrels_path, [row])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})

    with pytest.raises(ValueError, match="relevance_grade는 0~3"):
        evaluate_retriever_sections(db_path, model_path, qrels_path)


def test_qrel_validation_requires_fact_ids_for_relevant_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    row = _case()
    row["qrels"] = [
        {
            "evidence_id": "E-RELEVANT",
            "relevance_grade": 3,
            "required_fact_ids": [],
        }
    ]
    _write(qrels_path, [row])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})

    with pytest.raises(ValueError, match="관련 qrel에는 required_fact_ids"):
        evaluate_retriever_sections(db_path, model_path, qrels_path)


def test_qrel_validation_requires_high_relevance_evidence_for_answerable_query(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    row = _case()
    row["qrels"] = [
        {
            "evidence_id": "E-SUPPORT",
            "relevance_grade": 1,
            "required_fact_ids": ["SUPPORTING_CONTEXT"],
        }
    ]
    _write(qrels_path, [row])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})

    with pytest.raises(ValueError, match="grade 2 이상의 핵심 qrel"):
        evaluate_retriever_sections(db_path, model_path, qrels_path)


def test_precision_at_k_uses_k_and_reports_unjudged_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    _write(qrels_path, [_case()])
    monkeypatch.setattr(evaluation_module, "load_retriever", lambda _path: {})
    monkeypatch.setattr(
        evaluation_module,
        "search_evidence",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "evidence_id": "E-RELEVANT",
                    "cas_number": "7647-01-0",
                    "source_url": "https://example.test/ppe",
                },
                {
                    "evidence_id": "E-WRONG-CAS",
                    "cas_number": "64-17-5",
                    "source_url": "",
                },
            ]
        },
    )

    report = evaluate_retriever_sections(
        db_path,
        model_path,
        qrels_path,
        top_k=5,
    )

    row = report["rows"][0]
    assert row["precision_at_k"] == pytest.approx(1 / 5)
    assert row["judged_relevant_rate_at_k"] == 1.0
    assert row["judged_coverage_at_k"] == pytest.approx(1 / 2)
    assert row["unjudged_rate_at_k"] == pytest.approx(1 / 2)
    assert report["metrics"]["precision_at_k"] == pytest.approx(1 / 5)
    assert report["metrics"]["judged_relevant_rate_at_k"] == 1.0
    assert report["metrics"]["unjudged_rate_at_k"] == pytest.approx(1 / 2)


def test_reviewed_profile_rejects_draft_section_qrels(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "evidence.sqlite"
    model_path = tmp_path / "retriever.joblib"
    qrels_path = tmp_path / "qrels.jsonl"
    _database(db_path)
    model_path.write_bytes(b"test-model")
    _write(qrels_path, [_case()])

    with pytest.raises(ValueError, match="평가 데이터 계약 실패"):
        evaluate_retriever_sections(
            db_path,
            model_path,
            qrels_path,
            profile="COMPETITION_REVIEWED",
        )
