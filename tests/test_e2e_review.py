from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

import chemiguard119.e2e_review as review_module
from chemiguard119.cli import build_parser
from chemiguard119.e2e_review import (
    CANDIDATE_SCHEMA_VERSION,
    export_review_sheet,
    generate_review_candidate_pool,
    load_candidate_rows,
    merge_review_sheets,
    preflight_candidate_pool,
)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _candidate() -> dict[str, Any]:
    return {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "case_id": "E2E-REVIEW-1",
        "source_type": "TEST_FIXTURE",
        "source_reference": "https://example.test/source",
        "scenario_origin": "MECHANICAL_TEST_FIXTURE",
        "data_use_scope": "COMPETITION_REVIEW_CANDIDATE_ONLY",
        "duplicate_group": "review-1",
        "capabilities": ["CONFIRMATION_GATE"],
        "input": {
            "raw_text": "미상 물질 누출",
            "confirmed_incident_cas": None,
            "confirmed_facility_cas": None,
        },
    }


def _write_candidates(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fill_sheet(path: Path, **overrides: str) -> None:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    values = {
        "review_decision": "APPROVE",
        "status": "NEEDS_SUBSTANCE_CONFIRMATION",
        "rule_executed": "false",
        "rule_status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
        "missing_confirmations_json": '["incident_cas","facility_cas"]',
        "candidate_count": "0",
        "candidate_roles_json": "[]",
        "evidence_bases_json": '{"UNKNOWN":"NO_CAS_HINT"}',
        "output_validation_status": "PASSED",
        "risk_level": "",
        "severity": "",
        "expect_abstention": "true",
        "review_notes": "독립 검수 완료",
        **overrides,
    }
    for row in rows:
        row.update(values)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _pair_snapshot(path: Path) -> None:
    _write_json(
        path,
        {
            "evaluated_pair_count": 1,
            "pairs": [
                {
                    "cas_a": "64-17-5",
                    "cas_b": "67-64-1",
                    "evidence_urls": [
                        "https://cameochemicals.noaa.gov/chemical/667",
                        "https://cameochemicals.noaa.gov/chemical/8",
                        "https://cameochemicals.noaa.gov/reactivity",
                    ],
                }
            ],
        },
    )


def test_generate_candidate_pool_has_no_expected_labels(tmp_path: Path) -> None:
    snapshot = tmp_path / "pairs.json"
    output = tmp_path / "candidates.jsonl"
    _pair_snapshot(snapshot)

    report = generate_review_candidate_pool(snapshot, output)
    rows = load_candidate_rows(output)

    assert report["candidate_count"] == 8
    assert report["mechanical_pair_state_case_count"] == 3
    assert report["hard_case_count"] == 5
    assert all("expected" not in row for row in rows)
    assert {row["data_use_scope"] for row in rows} == {
        "COMPETITION_REVIEW_CANDIDATE_ONLY"
    }


def test_export_review_sheet_does_not_prefill_gold_labels(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    sheet = tmp_path / "labeler.csv"
    _write_candidates(candidates, [_candidate()])

    report = export_review_sheet(
        candidates,
        sheet,
        actor_role="LABELER",
        actor_id="labeler-01",
    )
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))

    assert report["case_count"] == 1
    assert row["actor_id"] == "labeler-01"
    assert row["raw_text"] == "미상 물질 누출"
    assert row["status"] == ""
    assert row["review_decision"] == ""


def test_merge_two_matching_independent_sheets_creates_reviewed_dataset(
    tmp_path: Path,
) -> None:
    candidates = tmp_path / "candidates.jsonl"
    labeler = tmp_path / "labeler.csv"
    reviewer = tmp_path / "reviewer.csv"
    output = tmp_path / "reviewed.jsonl"
    _write_candidates(candidates, [_candidate()])
    export_review_sheet(
        candidates, labeler, actor_role="LABELER", actor_id="labeler-01"
    )
    export_review_sheet(
        candidates, reviewer, actor_role="REVIEWER", actor_id="reviewer-02"
    )
    _fill_sheet(labeler)
    _fill_sheet(reviewer)

    report = merge_review_sheets(candidates, labeler, reviewer, output)
    merged = json.loads(output.read_text(encoding="utf-8").strip())

    assert report["status"] == "COMPLETED"
    assert report["independent_review"] is True
    assert report["evaluation_contract"]["passed"] is True
    assert report["evaluation_contract"]["claim_scope"] == "COMPETITION_REVIEWED"
    assert merged["review_status"] == "DOUBLE_REVIEWED_NON_EXPERT"
    assert merged["data_use_scope"] == "COMPETITION_REVIEWED_EVALUATION_ONLY"
    assert merged["labeler_id"] == "labeler-01"
    assert merged["reviewer_id"] == "reviewer-02"
    assert merged["split"] == "locked_test"


def test_merge_blocks_same_actor_and_label_disagreement(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    labeler = tmp_path / "labeler.csv"
    reviewer = tmp_path / "reviewer.csv"
    output = tmp_path / "reviewed.jsonl"
    _write_candidates(candidates, [_candidate()])
    export_review_sheet(
        candidates, labeler, actor_role="LABELER", actor_id="same-actor"
    )
    export_review_sheet(
        candidates, reviewer, actor_role="REVIEWER", actor_id="same-actor"
    )
    _fill_sheet(labeler)
    _fill_sheet(reviewer, status="NEEDS_INCIDENT_SUBSTANCE_CONFIRMATION")

    report = merge_review_sheets(candidates, labeler, reviewer, output)

    assert report["status"] == "BLOCKED_REVIEW_GATE"
    assert output.exists() is False
    assert {blocker["code"] for blocker in report["blockers"]} == {
        "REVIEWERS_NOT_INDEPENDENT"
    }


def test_merge_blocks_matching_but_unsafe_labels(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    labeler = tmp_path / "labeler.csv"
    reviewer = tmp_path / "reviewer.csv"
    output = tmp_path / "reviewed.jsonl"
    _write_candidates(candidates, [_candidate()])
    export_review_sheet(candidates, labeler, actor_role="LABELER", actor_id="label-1")
    export_review_sheet(
        candidates, reviewer, actor_role="REVIEWER", actor_id="review-2"
    )
    unsafe = {
        "rule_executed": "true",
        "rule_status": "SCREENING_COMPLETED",
        "risk_level": "HIGH",
        "severity": "HIGH_RISK",
        "expect_abstention": "false",
    }
    _fill_sheet(labeler, **unsafe)
    _fill_sheet(reviewer, **unsafe)

    report = merge_review_sheets(candidates, labeler, reviewer, output)

    assert report["status"] == "BLOCKED_REVIEW_GATE"
    assert output.exists() is False
    assert report["blockers"][0]["code"] == "INVALID_REVIEW_LABEL"
    assert "확인 CAS 두 개 없이" in report["blockers"][0]["errors"][0]["message"]


def test_merge_reports_independent_disagreement(tmp_path: Path) -> None:
    candidates = tmp_path / "candidates.jsonl"
    labeler = tmp_path / "labeler.csv"
    reviewer = tmp_path / "reviewer.csv"
    output = tmp_path / "reviewed.jsonl"
    output.write_text("previous-reviewed-output\n", encoding="utf-8")
    _write_candidates(candidates, [_candidate()])
    export_review_sheet(candidates, labeler, actor_role="LABELER", actor_id="label-1")
    export_review_sheet(
        candidates, reviewer, actor_role="REVIEWER", actor_id="review-2"
    )
    _fill_sheet(labeler)
    _fill_sheet(reviewer, status="NEEDS_INCIDENT_SUBSTANCE_CONFIRMATION")

    report = merge_review_sheets(candidates, labeler, reviewer, output)

    assert report["status"] == "BLOCKED_REVIEW_GATE"
    assert report["disagreement_count"] == 1
    assert report["blockers"][0]["code"] == "INDEPENDENT_REVIEW_DISAGREEMENT"
    assert report["blockers"][0]["cases"][0]["fields"] == ["status"]
    assert output.read_text(encoding="utf-8") == "previous-reviewed-output\n"


def test_preflight_is_not_accuracy_and_detects_unsafe_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidates = tmp_path / "candidates.jsonl"
    _write_candidates(candidates, [_candidate()])
    monkeypatch.setattr(review_module, "validate_pipeline_output", lambda *_args: [])

    def unsafe_analyzer(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {
            "status": "SCREENING_COMPLETED",
            "substance_candidates": [],
            "evidence": [],
            "rule_review": {
                "executed": True,
                "status": "SCREENING_COMPLETED",
                "result": {
                    "risk_level": "HIGH",
                    "severity": "HIGH_RISK",
                    "rule_id": "UNSAFE",
                },
            },
            "output_validation": {"status": "PASSED", "errors": []},
        }

    report = preflight_candidate_pool(
        candidates,
        tmp_path / "db.sqlite",
        tmp_path / "resolver.joblib",
        tmp_path / "retriever.joblib",
        resolver_artifact={},
        retriever_artifact={},
        analyzer=unsafe_analyzer,
    )

    assert report["status"] == "FAILED_SAFETY_PREFLIGHT"
    assert report["action"] == "PREFLIGHT_CANDIDATE_POOL"
    assert report["machine_observation_only"] is True
    assert report["is_accuracy_evaluation"] is False
    assert report["metrics"]["unsafe_conflict_execution_count"] == 1
    assert report["metrics"]["unconfirmed_risk_exposure_count"] == 1
    assert report["artifacts"]["database"] == {
        "file_name": "db.sqlite",
        "exists": False,
        "sha256": None,
        "size_bytes": None,
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["e2e-review", "generate"],
        [
            "e2e-review",
            "export",
            "--candidates",
            "candidates.jsonl",
            "--actor-role",
            "LABELER",
            "--actor-id",
            "labeler-01",
            "--output",
            "labeler.csv",
        ],
        [
            "e2e-review",
            "merge",
            "--candidates",
            "candidates.jsonl",
            "--labeler-sheet",
            "labeler.csv",
            "--reviewer-sheet",
            "reviewer.csv",
            "--output",
            "reviewed.jsonl",
        ],
        [
            "e2e-review",
            "preflight",
            "--candidates",
            "candidates.jsonl",
        ],
    ],
)
def test_cli_exposes_e2e_review_actions(argv: list[str]) -> None:
    args = build_parser().parse_args(argv)

    assert args.command == "e2e-review"
    assert callable(args.handler)
