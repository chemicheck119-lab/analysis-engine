"""사고 상태를 외부 메모리로 이어가는 안전한 도구 실행 에이전트.

이 에이전트는 화학 위험을 자유 생성하지 않는다. 현재 요청과 이전 메모리를 보고
다음 도구를 선택하고, 실제 분석 결과를 관찰한 뒤 재계획한다. 위험 판정은 기존
``analyze_incident`` 경로의 현장 확인 gate와 CAMEO 규칙만 수행한다.

Cloud Run 다중 인스턴스에서 서버 메모리에 사고 상태를 저장하지 않는다. API가 반환한
``IncidentAgentMemory``를 BE가 사고 레코드와 함께 저장하고 다음 step 요청에 그대로
전달한다. 메모리는 작업 순서에만 사용하며 Rule 실행 권한으로 신뢰하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from chemiguard119.api_models import (
    IDENTIFIER_PATTERN,
    AnalysisResponse,
    IncidentAnalyzeRequest,
    analysis_state_for_review_status,
)
from chemiguard119.observability import emit_json_event


AGENT_SCHEMA_VERSION = "chemicheck119-incident-agent-v1"
AGENT_MEMORY_SCHEMA_VERSION = "chemicheck119-incident-agent-memory-v1"
MAX_MEMORY_EVENTS = 64
LOCAL_RUNTIME_STATE_FINGERPRINT = hashlib.sha256(
    b"chemicheck119-local-agent-runtime"
).hexdigest()

AgentToolId = Literal[
    "RUN_INCIDENT_ANALYSIS",
    "REQUEST_INCIDENT_CONFIRMATION",
    "REQUEST_FACILITY_CONFIRMATION",
    "VERIFY_SAFETY_CONTRACT",
    "REQUEST_OFFICIAL_EVIDENCE_REVIEW",
    "PRESENT_DECISION_SUPPORT",
]
AgentPendingInput = Literal[
    "INCIDENT_SUBSTANCE_CONFIRMATION",
    "FACILITY_SUBSTANCE_CONFIRMATION",
    "OFFICIAL_EVIDENCE_REVIEW",
]
AgentStepStatus = Literal[
    "GOAL_COMPLETED",
    "WAITING_FOR_HUMAN",
    "PARTIAL_MAX_ACTIONS",
    "FAILED_RETRYABLE",
    "FAILED_SAFETY",
]


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AgentMemoryEvent(AgentModel):
    revision: int = Field(ge=1)
    run_id: str = Field(min_length=1, max_length=80)
    tool_id: AgentToolId
    outcome: Literal["COMPLETED", "WAITING", "FAILED"]
    observation_code: str = Field(min_length=1, max_length=120)
    occurred_at: datetime


class IncidentAgentMemory(AgentModel):
    schema_version: Literal["chemicheck119-incident-agent-memory-v1"] = (
        AGENT_MEMORY_SCHEMA_VERSION
    )
    incident_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    revision: int = Field(ge=0, le=1_000_000)
    request_state_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_state_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AgentStepStatus
    pending_inputs: list[AgentPendingInput] = Field(default_factory=list, max_length=3)
    last_analysis_state: str | None = Field(default=None, max_length=80)
    last_analysis_id: str | None = Field(default=None, max_length=128)
    last_run_id: str = Field(min_length=1, max_length=80)
    history: list[AgentMemoryEvent] = Field(default_factory=list, max_length=64)
    parent_memory_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    memory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_trust_scope: Literal["ORCHESTRATION_ONLY"] = "ORCHESTRATION_ONLY"
    memory_can_trigger_rule: Literal[False] = False

    @model_validator(mode="after")
    def pending_inputs_must_be_unique(self) -> "IncidentAgentMemory":
        if len(self.pending_inputs) != len(set(self.pending_inputs)):
            raise ValueError("agent memory pending_inputs에는 중복을 넣을 수 없습니다.")
        return self


class IncidentAgentStepRequest(AgentModel):
    analysis: IncidentAnalyzeRequest
    memory: IncidentAgentMemory | None = None
    max_actions: int = Field(default=6, ge=4, le=8)

    @model_validator(mode="after")
    def incident_and_memory_must_match(self) -> "IncidentAgentStepRequest":
        incident_id = self.analysis.incident_id
        if not incident_id:
            raise ValueError("에이전트 step에는 analysis.incident_id가 필요합니다.")
        if self.memory is not None:
            if self.memory.incident_id != incident_id:
                raise ValueError("agent memory와 analysis의 incident_id가 다릅니다.")
            if not verify_memory_checksum(self.memory):
                raise ValueError("agent memory checksum이 일치하지 않습니다.")
        return self

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "analysis": {
                    "request_id": "REQ-AGENT-0001",
                    "incident_id": "INC-AGENT-0001",
                    "input": {
                        "type": "DISPATCH_TEXT",
                        "text": "차아염소산나트륨 저장탱크 누출 신고",
                    },
                    "location": {
                        "facility_name": "OO전자 공장",
                        "province": "경기도",
                    },
                    "evidence_top_k": 5,
                },
                "max_actions": 6,
            }
        },
    )


class AgentLoopEvent(AgentModel):
    sequence: int = Field(ge=1, le=128)
    phase: Literal["PLAN", "ACT", "OBSERVE", "REPLAN"]
    tool_id: AgentToolId | None = None
    status: Literal["PLANNED", "COMPLETED", "WAITING", "FAILED"]
    decision_code: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=500)
    output_reference: str | None = Field(default=None, max_length=160)
    occurred_at: datetime


class IncidentAgentStepResponse(AgentModel):
    schema_version: Literal["chemicheck119-incident-agent-v1"] = AGENT_SCHEMA_VERSION
    run_id: str = Field(min_length=1, max_length=80)
    request_id: str = Field(min_length=1, max_length=128)
    incident_id: str = Field(min_length=1, max_length=128)
    status: AgentStepStatus
    objective: Literal["공식근거 기반 화학사고 충돌 검토 준비"] = (
        "공식근거 기반 화학사고 충돌 검토 준비"
    )
    selected_tool_count: int = Field(ge=0, le=8)
    events: list[AgentLoopEvent] = Field(min_length=1, max_length=40)
    memory: IncidentAgentMemory
    analysis: AnalysisResponse | None = None
    pending_inputs: list[AgentPendingInput] = Field(default_factory=list, max_length=3)
    next_actions: list[str] = Field(min_length=1, max_length=8)
    retryable: bool
    tool_registry: list[AgentToolId] = Field(min_length=6, max_length=6)
    planning_mode: Literal["DETERMINISTIC_POLICY_PLANNER"] = (
        "DETERMINISTIC_POLICY_PLANNER"
    )
    memory_mode: Literal["BE_PERSISTED_EXTERNAL_MEMORY"] = (
        "BE_PERSISTED_EXTERNAL_MEMORY"
    )
    memory_can_trigger_rule: Literal[False] = False
    autonomous_risk_decision_allowed: Literal[False] = False
    trace_is_chain_of_thought: Literal[False] = False
    decision_support_only: Literal[True] = True
    final_decision_authority: Literal["현장 지휘관"] = "현장 지휘관"


class AgentSafetyViolation(RuntimeError):
    """에이전트 단계가 기존 확인·Rule 안전 계약과 충돌할 때 발생한다."""


@dataclass
class _RunState:
    analysis: AnalysisResponse | None = None
    requested_inputs: set[AgentPendingInput] = field(default_factory=set)
    safety_verified: bool = False
    presented: bool = False
    evidence_review_requested: bool = False
    failure_status: AgentStepStatus | None = None
    failure_code: str | None = None


TOOL_REGISTRY: tuple[AgentToolId, ...] = (
    "RUN_INCIDENT_ANALYSIS",
    "REQUEST_INCIDENT_CONFIRMATION",
    "REQUEST_FACILITY_CONFIRMATION",
    "VERIFY_SAFETY_CONTRACT",
    "REQUEST_OFFICIAL_EVIDENCE_REVIEW",
    "PRESENT_DECISION_SUPPORT",
)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def memory_checksum(memory: IncidentAgentMemory) -> str:
    payload = memory.model_dump(mode="json", exclude={"memory_sha256"})
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def verify_memory_checksum(memory: IncidentAgentMemory) -> bool:
    return memory.memory_sha256 == memory_checksum(memory)


def request_state_fingerprint(request: IncidentAnalyzeRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"request_id"})
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _new_run_id() -> str:
    return f"AGR-{uuid.uuid4().hex.upper()}"


def _pending_next_actions(pending: list[AgentPendingInput]) -> list[str]:
    messages = {
        "INCIDENT_SUBSTANCE_CONFIRMATION": (
            "용기 라벨·현장 MSDS 등으로 사고물질 CAS를 확인하고 같은 memory로 다시 호출하세요."
        ),
        "FACILITY_SUBSTANCE_CONFIRMATION": (
            "시설 과거 이력과 구분해 현장 시설물질 CAS를 확인하고 다시 호출하세요."
        ),
        "OFFICIAL_EVIDENCE_REVIEW": (
            "지원 범위 밖 조합은 추가 공식 MSDS·기관 근거를 확인한 뒤 다시 검토하세요."
        ),
    }
    return [messages[item] for item in pending] or [
        "반환된 memory를 사고 레코드에 저장하고 다음 현장 관찰과 함께 다시 호출하세요."
    ]


def _verify_analysis_safety(analysis: AnalysisResponse) -> None:
    gate = analysis.confirmation_gate
    review = analysis.conflict_review
    expected_all_confirmed = gate.incident_confirmed and gate.facility_confirmed
    if (
        gate.all_required_confirmed != expected_all_confirmed
        or gate.rule_execution_allowed != expected_all_confirmed
    ):
        raise AgentSafetyViolation("확인 gate 내부 상태가 서로 모순됩니다.")

    if expected_all_confirmed:
        if not review.executed:
            raise AgentSafetyViolation("확인 완료 상태에서 Rule 실행 결과가 없습니다.")
        if analysis.state != analysis_state_for_review_status(review.status):
            raise AgentSafetyViolation("Rule 결과와 분석 상태가 일치하지 않습니다.")
    elif review.executed:
        raise AgentSafetyViolation("확인 전 Rule 실행 결과가 포함됐습니다.")
    else:
        expected_state = {
            (False, False): "AWAITING_SUBSTANCE_CONFIRMATION",
            (True, False): "AWAITING_FACILITY_CONFIRMATION",
            (False, True): "AWAITING_INCIDENT_CONFIRMATION",
        }[(gate.incident_confirmed, gate.facility_confirmed)]
        if analysis.state != expected_state:
            raise AgentSafetyViolation(
                "미확인 역할과 분석 대기 상태가 일치하지 않습니다."
            )
    if analysis.provenance.get("decision_support_only") is not True:
        raise AgentSafetyViolation("의사결정 보조 provenance가 누락됐습니다.")
    if analysis.agent and analysis.agent.autonomous_risk_decision_allowed is not False:
        raise AgentSafetyViolation("자율 위험판정 금지 계약이 깨졌습니다.")


class IncidentAgentPlanner:
    """현재 관찰만으로 다음 허용 도구 하나를 선택하는 정책 planner."""

    def select(self, state: _RunState) -> tuple[AgentToolId, str] | None:
        if state.analysis is None:
            return "RUN_INCIDENT_ANALYSIS", "CURRENT_REQUEST_REQUIRES_FRESH_ANALYSIS"

        if not state.safety_verified:
            return "VERIFY_SAFETY_CONTRACT", "ANALYSIS_REQUIRES_SAFETY_REVALIDATION"

        gate = state.analysis.confirmation_gate
        if (
            not gate.incident_confirmed
            and "INCIDENT_SUBSTANCE_CONFIRMATION" not in state.requested_inputs
        ):
            return (
                "REQUEST_INCIDENT_CONFIRMATION",
                "INCIDENT_CAS_CONFIRMATION_MISSING",
            )
        if (
            not gate.facility_confirmed
            and "FACILITY_SUBSTANCE_CONFIRMATION" not in state.requested_inputs
        ):
            return (
                "REQUEST_FACILITY_CONFIRMATION",
                "FACILITY_CAS_CONFIRMATION_MISSING",
            )
        if not gate.all_required_confirmed:
            return None

        if state.analysis.state in {"COMPLETED", "SCREENING_COMPLETED"}:
            if not state.presented:
                return "PRESENT_DECISION_SUPPORT", "SCREENING_READY_FOR_PRESENTATION"
            return None

        if not state.evidence_review_requested:
            return (
                "REQUEST_OFFICIAL_EVIDENCE_REVIEW",
                "RULE_RESULT_INCONCLUSIVE_OR_UNSUPPORTED",
            )
        return None


class IncidentAgentRunner:
    """한 API 요청 안에서 제한된 PLAN→ACT→OBSERVE→REPLAN 루프를 실행한다."""

    def __init__(self, planner: IncidentAgentPlanner | None = None) -> None:
        self.planner = planner or IncidentAgentPlanner()

    def run(
        self,
        request: IncidentAgentStepRequest,
        *,
        request_id: str,
        analysis_tool: Callable[[], AnalysisResponse],
        now: Callable[[], datetime] | None = None,
        runtime_state_fingerprint: str = LOCAL_RUNTIME_STATE_FINGERPRINT,
    ) -> IncidentAgentStepResponse:
        clock = now or (lambda: datetime.now(timezone.utc))
        run_id = _new_run_id()
        incident_id = request.analysis.incident_id
        if incident_id is None:  # Pydantic validator 이후 타입 좁히기
            raise ValueError("incident_id가 필요합니다.")
        fingerprint = request_state_fingerprint(request.analysis)
        previous = request.memory

        if (
            previous is not None
            and previous.request_state_fingerprint == fingerprint
            and previous.runtime_state_fingerprint == runtime_state_fingerprint
            and previous.status == "WAITING_FOR_HUMAN"
            and previous.pending_inputs
        ):
            event = AgentLoopEvent(
                sequence=1,
                phase="PLAN",
                status="WAITING",
                decision_code="NO_NEW_OBSERVATION",
                summary="새 현장 관찰이 없어 이전 대기 입력을 유지합니다.",
                occurred_at=clock(),
            )
            memory = self._build_memory(
                previous=previous,
                incident_id=incident_id,
                fingerprint=fingerprint,
                runtime_state_fingerprint=runtime_state_fingerprint,
                run_id=run_id,
                status="WAITING_FOR_HUMAN",
                pending=list(previous.pending_inputs),
                analysis=None,
                new_history=[],
            )
            return IncidentAgentStepResponse(
                run_id=run_id,
                request_id=request_id,
                incident_id=incident_id,
                status="WAITING_FOR_HUMAN",
                selected_tool_count=0,
                events=[event],
                memory=memory,
                pending_inputs=list(previous.pending_inputs),
                next_actions=_pending_next_actions(list(previous.pending_inputs)),
                retryable=False,
                tool_registry=list(TOOL_REGISTRY),
            )

        state = _RunState()
        events: list[AgentLoopEvent] = []
        history: list[AgentMemoryEvent] = []
        selected_tool_count = 0

        for _ in range(request.max_actions):
            selection = self.planner.select(state)
            if selection is None:
                break
            tool_id, decision_code = selection
            selected_tool_count += 1
            events.append(
                AgentLoopEvent(
                    sequence=len(events) + 1,
                    phase="PLAN" if selected_tool_count == 1 else "REPLAN",
                    tool_id=tool_id,
                    status="PLANNED",
                    decision_code=decision_code,
                    summary="현재 상태와 도구 전제조건으로 다음 실행 도구를 선택했습니다.",
                    occurred_at=clock(),
                )
            )
            try:
                observation_code, output_reference, waiting = self._execute_tool(
                    tool_id,
                    state,
                    analysis_tool,
                )
                outcome: Literal["COMPLETED", "WAITING", "FAILED"] = (
                    "WAITING" if waiting else "COMPLETED"
                )
                events.append(
                    AgentLoopEvent(
                        sequence=len(events) + 1,
                        phase="ACT",
                        tool_id=tool_id,
                        status="WAITING" if waiting else "COMPLETED",
                        decision_code=f"{tool_id}_{outcome}",
                        summary="선택한 도구를 허용된 입력 범위에서 실행했습니다.",
                        output_reference=output_reference,
                        occurred_at=clock(),
                    )
                )
                events.append(
                    AgentLoopEvent(
                        sequence=len(events) + 1,
                        phase="OBSERVE",
                        tool_id=tool_id,
                        status="WAITING" if waiting else "COMPLETED",
                        decision_code=observation_code,
                        summary="도구의 구조화된 결과로 에이전트 상태를 갱신했습니다.",
                        output_reference=output_reference,
                        occurred_at=clock(),
                    )
                )
                history.append(
                    AgentMemoryEvent(
                        revision=(previous.revision if previous else 0) + 1,
                        run_id=run_id,
                        tool_id=tool_id,
                        outcome=outcome,
                        observation_code=observation_code,
                        occurred_at=clock(),
                    )
                )
            except AgentSafetyViolation:
                state.failure_status = "FAILED_SAFETY"
                state.failure_code = "AGENT_SAFETY_CONTRACT_VIOLATION"
                emit_json_event(
                    "incident_agent_safety_violation",
                    level=logging.ERROR,
                    request_id=request_id,
                    run_id=run_id,
                    tool_id=tool_id,
                    error_code=state.failure_code,
                )
                self._append_failure(
                    events, history, previous, run_id, tool_id, state, clock
                )
                break
            except Exception as error:
                state.failure_status = "FAILED_RETRYABLE"
                state.failure_code = "AGENT_TOOL_EXECUTION_FAILED"
                emit_json_event(
                    "incident_agent_tool_failed",
                    level=logging.ERROR,
                    request_id=request_id,
                    run_id=run_id,
                    tool_id=tool_id,
                    error_code=state.failure_code,
                    error_type=type(error).__name__,
                )
                self._append_failure(
                    events, history, previous, run_id, tool_id, state, clock
                )
                break

        pending = sorted(state.requested_inputs)
        status = self._status(state, pending, selected_tool_count, request.max_actions)
        if status == "GOAL_COMPLETED":
            next_actions = [
                "근거와 현장 상태를 함께 확인한 뒤 현장 지휘관이 최종 판단합니다.",
                "BE가 대화·확인·판정·근거를 하나의 대응 기록으로 저장합니다.",
            ]
        elif status.startswith("FAILED"):
            next_actions = [
                "위험 결과를 사용하지 말고 request_id로 운영 로그를 확인한 뒤 다시 실행하세요."
            ]
        elif status == "PARTIAL_MAX_ACTIONS":
            next_actions = [
                "반환된 memory를 저장하고 같은 최신 관찰로 다음 agent step을 호출하세요."
            ]
        else:
            next_actions = _pending_next_actions(pending)

        response_analysis = None if status == "FAILED_SAFETY" else state.analysis
        memory = self._build_memory(
            previous=previous,
            incident_id=incident_id,
            fingerprint=fingerprint,
            runtime_state_fingerprint=runtime_state_fingerprint,
            run_id=run_id,
            status=status,
            pending=pending,
            analysis=response_analysis,
            new_history=history,
        )
        return IncidentAgentStepResponse(
            run_id=run_id,
            request_id=request_id,
            incident_id=incident_id,
            status=status,
            selected_tool_count=selected_tool_count,
            events=events,
            memory=memory,
            analysis=response_analysis,
            pending_inputs=pending,
            next_actions=next_actions,
            retryable=status == "FAILED_RETRYABLE",
            tool_registry=list(TOOL_REGISTRY),
        )

    @staticmethod
    def _execute_tool(
        tool_id: AgentToolId,
        state: _RunState,
        analysis_tool: Callable[[], AnalysisResponse],
    ) -> tuple[str, str, bool]:
        if tool_id == "RUN_INCIDENT_ANALYSIS":
            analysis = analysis_tool()
            # FastAPI 내부 도구는 이미 ``AnalysisResponse`` 계약으로 반환한다. 여기서
            # 다시 Pydantic 검증에만 의존하지 않고, 뒤의 VERIFY_SAFETY_CONTRACT가
            # 확인 gate와 Rule 실행 상태를 독립적으로 재검증하게 한다.
            state.analysis = (
                analysis
                if isinstance(analysis, AnalysisResponse)
                else AnalysisResponse.model_validate(analysis)
            )
            return (
                f"ANALYSIS_STATE_{state.analysis.state}",
                "analysis",
                False,
            )
        if tool_id == "REQUEST_INCIDENT_CONFIRMATION":
            state.requested_inputs.add("INCIDENT_SUBSTANCE_CONFIRMATION")
            return (
                "WAITING_INCIDENT_SUBSTANCE_CONFIRMATION",
                "pending_inputs",
                True,
            )
        if tool_id == "REQUEST_FACILITY_CONFIRMATION":
            state.requested_inputs.add("FACILITY_SUBSTANCE_CONFIRMATION")
            return (
                "WAITING_FACILITY_SUBSTANCE_CONFIRMATION",
                "pending_inputs",
                True,
            )
        if tool_id == "VERIFY_SAFETY_CONTRACT":
            if state.analysis is None:
                raise AgentSafetyViolation("분석 없이 안전 계약을 검증할 수 없습니다.")
            _verify_analysis_safety(state.analysis)
            state.safety_verified = True
            return "SAFETY_CONTRACT_VERIFIED", "analysis.confirmation_gate", False
        if tool_id == "REQUEST_OFFICIAL_EVIDENCE_REVIEW":
            state.evidence_review_requested = True
            state.requested_inputs.add("OFFICIAL_EVIDENCE_REVIEW")
            return "WAITING_OFFICIAL_EVIDENCE_REVIEW", "pending_inputs", True
        if tool_id == "PRESENT_DECISION_SUPPORT":
            if state.analysis is None or not state.safety_verified:
                raise AgentSafetyViolation("안전 검증 전 결과를 제시할 수 없습니다.")
            state.presented = True
            return "DECISION_SUPPORT_READY", "analysis", False
        raise RuntimeError("등록되지 않은 agent tool")

    @staticmethod
    def _append_failure(
        events: list[AgentLoopEvent],
        history: list[AgentMemoryEvent],
        previous: IncidentAgentMemory | None,
        run_id: str,
        tool_id: AgentToolId,
        state: _RunState,
        clock: Callable[[], datetime],
    ) -> None:
        failure_code = state.failure_code or "AGENT_TOOL_EXECUTION_FAILED"
        events.append(
            AgentLoopEvent(
                sequence=len(events) + 1,
                phase="ACT",
                tool_id=tool_id,
                status="FAILED",
                decision_code=failure_code,
                summary="도구 실패를 안전한 구조화 상태로 변환하고 실행을 중단했습니다.",
                occurred_at=clock(),
            )
        )
        events.append(
            AgentLoopEvent(
                sequence=len(events) + 1,
                phase="OBSERVE",
                tool_id=tool_id,
                status="FAILED",
                decision_code=failure_code,
                summary="실패 이후 다른 도구나 위험 결과를 실행하지 않았습니다.",
                occurred_at=clock(),
            )
        )
        history.append(
            AgentMemoryEvent(
                revision=(previous.revision if previous else 0) + 1,
                run_id=run_id,
                tool_id=tool_id,
                outcome="FAILED",
                observation_code=failure_code,
                occurred_at=clock(),
            )
        )

    @staticmethod
    def _status(
        state: _RunState,
        pending: list[AgentPendingInput],
        selected_tool_count: int,
        max_actions: int,
    ) -> AgentStepStatus:
        if state.failure_status:
            return state.failure_status
        if pending:
            return "WAITING_FOR_HUMAN"
        if state.presented:
            return "GOAL_COMPLETED"
        if selected_tool_count >= max_actions:
            return "PARTIAL_MAX_ACTIONS"
        return "PARTIAL_MAX_ACTIONS"

    @staticmethod
    def _build_memory(
        *,
        previous: IncidentAgentMemory | None,
        incident_id: str,
        fingerprint: str,
        runtime_state_fingerprint: str,
        run_id: str,
        status: AgentStepStatus,
        pending: list[AgentPendingInput],
        analysis: AnalysisResponse | None,
        new_history: list[AgentMemoryEvent],
    ) -> IncidentAgentMemory:
        prior_history = list(previous.history) if previous else []
        history = (prior_history + new_history)[-MAX_MEMORY_EVENTS:]
        memory = IncidentAgentMemory(
            incident_id=incident_id,
            revision=(previous.revision if previous else 0) + 1,
            request_state_fingerprint=fingerprint,
            runtime_state_fingerprint=runtime_state_fingerprint,
            status=status,
            pending_inputs=pending,
            last_analysis_state=(
                analysis.state
                if analysis
                else (previous.last_analysis_state if previous else None)
            ),
            last_analysis_id=(
                analysis.analysis_id
                if analysis
                else (previous.last_analysis_id if previous else None)
            ),
            last_run_id=run_id,
            history=history,
            parent_memory_sha256=previous.memory_sha256 if previous else None,
            memory_sha256="0" * 64,
        )
        return memory.model_copy(update={"memory_sha256": memory_checksum(memory)})


__all__ = [
    "AGENT_MEMORY_SCHEMA_VERSION",
    "AGENT_SCHEMA_VERSION",
    "AgentLoopEvent",
    "AgentMemoryEvent",
    "AgentSafetyViolation",
    "IncidentAgentMemory",
    "IncidentAgentPlanner",
    "IncidentAgentRunner",
    "IncidentAgentStepRequest",
    "IncidentAgentStepResponse",
    "TOOL_REGISTRY",
    "memory_checksum",
    "request_state_fingerprint",
    "verify_memory_checksum",
]
