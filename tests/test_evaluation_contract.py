from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from chemiguard119.evaluation_contract import (
    INTERNAL_CLAIM_SCOPE,
    EvaluationContractError,
    EvaluationProfile,
    audit_evaluation_dataset,
    evaluate_dataset_contract,
    load_evaluation_rows,
    require_evaluation_dataset,
)


FIELDNAMES = (
    "case_id",
    "query",
    "review_status",
    "source_type",
    "source_reference",
    "labeler_id",
    "reviewer_id",
    "expert_reviewed",
    "split",
    "duplicate_group",
)


def _row(case_id: str, **overrides: str) -> dict[str, str]:
    row = {
        "case_id": case_id,
        "query": f"{case_id} 질의",
        "review_status": "DOUBLE_REVIEWED_NON_EXPERT",
        "source_type": "CURATED_OFFICIAL_DOCUMENT_QUERY",
        "source_reference": "https://example.test/source",
        "labeler_id": f"labeler-{case_id}",
        "reviewer_id": f"reviewer-{case_id}",
        "expert_reviewed": "false",
        "split": "locked_test",
        "duplicate_group": f"group-{case_id}",
    }
    row.update(overrides)
    return row


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def _codes(report: dict, key: str = "blockers") -> set[str]:
    return {item["code"] for item in report[key]}


def test_internal_regression_allows_draft_but_limits_claim_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "internal.csv"
    _write_csv(
        path,
        [
            _row(
                "DRAFT-1",
                review_status="DRAFT_INTERNAL_REGRESSION",
                source_reference="",
                labeler_id="",
                reviewer_id="",
            )
        ],
    )

    report = audit_evaluation_dataset(path, EvaluationProfile.INTERNAL_REGRESSION)

    assert report["passed"] is True
    assert report["claim_scope"] == INTERNAL_CLAIM_SCOPE
    assert report["eligible_case_count"] == 1
    assert report["expert_reviewed"] is False
    assert {
        "MISSING_PROVENANCE",
        "DRAFT_ROWS_NOT_ALLOWED",
        "UNREVIEWED_STATUS",
    }.issubset(_codes(report, "warnings"))
    assert report["dataset_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "profile",
    [
        EvaluationProfile.COMPETITION_REVIEWED,
        EvaluationProfile.PILOT_REVIEWED,
    ],
)
def test_reviewed_profiles_block_draft_rows(
    tmp_path: Path,
    profile: EvaluationProfile,
) -> None:
    path = tmp_path / "draft.csv"
    _write_csv(path, [_row("DRAFT-1", review_status="DRAFT_INTERNAL_REGRESSION")])

    report = audit_evaluation_dataset(path, profile)

    assert report["passed"] is False
    assert report["claim_scope"] == INTERNAL_CLAIM_SCOPE
    assert report["eligible_case_count"] == 0
    assert "DRAFT_ROWS_NOT_ALLOWED" in _codes(report)
    assert "UNREVIEWED_STATUS" in _codes(report)


def test_double_reviewed_non_expert_can_pass_competition_profile(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reviewed.csv"
    _write_csv(path, [_row("CASE-1"), _row("CASE-2")])

    report = require_evaluation_dataset(
        path,
        EvaluationProfile.COMPETITION_REVIEWED,
    )

    assert report["passed"] is True
    assert report["claim_scope"] == "COMPETITION_REVIEWED"
    assert report["eligible_case_count"] == 2
    assert report["expert_reviewed"] is False
    assert report["blockers"] == []


def test_rows_can_be_reused_by_specialized_evaluators(tmp_path: Path) -> None:
    path = tmp_path / "reviewed.csv"
    rows = [_row("CASE-1")]
    _write_csv(path, rows)

    report = evaluate_dataset_contract(
        rows,
        EvaluationProfile.COMPETITION_REVIEWED,
        path,
    )

    assert report["passed"] is True
    assert report["dataset"] == str(path)
    assert report["dataset_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_all_rows_must_be_explicitly_expert_reviewed_for_expert_flag(
    tmp_path: Path,
) -> None:
    path = tmp_path / "expert.jsonl"
    rows = [
        _row("EXPERT-1", review_status="EXPERT_REVIEWED"),
        _row(
            "EXPERT-2",
            review_status="APPROVED",
            expert_reviewed="true",
        ),
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    report = audit_evaluation_dataset(path, EvaluationProfile.PILOT_REVIEWED)

    assert report["passed"] is True
    assert report["claim_scope"] == "PILOT_REVIEWED"
    assert report["expert_reviewed"] is True
    assert load_evaluation_rows(path) == rows


def test_reviewed_profile_blocks_missing_provenance_and_same_reviewer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bad-provenance.csv"
    _write_csv(
        path,
        [
            _row(
                "CASE-1",
                source_reference="",
                labeler_id="same-person",
                reviewer_id="same-person",
            )
        ],
    )

    report = audit_evaluation_dataset(path, "competition-reviewed")

    assert report["passed"] is False
    assert report["missing_provenance_count"] == 1
    assert report["missing_provenance"]["CASE-1"] == ["source_reference"]
    assert "MISSING_PROVENANCE" in _codes(report)
    assert "LABELER_REVIEWER_NOT_INDEPENDENT" in _codes(report)


def test_duplicate_case_id_and_duplicate_group_split_leakage_are_blocked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "leakage.csv"
    _write_csv(
        path,
        [
            _row("DUPLICATE", duplicate_group="shared", split="train"),
            _row("DUPLICATE", duplicate_group="shared", split="locked_test"),
        ],
    )

    report = audit_evaluation_dataset(path, EvaluationProfile.INTERNAL_REGRESSION)

    assert report["passed"] is False
    assert report["duplicate_case_ids"] == ["DUPLICATE"]
    assert report["split_leakage_groups"] == ["shared"]
    assert "DUPLICATE_CASE_ID" in _codes(report)
    assert "DUPLICATE_GROUP_SPLIT_LEAKAGE" in _codes(report)


def test_reviewed_profile_requires_locked_test_rows(tmp_path: Path) -> None:
    path = tmp_path / "development-split.csv"
    _write_csv(path, [_row("CASE-1", split="valid")])

    report = audit_evaluation_dataset(path, EvaluationProfile.PILOT_REVIEWED)

    assert report["passed"] is False
    assert report["split_counts"] == {"valid": 1}
    assert "NON_LOCKED_TEST_ROWS" in _codes(report)


def test_require_gate_raises_with_structured_report(tmp_path: Path) -> None:
    path = tmp_path / "draft.csv"
    _write_csv(path, [_row("CASE-1", review_status="DRAFT")])

    with pytest.raises(EvaluationContractError) as caught:
        require_evaluation_dataset(path, EvaluationProfile.PILOT_REVIEWED)

    assert caught.value.report["passed"] is False
    assert "DRAFT_ROWS_NOT_ALLOWED" in _codes(caught.value.report)
