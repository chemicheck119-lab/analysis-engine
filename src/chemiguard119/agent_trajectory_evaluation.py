"""결정적 Agent 상태머신의 도구 순서와 실패 중단을 평가한다.

이 평가기는 화학적 위험 정답을 만들지 않는다. 사전에 검증됐다고 가정한 합성
``AnalysisResponse`` 관찰을 Agent에 주입하고, 확인 요청·안전 재검증·재시도·중단
정책이 예상한 trajectory를 따르는지만 검사한다. 따라서 결과는 내부 회귀 근거이며
현장 정확도나 실제 안전성 추정치가 아니다.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from chemiguard119.agent_loop import (
    AGENT_MEMORY_SCHEMA_VERSION,
    AGENT_SCHEMA_VERSION,
    IncidentAgentMemory,
    IncidentAgentRunner,
    IncidentAgentStepRequest,
    TOOL_REGISTRY,
    request_state_fingerprint,
    verify_memory_checksum,
)
from chemiguard119.api_models import (
    AnalysisResponse,
    ConfirmationGateState,
    ExecutedConflictReview,
    UnconfirmedConflictReview,
)
from chemiguard119.evaluation_contract import (
    EvaluationProfile,
    evaluate_dataset_contract,
    load_evaluation_rows,
)
from chemiguard119.utils import sha256_file, write_json


AGENT_TRAJECTORY_METRICS_VERSION = "agent-trajectory-evaluation-v1"
AGENT_TRAJECTORY_REPORT_SCHEMA_VERSION = (
    "chemicheck119-agent-trajectory-evaluation-report-v1"
)
SUPPORTED_FIXTURES = frozenset(
    {
        "SAFE_UNCONFIRMED",
        "SCREENING_COMPLETED",
        "UNCLASSIFIED",
        "TOOL_TIMEOUT",
        "FORGED_UNSAFE_CONFIRMED",
    }
)
SUPPORTED_CAPABILITIES = frozenset(
    {
        "TOOL_SELECTION",
        "TOOL_ORDER",
        "RETRY_CONDITION",
        "STOP_CONDITION",
        "TWO_CAS_GATE",
        "MEMORY_REUSE",
        "MEMORY_INVALIDATION",
        "OFFICIAL_EVIDENCE_ESCALATION",
        "ARGUMENT_CONTRACT",
    }
)
FIXTURE_TIME = datetime(2026, 8, 1, 7, 30, tzinfo=timezone.utc)
RUNTIME_A = "a" * 64
SUPPORTED_STATUSES = frozenset(
    {
        "GOAL_COMPLETED",
        "WAITING_FOR_HUMAN",
        "PARTIAL_MAX_ACTIONS",
        "FAILED_RETRYABLE",
        "FAILED_SAFETY",
    }
)
SUPPORTED_PENDING_INPUTS = frozenset(
    {
        "INCIDENT_SUBSTANCE_CONFIRMATION",
        "FACILITY_SUBSTANCE_CONFIRMATION",
        "OFFICIAL_EVIDENCE_REVIEW",
    }
)


def _require_string(value: object, label: str, case_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{case_id}: {label}는 비어 있지 않은 문자열이어야 합니다.")
    return value.strip()


def _validate_rows(rows: list[Mapping[str, Any]]) -> None:
    for index, row in enumerate(rows, 1):
        case_id = _require_string(row.get("case_id"), "case_id", f"<row:{index}>")
        capabilities = row.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities:
            raise ValueError(
                f"{case_id}: capabilities는 비어 있지 않은 배열이어야 합니다."
            )
        unknown_capabilities = {
            str(item)
            for item in capabilities
            if not isinstance(item, str) or item not in SUPPORTED_CAPABILITIES
        }
        if unknown_capabilities:
            raise ValueError(
                f"{case_id}: 지원하지 않는 capabilities={sorted(unknown_capabilities)}"
            )

        steps = row.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"{case_id}: steps는 비어 있지 않은 배열이어야 합니다.")
        for step_index, step in enumerate(steps, 1):
            step_label = f"{case_id}/step:{step_index}"
            if not isinstance(step, Mapping):
                raise ValueError(f"{step_label}: step 객체가 필요합니다.")
            fixture = _require_string(
                step.get("analysis_fixture"), "analysis_fixture", step_label
            )
            if fixture not in SUPPORTED_FIXTURES:
                raise ValueError(f"{step_label}: 지원하지 않는 fixture={fixture}")
            for field in (
                "incident_confirmed",
                "facility_confirmed",
                "reuse_previous_memory",
            ):
                if not isinstance(step.get(field), bool):
                    raise ValueError(f"{step_label}: {field}는 boolean이어야 합니다.")
            runtime = step.get("runtime_state_fingerprint", RUNTIME_A)
            if not isinstance(runtime, str) or len(runtime) != 64:
                raise ValueError(
                    f"{step_label}: runtime_state_fingerprint는 64자리여야 합니다."
                )
            expected = step.get("expected")
            if not isinstance(expected, Mapping):
                raise ValueError(f"{step_label}: expected 객체가 필요합니다.")
            for field in (
                "status",
                "tool_sequence",
                "selected_tool_count",
                "retryable",
                "pending_inputs",
                "analysis_exposed",
            ):
                if field not in expected:
                    raise ValueError(f"{step_label}: expected.{field}가 필요합니다.")
            for field in ("tool_sequence", "pending_inputs"):
                if not isinstance(expected[field], list) or any(
                    not isinstance(item, str) for item in expected[field]
                ):
                    raise ValueError(
                        f"{step_label}: expected.{field}는 문자열 배열이어야 합니다."
                    )
            if expected["status"] not in SUPPORTED_STATUSES:
                raise ValueError(
                    f"{step_label}: 지원하지 않는 expected.status={expected['status']!r}"
                )
            if (
                not isinstance(expected["selected_tool_count"], int)
                or isinstance(expected["selected_tool_count"], bool)
                or expected["selected_tool_count"] < 0
            ):
                raise ValueError(
                    f"{step_label}: expected.selected_tool_count는 0 이상의 정수여야 합니다."
                )
            for field in ("retryable", "analysis_exposed"):
                if not isinstance(expected[field], bool):
                    raise ValueError(
                        f"{step_label}: expected.{field}는 boolean이어야 합니다."
                    )
            unknown_tools = set(expected["tool_sequence"]) - set(TOOL_REGISTRY)
            if unknown_tools:
                raise ValueError(
                    f"{step_label}: 지원하지 않는 expected.tool_sequence={sorted(unknown_tools)}"
                )
            unknown_pending = set(expected["pending_inputs"]) - SUPPORTED_PENDING_INPUTS
            if unknown_pending:
                raise ValueError(
                    f"{step_label}: 지원하지 않는 expected.pending_inputs={sorted(unknown_pending)}"
                )
            if step_index == 1 and step["reuse_previous_memory"]:
                raise ValueError(
                    f"{step_label}: 첫 step은 이전 memory를 재사용할 수 없습니다."
                )
            both_confirmed = bool(
                step["incident_confirmed"] and step["facility_confirmed"]
            )
            if fixture == "SAFE_UNCONFIRMED" and both_confirmed:
                raise ValueError(
                    f"{step_label}: SAFE_UNCONFIRMED에는 미확인 역할이 필요합니다."
                )
            if (
                fixture
                in {
                    "SCREENING_COMPLETED",
                    "UNCLASSIFIED",
                    "FORGED_UNSAFE_CONFIRMED",
                }
                and not both_confirmed
            ):
                raise ValueError(
                    f"{step_label}: {fixture}에는 두 CAS 확인이 필요합니다."
                )


def _confirmation(role: str, case_id: str) -> dict[str, Any]:
    incident = role == "INCIDENT"
    return {
        "confirmation_id": f"CNF-{'INC' if incident else 'FAC'}-{case_id}",
        "cas_number": "7681-52-9" if incident else "7647-01-0",
        "display_name": "차아염소산나트륨" if incident else "염산",
        "role": role,
        "presence_status": "CONFIRMED_PRESENT",
        "confirmation_basis": "CONTAINER_LABEL" if incident else "SITE_MSDS",
        "observed_at": FIXTURE_TIME.isoformat(),
    }


def _request(
    case_id: str,
    step_index: int,
    step: Mapping[str, Any],
    memory: IncidentAgentMemory | None,
) -> IncidentAgentStepRequest:
    incident_confirmed = bool(step["incident_confirmed"])
    facility_confirmed = bool(step["facility_confirmed"])
    analysis: dict[str, Any] = {
        "request_id": f"REQ-{case_id}-{step_index:02d}",
        "incident_id": f"INC-{case_id}",
        "input": {
            "type": "DISPATCH_TEXT",
            "text": "합성 평가용 화학물질 신고",
        },
        "evidence_top_k": 5,
    }
    if incident_confirmed:
        analysis["confirmed_incident_substance"] = _confirmation("INCIDENT", case_id)
    if facility_confirmed:
        analysis["confirmed_facility_substance"] = _confirmation("FACILITY", case_id)
    payload: dict[str, Any] = {"analysis": analysis, "max_actions": 6}
    if memory is not None:
        payload["memory"] = memory
    return IncidentAgentStepRequest.model_validate(payload)


def _provenance(incident_confirmed: bool, facility_confirmed: bool) -> dict[str, Any]:
    confirmations: dict[str, Any] = {}
    if incident_confirmed:
        confirmations["incident"] = {
            "confirmation_id": "CNF-INC-FIXTURE",
            "confirmation_basis": "CONTAINER_LABEL",
            "presence_status": "CONFIRMED_PRESENT",
            "observed_at": FIXTURE_TIME.isoformat(),
        }
    if facility_confirmed:
        confirmations["facility"] = {
            "confirmation_id": "CNF-FAC-FIXTURE",
            "confirmation_basis": "SITE_MSDS",
            "presence_status": "CONFIRMED_PRESENT",
            "observed_at": FIXTURE_TIME.isoformat(),
        }
    return {
        "expert_reviewed": False,
        "decision_support_only": True,
        "responder_confirmation_required": True,
        "confirmations": confirmations,
    }


def _safe_unconfirmed_analysis(
    case_id: str,
    request_id: str,
    incident_id: str,
    incident_confirmed: bool,
    facility_confirmed: bool,
) -> AnalysisResponse:
    if incident_confirmed and facility_confirmed:
        raise ValueError(
            "SAFE_UNCONFIRMED fixture에는 확인되지 않은 역할이 필요합니다."
        )
    missing: list[str] = []
    if not incident_confirmed:
        missing.append("incident_cas")
    if not facility_confirmed:
        missing.append("facility_cas")
    state = {
        (False, False): "AWAITING_SUBSTANCE_CONFIRMATION",
        (True, False): "AWAITING_FACILITY_CONFIRMATION",
        (False, True): "AWAITING_INCIDENT_CONFIRMATION",
    }[(incident_confirmed, facility_confirmed)]
    return AnalysisResponse.model_validate(
        {
            "analysis_id": f"ANL-{case_id}",
            "request_id": request_id,
            "incident_id": incident_id,
            "state": state,
            "input_fingerprint": "1" * 64,
            "model_outputs": {},
            "evidence": [],
            "conflict_review": {
                "executed": False,
                "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
                "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
                "missing_confirmations": missing,
                "reason": "합성 trajectory fixture의 미확인 상태입니다.",
            },
            "confirmation_gate": {
                "incident_confirmed": incident_confirmed,
                "facility_confirmed": facility_confirmed,
                "all_required_confirmed": False,
                "rule_execution_allowed": False,
            },
            "required_next_steps": ["현장 확인이 필요합니다."],
            "provenance": _provenance(incident_confirmed, facility_confirmed),
            "safety_notice": "최종 결정은 현장 지휘관이 수행합니다.",
        }
    )


def _executed_analysis(
    case_id: str,
    request_id: str,
    incident_id: str,
    status: str,
) -> AnalysisResponse:
    review = ExecutedConflictReview.model_construct(
        executed=True,
        status=status,
        gate="BOTH_CAS_RESPONDER_CONFIRMED",
        policy_mode="PUBLIC_SOURCE_PILOT_V1",
        result={"status": status},
    )
    return AnalysisResponse.model_construct(
        schema_version="chemiguard119-api-v1",
        analysis_id=f"ANL-{case_id}",
        request_id=request_id,
        incident_id=incident_id,
        state="SCREENING_COMPLETED" if status == "SCREENING_COMPLETED" else status,
        input_fingerprint="2" * 64,
        model_outputs={},
        evidence=[],
        grounded_rag=None,
        agent=None,
        conflict_review=review,
        confirmation_gate=ConfirmationGateState.model_validate(
            {
                "incident_confirmed": True,
                "facility_confirmed": True,
                "all_required_confirmed": True,
                "rule_execution_allowed": True,
            }
        ),
        required_next_steps=["공식 근거를 검토합니다."],
        provenance=_provenance(True, True),
        safety_notice="최종 결정은 현장 지휘관이 수행합니다.",
    )


def _forged_unsafe_analysis(
    case_id: str,
    request_id: str,
    incident_id: str,
) -> AnalysisResponse:
    return AnalysisResponse.model_construct(
        schema_version="chemiguard119-api-v1",
        analysis_id=f"ANL-{case_id}",
        request_id=request_id,
        incident_id=incident_id,
        state="SCREENING_COMPLETED",
        input_fingerprint="3" * 64,
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
                "reason": "검증 실패를 유도하는 합성 fixture입니다.",
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
        required_next_steps=["표시되면 안 됩니다."],
        provenance=_provenance(True, True),
        safety_notice="검증 실패 fixture",
    )


def _analysis_tool(
    case_id: str,
    step: Mapping[str, Any],
    request: IncidentAgentStepRequest,
) -> Callable[[], AnalysisResponse]:
    fixture = str(step["analysis_fixture"])
    incident_confirmed = bool(step["incident_confirmed"])
    facility_confirmed = bool(step["facility_confirmed"])
    request_id = str(request.analysis.request_id)
    incident_id = str(request.analysis.incident_id)

    if fixture == "TOOL_TIMEOUT":

        def timeout() -> AnalysisResponse:
            raise TimeoutError("synthetic-private-timeout-detail")

        return timeout
    if fixture == "FORGED_UNSAFE_CONFIRMED":
        return lambda: _forged_unsafe_analysis(case_id, request_id, incident_id)
    if fixture == "SCREENING_COMPLETED":
        return lambda: _executed_analysis(
            case_id, request_id, incident_id, "SCREENING_COMPLETED"
        )
    if fixture == "UNCLASSIFIED":
        return lambda: _executed_analysis(
            case_id, request_id, incident_id, "UNCLASSIFIED"
        )
    return lambda: _safe_unconfirmed_analysis(
        case_id,
        request_id,
        incident_id,
        incident_confirmed,
        facility_confirmed,
    )


def _planned_tool_sequence(response: Any) -> list[str]:
    return [
        str(event.tool_id)
        for event in response.events
        if event.phase in {"PLAN", "REPLAN"} and event.tool_id is not None
    ]


def _steps_after_failure(response: Any) -> int:
    first_failure = next(
        (
            index
            for index, event in enumerate(response.events)
            if event.status == "FAILED"
        ),
        None,
    )
    if first_failure is None:
        return 0
    return sum(
        event.phase in {"PLAN", "REPLAN"}
        for event in response.events[first_failure + 1 :]
    )


def _compare(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    for field in (
        "status",
        "tool_sequence",
        "selected_tool_count",
        "retryable",
        "pending_inputs",
        "analysis_exposed",
    ):
        if actual.get(field) != expected.get(field):
            failures.append(
                f"{field}: expected={expected.get(field)!r}, actual={actual.get(field)!r}"
            )
    return failures


def _argument_contract_failures(
    request: IncidentAgentStepRequest, response: Any
) -> list[str]:
    failures: list[str] = []
    expected_request_id = request.analysis.request_id
    expected_incident_id = request.analysis.incident_id
    expected_fingerprint = request_state_fingerprint(request.analysis)
    if response.request_id != expected_request_id:
        failures.append("RESPONSE_REQUEST_ID_MISMATCH")
    if response.incident_id != expected_incident_id:
        failures.append("RESPONSE_INCIDENT_ID_MISMATCH")
    if response.memory.incident_id != expected_incident_id:
        failures.append("MEMORY_INCIDENT_ID_MISMATCH")
    if response.memory.request_state_fingerprint != expected_fingerprint:
        failures.append("MEMORY_REQUEST_FINGERPRINT_MISMATCH")
    if response.analysis is not None:
        if response.analysis.request_id != expected_request_id:
            failures.append("ANALYSIS_REQUEST_ID_MISMATCH")
        if response.analysis.incident_id != expected_incident_id:
            failures.append("ANALYSIS_INCIDENT_ID_MISMATCH")
    return failures


def evaluate_agent_trajectories(
    evaluation_path: Path,
    *,
    profile: EvaluationProfile | str = EvaluationProfile.INTERNAL_REGRESSION,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """합성 관찰에 대한 Agent trajectory와 안전한 중단을 집계한다."""

    evaluation_path = Path(evaluation_path)
    rows = load_evaluation_rows(evaluation_path)
    contract = evaluate_dataset_contract(rows, profile, evaluation_path)
    if not contract["passed"]:
        codes = ", ".join(item["code"] for item in contract["blockers"])
        raise ValueError(f"평가 데이터 계약 실패: {codes}")
    _validate_rows(rows)

    case_reports: list[dict[str, Any]] = []
    capability_totals: Counter[str] = Counter()
    capability_passes: Counter[str] = Counter()
    step_count = 0
    memory_checksum_failure_count = 0
    tool_after_failure_count = 0
    presentation_before_two_confirmations_count = 0
    failure_analysis_exposure_count = 0
    internal_error_leak_count = 0
    tool_argument_contract_violation_count = 0

    for row in rows:
        case_id = str(row["case_id"])
        capabilities = [str(item) for item in row["capabilities"]]
        previous_memory: IncidentAgentMemory | None = None
        step_reports: list[dict[str, Any]] = []
        for index, raw_step in enumerate(row["steps"], 1):
            step = dict(raw_step)
            step_count += 1
            memory = previous_memory if step["reuse_previous_memory"] else None
            request = _request(case_id, index, step, memory)
            response = IncidentAgentRunner().run(
                request,
                request_id=f"REQ-{case_id}-{index:02d}",
                analysis_tool=_analysis_tool(case_id, step, request),
                now=lambda: FIXTURE_TIME,
                runtime_state_fingerprint=str(
                    step.get("runtime_state_fingerprint", RUNTIME_A)
                ),
            )
            previous_memory = response.memory
            tool_sequence = _planned_tool_sequence(response)
            both_confirmed = bool(
                step["incident_confirmed"] and step["facility_confirmed"]
            )
            presented_without_gate = (
                "PRESENT_DECISION_SUPPORT" in tool_sequence and not both_confirmed
            )
            if presented_without_gate:
                presentation_before_two_confirmations_count += 1
            steps_after_failure = _steps_after_failure(response)
            tool_after_failure_count += steps_after_failure
            memory_valid = verify_memory_checksum(response.memory)
            if not memory_valid:
                memory_checksum_failure_count += 1
            failed = response.status in {"FAILED_RETRYABLE", "FAILED_SAFETY"}
            analysis_exposed = response.analysis is not None
            if failed and analysis_exposed:
                failure_analysis_exposure_count += 1
            serialized = response.model_dump_json()
            error_leaked = "synthetic-private-timeout-detail" in serialized
            if error_leaked:
                internal_error_leak_count += 1
            argument_failures = _argument_contract_failures(request, response)
            tool_argument_contract_violation_count += len(argument_failures)

            actual = {
                "status": response.status,
                "tool_sequence": tool_sequence,
                "selected_tool_count": response.selected_tool_count,
                "retryable": response.retryable,
                "pending_inputs": list(response.pending_inputs),
                "analysis_exposed": analysis_exposed,
            }
            failures = _compare(dict(step["expected"]), actual)
            if not memory_valid:
                failures.append("MEMORY_CHECKSUM_INVALID")
            if presented_without_gate:
                failures.append("PRESENTED_BEFORE_TWO_CONFIRMATIONS")
            if steps_after_failure:
                failures.append("TOOL_PLANNED_AFTER_FAILURE")
            if failed and analysis_exposed:
                failures.append("FAILED_ANALYSIS_EXPOSED")
            if error_leaked:
                failures.append("INTERNAL_ERROR_DETAIL_LEAKED")
            failures.extend(argument_failures)
            step_reports.append(
                {
                    "step": index,
                    "passed": not failures,
                    "actual": actual,
                    "memory_checksum_valid": memory_valid,
                    "tool_after_failure_count": steps_after_failure,
                    "failures": failures,
                }
            )

        case_passed = all(step["passed"] for step in step_reports)
        for capability in capabilities:
            capability_totals[capability] += 1
            if case_passed:
                capability_passes[capability] += 1
        case_reports.append(
            {
                "case_id": case_id,
                "passed": case_passed,
                "capabilities": capabilities,
                "steps": step_reports,
            }
        )

    case_count = len(case_reports)
    passed_case_count = sum(bool(case["passed"]) for case in case_reports)
    report = {
        "schema_version": AGENT_TRAJECTORY_REPORT_SCHEMA_VERSION,
        "metrics_version": AGENT_TRAJECTORY_METRICS_VERSION,
        "fact_status": "부분 구현 또는 개발용 데모",
        "status": "COMPLETED" if passed_case_count == case_count else "FAILED",
        "decision": (
            "ADOPT_FOR_INTERNAL_REGRESSION_ONLY"
            if passed_case_count == case_count
            else "REJECT_TRAJECTORY_POLICY"
        ),
        "evaluation_mode": "DETERMINISTIC_AGENT_TRAJECTORY_REGRESSION",
        "evaluation_contract": contract,
        "claim_scope": contract["claim_scope"],
        "field_validated": False,
        "is_field_performance_estimate": False,
        "case_count": case_count,
        "step_count": step_count,
        "passed_case_count": passed_case_count,
        "failed_case_count": case_count - passed_case_count,
        "metrics": {
            "scenario_pass_rate": (
                passed_case_count / case_count if case_count else 0.0
            ),
            "memory_checksum_failure_count": memory_checksum_failure_count,
            "tool_after_failure_count": tool_after_failure_count,
            "presentation_before_two_confirmations_count": (
                presentation_before_two_confirmations_count
            ),
            "failure_analysis_exposure_count": failure_analysis_exposure_count,
            "internal_error_leak_count": internal_error_leak_count,
            "tool_argument_contract_violation_count": (
                tool_argument_contract_violation_count
            ),
        },
        "capability_coverage": {
            capability: {
                "case_count": capability_totals[capability],
                "passed_case_count": capability_passes[capability],
                "pass_rate": (
                    capability_passes[capability] / capability_totals[capability]
                ),
            }
            for capability in sorted(capability_totals)
        },
        "artifact": {
            "dataset_file_name": evaluation_path.name,
            "dataset_sha256": sha256_file(evaluation_path),
            "evaluator_file_name": "src/chemiguard119/agent_trajectory_evaluation.py",
            "evaluator_sha256": sha256_file(Path(__file__)),
            "agent_policy_file_name": "src/chemiguard119/agent_loop.py",
            "agent_policy_sha256": sha256_file(
                Path(__file__).with_name("agent_loop.py")
            ),
            "agent_schema_version": AGENT_SCHEMA_VERSION,
            "agent_memory_schema_version": AGENT_MEMORY_SCHEMA_VERSION,
        },
        "acceptance_gate": {
            "passed": (
                passed_case_count == case_count
                and memory_checksum_failure_count == 0
                and tool_after_failure_count == 0
                and presentation_before_two_confirmations_count == 0
                and failure_analysis_exposure_count == 0
                and internal_error_leak_count == 0
                and tool_argument_contract_violation_count == 0
            ),
            "required_scenario_pass_rate": 1.0,
            "required_safety_violation_count": 0,
        },
        "cases": case_reports,
        "claims_allowed": [
            "고정 합성 fixture에서 결정적 Agent 정책의 도구 선택·순서·중단 회귀",
            "고정 합성 fixture에서 request·incident·memory 인자 연결 계약 회귀",
            "고정 합성 fixture에서 2-CAS Gate 이전 결과 제시 차단 회귀",
        ],
        "claims_not_allowed": [
            "실제 LLM Agent의 자율 추론 성능",
            "실제 화학사고·현장 무전·대원 행동 성능",
            "화학적 위험 정답 또는 운영 안전성 보장",
        ],
        "limitations": [
            "합성 AnalysisResponse 관찰을 사용한 결정적 상태머신 내부 회귀입니다.",
            "Retriever·LLM timeout은 도구 실패 전달을 모의하며 실제 네트워크 SLO가 아닙니다.",
            "화학적 위험 정답, 현장 음성, 실제 대원 행동 또는 운영 안전성을 평가하지 않습니다.",
        ],
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


__all__ = [
    "AGENT_TRAJECTORY_METRICS_VERSION",
    "AGENT_TRAJECTORY_REPORT_SCHEMA_VERSION",
    "evaluate_agent_trajectories",
]
