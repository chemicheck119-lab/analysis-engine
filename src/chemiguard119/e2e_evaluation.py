"""사고 입력부터 충돌 검토까지 실제 파이프라인을 실행하는 E2E 평가기.

모듈별 작은 회귀셋만으로는 후보가 현장 확인으로 잘못 승격되거나, 미확인
물질쌍에 위험등급이 노출되는 통합 오류를 찾기 어렵다. 이 평가기는 실제
``analyze_incident`` 경로를 호출하고, 각 시나리오의 상태·확인 gate·근거 CAS
귀속·기권 동작을 함께 검사한다.

DRAFT 시나리오는 내부 회귀에만 사용할 수 있다. 현장 성능이나 상용 정확도로
해석하지 않도록 보고서에 claim scope와 한계를 고정한다.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping

from chemiguard119.api_models import GroundedRagAnswer, contains_unconfirmed_risk_output
from chemiguard119.evaluation_contract import (
    EvaluationProfile,
    evaluate_dataset_contract,
    load_evaluation_rows,
)
from chemiguard119.facility import search_facility_history
from chemiguard119.paths import CONFIG_DIR
from chemiguard119.pipeline import analyze_incident, validate_pipeline_output
from chemiguard119.rag import GroundedRagService, RagConfig
from chemiguard119.resolver import load_resolver
from chemiguard119.retrieval import load_retriever
from chemiguard119.rules import PUBLIC_SOURCE_PILOT_POLICY
from chemiguard119.utils import sha256_file, write_json


E2E_METRICS_VERSION = "incident-e2e-evaluation-v4"
E2E_REPORT_SCHEMA_VERSION = "chemicheck119-e2e-evaluation-report-v4"
SUPPORTED_CAPABILITIES = frozenset(
    {
        "PARSER_CANDIDATE",
        "AMBIGUITY_ABSTENTION",
        "EMBEDDED_ALIAS_REJECTION",
        "CONFIRMATION_GATE",
        "DETERMINISTIC_CONFLICT_RULE",
        "EVIDENCE_CAS_LOCK",
        "INVALID_INPUT_REJECTION",
        "UNSUPPORTED_PAIR_ABSTENTION",
        "UNREGISTERED_PRODUCT_ABSTENTION",
        "RETRIEVER_TIMEOUT_ABSTENTION",
        "LLM_TIMEOUT_EXTRACTIVE_FALLBACK",
        "FACILITY_HISTORY_ABSENCE",
    }
)
SUPPORTED_FAULTS = frozenset({"RETRIEVER_TIMEOUT", "LLM_TIMEOUT"})

Analyzer = Callable[..., dict[str, Any]]
FacilitySearcher = Callable[..., dict[str, Any]]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _artifact_identity(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"file_name": path.name, "sha256": None}
    if path.is_file():
        result["sha256"] = sha256_file(path)
    return result


def _require_string(value: object, label: str, case_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{case_id}: {label}는 비어 있지 않은 문자열이어야 합니다.")
    return value.strip()


def _validate_rows(rows: list[Mapping[str, Any]]) -> None:
    for index, row in enumerate(rows, 1):
        case_id = _require_string(row.get("case_id"), "case_id", f"<row:{index}>")
        input_payload = row.get("input")
        expected = row.get("expected")
        capabilities = row.get("capabilities")
        if not isinstance(input_payload, Mapping):
            raise ValueError(f"{case_id}: input 객체가 필요합니다.")
        _require_string(input_payload.get("raw_text"), "input.raw_text", case_id)
        for field in ("confirmed_incident_cas", "confirmed_facility_cas"):
            value = input_payload.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"{case_id}: input.{field}는 문자열 또는 null이어야 합니다."
                )
        planned_actions = input_payload.get("planned_actions", [])
        if not isinstance(planned_actions, list) or any(
            not isinstance(item, str) for item in planned_actions
        ):
            raise ValueError(
                f"{case_id}: input.planned_actions는 문자열 배열이어야 합니다."
            )
        faults = input_payload.get("faults", [])
        if not isinstance(faults, list) or any(
            not isinstance(item, str) or item not in SUPPORTED_FAULTS for item in faults
        ):
            raise ValueError(
                f"{case_id}: input.faults는 지원하는 fault 문자열 배열이어야 합니다."
            )
        facility_query = input_payload.get("facility_history_query")
        if facility_query is not None:
            _require_string(
                facility_query,
                "input.facility_history_query",
                case_id,
            )
            province = input_payload.get("facility_history_province")
            if province is not None and not isinstance(province, str):
                raise ValueError(
                    f"{case_id}: input.facility_history_province는 문자열 또는 null이어야 합니다."
                )
        if not isinstance(expected, Mapping):
            raise ValueError(f"{case_id}: expected 객체가 필요합니다.")
        for field in (
            "status",
            "rule_executed",
            "rule_status",
            "missing_confirmations",
            "candidate_count",
            "candidate_roles",
            "evidence_bases",
            "output_validation_status",
            "expect_abstention",
        ):
            if field not in expected:
                raise ValueError(f"{case_id}: expected.{field}가 필요합니다.")
        if not isinstance(expected["rule_executed"], bool):
            raise ValueError(
                f"{case_id}: expected.rule_executed는 boolean이어야 합니다."
            )
        if not isinstance(expected["candidate_count"], int) or isinstance(
            expected["candidate_count"], bool
        ):
            raise ValueError(f"{case_id}: expected.candidate_count는 정수여야 합니다.")
        for field in ("missing_confirmations", "candidate_roles"):
            if not isinstance(expected[field], list) or any(
                not isinstance(item, str) for item in expected[field]
            ):
                raise ValueError(
                    f"{case_id}: expected.{field}는 문자열 배열이어야 합니다."
                )
        if not isinstance(expected["evidence_bases"], Mapping):
            raise ValueError(f"{case_id}: expected.evidence_bases는 객체여야 합니다.")
        if not isinstance(expected["expect_abstention"], bool):
            raise ValueError(
                f"{case_id}: expected.expect_abstention은 boolean이어야 합니다."
            )
        expected_facility = expected.get("facility_history")
        if facility_query is not None:
            if not isinstance(expected_facility, Mapping):
                raise ValueError(
                    f"{case_id}: 시설 이력 조회에는 expected.facility_history가 필요합니다."
                )
            for field in (
                "status",
                "result_count",
                "any_current_inventory_confirmed",
                "any_rule_eligible",
            ):
                if field not in expected_facility:
                    raise ValueError(
                        f"{case_id}: expected.facility_history.{field}가 필요합니다."
                    )
        elif expected_facility is not None:
            raise ValueError(
                f"{case_id}: input.facility_history_query 없이 expected.facility_history를 사용할 수 없습니다."
            )
        expected_rag = expected.get("grounded_rag")
        if "LLM_TIMEOUT" in faults and expected_rag is None:
            raise ValueError(
                f"{case_id}: LLM_TIMEOUT에는 expected.grounded_rag가 필요합니다."
            )
        if expected_rag is not None:
            if not isinstance(expected_rag, Mapping):
                raise ValueError(f"{case_id}: expected.grounded_rag는 객체여야 합니다.")
            for field in (
                "status",
                "used_llm",
                "fallback_reason",
                "minimum_statement_count",
                "minimum_citation_count",
                "citation_validation_passed",
                "risk_decision_source",
            ):
                if field not in expected_rag:
                    raise ValueError(
                        f"{case_id}: expected.grounded_rag.{field}가 필요합니다."
                    )
        if not isinstance(capabilities, list) or not capabilities:
            raise ValueError(
                f"{case_id}: capabilities는 비어 있지 않은 문자열 배열이어야 합니다."
            )
        unknown = {
            str(item)
            for item in capabilities
            if not isinstance(item, str) or item not in SUPPORTED_CAPABILITIES
        }
        if unknown:
            raise ValueError(f"{case_id}: 지원하지 않는 capabilities={sorted(unknown)}")


def summarize_pipeline_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    """E2E 평가와 검수 preflight가 공유하는 비민감 출력 요약을 만든다."""

    rule_wrapper = payload.get("rule_review")
    rule = rule_wrapper if isinstance(rule_wrapper, Mapping) else {}
    result_payload = rule.get("result")
    rule_result = result_payload if isinstance(result_payload, Mapping) else {}
    candidates = payload.get("substance_candidates")
    candidate_rows = candidates if isinstance(candidates, list) else []
    evidence = payload.get("evidence")
    evidence_rows = evidence if isinstance(evidence, list) else []
    validation = payload.get("output_validation")
    validation_payload = validation if isinstance(validation, Mapping) else {}
    return {
        "status": payload.get("status"),
        "rule_executed": rule.get("executed") is True,
        "rule_status": rule.get("status"),
        "missing_confirmations": list(rule.get("missing_confirmations") or []),
        "candidate_count": len(candidate_rows),
        "candidate_roles": [item.get("role") for item in candidate_rows],
        "evidence_bases": {
            str(item.get("role")): item.get("cas_basis") for item in evidence_rows
        },
        "retrieval_statuses": [
            (item.get("retrieval") or {}).get("status") for item in evidence_rows
        ],
        "output_validation_status": validation_payload.get("status"),
        "risk_level": rule_result.get("risk_level"),
        "severity": rule_result.get("severity"),
    }


def summarize_grounded_rag(payload: Mapping[str, Any]) -> dict[str, Any]:
    """LLM 원문이나 내부 오류 없이 fallback 안전 속성만 요약한다."""

    statements = payload.get("statements")
    statement_rows = statements if isinstance(statements, list) else []
    citations = payload.get("citations")
    citation_rows = citations if isinstance(citations, list) else []
    citation_ids = {
        str(item.get("source_id"))
        for item in citation_rows
        if isinstance(item, Mapping) and item.get("source_id")
    }
    referenced_ids = {
        str(source_id)
        for statement in statement_rows
        if isinstance(statement, Mapping)
        for source_id in statement.get("source_ids") or []
    }
    citation_validation = payload.get("citation_validation")
    validation_payload = (
        citation_validation if isinstance(citation_validation, Mapping) else {}
    )
    return {
        "status": payload.get("status"),
        "used_llm": payload.get("used_llm"),
        "fallback_reason": payload.get("fallback_reason"),
        "statement_count": len(statement_rows),
        "citation_count": len(citation_rows),
        "citation_validation_passed": validation_payload.get("passed"),
        "all_statement_sources_cited": bool(statement_rows)
        and referenced_ids.issubset(citation_ids),
        "risk_decision_source": payload.get("risk_decision_source"),
    }


def summarize_facility_history(payload: Mapping[str, Any]) -> dict[str, Any]:
    """현재 재고로 오해할 필드를 제거한 시설 과거 이력 안전 요약을 만든다."""

    results = payload.get("results")
    rows = results if isinstance(results, list) else []
    return {
        "status": payload.get("status"),
        "result_count": len(rows),
        "any_current_inventory_confirmed": any(
            isinstance(row, Mapping) and row.get("current_inventory_confirmed") is True
            for row in rows
        ),
        "any_rule_eligible": any(
            isinstance(row, Mapping) and row.get("rule_eligible") is True
            for row in rows
        ),
    }


def _compare(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    exact_fields = (
        "status",
        "rule_executed",
        "rule_status",
        "missing_confirmations",
        "candidate_count",
        "candidate_roles",
        "evidence_bases",
        "output_validation_status",
    )
    for field in exact_fields:
        if actual.get(field) != expected.get(field):
            failures.append(
                f"{field}: expected={expected.get(field)!r}, actual={actual.get(field)!r}"
            )
    if "retrieval_statuses" in expected and actual.get(
        "retrieval_statuses"
    ) != expected.get("retrieval_statuses"):
        failures.append(
            "retrieval_statuses: "
            f"expected={expected.get('retrieval_statuses')!r}, "
            f"actual={actual.get('retrieval_statuses')!r}"
        )
    for field in ("risk_level", "severity"):
        if field in expected and actual.get(field) != expected.get(field):
            failures.append(
                f"{field}: expected={expected.get(field)!r}, actual={actual.get(field)!r}"
            )
    return failures


def _compare_grounded_rag(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> list[str]:
    failures: list[str] = []
    for field in (
        "status",
        "used_llm",
        "fallback_reason",
        "citation_validation_passed",
        "risk_decision_source",
    ):
        if actual.get(field) != expected.get(field):
            failures.append(
                "grounded_rag."
                f"{field}: expected={expected.get(field)!r}, actual={actual.get(field)!r}"
            )
    for field, actual_field in (
        ("minimum_statement_count", "statement_count"),
        ("minimum_citation_count", "citation_count"),
    ):
        if int(actual.get(actual_field) or 0) < int(expected.get(field) or 0):
            failures.append(
                "grounded_rag."
                f"{actual_field}: expected>={expected.get(field)!r}, "
                f"actual={actual.get(actual_field)!r}"
            )
    if actual.get("all_statement_sources_cited") is not True:
        failures.append("grounded_rag.all_statement_sources_cited=false")
    return failures


def _compare_facility_history(
    expected: Mapping[str, Any], actual: Mapping[str, Any]
) -> list[str]:
    failures: list[str] = []
    for field in (
        "status",
        "result_count",
        "any_current_inventory_confirmed",
        "any_rule_eligible",
    ):
        if actual.get(field) != expected.get(field):
            failures.append(
                "facility_history."
                f"{field}: expected={expected.get(field)!r}, actual={actual.get(field)!r}"
            )
    if actual.get("status") == "NO_HISTORY_MATCH" and actual.get("result_count") != 0:
        failures.append("facility_history.NO_HISTORY_MATCH_WITH_RESULTS")
    if actual.get("any_current_inventory_confirmed") is True:
        failures.append("facility_history.CURRENT_INVENTORY_AUTO_CONFIRMED")
    if actual.get("any_rule_eligible") is True:
        failures.append("facility_history.CANDIDATE_PROMOTED_TO_RULE_INPUT")
    return failures


def evaluate_incident_scenarios(
    db_path: Path,
    resolver_model_path: Path,
    retriever_model_path: Path,
    evaluation_path: Path,
    *,
    config_dir: Path = CONFIG_DIR,
    profile: EvaluationProfile | str = EvaluationProfile.INTERNAL_REGRESSION,
    report_path: Path | None = None,
    resolver_artifact: dict[str, Any] | None = None,
    retriever_artifact: dict[str, Any] | None = None,
    analyzer: Analyzer | None = None,
    facility_searcher: FacilitySearcher | None = None,
) -> dict[str, Any]:
    """실제 사고 분석 경로의 안전 상태 전이를 시나리오별로 검사한다."""

    db_path = Path(db_path)
    resolver_model_path = Path(resolver_model_path)
    retriever_model_path = Path(retriever_model_path)
    evaluation_path = Path(evaluation_path)
    rows = load_evaluation_rows(evaluation_path)
    contract = evaluate_dataset_contract(rows, profile, evaluation_path)
    if not contract["passed"]:
        codes = ", ".join(item["code"] for item in contract["blockers"])
        raise ValueError(f"평가 데이터 계약 실패: {codes}")
    _validate_rows(rows)

    resolver = (
        resolver_artifact
        if resolver_artifact is not None
        else load_resolver(resolver_model_path)
    )
    retriever = (
        retriever_artifact
        if retriever_artifact is not None
        else load_retriever(retriever_model_path)
    )
    analyze = analyzer or analyze_incident
    search_facility = facility_searcher or search_facility_history

    case_reports: list[dict[str, Any]] = []
    unsafe_conflict_execution_count = 0
    unconfirmed_risk_exposure_count = 0
    contract_pass_count = 0
    abstention_expected_count = 0
    abstention_pass_count = 0
    llm_timeout_expected_count = 0
    llm_timeout_fallback_pass_count = 0
    grounded_rag_contract_pass_count = 0
    uncited_grounded_rag_case_count = 0
    facility_history_expected_count = 0
    facility_history_pass_count = 0
    capability_totals: Counter[str] = Counter()
    capability_passes: Counter[str] = Counter()

    for row in rows:
        case_id = str(row["case_id"])
        input_payload = dict(row["input"])
        expected = dict(row["expected"])
        capabilities = [str(item) for item in row["capabilities"]]
        faults = [str(item) for item in input_payload.get("faults", [])]
        started = time.perf_counter()
        facility_history_actual: dict[str, Any] | None = None
        facility_history_failures: list[str] = []
        facility_query = input_payload.get("facility_history_query")
        if facility_query is not None:
            facility_history_expected_count += 1
            facility_result = search_facility(
                str(facility_query),
                db_path,
                province=input_payload.get("facility_history_province"),
                top_k=10,
            )
            facility_history_actual = summarize_facility_history(facility_result)
            expected_facility = expected.get("facility_history")
            facility_history_failures = _compare_facility_history(
                expected_facility if isinstance(expected_facility, Mapping) else {},
                facility_history_actual,
            )
            if not facility_history_failures:
                facility_history_pass_count += 1
        analyzer_kwargs: dict[str, Any] = {}
        if "RETRIEVER_TIMEOUT" in faults:

            def timeout_searcher(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                raise TimeoutError("deterministic retriever timeout fixture")

            analyzer_kwargs["evidence_searcher"] = timeout_searcher
        output = analyze(
            str(input_payload["raw_text"]),
            db_path=db_path,
            resolver_artifact=resolver,
            retriever_artifact=retriever,
            confirmed_incident_cas=input_payload.get("confirmed_incident_cas"),
            confirmed_facility_cas=input_payload.get("confirmed_facility_cas"),
            planned_actions=input_payload.get("planned_actions") or [],
            policy_mode=input_payload.get("policy_mode", PUBLIC_SOURCE_PILOT_POLICY),
            config_dir=config_dir,
            **analyzer_kwargs,
        )
        actual = summarize_pipeline_output(output)
        validation_errors = validate_pipeline_output(
            output, str(input_payload["raw_text"])
        )
        contract_passed = (
            not validation_errors and actual["output_validation_status"] == "PASSED"
        )
        if contract_passed:
            contract_pass_count += 1

        both_confirmed = bool(
            input_payload.get("confirmed_incident_cas")
            and input_payload.get("confirmed_facility_cas")
        )
        unsafe_execution = actual["rule_executed"] and not both_confirmed
        if unsafe_execution:
            unsafe_conflict_execution_count += 1
        rule_wrapper = output.get("rule_review")
        unconfirmed_risk = not both_confirmed and contains_unconfirmed_risk_output(
            rule_wrapper if isinstance(rule_wrapper, Mapping) else {}
        )
        if unconfirmed_risk:
            unconfirmed_risk_exposure_count += 1

        failures = [*_compare(expected, actual), *facility_history_failures]
        grounded_rag_actual: dict[str, Any] | None = None
        grounded_rag_contract_passed: bool | None = None
        if "LLM_TIMEOUT" in faults:
            llm_timeout_expected_count += 1
            private_timeout_detail = (
                "private-llm-host.example.internal must not be exposed"
            )

            def timeout_requester(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
                raise TimeoutError(private_timeout_detail)

            rag_service = GroundedRagService(
                RagConfig(
                    mode="llm",
                    base_url="https://llm-timeout.invalid/v1",
                    model="deterministic-timeout-fixture",
                ),
                requester=timeout_requester,
            )
            grounded_rag = rag_service.answer(
                list(output.get("evidence") or []),
                output.get("rule_review") or {},
            )
            try:
                GroundedRagAnswer.model_validate(grounded_rag)
            except Exception as error:
                grounded_rag_contract_passed = False
                failures.append(f"grounded_rag_contract_error={type(error).__name__}")
            else:
                grounded_rag_contract_passed = True
                grounded_rag_contract_pass_count += 1
            grounded_rag_actual = summarize_grounded_rag(grounded_rag)
            expected_rag = expected.get("grounded_rag")
            rag_failures = _compare_grounded_rag(
                expected_rag if isinstance(expected_rag, Mapping) else {},
                grounded_rag_actual,
            )
            failures.extend(rag_failures)
            timeout_detail_exposed = private_timeout_detail in json.dumps(
                grounded_rag, ensure_ascii=False
            )
            if timeout_detail_exposed:
                failures.append("LLM_TIMEOUT_INTERNAL_DETAIL_EXPOSED")
            if not grounded_rag_actual["all_statement_sources_cited"]:
                uncited_grounded_rag_case_count += 1
            if (
                grounded_rag_contract_passed
                and not rag_failures
                and not timeout_detail_exposed
            ):
                llm_timeout_fallback_pass_count += 1
        if validation_errors:
            failures.append(f"pipeline_contract_errors={validation_errors!r}")
        if unsafe_execution:
            failures.append("UNSAFE_CONFLICT_EXECUTION_WITHOUT_TWO_CONFIRMED_CAS")
        if unconfirmed_risk:
            failures.append("UNCONFIRMED_RISK_OUTPUT_EXPOSED")

        abstention_expected = bool(expected["expect_abstention"])
        if abstention_expected:
            abstention_expected_count += 1
            abstained = not actual["rule_executed"] or (
                actual["rule_status"]
                in {"UNCLASSIFIED", "VERIFY_REQUIRED", "CAMEO_GROUP_SCREENING_ONLY"}
                and actual["risk_level"] is None
                and actual["severity"] is None
            )
            if abstained:
                abstention_pass_count += 1
            else:
                failures.append("EXPECTED_ABSTENTION_NOT_OBSERVED")

        passed = not failures
        latency_ms = (time.perf_counter() - started) * 1_000
        for capability in capabilities:
            capability_totals[capability] += 1
            if passed:
                capability_passes[capability] += 1
        case_reports.append(
            {
                "case_id": case_id,
                "passed": passed,
                "capabilities": capabilities,
                "actual": actual,
                "contract_passed": contract_passed,
                "grounded_rag": grounded_rag_actual,
                "grounded_rag_contract_passed": grounded_rag_contract_passed,
                "facility_history": facility_history_actual,
                "unsafe_conflict_execution": unsafe_execution,
                "unconfirmed_risk_exposure": unconfirmed_risk,
                "failures": failures,
                "latency_ms": round(latency_ms, 6),
            }
        )

    case_count = len(case_reports)
    passed_count = sum(bool(item["passed"]) for item in case_reports)
    latencies = [float(item["latency_ms"]) for item in case_reports]
    metrics = {
        "output_contract_pass_rate": (
            contract_pass_count / case_count if case_count else 0.0
        ),
        "scenario_pass_rate": passed_count / case_count if case_count else 0.0,
        "unsafe_conflict_execution_count": unsafe_conflict_execution_count,
        "unconfirmed_risk_exposure_count": unconfirmed_risk_exposure_count,
        "expected_abstention_count": abstention_expected_count,
        "expected_abstention_pass_rate": (
            abstention_pass_count / abstention_expected_count
            if abstention_expected_count
            else None
        ),
        "llm_timeout_expected_count": llm_timeout_expected_count,
        "llm_timeout_fallback_pass_rate": (
            llm_timeout_fallback_pass_count / llm_timeout_expected_count
            if llm_timeout_expected_count
            else None
        ),
        "grounded_rag_contract_pass_rate": (
            grounded_rag_contract_pass_count / llm_timeout_expected_count
            if llm_timeout_expected_count
            else None
        ),
        "uncited_grounded_rag_case_count": uncited_grounded_rag_case_count,
        "facility_history_expected_count": facility_history_expected_count,
        "facility_history_absence_pass_rate": (
            facility_history_pass_count / facility_history_expected_count
            if facility_history_expected_count
            else None
        ),
        "latency_ms": {
            "mean": sum(latencies) / len(latencies) if latencies else None,
            "p95": _percentile(latencies, 0.95),
        },
    }
    report = {
        "schema_version": E2E_REPORT_SCHEMA_VERSION,
        "metrics_version": E2E_METRICS_VERSION,
        "status": "COMPLETED" if passed_count == case_count else "FAILED",
        "evaluation_mode": "INCIDENT_PIPELINE_SAFETY_SCENARIOS",
        "evaluation_contract": contract,
        "claim_scope": contract["claim_scope"],
        "field_validated": False,
        "is_field_performance_estimate": False,
        "case_count": case_count,
        "passed_case_count": passed_count,
        "failed_case_count": case_count - passed_count,
        "metrics": metrics,
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
        "artifacts": {
            "database": _artifact_identity(db_path),
            "resolver": _artifact_identity(resolver_model_path),
            "retriever": _artifact_identity(retriever_model_path),
            "evaluator_source": _artifact_identity(Path(__file__)),
            "pipeline_source": _artifact_identity(
                Path(analyze_incident.__code__.co_filename)
            ),
            "rag_source": _artifact_identity(
                Path(GroundedRagService.answer.__code__.co_filename)
            ),
            "facility_source": _artifact_identity(
                Path(search_facility_history.__code__.co_filename)
            ),
        },
        "cases": case_reports,
        "limitations": [
            "DRAFT 시나리오는 내부 안전 회귀용이며 현장 정확도를 나타내지 않습니다.",
            "현재 시나리오는 독립 검수·현장 표본·전국 분포를 포함하지 않습니다.",
            "파일럿 판정에는 별도 PILOT_REVIEWED 200건 이상과 현장 검증이 필요합니다.",
            "LLM timeout은 결정적 fault injection이며 실제 네트워크 가용성이나 복구 시간을 측정하지 않습니다.",
            "시설 이력 없음은 모의 시설명과 과거 공개 이력 DB의 NO_HISTORY_MATCH 회귀이며 실제 현장 재고 부재를 뜻하지 않습니다.",
        ],
    }
    if report_path is not None:
        report_path = Path(report_path)
        write_json(report_path, report)
        report_path.chmod(0o600)
    return report


__all__ = [
    "E2E_METRICS_VERSION",
    "E2E_REPORT_SCHEMA_VERSION",
    "SUPPORTED_CAPABILITIES",
    "SUPPORTED_FAULTS",
    "evaluate_incident_scenarios",
    "summarize_grounded_rag",
    "summarize_facility_history",
    "summarize_pipeline_output",
]
