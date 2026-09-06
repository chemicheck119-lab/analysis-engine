from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import chemiguard119.agent_trajectory_evaluation as trajectory_module
from chemiguard119.agent_trajectory_evaluation import evaluate_agent_trajectories
from chemiguard119.paths import EVALUATION_DIR


DATASET = EVALUATION_DIR / "agent_trajectory_scenarios_draft.jsonl"


def _rows() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_repository_trajectory_dataset_passes_all_internal_gates(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"

    report = evaluate_agent_trajectories(DATASET, report_path=report_path)

    assert report["fact_status"] == "부분 구현 또는 개발용 데모"
    assert report["status"] == "COMPLETED"
    assert report["decision"] == "ADOPT_FOR_INTERNAL_REGRESSION_ONLY"
    assert report["claim_scope"] == "INTERNAL_REGRESSION_ONLY"
    assert report["case_count"] == 10
    assert report["step_count"] == 13
    assert report["passed_case_count"] == 10
    assert report["acceptance_gate"]["passed"] is True
    assert all(
        value == 0 for key, value in report["metrics"].items() if key.endswith("_count")
    )
    assert report_path.is_file()


def test_trajectory_report_is_deterministic_for_same_dataset() -> None:
    first = evaluate_agent_trajectories(DATASET)
    second = evaluate_agent_trajectories(DATASET)

    assert first == second


def test_trajectory_evaluator_detects_wrong_expected_tool_order(
    tmp_path: Path,
) -> None:
    rows = _rows()[:1]
    rows[0]["steps"][0]["expected"]["tool_sequence"] = [
        "VERIFY_SAFETY_CONTRACT",
        "RUN_INCIDENT_ANALYSIS",
    ]
    path = tmp_path / "wrong-order.jsonl"
    _write(path, rows)

    report = evaluate_agent_trajectories(path)

    assert report["status"] == "FAILED"
    assert report["decision"] == "REJECT_TRAJECTORY_POLICY"
    assert report["acceptance_gate"]["passed"] is False
    assert "tool_sequence:" in report["cases"][0]["steps"][0]["failures"][0]


def test_trajectory_evaluator_detects_cross_request_argument_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_rows()[5]]
    path = tmp_path / "argument-mismatch.jsonl"
    _write(path, rows)
    original = trajectory_module._analysis_tool

    def mismatched_tool(case_id: str, step: Any, request: Any) -> Any:
        analysis_tool = original(case_id, step, request)

        def run() -> Any:
            return analysis_tool().model_copy(update={"request_id": "REQ-WRONG"})

        return run

    monkeypatch.setattr(trajectory_module, "_analysis_tool", mismatched_tool)

    report = evaluate_agent_trajectories(path)

    assert report["status"] == "FAILED"
    assert report["metrics"]["tool_argument_contract_violation_count"] == 1
    assert "ANALYSIS_REQUEST_ID_MISMATCH" in report["cases"][0]["steps"][0]["failures"]


def test_trajectory_evaluator_blocks_unknown_capability(tmp_path: Path) -> None:
    rows = _rows()[:1]
    rows[0]["capabilities"] = ["AUTONOMOUS_RISK_DECISION"]
    path = tmp_path / "unknown-capability.jsonl"
    _write(path, rows)

    with pytest.raises(ValueError, match="지원하지 않는 capabilities"):
        evaluate_agent_trajectories(path)


def test_reviewed_profile_blocks_draft_trajectory_rows() -> None:
    with pytest.raises(ValueError, match="평가 데이터 계약 실패"):
        evaluate_agent_trajectories(DATASET, profile="PILOT_REVIEWED")


def test_first_step_cannot_claim_previous_memory(tmp_path: Path) -> None:
    rows = _rows()[:1]
    rows[0]["steps"][0]["reuse_previous_memory"] = True
    path = tmp_path / "invalid-memory.jsonl"
    _write(path, rows)

    with pytest.raises(
        ValueError, match="첫 step은 이전 memory를 재사용할 수 없습니다"
    ):
        evaluate_agent_trajectories(path)
