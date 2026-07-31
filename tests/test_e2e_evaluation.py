from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import chemiguard119.e2e_evaluation as evaluation_module
from chemiguard119.cli import build_parser
from chemiguard119.e2e_evaluation import evaluate_incident_scenarios
from chemiguard119.paths import EVALUATION_DIR


def _row(**overrides: object) -> dict[str, Any]:
    row: dict[str, Any] = {
        "case_id": "E2E-TEST-1",
        "review_status": "DRAFT_INTERNAL_REGRESSION",
        "source_type": "TEST_FIXTURE",
        "source_reference": "pipeline contract",
        "labeler_id": "labeler",
        "reviewer_id": "",
        "expert_reviewed": False,
        "split": "locked_safety_regression",
        "duplicate_group": "e2e-test-1",
        "capabilities": ["CONFIRMATION_GATE"],
        "input": {
            "raw_text": "염산 누출",
            "confirmed_incident_cas": None,
            "confirmed_facility_cas": None,
        },
        "expected": {
            "status": "NEEDS_SUBSTANCE_CONFIRMATION",
            "rule_executed": False,
            "rule_status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
            "missing_confirmations": ["incident_cas", "facility_cas"],
            "candidate_count": 0,
            "candidate_roles": [],
            "evidence_bases": {"UNKNOWN": "NO_CAS_HINT"},
            "output_validation_status": "PASSED",
            "expect_abstention": True,
        },
    }
    row.update(overrides)
    return row


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _safe_output() -> dict[str, Any]:
    return {
        "status": "NEEDS_SUBSTANCE_CONFIRMATION",
        "substance_candidates": [],
        "evidence": [{"role": "UNKNOWN", "cas_basis": "NO_CAS_HINT"}],
        "rule_review": {
            "executed": False,
            "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
            "missing_confirmations": ["incident_cas", "facility_cas"],
        },
        "output_validation": {"status": "PASSED", "errors": []},
    }


def _evaluate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]],
    analyzer: Any,
) -> dict[str, Any]:
    path = tmp_path / "e2e.jsonl"
    _write(path, rows)
    monkeypatch.setattr(
        evaluation_module, "validate_pipeline_output", lambda *_args: []
    )
    return evaluate_incident_scenarios(
        tmp_path / "db.sqlite",
        tmp_path / "resolver.joblib",
        tmp_path / "retriever.joblib",
        path,
        resolver_artifact={},
        retriever_artifact={},
        analyzer=analyzer,
    )


def test_e2e_evaluator_reports_safe_abstention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _evaluate(
        tmp_path,
        monkeypatch,
        [_row()],
        lambda *_args, **_kwargs: _safe_output(),
    )

    assert report["metrics_version"] == "incident-e2e-evaluation-v1"
    assert report["status"] == "COMPLETED"
    assert report["claim_scope"] == "INTERNAL_REGRESSION_ONLY"
    assert report["is_field_performance_estimate"] is False
    assert report["metrics"]["scenario_pass_rate"] == 1.0
    assert report["metrics"]["output_contract_pass_rate"] == 1.0
    assert report["metrics"]["unsafe_conflict_execution_count"] == 0
    assert report["metrics"]["unconfirmed_risk_exposure_count"] == 0
    assert report["metrics"]["expected_abstention_pass_rate"] == 1.0


def test_e2e_evaluator_detects_rule_execution_without_two_confirmations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe = _safe_output()
    unsafe["rule_review"] = {
        "executed": True,
        "status": "SCREENING_COMPLETED",
        "result": {
            "risk_level": "HIGH",
            "severity": "HIGH_RISK",
            "rule_id": "UNSAFE",
        },
    }
    report = _evaluate(
        tmp_path,
        monkeypatch,
        [_row()],
        lambda *_args, **_kwargs: unsafe,
    )

    assert report["metrics"]["scenario_pass_rate"] == 0.0
    assert report["status"] == "FAILED"
    assert report["metrics"]["unsafe_conflict_execution_count"] == 1
    assert report["metrics"]["unconfirmed_risk_exposure_count"] == 1
    assert (
        "UNSAFE_CONFLICT_EXECUTION_WITHOUT_TWO_CONFIRMED_CAS"
        in report["cases"][0]["failures"]
    )


def test_e2e_evaluator_rejects_unknown_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="지원하지 않는 capabilities"):
        _evaluate(
            tmp_path,
            monkeypatch,
            [_row(capabilities=["MAGIC_LLM"])],
            lambda *_args, **_kwargs: _safe_output(),
        )


def test_reviewed_profile_blocks_draft_e2e_rows(tmp_path: Path) -> None:
    path = tmp_path / "e2e.jsonl"
    _write(path, [_row()])

    with pytest.raises(ValueError, match="평가 데이터 계약 실패"):
        evaluate_incident_scenarios(
            tmp_path / "db.sqlite",
            tmp_path / "resolver.joblib",
            tmp_path / "retriever.joblib",
            path,
            profile="PILOT_REVIEWED",
            resolver_artifact={},
            retriever_artifact={},
            analyzer=lambda *_args, **_kwargs: _safe_output(),
        )


def test_repository_e2e_scenarios_have_supported_schema(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        json.loads(line)
        for line in (EVALUATION_DIR / "e2e_scenarios_draft.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]

    def analyzer(raw_text: str, **kwargs: object) -> dict[str, Any]:
        expected = next(
            row["expected"]
            for row in rows
            if row["input"]["raw_text"] == raw_text
            and row["input"].get("confirmed_incident_cas")
            == kwargs.get("confirmed_incident_cas")
            and row["input"].get("confirmed_facility_cas")
            == kwargs.get("confirmed_facility_cas")
        )
        return {
            "status": expected["status"],
            "substance_candidates": [
                {"role": role} for role in expected["candidate_roles"]
            ],
            "evidence": [
                {"role": role, "cas_basis": basis}
                for role, basis in expected["evidence_bases"].items()
            ],
            "rule_review": {
                "executed": expected["rule_executed"],
                "status": expected["rule_status"],
                "missing_confirmations": expected["missing_confirmations"],
                "result": {
                    "risk_level": expected.get("risk_level"),
                    "severity": expected.get("severity"),
                },
            },
            "output_validation": {
                "status": expected["output_validation_status"],
                "errors": [],
            },
        }

    monkeypatch.setattr(
        evaluation_module, "validate_pipeline_output", lambda *_args: []
    )
    report = evaluate_incident_scenarios(
        tmp_path / "db.sqlite",
        tmp_path / "resolver.joblib",
        tmp_path / "retriever.joblib",
        EVALUATION_DIR / "e2e_scenarios_draft.jsonl",
        resolver_artifact={},
        retriever_artifact={},
        analyzer=analyzer,
    )
    assert report["case_count"] == 8


def test_cli_exposes_e2e_evaluation_command() -> None:
    args = build_parser().parse_args(["evaluate-e2e"])

    assert args.command == "evaluate-e2e"
    assert args.evaluation == EVALUATION_DIR / "e2e_scenarios_draft.jsonl"
    assert args.handler.__name__ == "_evaluate_e2e"
