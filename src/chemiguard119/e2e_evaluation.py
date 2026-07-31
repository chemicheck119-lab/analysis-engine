"""사고 입력부터 충돌 검토까지 실제 파이프라인을 실행하는 E2E 평가기.

모듈별 작은 회귀셋만으로는 후보가 현장 확인으로 잘못 승격되거나, 미확인
물질쌍에 위험등급이 노출되는 통합 오류를 찾기 어렵다. 이 평가기는 실제
``analyze_incident`` 경로를 호출하고, 각 시나리오의 상태·확인 gate·근거 CAS
귀속·기권 동작을 함께 검사한다.

DRAFT 시나리오는 내부 회귀에만 사용할 수 있다. 현장 성능이나 상용 정확도로
해석하지 않도록 보고서에 claim scope와 한계를 고정한다.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping

from chemiguard119.api_models import contains_unconfirmed_risk_output
from chemiguard119.evaluation_contract import (
    EvaluationProfile,
    evaluate_dataset_contract,
    load_evaluation_rows,
)
from chemiguard119.paths import CONFIG_DIR
from chemiguard119.pipeline import analyze_incident, validate_pipeline_output
from chemiguard119.resolver import load_resolver
from chemiguard119.retrieval import load_retriever
from chemiguard119.rules import PUBLIC_SOURCE_PILOT_POLICY
from chemiguard119.utils import sha256_file, write_json


E2E_METRICS_VERSION = "incident-e2e-evaluation-v1"
E2E_REPORT_SCHEMA_VERSION = "chemicheck119-e2e-evaluation-report-v1"
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
    }
)

Analyzer = Callable[..., dict[str, Any]]


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
        "output_validation_status": validation_payload.get("status"),
        "risk_level": rule_result.get("risk_level"),
        "severity": rule_result.get("severity"),
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
    for field in ("risk_level", "severity"):
        if field in expected and actual.get(field) != expected.get(field):
            failures.append(
                f"{field}: expected={expected.get(field)!r}, actual={actual.get(field)!r}"
            )
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

    case_reports: list[dict[str, Any]] = []
    unsafe_conflict_execution_count = 0
    unconfirmed_risk_exposure_count = 0
    contract_pass_count = 0
    abstention_expected_count = 0
    abstention_pass_count = 0
    capability_totals: Counter[str] = Counter()
    capability_passes: Counter[str] = Counter()

    for row in rows:
        case_id = str(row["case_id"])
        input_payload = dict(row["input"])
        expected = dict(row["expected"])
        capabilities = [str(item) for item in row["capabilities"]]
        started = time.perf_counter()
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
        )
        latency_ms = (time.perf_counter() - started) * 1_000
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

        failures = _compare(expected, actual)
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
        },
        "cases": case_reports,
        "limitations": [
            "DRAFT 시나리오는 내부 안전 회귀용이며 현장 정확도를 나타내지 않습니다.",
            "현재 시나리오는 독립 검수·현장 표본·전국 분포를 포함하지 않습니다.",
            "파일럿 판정에는 별도 PILOT_REVIEWED 200건 이상과 현장 검증이 필요합니다.",
        ],
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


__all__ = [
    "E2E_METRICS_VERSION",
    "E2E_REPORT_SCHEMA_VERSION",
    "SUPPORTED_CAPABILITIES",
    "evaluate_incident_scenarios",
    "summarize_pipeline_output",
]
