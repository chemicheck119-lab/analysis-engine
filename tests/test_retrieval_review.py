from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from chemiguard119.cli import build_parser
from chemiguard119.retrieval_review import (
    CANDIDATE_SCHEMA_VERSION,
    POOL_RUN_SCHEMA_VERSION,
    QUERY_TEMPLATES,
    assemble_review_batches,
    audit_candidate_pool_coverage,
    audit_review_sheet,
    create_review_batches,
    export_review_sheet,
    generate_qrel_candidate_pool,
    generate_retriever_pool_run,
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


def _pool_run(
    path: Path,
    db: Path,
    candidates: Path,
    *,
    evidence_id: str | None = None,
) -> None:
    candidate_rows = load_candidate_rows(candidates)
    results = []
    for candidate in candidate_rows:
        returned = [
            evidence_id or str(candidate["evidence_candidates"][0]["evidence_id"])
        ]
        results.append(
            {
                "case_id": candidate["case_id"],
                "query_sha256": hashlib.sha256(
                    str(candidate["query"]).encode("utf-8")
                ).hexdigest(),
                "returned_evidence_ids": returned,
            }
        )
    payload = {
        "schema_version": POOL_RUN_SCHEMA_VERSION,
        "system_id": "test-system",
        "system_version": "test-v1",
        "candidate_sha256": hashlib.sha256(candidates.read_bytes()).hexdigest(),
        "system_artifact_sha256": "1" * 64,
        "database_sha256": hashlib.sha256(db.read_bytes()).hexdigest(),
        "top_k": 5,
        "results": results,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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


def test_review_batches_are_deterministic_balanced_and_keep_cases_together(
    tmp_path: Path,
) -> None:
    _db, candidates = _generate(tmp_path)
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first = create_review_batches(
        candidates,
        first_dir,
        actor_role="LABELER",
        actor_id="labeler-01",
        questions_per_batch=4,
    )
    create_review_batches(
        candidates,
        second_dir,
        actor_role="LABELER",
        actor_id="labeler-01",
        questions_per_batch=4,
    )
    first_manifest = json.loads(
        (first_dir / "batch_manifest.json").read_text(encoding="utf-8")
    )
    second_manifest = json.loads(
        (second_dir / "batch_manifest.json").read_text(encoding="utf-8")
    )

    assert first["batch_count"] == 5
    assert [entry["case_ids"] for entry in first_manifest["batches"]] == [
        entry["case_ids"] for entry in second_manifest["batches"]
    ]
    assert [entry["template_sha256"] for entry in first_manifest["batches"]] == [
        entry["template_sha256"] for entry in second_manifest["batches"]
    ]
    assert max(entry["case_count"] for entry in first_manifest["batches"]) <= 4
    all_case_ids: list[str] = []
    for entry in first_manifest["batches"]:
        batch_path = first_dir / entry["filename"]
        with batch_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        assert {row["case_id"] for row in rows} == set(entry["case_ids"])
        assert all(row["review_decision"] == "" for row in rows)
        assert all(row["answerable"] == "" for row in rows)
        assert all(row["relevance_grade"] == "" for row in rows)
        all_case_ids.extend(entry["case_ids"])
        assert stat.S_IMODE(batch_path.stat().st_mode) == 0o600
    assert len(all_case_ids) == len(set(all_case_ids)) == len(QUERY_TEMPLATES)
    assert stat.S_IMODE(first_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((first_dir / "batch_manifest.json").stat().st_mode) == 0o600


def test_review_batches_refuse_existing_directory(tmp_path: Path) -> None:
    _db, candidates = _generate(tmp_path)
    output_dir = tmp_path / "batches"
    output_dir.mkdir()

    with pytest.raises(FileExistsError, match="덮어쓰지 않습니다"):
        create_review_batches(
            candidates,
            output_dir,
            actor_role="LABELER",
            actor_id="labeler-01",
        )


def test_assemble_completed_batches_restores_canonical_review_sheet(
    tmp_path: Path,
) -> None:
    _db, candidates = _generate(tmp_path)
    batch_dir = tmp_path / "batches"
    output = tmp_path / "labeler.csv"
    create_review_batches(
        candidates,
        batch_dir,
        actor_role="LABELER",
        actor_id="labeler-01",
        questions_per_batch=4,
    )
    manifest = json.loads(
        (batch_dir / "batch_manifest.json").read_text(encoding="utf-8")
    )
    for entry in manifest["batches"]:
        _fill_sheet(batch_dir / entry["filename"])

    report = assemble_review_batches(candidates, batch_dir, output)
    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    expected_rows = sum(
        len(row["evidence_candidates"]) for row in load_candidate_rows(candidates)
    )
    assert report["status"] == "COMPLETED"
    assert report["claim_scope"] == "COMPLETED_SINGLE_REVIEW_SHEET_ONLY"
    assert len(rows) == expected_rows
    assert list(dict.fromkeys(row["case_id"] for row in rows)) == [
        row["case_id"] for row in load_candidate_rows(candidates)
    ]
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_assemble_batches_rejects_modified_candidate_context(tmp_path: Path) -> None:
    _db, candidates = _generate(tmp_path)
    batch_dir = tmp_path / "batches"
    output = tmp_path / "labeler.csv"
    create_review_batches(
        candidates,
        batch_dir,
        actor_role="LABELER",
        actor_id="labeler-01",
        questions_per_batch=4,
    )
    manifest = json.loads(
        (batch_dir / "batch_manifest.json").read_text(encoding="utf-8")
    )
    for entry in manifest["batches"]:
        batch_path = batch_dir / entry["filename"]
        _fill_sheet(batch_path)
    first_batch = batch_dir / manifest["batches"][0]["filename"]
    with first_batch.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    rows[0]["query"] = "변조된 질문"
    with first_batch.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(ValueError, match="candidate context가 변경"):
        assemble_review_batches(candidates, batch_dir, output)


@pytest.mark.parametrize(
    "tamper_target",
    (
        "root_case_count",
        "root_judgment_count",
        "root_batch_count",
        "entry_case_count",
        "entry_judgment_count",
        "entry_intent_counts",
        "entry_template_sha256",
    ),
)
def test_assemble_batches_recomputes_manifest_provenance(
    tmp_path: Path,
    tamper_target: str,
) -> None:
    _db, candidates = _generate(tmp_path)
    batch_dir = tmp_path / "batches"
    output = tmp_path / "labeler.csv"
    create_review_batches(
        candidates,
        batch_dir,
        actor_role="LABELER",
        actor_id="labeler-01",
        questions_per_batch=4,
    )
    manifest_path = batch_dir / "batch_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if tamper_target == "root_case_count":
        manifest["case_count"] += 1
    elif tamper_target == "root_judgment_count":
        manifest["evidence_judgment_count"] += 1
    elif tamper_target == "root_batch_count":
        manifest["batch_count"] += 1
    elif tamper_target == "entry_case_count":
        manifest["batches"][0]["case_count"] += 1
    elif tamper_target == "entry_judgment_count":
        manifest["batches"][0]["evidence_judgment_count"] += 1
    elif tamper_target == "entry_intent_counts":
        manifest["batches"][0]["intent_counts"] = {"UNANSWERABLE": 99}
    elif tamper_target == "entry_template_sha256":
        manifest["batches"][0]["template_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="provenance"):
        assemble_review_batches(candidates, batch_dir, output)


def test_pool_audit_accepts_declared_results_already_in_candidate_pool(
    tmp_path: Path,
) -> None:
    db, candidates = _generate(tmp_path)
    run = tmp_path / "system.json"
    _pool_run(run, db, candidates)

    report = audit_candidate_pool_coverage(candidates, db, [run])

    assert report["status"] == "COMPLETE_FOR_DECLARED_SYSTEMS"
    assert report["missing_unique_case_evidence_pair_count"] == 0
    assert report["systems"][0]["system_artifact_sha256"] == "1" * 64
    assert report["is_performance_result"] is False


def test_generate_pool_run_is_private_and_auditable(tmp_path: Path) -> None:
    db, candidates = _generate(tmp_path)
    model = tmp_path / "retriever.joblib"
    output = tmp_path / "pool-run.json"

    generation = generate_retriever_pool_run(
        candidates,
        db,
        model,
        output,
        system_id="baseline-lexical-hybrid",
        system_version="evidence-hybrid-tfidf-v2@test",
        retriever_artifact={},
        searcher=_searcher,
    )
    audit = audit_candidate_pool_coverage(candidates, db, [output])
    run = json.loads(output.read_text(encoding="utf-8"))

    assert generation["status"] == "COMPLETED"
    assert generation["case_count"] == len(QUERY_TEMPLATES)
    assert generation["is_performance_result"] is False
    assert output.stat().st_mode & 0o777 == 0o600
    assert "query" not in run["results"][0]
    assert audit["status"] == "COMPLETE_FOR_DECLARED_SYSTEMS"


def test_generate_pool_run_rejects_changed_database(tmp_path: Path) -> None:
    db, candidates = _generate(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "KOSHA:CHANGED",
                "KOSHA",
                "64-17-5",
                "변경된 문서",
                "변경된 본문",
                "https://example.test/changed",
                "2026-01-02",
            ),
        )

    with pytest.raises(ValueError, match="DB artifact가 변경"):
        generate_retriever_pool_run(
            candidates,
            db,
            tmp_path / "retriever.joblib",
            tmp_path / "pool-run.json",
            system_id="baseline-lexical-hybrid",
            system_version="test",
            retriever_artifact={},
            searcher=_searcher,
        )


def test_pool_audit_requires_expansion_for_unpooled_same_cas_result(
    tmp_path: Path,
) -> None:
    db, candidates = _generate(tmp_path)
    run = tmp_path / "system.json"
    _pool_run(run, db, candidates, evidence_id="KOSHA:SECTION-2")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "KOSHA:SECTION-2",
                "KOSHA",
                "64-17-5",
                "에탄올 MSDS 2장 시험 항목",
                "에탄올 2장 공식 시험 본문",
                "https://example.test/msds/2",
                "2026-01-01",
            ),
        )
    # DB 변경을 허용하는 테스트가 아니라 동일 artifact에서 빠진 pool을 검사해야 하므로
    # 후보를 새 DB hash로 다시 생성한다.
    candidates.unlink()
    model = tmp_path / "retriever.joblib"
    generate_qrel_candidate_pool(
        db,
        model,
        candidates,
        max_substances=1,
        retriever_artifact={},
        searcher=_searcher,
    )
    _pool_run(run, db, candidates, evidence_id="KOSHA:SECTION-2")

    report = audit_candidate_pool_coverage(candidates, db, [run])

    assert report["status"] == "POOL_EXPANSION_REQUIRED"
    assert report["missing_unique_case_evidence_pair_count"] == len(QUERY_TEMPLATES)


def test_pool_audit_blocks_unknown_evidence(tmp_path: Path) -> None:
    db, candidates = _generate(tmp_path)
    run = tmp_path / "system.json"
    _pool_run(run, db, candidates, evidence_id="KOSHA:UNKNOWN")

    report = audit_candidate_pool_coverage(candidates, db, [run])

    assert report["status"] == "BLOCKED_POOL_AUDIT"
    assert {item["code"] for item in report["blockers"]} == {
        "POOL_RUN_UNKNOWN_EVIDENCE"
    }


def test_pool_audit_blocks_wrong_cas_evidence(tmp_path: Path) -> None:
    db, candidates = _generate(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO substance VALUES (?, ?, ?)",
            ("7732-18-5", "물", 1),
        )
        connection.execute(
            "INSERT INTO evidence VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "KOSHA:WATER-1",
                "KOSHA",
                "7732-18-5",
                "물 MSDS 1장 시험 항목",
                "물 1장 공식 시험 본문",
                "https://example.test/msds/water/1",
                "2026-01-01",
            ),
        )
    candidates.unlink()
    generate_qrel_candidate_pool(
        db,
        tmp_path / "retriever.joblib",
        candidates,
        max_substances=1,
        retriever_artifact={},
        searcher=_searcher,
    )
    run = tmp_path / "system.json"
    _pool_run(run, db, candidates, evidence_id="KOSHA:WATER-1")

    report = audit_candidate_pool_coverage(candidates, db, [run])

    assert report["status"] == "BLOCKED_POOL_AUDIT"
    assert {item["code"] for item in report["blockers"]} == {
        "POOL_RUN_WRONG_CAS_EVIDENCE"
    }


def test_pool_audit_blocks_changed_query(tmp_path: Path) -> None:
    db, candidates = _generate(tmp_path)
    run = tmp_path / "system.json"
    _pool_run(run, db, candidates)
    payload = json.loads(run.read_text(encoding="utf-8"))
    payload["results"][0]["query_sha256"] = "2" * 64
    run.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = audit_candidate_pool_coverage(candidates, db, [run])

    assert report["status"] == "BLOCKED_POOL_AUDIT"
    assert {item["code"] for item in report["blockers"]} == {"POOL_RUN_QUERY_MISMATCH"}


def test_pool_audit_blocks_different_candidate_artifact(tmp_path: Path) -> None:
    db, candidates = _generate(tmp_path)
    run = tmp_path / "system.json"
    _pool_run(run, db, candidates)
    payload = json.loads(run.read_text(encoding="utf-8"))
    payload["candidate_sha256"] = "3" * 64
    run.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = audit_candidate_pool_coverage(candidates, db, [run])

    assert report["status"] == "BLOCKED_POOL_AUDIT"
    assert {item["code"] for item in report["blockers"]} == {
        "POOL_RUN_CANDIDATE_MISMATCH"
    }


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
    batch = parser.parse_args(
        [
            "retriever-review",
            "batch",
            "--candidates",
            "candidates.jsonl",
            "--actor-role",
            "REVIEWER",
            "--actor-id",
            "reviewer-02",
            "--output-dir",
            "reviewer-batches",
        ]
    )
    assemble = parser.parse_args(
        [
            "retriever-review",
            "assemble",
            "--candidates",
            "candidates.jsonl",
            "--batch-dir",
            "reviewer-batches",
            "--output",
            "reviewer.csv",
        ]
    )

    assert generate.handler.__name__ == "_retriever_review"
    assert export.retriever_review_action == "export"
    assert batch.retriever_review_action == "batch"
    assert assemble.retriever_review_action == "assemble"


def test_audit_blank_review_sheet_reports_not_started(tmp_path: Path) -> None:
    _db, candidates = _generate(tmp_path)
    sheet = tmp_path / "labeler.csv"
    export_review_sheet(candidates, sheet, actor_role="LABELER", actor_id="labeler-01")

    report = audit_review_sheet(candidates, sheet, actor_role="LABELER")

    assert report["status"] == "NOT_STARTED"
    assert report["progress"]["valid_completed_case_count"] == 0
    assert report["progress"]["untouched_case_count"] == len(QUERY_TEMPLATES)
    assert report["is_performance_result"] is False


def test_audit_complete_review_sheet_is_ready_for_independent_merge(
    tmp_path: Path,
) -> None:
    _db, candidates = _generate(tmp_path)
    sheet = tmp_path / "labeler.csv"
    export_review_sheet(candidates, sheet, actor_role="LABELER", actor_id="labeler-01")
    _fill_sheet(sheet)

    report = audit_review_sheet(candidates, sheet, actor_role="LABELER")

    assert report["status"] == "READY_FOR_INDEPENDENT_MERGE"
    assert report["progress"]["valid_completed_case_count"] == len(QUERY_TEMPLATES)
    assert report["ready_for_independent_merge"] is True


def test_audit_changed_candidate_context_blocks_review_gate(tmp_path: Path) -> None:
    _db, candidates = _generate(tmp_path)
    sheet = tmp_path / "labeler.csv"
    export_review_sheet(candidates, sheet, actor_role="LABELER", actor_id="labeler-01")
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    rows[0]["body"] = "수정된 원문"
    with sheet.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    report = audit_review_sheet(candidates, sheet, actor_role="LABELER")

    assert report["status"] == "BLOCKED_REVIEW_GATE"
    assert report["blockers"] == [
        {"code": "CANDIDATE_CONTEXT_CHANGED", "evidence_row_count": 1}
    ]


def test_cli_exposes_review_status_action():
    args = build_parser().parse_args(
        [
            "retriever-review",
            "status",
            "--candidates",
            "candidates.jsonl",
            "--review-sheet",
            "sheet.csv",
            "--actor-role",
            "LABELER",
        ]
    )
    assert args.retriever_review_action == "status"


def test_cli_exposes_pool_audit_action():
    pool_audit = build_parser().parse_args(
        [
            "retriever-review",
            "pool-audit",
            "--candidates",
            "candidates.jsonl",
            "--db",
            "db.sqlite",
            "--system-run",
            "bm25.json",
        ]
    )
    pool_run = parser.parse_args(
        [
            "retriever-review",
            "pool-run",
            "--candidates",
            "candidates.jsonl",
            "--db",
            "db.sqlite",
            "--retriever-model",
            "retriever.joblib",
            "--system-id",
            "baseline",
            "--system-version",
            "v1",
            "--output",
            "pool-run.json",
        ]
    )

    assert pool_run.retriever_review_action == "pool-run"
    assert pool_audit.retriever_review_action == "pool-audit"
