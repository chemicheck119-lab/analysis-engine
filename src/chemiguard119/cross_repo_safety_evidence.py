"""Speech·Analysis·Backend의 잠금 안전 보고서를 과장 없이 결합한다.

이 모듈은 서로 다른 평가 실행을 하나의 현장 E2E 실험으로 둔갑시키지 않는다.
각 보고서의 SHA-256·schema·내부 안전 Gate를 검증하고, 아직 한 요청으로 연결하지
않은 구간과 현장 정답 부재를 결과에 명시한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from chemiguard119.utils import sha256_file, write_json


MANIFEST_SCHEMA_VERSION = "chemicheck119-cross-repo-safety-evidence-manifest-v1"
REPORT_SCHEMA_VERSION = "chemicheck119-cross-repo-safety-evidence-report-v1"
SOURCE_IDS = (
    "analysis_engine",
    "backend_state",
    "speech_seoul_radio_sim",
    "speech_incheon_radio_sim",
)
REQUIRED_ANALYSIS_CAPABILITIES = frozenset(
    {
        "AMBIGUITY_ABSTENTION",
        "CONFIRMATION_GATE",
        "DETERMINISTIC_CONFLICT_RULE",
        "EVIDENCE_CAS_LOCK",
        "INVALID_INPUT_REJECTION",
        "LLM_TIMEOUT_EXTRACTIVE_FALLBACK",
        "RETRIEVER_TIMEOUT_ABSTENTION",
        "UNREGISTERED_PRODUCT_ABSTENTION",
        "UNSUPPORTED_PAIR_ABSTENTION",
    }
)
REQUIRED_BACKEND_CHECKS: dict[str, Any] = {
    "old_confirmation_status": "SUPERSEDED",
    "active_confirmation_status": "ACTIVE",
    "stale_record_http_status": 409,
    "record_count_after_stale_attempt": 0,
    "fresh_record_http_status": 201,
    "retry_record_http_status": 201,
    "record_count_after_exact_retry": 1,
    "analysis_reference_count_after_exact_retry": 1,
}
SPEECH_SAFETY_FIELDS = (
    "candidate_promotion_violation_count",
    "rule_execution_before_confirmation_count",
    "two_cas_gate_violation_count",
    "unconfirmed_risk_output_violation_count",
)


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: JSON 최상위 값은 객체여야 합니다.")
    return payload


def _append_if(errors: list[str], condition: bool, code: str) -> None:
    if condition:
        errors.append(code)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count_or_zero(value: object) -> int:
    return value if _is_count(value) else 0


def _validate_manifest(manifest: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    _append_if(
        errors,
        manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION,
        "MANIFEST_SCHEMA_MISMATCH",
    )
    _append_if(
        errors,
        manifest.get("fact_status") != "부분 구현 또는 개발용 데모",
        "MANIFEST_FACT_STATUS_MISMATCH",
    )
    sources = manifest.get("sources")
    if not isinstance(sources, Mapping):
        return [*errors, "MANIFEST_SOURCES_MISSING"]
    _append_if(
        errors,
        set(sources) != set(SOURCE_IDS),
        "MANIFEST_SOURCE_SET_MISMATCH",
    )
    for source_id in SOURCE_IDS:
        source = sources.get(source_id)
        if not isinstance(source, Mapping):
            errors.append(f"{source_id}:MANIFEST_SOURCE_MISSING")
            continue
        digest = source.get("expected_sha256")
        _append_if(
            errors,
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest),
            f"{source_id}:EXPECTED_SHA256_INVALID",
        )
        for field in ("repository", "expected_schema_version"):
            _append_if(
                errors,
                not isinstance(source.get(field), str) or not source.get(field),
                f"{source_id}:{field.upper()}_MISSING",
            )
    return errors


def _validate_analysis(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    _append_if(errors, report.get("status") != "COMPLETED", "ANALYSIS_NOT_COMPLETED")
    _append_if(
        errors,
        report.get("metrics_version") != "incident-e2e-evaluation-v3",
        "ANALYSIS_METRICS_VERSION_MISMATCH",
    )
    _append_if(
        errors,
        report.get("claim_scope") != "INTERNAL_REGRESSION_ONLY",
        "ANALYSIS_CLAIM_SCOPE_UNSAFE",
    )
    _append_if(
        errors,
        report.get("field_validated") is not False,
        "ANALYSIS_FIELD_SCOPE_UNSAFE",
    )
    _append_if(
        errors,
        report.get("is_field_performance_estimate") is not False,
        "ANALYSIS_FIELD_ESTIMATE_UNSAFE",
    )
    cases = report.get("cases")
    case_rows = cases if isinstance(cases, list) else []
    case_count = report.get("case_count")
    passed_count = report.get("passed_case_count")
    failed_count = report.get("failed_case_count")
    _append_if(errors, case_count != len(case_rows), "ANALYSIS_CASE_COUNT_MISMATCH")
    _append_if(
        errors,
        passed_count
        != sum(
            item.get("passed") is True
            for item in case_rows
            if isinstance(item, Mapping)
        ),
        "ANALYSIS_PASS_COUNT_MISMATCH",
    )
    _append_if(
        errors,
        not _is_count(case_count)
        or not _is_count(passed_count)
        or not _is_count(failed_count)
        or case_count < 1
        or passed_count != case_count
        or failed_count != 0,
        "ANALYSIS_SCENARIO_GATE_FAILED",
    )
    contract = report.get("evaluation_contract")
    contract_payload = contract if isinstance(contract, Mapping) else {}
    _append_if(
        errors,
        contract_payload.get("passed") is not True
        or contract_payload.get("profile") != "INTERNAL_REGRESSION"
        or contract_payload.get("expert_reviewed") is not False,
        "ANALYSIS_DATASET_CONTRACT_FAILED",
    )
    capabilities = report.get("capability_coverage")
    capability_rows = capabilities if isinstance(capabilities, Mapping) else {}
    missing = REQUIRED_ANALYSIS_CAPABILITIES - set(capability_rows)
    _append_if(errors, bool(missing), "ANALYSIS_REQUIRED_CAPABILITY_MISSING")
    for capability in REQUIRED_ANALYSIS_CAPABILITIES & set(capability_rows):
        row = capability_rows[capability]
        if not isinstance(row, Mapping) or row.get("pass_rate") != 1.0:
            errors.append(f"ANALYSIS_CAPABILITY_FAILED:{capability}")
    metrics = report.get("metrics")
    metric_payload = metrics if isinstance(metrics, Mapping) else {}
    expected_metrics: dict[str, Any] = {
        "output_contract_pass_rate": 1.0,
        "scenario_pass_rate": 1.0,
        "unsafe_conflict_execution_count": 0,
        "unconfirmed_risk_exposure_count": 0,
        "llm_timeout_fallback_pass_rate": 1.0,
        "grounded_rag_contract_pass_rate": 1.0,
        "uncited_grounded_rag_case_count": 0,
    }
    for field, expected in expected_metrics.items():
        _append_if(
            errors,
            metric_payload.get(field) != expected,
            f"ANALYSIS_METRIC_FAILED:{field}",
        )
    return errors


def _backend_check_map(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    checks = report.get("checks")
    rows = checks if isinstance(checks, list) else []
    return {
        str(row.get("name")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("name")
    }


def _validate_backend(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    _append_if(errors, report.get("status") != "COMPLETED", "BACKEND_NOT_COMPLETED")
    _append_if(
        errors,
        report.get("claim_scope") != "INTERNAL_REGRESSION_ONLY",
        "BACKEND_CLAIM_SCOPE_UNSAFE",
    )
    for field in ("field_validated", "cloud_sql_validated", "concurrency_validated"):
        _append_if(
            errors, report.get(field) is not False, f"BACKEND_SCOPE_UNSAFE:{field}"
        )
    _append_if(
        errors,
        report.get("database_runtime") != "H2_POSTGRESQL_COMPATIBILITY_MODE",
        "BACKEND_RUNTIME_MISMATCH",
    )
    checks = report.get("checks")
    check_rows = checks if isinstance(checks, list) else []
    check_count = report.get("check_count")
    passed_count = report.get("passed_check_count")
    failed_count = report.get("failed_check_count")
    _append_if(errors, check_count != len(check_rows), "BACKEND_CHECK_COUNT_MISMATCH")
    _append_if(
        errors,
        passed_count
        != sum(
            item.get("passed") is True
            for item in check_rows
            if isinstance(item, Mapping)
        ),
        "BACKEND_PASS_COUNT_MISMATCH",
    )
    _append_if(
        errors,
        not _is_count(check_count)
        or not _is_count(passed_count)
        or not _is_count(failed_count)
        or check_count < 1
        or passed_count != check_count
        or failed_count != 0,
        "BACKEND_STATE_GATE_FAILED",
    )
    check_map = _backend_check_map(report)
    check_names = [
        str(row.get("name"))
        for row in check_rows
        if isinstance(row, Mapping) and row.get("name")
    ]
    _append_if(
        errors,
        len(check_names) != len(set(check_names)),
        "BACKEND_DUPLICATE_CHECK_NAME",
    )
    for name, expected in REQUIRED_BACKEND_CHECKS.items():
        row = check_map.get(name)
        if (
            row is None
            or row.get("expected") != expected
            or row.get("actual") != expected
            or row.get("passed") is not True
        ):
            errors.append(f"BACKEND_REQUIRED_CHECK_FAILED:{name}")
    fresh_id = check_map.get("fresh_record_id", {}).get("actual")
    retry_id = check_map.get("retry_record_id", {}).get("actual")
    _append_if(
        errors,
        not fresh_id or fresh_id != retry_id,
        "BACKEND_IDEMPOTENCY_ID_MISMATCH",
    )
    return errors


def _validate_speech(report: Mapping[str, Any], region: str) -> list[str]:
    prefix = f"SPEECH_{region.upper()}"
    errors: list[str] = []
    _append_if(
        errors,
        report.get("fact_status") != "부분 구현 또는 개발용 데모",
        f"{prefix}_FACT_STATUS_UNSAFE",
    )
    evidence_scope = report.get("evidence_scope")
    _append_if(
        errors,
        not isinstance(evidence_scope, str)
        or "현장 무전" not in evidence_scope
        or "검증 아님" not in evidence_scope,
        f"{prefix}_EVIDENCE_SCOPE_UNSAFE",
    )
    dataset = report.get("dataset")
    dataset_payload = dataset if isinstance(dataset, Mapping) else {}
    _append_if(
        errors,
        dataset_payload.get("profile_id") != "radio-sim-v1"
        or dataset_payload.get("derived_data") is not True,
        f"{prefix}_DATASET_SCOPE_MISMATCH",
    )
    metrics = report.get("metrics")
    metric_payload = metrics if isinstance(metrics, Mapping) else {}
    condition_count = metric_payload.get("condition_count")
    per_condition = metric_payload.get("record_count_per_condition")
    condition_records = metric_payload.get("condition_record_count")
    _append_if(
        errors,
        not _is_count(condition_count)
        or not _is_count(per_condition)
        or condition_count < 1
        or per_condition < 1
        or condition_records != condition_count * per_condition,
        f"{prefix}_CONDITION_COUNT_MISMATCH",
    )
    for gate in (
        "evaluation_integrity_gate",
        "analysis_coverage_gate",
        "safety_contract_gate",
        "downstream_evaluation_gate",
    ):
        gate_payload = metric_payload.get(gate)
        _append_if(
            errors,
            not isinstance(gate_payload, Mapping)
            or gate_payload.get("passed") is not True,
            f"{prefix}_GATE_FAILED:{gate}",
        )
    safety = metric_payload.get("safety_violation_totals")
    safety_payload = safety if isinstance(safety, Mapping) else {}
    for field in SPEECH_SAFETY_FIELDS:
        _append_if(
            errors,
            safety_payload.get(field) != 0,
            f"{prefix}_SAFETY_METRIC_FAILED:{field}",
        )
    _append_if(
        errors,
        metric_payload.get("cas_ground_truth_available") is not False
        or metric_payload.get("is_cas_accuracy_evaluation") is not False
        or metric_payload.get("wrong_single_cas_promotion_ground_truth_count")
        is not None,
        f"{prefix}_CAS_SCOPE_UNSAFE",
    )
    return errors


def _source_summary(
    path: Path, expected: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    digest = sha256_file(path)
    report = _load_object(path)
    errors: list[str] = []
    _append_if(
        errors,
        digest != expected.get("expected_sha256"),
        "SHA256_MISMATCH",
    )
    _append_if(
        errors,
        report.get("schema_version") != expected.get("expected_schema_version"),
        "SCHEMA_VERSION_MISMATCH",
    )
    summary = {
        "repository": expected.get("repository"),
        "file_name": path.name,
        "sha256": digest,
        "expected_sha256": expected.get("expected_sha256"),
        "sha256_matched": digest == expected.get("expected_sha256"),
        "schema_version": report.get("schema_version"),
        "expected_schema_version": expected.get("expected_schema_version"),
    }
    return report, summary, errors


def _speech_runtime_identity(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stt_runtime": report.get("stt_runtime"),
        "model_api_runtime": report.get("model_api_runtime"),
        "speech_evaluator_artifact": report.get("speech_evaluator_artifact"),
        "evaluation_runtime": report.get("evaluation_runtime"),
    }


def aggregate_cross_repo_safety_evidence(
    *,
    manifest_path: Path,
    analysis_report_path: Path,
    backend_report_path: Path,
    seoul_speech_report_path: Path,
    incheon_speech_report_path: Path,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """잠금 보고서 네 개의 무결성·범위·안전 Gate를 결합한다."""

    manifest_path = Path(manifest_path)
    manifest = _load_object(manifest_path)
    manifest_errors = _validate_manifest(manifest)
    sources = (
        manifest.get("sources") if isinstance(manifest.get("sources"), Mapping) else {}
    )
    paths = {
        "analysis_engine": Path(analysis_report_path),
        "backend_state": Path(backend_report_path),
        "speech_seoul_radio_sim": Path(seoul_speech_report_path),
        "speech_incheon_radio_sim": Path(incheon_speech_report_path),
    }
    reports: dict[str, dict[str, Any]] = {}
    artifact_summaries: dict[str, dict[str, Any]] = {}
    validation_errors: dict[str, list[str]] = {"manifest": manifest_errors}
    for source_id in SOURCE_IDS:
        expected = sources.get(source_id) if isinstance(sources, Mapping) else {}
        expected_payload = expected if isinstance(expected, Mapping) else {}
        report, artifact, errors = _source_summary(paths[source_id], expected_payload)
        reports[source_id] = report
        artifact_summaries[source_id] = artifact
        validation_errors[source_id] = errors

    validation_errors["analysis_engine"].extend(
        _validate_analysis(reports["analysis_engine"])
    )
    validation_errors["backend_state"].extend(
        _validate_backend(reports["backend_state"])
    )
    validation_errors["speech_seoul_radio_sim"].extend(
        _validate_speech(reports["speech_seoul_radio_sim"], "seoul")
    )
    validation_errors["speech_incheon_radio_sim"].extend(
        _validate_speech(reports["speech_incheon_radio_sim"], "incheon")
    )

    seoul = reports["speech_seoul_radio_sim"]
    incheon = reports["speech_incheon_radio_sim"]
    seoul_dataset = (
        seoul.get("dataset") if isinstance(seoul.get("dataset"), Mapping) else {}
    )
    incheon_dataset = (
        incheon.get("dataset") if isinstance(incheon.get("dataset"), Mapping) else {}
    )
    cross_region_errors: list[str] = []
    _append_if(
        cross_region_errors,
        seoul_dataset.get("source_manifest_sha256")
        == incheon_dataset.get("source_manifest_sha256"),
        "SPEECH_SOURCE_MANIFESTS_NOT_DISTINCT",
    )
    _append_if(
        cross_region_errors,
        _speech_runtime_identity(seoul) != _speech_runtime_identity(incheon),
        "SPEECH_RUNTIME_NOT_COMPARABLE",
    )
    validation_errors["cross_region"] = cross_region_errors

    analysis_metrics = reports["analysis_engine"].get("metrics") or {}
    backend_checks = _backend_check_map(reports["backend_state"])
    speech_metrics = [
        (seoul.get("metrics") or {}),
        (incheon.get("metrics") or {}),
    ]
    speech_safety = [
        metrics.get("safety_violation_totals") or {} for metrics in speech_metrics
    ]
    combined_safety = {
        "rule_execution_before_two_confirmations_observed_count": _count_or_zero(
            analysis_metrics.get("unsafe_conflict_execution_count")
        )
        + sum(
            _count_or_zero(item.get("rule_execution_before_confirmation_count"))
            for item in speech_safety
        ),
        "unconfirmed_risk_exposure_observed_count": _count_or_zero(
            analysis_metrics.get("unconfirmed_risk_exposure_count")
        )
        + sum(
            _count_or_zero(item.get("unconfirmed_risk_output_violation_count"))
            for item in speech_safety
        ),
        "candidate_promotion_violation_observed_count": sum(
            _count_or_zero(item.get("candidate_promotion_violation_count"))
            for item in speech_safety
        ),
        "uncited_grounded_rag_case_count": _count_or_zero(
            analysis_metrics.get("uncited_grounded_rag_case_count")
        ),
        "stale_attempt_persisted_record_count": backend_checks.get(
            "record_count_after_stale_attempt", {}
        ).get("actual"),
        "record_count_after_exact_retry": backend_checks.get(
            "record_count_after_exact_retry", {}
        ).get("actual"),
        "wrong_single_cas_promotion_ground_truth_count": None,
        "cas_ground_truth_available_for_speech": False,
    }
    all_errors = [error for errors in validation_errors.values() for error in errors]
    gate_passed = not all_errors
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "COMPLETED" if gate_passed else "FAILED",
        "fact_status": "부분 구현 또는 개발용 데모",
        "claim_scope": "CROSS_REPO_INTERNAL_REGRESSION_ONLY",
        "field_validated": False,
        "full_chain_executed": False,
        "training_executed": False,
        "decision": (
            "CONDITIONALLY_ADOPT_FOR_INTERNAL_REGRESSION"
            if gate_passed
            else "REJECT_EVIDENCE_BUNDLE"
        ),
        "evidence_integrity_gate": {
            "passed": gate_passed,
            "error_count": len(all_errors),
            "errors_by_source": validation_errors,
        },
        "coverage": {
            "speech": {
                "region_count": 2,
                "condition_input_count": sum(
                    _count_or_zero(metrics.get("condition_record_count"))
                    for metrics in speech_metrics
                ),
                "derived_radio_sim": True,
                "field_radio": False,
            },
            "analysis_engine": {
                "scenario_count": reports["analysis_engine"].get("case_count"),
                "passed_scenario_count": reports["analysis_engine"].get(
                    "passed_case_count"
                ),
            },
            "backend_state": {
                "check_count": reports["backend_state"].get("check_count"),
                "passed_check_count": reports["backend_state"].get(
                    "passed_check_count"
                ),
                "database_runtime": reports["backend_state"].get("database_runtime"),
            },
        },
        "safety_observations_across_separate_suites": combined_safety,
        "unverified_gaps": [
            "음성부터 Backend 인계 기록까지 동일 request_id로 실행한 단일 전체 경로",
            "시설 과거 이력이 없는 입력을 포함한 단일 전체 경로",
            "실제 현장 무전 음성과 실제 화학사고 결과",
            "음성 물질명의 CAS 사람 정답",
            "Cloud SQL PostgreSQL 동시성·복구·가용성",
            "독립 검수된 파일럿 E2E 200건 이상",
        ],
        "claims_allowed": [
            "잠긴 네 보고서가 manifest SHA-256·schema와 일치함",
            "분리된 내부 회귀 suite에서 관측된 안전 계약 위반 건수",
            "Speech·Analysis·Backend 각 구현 경계의 제한된 회귀 상태",
        ],
        "claims_not_allowed": [
            "한 요청의 음성→인계 전체 경로가 실행됐다는 주장",
            "현장 정확도·현장 안전성·상용 운영 성능",
            "speech의 잘못된 단일 CAS 확정이 0건이라는 정답 기반 주장",
            "서로 다른 suite의 입력 수를 독립 현장 표본 수로 합산",
        ],
        "input_artifacts": {
            "manifest": {
                "file_name": manifest_path.name,
                "sha256": sha256_file(manifest_path),
            },
            **artifact_summaries,
        },
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report
