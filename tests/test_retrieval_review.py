from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from chemiguard119.cli import build_parser
from chemiguard119.retrieval_review import (
    CANDIDATE_SCHEMA_VERSION,
    QUERY_TEMPLATES,
    export_review_sheet,
    generate_qrel_candidate_pool,
    load_candidate_rows,
    merge_review_sheets,
)


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE substance (
                cas_number TEXT PRIMARY KEY,
                canonical_name_ko TEXT NOT NULL,
                has_kosha_detail INTEGER NOT NULL
            );
            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                cas_number TEXT,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                source_url TEXT,
                document_version TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO substance VALUES (?, ?, ?)",
            ("64-17-5", "에탄올", 1),
        )
        for section in (1, 3, 4, 5, 6, 7, 8, 9, 10):
            connection.execute(
                "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"KOSHA:SECTION-{section}",
                    "KOSHA",
                    "64-17-5",
                    f"에탄올 MSDS {section}장 시험 항목",
                    f"에탄올 {section}장 공식 시험 본문",
                    f"https://example.test/msds/{section}",
                    "2026-01-01",
                ),
            )


def _searcher(
    _query: str,
    _db_path: Path,
    _artifact: dict[str, Any],
    **_kwargs: Any,
) -> dict[str, Any]:
    return {"results": [{"evidence_id": "KOSHA:SECTION-1"}]}


def _empty_searcher(
    _query: str,
    _db_path: Path,
    _artifact: dict[str, Any],
    **_kwargs: Any,
) -> dict[str, Any]:
    return {"results": []}


def _generate(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "test.sqlite"
    model = tmp_path / "retriever.joblib"
    candidates = tmp_path / "candidates.jsonl"
    _database(db)
    model.write_bytes(b"test-retriever")
    report = generate_qrel_candidate_pool(
        db,
        model,
        candidates,
        max_substances=1,
        retriever_artifact={},
        searcher=_searcher,
    )
    assert report["candidate_count"] == len(QUERY_TEMPLATES)
    return db, candidates


def _fill_sheet(path: Path, *, disagreement: bool = False) -> None:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    first_by_case: set[str] = set()
    for row in rows:
        case_id = row["case_id"]
        answerable = row["intent"] != "UNANSWERABLE"
        first = case_id not in first_by_case
        first_by_case.add(case_id)
        grade = 3 if answerable and first else 0
        row.update(
            {
                "review_decision": "APPROVE",
                "answerable": str(answerable).lower(),
                "relevance_grade": str(grade),
                "required_fact_ids_json": '["TEST_FACT"]' if grade else "[]",
                "supporting_sentence": row["body"] if grade else "",
                "review_notes": "독립 검수 시험",
            }
        )
    if disagreement:
        rows[0]["relevance_grade"] = "2"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_generate_qrel_candidates_has_no_gold_labels(tmp_path: Path) -> None:
    _db, candidates = _generate(tmp_path)
    rows = load_candidate_rows(candidates)

    assert len(rows) == 19
    assert all(
        row["candidate_schema_version"] == CANDIDATE_SCHEMA_VERSION for row in rows
    )
    assert all("answerable" not in row and "qrels" not in row for row in rows)
    assert {row["intent"] for row in rows} >= {
        "PPE",
        "SPILL_RESPONSE",
        "FIRE_RESPONSE",
        "FIRST_AID",
        "STORAGE_HANDLING",
        "STABILITY_REACTIVITY",
        "IDENTIFICATION",
        "UNANSWERABLE",
    }
    assert all(
        evidence["cas_number"] == row["cas_number"]
        for row in rows
        for evidence in row["evidence_candidates"]
    )


def test_export_has_blank_labels_and_hides_pool_hint(tmp_path: Path) -> None:
    _db, candidates = _generate(tmp_path)
    sheet = tmp_path / "labeler.csv"

    report = export_review_sheet(
        candidates,
        sheet,
        actor_role="LABELER",
        actor_id="labeler-01",
    )
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert report["case_count"] == 19
    assert rows
    assert rows[0]["answerable"] == ""
    assert rows[0]["relevance_grade"] == ""
    assert rows[0]["required_fact_ids_json"] == ""
    assert "target_sections" not in rows[0]
    assert "pool_sources" not in rows[0]


def test_unanswerable_no_result_uses_explicit_negative_control_pool(
    tmp_path: Path,
) -> None:
    db = tmp_path / "test.sqlite"
    model = tmp_path / "retriever.joblib"
    candidates = tmp_path / "candidates.jsonl"
    _database(db)
    model.write_bytes(b"test-retriever")

    generate_qrel_candidate_pool(
        db,
        model,
        candidates,
        max_substances=1,
        retriever_artifact={},
        searcher=_empty_searcher,
    )
    rows = load_candidate_rows(candidates)
    unanswerable = [row for row in rows if row["intent"] == "UNANSWERABLE"]

    assert len(unanswerable) == 4
    assert all(
        {
            source
            for evidence in row["evidence_candidates"]
            for source in evidence["pool_sources"]
        }
        == {"NO_RESULT_NEGATIVE_CONTROL_POOL"}
        for row in unanswerable
    )


def test_candidate_generation_refuses_to_overwrite_existing_file(
    tmp_path: Path,
) -> None:
    db, candidates = _generate(tmp_path)
    model = tmp_path / "retriever.joblib"

    with pytest.raises(FileExistsError, match="덮어쓰지 않습니다"):
        generate_qrel_candidate_pool(
            db,
            model,
            candidates,
            max_substances=1,
            retriever_artifact={},
            searcher=_searcher,
        )


def test_merge_matching_independent_reviews_creates_locked_qrels(
    tmp_path: Path,
) -> None:
    db, candidates = _generate(tmp_path)
    labeler = tmp_path / "labeler.csv"
    reviewer = tmp_path / "reviewer.csv"
    output = tmp_path / "locked.jsonl"
    export_review_sheet(
        candidates, labeler, actor_role="LABELER", actor_id="labeler-01"
    )
    export_review_sheet(
        candidates, reviewer, actor_role="REVIEWER", actor_id="reviewer-02"
    )
    _fill_sheet(labeler)
    _fill_sheet(reviewer)

    report = merge_review_sheets(candidates, labeler, reviewer, db, output)
    rows = [
        json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()
    ]

    assert report["status"] == "COMPLETED"
    assert report["independent_review"] is True
    assert report["evaluation_contract"]["passed"] is True
    assert len(rows) == 19
    assert {row["review_status"] for row in rows} == {"DOUBLE_REVIEWED_NON_EXPERT"}
    unanswerable = next(row for row in rows if row["intent"] == "UNANSWERABLE")
    assert unanswerable["answerable"] is False
    assert all(qrel["relevance_grade"] == 0 for qrel in unanswerable["qrels"])


def test_merge_blocks_disagreement_without_writing_output(tmp_path: Path) -> None:
    db, candidates = _generate(tmp_path)
    labeler = tmp_path / "labeler.csv"
    reviewer = tmp_path / "reviewer.csv"
    output = tmp_path / "locked.jsonl"
    export_review_sheet(
        candidates, labeler, actor_role="LABELER", actor_id="labeler-01"
    )
    export_review_sheet(
        candidates, reviewer, actor_role="REVIEWER", actor_id="reviewer-02"
    )
    _fill_sheet(labeler)
    _fill_sheet(reviewer, disagreement=True)

    report = merge_review_sheets(candidates, labeler, reviewer, db, output)

    assert report["status"] == "BLOCKED_REVIEW_GATE"
    assert report["disagreement_count"] == 1
    assert not output.exists()


def test_cli_exposes_retriever_review_actions() -> None:
    parser = build_parser()

    generate = parser.parse_args(["retriever-review", "generate"])
    export = parser.parse_args(
        [
            "retriever-review",
            "export",
            "--candidates",
            "candidates.jsonl",
            "--actor-role",
            "LABELER",
            "--actor-id",
            "labeler-01",
            "--output",
            "labeler.csv",
        ]
    )

    assert generate.handler.__name__ == "_retriever_review"
    assert export.retriever_review_action == "export"
