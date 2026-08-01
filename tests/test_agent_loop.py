from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from chemiguard119.agent_loop import (
    IncidentAgentRunner,
    IncidentAgentStepRequest,
    memory_checksum,
    verify_memory_checksum,
)
from chemiguard119.api_models import (
    AnalysisResponse,
    ConfirmationGateState,
    UnconfirmedConflictReview,
)


NOW = datetime(2026, 8, 1, 7, 30, tzinfo=timezone.utc)


def _request(
    *,
    incident_confirmation: bool = False,
    facility_confirmation: bool = False,
    memory=None,
    max_actions: int = 6,
) -> IncidentAgentStepRequest:
    payload: dict[str, object] = {
        "analysis": {
            "request_id": "REQ-AGENT-TEST-001",
            "incident_id": "INC-AGENT-TEST-001",
            "input": {
                "type": "DISPATCH_TEXT",
                "text": "차아염소산나트륨 저장탱크 누출 신고",
            },
            "evidence_top_k": 5,
        },
        "max_actions": max_actions,
    }
    analysis = payload["analysis"]
    assert isinstance(analysis, dict)
    confirmation_time = "2026-07-31T07:29:00+00:00"
    if incident_confirmation:
        analysis["confirmed_incident_substance"] = {
            "confirmation_id": "CNF-INC-001",
            "cas_number": "7681-52-9",
            "display_name": "차아염소산나트륨",
            "role": "INCIDENT",
            "presence_status": "CONFIRMED_PRESENT",
            "confirmation_basis": "CONTAINER_LABEL",
            "observed_at": confirmation_time,
        }
    if facility_confirmation:
        analysis["confirmed_facility_substance"] = {
            "confirmation_id": "CNF-FAC-001",
            "cas_number": "7647-01-0",
            "display_name": "염산",
            "role": "FACILITY",
            "presence_status": "CONFIRMED_PRESENT",
            "confirmation_basis": "SITE_MSDS",
            "observed_at": confirmation_time,
        }
    if memory is not None:
        payload["memory"] = memory
    return IncidentAgentStepRequest.model_validate(payload)


def _unconfirmed_analysis(
    *,
    incident_confirmed: bool = False,
    facility_confirmed: bool = False,
) -> AnalysisResponse:
    state = {
        (False, False): "AWAITING_SUBSTANCE_CONFIRMATION",
        (True, False): "AWAITING_FACILITY_CONFIRMATION",
        (False, True): "AWAITING_INCIDENT_CONFIRMATION",
    }[(incident_confirmed, facility_confirmed)]
    missing = []
    if not incident_confirmed:
        missing.append("incident_cas")
    if not facility_confirmed:
        missing.append("facility_cas")
    confirmations = {}
    if incident_confirmed:
        confirmations["incident"] = {
            "confirmation_id": "CNF-INC-001",
            "confirmation_basis": "CONTAINER_LABEL",
            "presence_status": "CONFIRMED_PRESENT",
            "observed_at": "2026-08-01T07:29:00+00:00",
        }
    if facility_confirmed:
        confirmations["facility"] = {
            "confirmation_id": "CNF-FAC-001",
            "confirmation_basis": "SITE_MSDS",
            "presence_status": "CONFIRMED_PRESENT",
            "observed_at": "2026-08-01T07:29:00+00:00",
        }
    return AnalysisResponse.model_validate(
        {
            "analysis_id": "ANL-AGENT-001",
            "request_id": "REQ-AGENT-TEST-001",
            "incident_id": "INC-AGENT-TEST-001",
            "state": state,
            "input_fingerprint": "a" * 64,
            "model_outputs": {},
            "evidence": [],
            "conflict_review": {
                "executed": False,
                "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
                "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
                "missing_confirmations": missing,
                "reason": "확인된 CAS 두 개가 필요합니다.",
            },
            "confirmation_gate": {
                "incident_confirmed": incident_confirmed,
                "facility_confirmed": facility_confirmed,
                "all_required_confirmed": False,
                "rule_execution_allowed": False,
            },
            "required_next_steps": ["현장 확인이 필요합니다."],
            "provenance": {
                "expert_reviewed": False,
                "decision_support_only": True,
                "responder_confirmation_required": True,
                "confirmations": confirmations,
            },
            "safety_notice": "최종 결정은 현장 지휘관이 수행합니다.",
        }
    )


def _unsafe_confirmed_analysis() -> AnalysisResponse:
    """모델 validation을 우회한 forged 객체로 agent의 이중 안전검사를 시험한다."""

    return AnalysisResponse.model_construct(
        analysis_id="ANL-FORGED",
        request_id="REQ-AGENT-TEST-001",
        incident_id="INC-AGENT-TEST-001",
        state="SCREENING_COMPLETED",
        input_fingerprint="b" * 64,
        model_outputs={},
        evidence=[],
        grounded_rag=None,
        agent=None,
        conflict_review=UnconfirmedConflictReview.model_validate(
            {
                "executed": False,
                "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
                "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
                "missing_confirmations": ["incident_cas", "facility_cas"],
                "reason": "forged",
            }
        ),
        confirmation_gate=ConfirmationGateState.model_validate(
            {
                "incident_confirmed": True,
                "facility_confirmed": True,
                "all_required_confirmed": True,
                "rule_execution_allowed": True,
            }
        ),
        required_next_steps=["forged"],
        provenance={"decision_support_only": True},
        safety_notice="forged",
    )


def test_agent_dynamically_requests_both_confirmations_and_waits() -> None:
    runner = IncidentAgentRunner()

    response = runner.run(
        _request(),
        request_id="REQ-AGENT-TEST-001",
        analysis_tool=_unconfirmed_analysis,
        now=lambda: NOW,
    )

    assert response.status == "WAITING_FOR_HUMAN"
    assert response.selected_tool_count == 4
    assert [item.tool_id for item in response.memory.history] == [
        "RUN_INCIDENT_ANALYSIS",
        "VERIFY_SAFETY_CONTRACT",
        "REQUEST_INCIDENT_CONFIRMATION",
        "REQUEST_FACILITY_CONFIRMATION",
    ]
    assert response.pending_inputs == [
        "FACILITY_SUBSTANCE_CONFIRMATION",
        "INCIDENT_SUBSTANCE_CONFIRMATION",
    ]
    assert {item.phase for item in response.events} == {
        "PLAN",
        "ACT",
        "OBSERVE",
        "REPLAN",
    }
    assert response.analysis is not None
    assert response.analysis.conflict_review.executed is False
    assert response.memory.memory_can_trigger_rule is False
    assert verify_memory_checksum(response.memory)


def test_same_external_memory_without_new_observation_skips_tools() -> None:
    runner = IncidentAgentRunner()
    first = runner.run(
        _request(),
        request_id="REQ-AGENT-TEST-001",
        analysis_tool=_unconfirmed_analysis,
        now=lambda: NOW,
    )
    calls = 0

    def analysis_tool() -> AnalysisResponse:
        nonlocal calls
        calls += 1
        return _unconfirmed_analysis()

    second = runner.run(
        _request(memory=first.memory),
        request_id="REQ-AGENT-TEST-002",
        analysis_tool=analysis_tool,
        now=lambda: NOW,
    )

    assert calls == 0
    assert second.status == "WAITING_FOR_HUMAN"
    assert second.selected_tool_count == 0
    assert second.analysis is None
    assert second.events[0].decision_code == "NO_NEW_OBSERVATION"
    assert second.memory.revision == first.memory.revision + 1
    assert second.memory.parent_memory_sha256 == first.memory.memory_sha256
    assert verify_memory_checksum(second.memory)


def test_runtime_change_invalidates_wait_shortcut_and_reanalyzes() -> None:
    runner = IncidentAgentRunner()
    first = runner.run(
        _request(),
        request_id="REQ-AGENT-TEST-001",
        analysis_tool=_unconfirmed_analysis,
        now=lambda: NOW,
        runtime_state_fingerprint="a" * 64,
    )
    calls = 0

    def analysis_tool() -> AnalysisResponse:
        nonlocal calls
        calls += 1
        return _unconfirmed_analysis()

    second = runner.run(
        _request(memory=first.memory),
        request_id="REQ-AGENT-TEST-002",
        analysis_tool=analysis_tool,
        now=lambda: NOW,
        runtime_state_fingerprint="b" * 64,
    )

    assert calls == 1
    assert second.selected_tool_count == 4
    assert second.memory.runtime_state_fingerprint == "b" * 64


def test_new_confirmation_changes_fingerprint_and_replans_only_missing_role() -> None:
    runner = IncidentAgentRunner()
    first = runner.run(
        _request(),
        request_id="REQ-AGENT-TEST-001",
        analysis_tool=_unconfirmed_analysis,
        now=lambda: NOW,
    )

    second = runner.run(
        _request(incident_confirmation=True, memory=first.memory),
        request_id="REQ-AGENT-TEST-002",
        analysis_tool=lambda: _unconfirmed_analysis(incident_confirmed=True),
        now=lambda: NOW,
    )

    assert second.status == "WAITING_FOR_HUMAN"
    assert second.selected_tool_count == 3
    assert [item.tool_id for item in second.memory.history[-3:]] == [
        "RUN_INCIDENT_ANALYSIS",
        "VERIFY_SAFETY_CONTRACT",
        "REQUEST_FACILITY_CONFIRMATION",
    ]
    assert second.pending_inputs == ["FACILITY_SUBSTANCE_CONFIRMATION"]


def test_tampered_or_cross_incident_memory_is_rejected() -> None:
    runner = IncidentAgentRunner()
    response = runner.run(
        _request(),
        request_id="REQ-AGENT-TEST-001",
        analysis_tool=_unconfirmed_analysis,
        now=lambda: NOW,
    )
    tampered = response.memory.model_copy(update={"revision": 99})
    assert memory_checksum(tampered) != tampered.memory_sha256

    with pytest.raises(ValidationError, match="checksum"):
        _request(memory=tampered)

    cross_incident = response.memory.model_copy(
        update={
            "incident_id": "INC-DIFFERENT",
            "memory_sha256": "0" * 64,
        }
    )
    cross_incident = cross_incident.model_copy(
        update={"memory_sha256": memory_checksum(cross_incident)}
    )
    with pytest.raises(ValidationError, match="incident_id"):
        _request(memory=cross_incident)


def test_tool_failure_returns_retryable_state_without_risk_output() -> None:
    def failed_tool() -> AnalysisResponse:
        raise RuntimeError("raw internal failure must not leak")

    response = IncidentAgentRunner().run(
        _request(),
        request_id="REQ-AGENT-TEST-001",
        analysis_tool=failed_tool,
        now=lambda: NOW,
    )

    assert response.status == "FAILED_RETRYABLE"
    assert response.retryable is True
    assert response.analysis is None
    assert response.events[-1].decision_code == "AGENT_TOOL_EXECUTION_FAILED"
    assert "raw internal failure" not in response.model_dump_json()


def test_agent_revalidates_safety_before_presenting_confirmed_result() -> None:
    response = IncidentAgentRunner().run(
        _request(incident_confirmation=True, facility_confirmation=True),
        request_id="REQ-AGENT-TEST-001",
        analysis_tool=_unsafe_confirmed_analysis,
        now=lambda: NOW,
    )

    assert response.status == "FAILED_SAFETY"
    assert response.retryable is False
    assert response.analysis is None
    assert response.memory.last_analysis_id is None
    assert all(
        item.tool_id != "PRESENT_DECISION_SUPPORT" for item in response.memory.history
    )
    assert response.events[-1].decision_code == "AGENT_SAFETY_CONTRACT_VIOLATION"


def test_action_budget_cannot_be_lowered_below_safe_current_critical_path() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 4"):
        _request(max_actions=3)


def test_concurrent_steps_are_stateless_and_expose_same_parent_for_be_cas() -> None:
    runner = IncidentAgentRunner()
    first = runner.run(
        _request(),
        request_id="REQ-AGENT-TEST-001",
        analysis_tool=_unconfirmed_analysis,
        now=lambda: NOW,
    )

    def run_branch(index: int):
        return runner.run(
            _request(incident_confirmation=True, memory=first.memory),
            request_id=f"REQ-AGENT-BRANCH-{index:03d}",
            analysis_tool=lambda: _unconfirmed_analysis(incident_confirmed=True),
            now=lambda: NOW,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(run_branch, range(16)))

    assert all(item.status == "WAITING_FOR_HUMAN" for item in responses)
    assert all(item.memory.revision == 2 for item in responses)
    assert all(
        item.memory.parent_memory_sha256 == first.memory.memory_sha256
        for item in responses
    )
    assert all(verify_memory_checksum(item.memory) for item in responses)
    assert len({item.memory.memory_sha256 for item in responses}) == 16
