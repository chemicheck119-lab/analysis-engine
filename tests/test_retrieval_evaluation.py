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
