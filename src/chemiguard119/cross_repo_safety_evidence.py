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


MANIFEST_SCHEMA_VERSION = "chemicheck119-cross-repo-safety-evidence-manifest-v4"
REPORT_SCHEMA_VERSION = "chemicheck119-cross-repo-safety-evidence-report-v6"
VOICE_FLOW_EXPECTED_CHECK_COUNT = 64
SOURCE_IDS = (
    "analysis_engine",
    "backend_state",
    "backend_cancellation",
    "cross_service_confirmation_flow",
    "cross_service_voice_to_record",
    "speech_seoul_radio_sim",
    "speech_incheon_radio_sim",
)
REQUIRED_ANALYSIS_CAPABILITIES = frozenset(
    {
        "AMBIGUITY_ABSTENTION",
        "CONFIRMATION_GATE",
        "DETERMINISTIC_CONFLICT_RULE",
        "EVIDENCE_CAS_LOCK",
        "FACILITY_HISTORY_ABSENCE",
        "INVALID_INPUT_REJECTION",
        "LLM_TIMEOUT_EXTRACTIVE_FALLBACK",
        "RETRIEVER_TIMEOUT_ABSTENTION",
        "UNREGISTERED_PRODUCT_ABSTENTION",
        "UNSUPPORTED_PAIR_ABSTENTION",
    }
)
REQUIRED_BACKEND_CHECKS: dict[str, Any] = {
    "confirmation_revision_count_after_new_evidence": 2,
    "old_confirmation_status": "SUPERSEDED",
    "old_confirmation_superseded_by_new_evidence": True,
    "active_confirmation_status": "ACTIVE",
    "new_evidence_confirmation_basis": "SITE_MSDS",
    "new_evidence_confirmed_cas": "7664-93-9",
    "new_evidence_confirmation_revision": 2,
    "new_evidence_reanalyze_required": True,
    "stale_record_http_status": 409,
    "record_count_after_stale_attempt": 0,
    "fresh_record_http_status": 201,
    "retry_record_http_status": 201,
    "record_count_after_exact_retry": 1,
    "analysis_reference_count_after_exact_retry": 1,
}
REQUIRED_CANCELLATION_CHECKS: dict[str, Any] = {
    "cancel_http_status": 200,
    "cancel_response_status": "CANCELLED",
    "cancel_reanalyze_required": True,
    "cancel_retry_http_status": 200,
    "cancel_retry_keeps_original_time": True,
    "active_confirmation_count": 1,
    "active_facility_confirmation_present": False,
    "cancelled_confirmation_status": "CANCELLED",
    "cancellation_audit_count": 1,
    "stale_record_http_status": 409,
    "stale_record_error_code": "INCIDENT_REFERENCE_CONFLICT",
    "record_count_after_stale_attempt": 0,
    "post_cancel_facility_binding_is_null": True,
    "post_cancel_rule_executed": False,
    "post_cancel_risk_display_allowed": False,
    "fresh_record_http_status": 201,
    "fresh_record_confirmation_reference_count": 1,
}
REQUIRED_CROSS_SERVICE_CHECKS: dict[str, Any] = {
    "model_ready": "READY",
    "model_runtime_integrity": "VERIFIED",
    "model_runtime_manifest_verified": True,
    "synthetic_data_classification": "PUBLIC_SYNTHETIC",
    "synthetic_contains_personal_information": False,
    "zero_rule_execution_allowed": False,
    "zero_conflict_executed": False,
    "zero_risk_display_allowed": False,
    "one_rule_execution_allowed": False,
    "one_conflict_executed": False,
    "one_risk_display_allowed": False,
    "two_rule_execution_allowed": True,
    "two_conflict_executed": True,
    "two_risk_display_allowed": True,
    "cancelled_rule_execution_allowed": False,
    "cancelled_conflict_executed": False,
    "cancelled_risk_display_allowed": False,
    "two_rule_id": "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
    "two_rule_incident_cas": "7681-52-9",
    "two_rule_facility_cas": "7647-01-0",
    "cancel_status": "CANCELLED",
    "cancel_reanalysis_required": True,
}
REQUIRED_VOICE_FLOW_CHECKS: dict[str, Any] = {
    "model_ready": "READY",
    "model_runtime_integrity": "VERIFIED",
    "model_runtime_manifest_verified": True,
    "speech_status": "TRANSCRIBED",
    "speech_abstained": False,
    "speech_requires_responder_review": True,
    "speech_audio_retained": False,
    "speech_hotwords_used": False,
    "speech_safety_uncertaintyPreserved": True,
    "speech_safety_qualitySignalsAreCalibratedProbabilities": False,
    "speech_safety_chemicalIdentificationPerformed": False,
    "speech_safety_casConfirmationPerformed": False,
    "speech_safety_riskAssessmentPerformed": False,
    "speech_safety_decisionSupportOnly": True,
    "zero_rule_execution_allowed": False,
    "zero_conflict_executed": False,
    "zero_risk_display_allowed": False,
    "incident_candidate_rule_eligible": False,
    "facility_candidate_rule_eligible": False,
    "one_rule_execution_allowed": False,
    "one_conflict_executed": False,
    "one_risk_display_allowed": False,
    "two_all_required_confirmed": True,
    "two_rule_execution_allowed": True,
    "two_conflict_executed": True,
    "two_risk_display_allowed": True,
    "two_rule_id": "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
    "two_rule_incident_cas": "7681-52-9",
    "two_rule_facility_cas": "7647-01-0",
    "incident_confirmation_type": "SYNTHETIC_DEMO_CONFIRMATION",
    "facility_confirmation_type": "SYNTHETIC_DEMO_CONFIRMATION",
    "record_id_present": True,
    "record_exact_retry_same_id": True,
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
        if source_id == "cross_service_voice_to_record":
            for field in ("expected_input_manifest_sha256", "expected_audio_sha256"):
                value = source.get(field)
                _append_if(
                    errors,
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(char not in "0123456789abcdef" for char in value),
                    f"{source_id}:{field.upper()}_INVALID",
                )
    return errors


def _validate_analysis(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    _append_if(errors, report.get("status") != "COMPLETED", "ANALYSIS_NOT_COMPLETED")
    _append_if(
        errors,
        report.get("metrics_version") != "incident-e2e-evaluation-v4",
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
        "facility_history_expected_count": 1,
        "facility_history_absence_pass_rate": 1.0,
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


def _validate_backend_cancellation(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    _append_if(
        errors,
        report.get("status") != "COMPLETED",
        "BACKEND_CANCELLATION_NOT_COMPLETED",
    )
    _append_if(
        errors,
        report.get("claim_scope") != "INTERNAL_REGRESSION_ONLY",
        "BACKEND_CANCELLATION_CLAIM_SCOPE_UNSAFE",
    )
    for field in ("field_validated", "cloud_sql_validated"):
        _append_if(
            errors,
            report.get(field) is not False,
            f"BACKEND_CANCELLATION_SCOPE_UNSAFE:{field}",
        )
    _append_if(
        errors,
        report.get("database_runtime") != "H2",
        "BACKEND_CANCELLATION_RUNTIME_MISMATCH",
    )
    checks = report.get("checks")
    check_rows = checks if isinstance(checks, list) else []
    check_count = report.get("check_count")
    passed_count = report.get("passed_check_count")
    failed_count = report.get("failed_check_count")
    _append_if(
        errors,
        check_count != len(check_rows),
        "BACKEND_CANCELLATION_CHECK_COUNT_MISMATCH",
    )
    _append_if(
        errors,
        passed_count
        != sum(
            item.get("passed") is True
            for item in check_rows
            if isinstance(item, Mapping)
        ),
        "BACKEND_CANCELLATION_PASS_COUNT_MISMATCH",
    )
    _append_if(
        errors,
        not _is_count(check_count)
        or not _is_count(passed_count)
        or not _is_count(failed_count)
        or check_count < 1
        or passed_count != check_count
        or failed_count != 0,
        "BACKEND_CANCELLATION_STATE_GATE_FAILED",
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
        "BACKEND_CANCELLATION_DUPLICATE_CHECK_NAME",
    )
    for name, expected in REQUIRED_CANCELLATION_CHECKS.items():
        row = check_map.get(name)
        if (
            row is None
            or row.get("expected") != expected
            or row.get("actual") != expected
            or row.get("passed") is not True
        ):
            errors.append(f"BACKEND_CANCELLATION_REQUIRED_CHECK_FAILED:{name}")
    return errors


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _validate_cross_service(report: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    expected_top_level: dict[str, Any] = {
        "status": "COMPLETED",
        "fact_status": "부분 구현 또는 개발용 데모",
        "claim_scope": "LOCAL_CROSS_SERVICE_SYNTHETIC_REGRESSION_ONLY",
        "field_validated": False,
        "cloud_run_validated": False,
        "speech_input_executed": False,
        "analysis_backend_http_chain_executed": True,
        "full_voice_to_handoff_chain_executed": False,
        "database_runtime": "H2_POSTGRESQL_COMPATIBILITY_MODE",
        "database_runtime_verified": False,
        "database_runtime_evidence": "OPERATOR_DECLARED",
        "data_classification": "PUBLIC_SYNTHETIC",
        "service_boundary": "BFF_REAL_HTTP_TO_MODEL_API_REAL_HTTP",
    }
    for field, expected in expected_top_level.items():
        _append_if(
            errors,
            report.get(field) != expected,
            f"CROSS_SERVICE_SCOPE_FAILED:{field}",
        )

    checks = report.get("checks")
    check_rows = checks if isinstance(checks, list) else []
    check_count = report.get("check_count")
    passed_count = report.get("passed_check_count")
    failed_count = report.get("failed_check_count")
    _append_if(
        errors,
        check_count != len(check_rows),
        "CROSS_SERVICE_CHECK_COUNT_MISMATCH",
    )
    _append_if(
        errors,
        passed_count
        != sum(
            item.get("passed") is True
            for item in check_rows
            if isinstance(item, Mapping)
        ),
        "CROSS_SERVICE_PASS_COUNT_MISMATCH",
    )
    _append_if(
        errors,
        not _is_count(check_count)
        or not _is_count(passed_count)
        or not _is_count(failed_count)
        or check_count < len(REQUIRED_CROSS_SERVICE_CHECKS)
        or passed_count != check_count
        or failed_count != 0,
        "CROSS_SERVICE_STATE_GATE_FAILED",
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
        "CROSS_SERVICE_DUPLICATE_CHECK_NAME",
    )
    for name, expected in REQUIRED_CROSS_SERVICE_CHECKS.items():
        row = check_map.get(name)
        if (
            row is None
            or row.get("expected") != expected
            or row.get("actual") != expected
            or row.get("passed") is not True
        ):
            errors.append(f"CROSS_SERVICE_REQUIRED_CHECK_FAILED:{name}")

    provenance = report.get("provenance")
    provenance_payload = provenance if isinstance(provenance, Mapping) else {}
    runtime_digest = provenance_payload.get("runtime_manifest_sha256")
    runtime_row = check_map.get("runtime_manifest_sha256")
    _append_if(
        errors,
        not _is_sha256(runtime_digest)
        or runtime_row is None
        or runtime_row.get("expected") != runtime_digest
        or runtime_row.get("actual") != runtime_digest
        or runtime_row.get("passed") is not True,
        "CROSS_SERVICE_RUNTIME_MANIFEST_UNVERIFIED",
    )
    _append_if(
        errors,
        not _is_sha256(provenance_payload.get("evaluator_source_sha256")),
        "CROSS_SERVICE_EVALUATOR_SHA256_INVALID",
    )
    for field in ("backend_git_commit", "model_git_commit"):
        value = provenance_payload.get(field)
        _append_if(
            errors,
            not isinstance(value, str)
            or len(value) != 40
            or any(char not in "0123456789abcdef" for char in value),
            f"CROSS_SERVICE_PROVENANCE_INVALID:{field}",
        )
    return errors


def _validate_voice_flow(
    report: Mapping[str, Any], source: Mapping[str, Any]
) -> list[str]:
    errors: list[str] = []
    expected_top_level: dict[str, Any] = {
        "status": "COMPLETED",
        "fact_status": "부분 구현 또는 개발용 데모",
        "claim_scope": "LOCAL_SYNTHETIC_VOICE_TO_RECORD_REGRESSION_ONLY",
        "field_validated": False,
        "cloud_run_validated": False,
        "database_runtime": "H2_POSTGRESQL_COMPATIBILITY_MODE",
        "database_runtime_verified": False,
        "database_runtime_evidence": "OPERATOR_DECLARED",
        "speech_input_executed": True,
        "speech_backend_http_chain_executed": True,
        "analysis_backend_http_chain_executed": True,
        "voice_to_record_http_chain_executed": True,
        "full_voice_to_operational_handoff_validated": False,
        "human_transcript_review_performed": False,
        "human_cas_confirmation_performed": False,
        "automatic_transcript_forwarding_for_test_harness": True,
        "operational_actions_performed": False,
        "data_classification": "PUBLIC_SYNTHETIC",
        "decision": "CONDITIONALLY_ADOPT_FOR_LOCAL_SYNTHETIC_CONNECTIVITY_REGRESSION",
    }
    for field, expected in expected_top_level.items():
        _append_if(
            errors,
            report.get(field) != expected,
            f"VOICE_FLOW_SCOPE_FAILED:{field}",
        )

    checks = report.get("checks")
    check_rows = checks if isinstance(checks, list) else []
    check_count = report.get("check_count")
    passed_count = report.get("passed_check_count")
    failed_count = report.get("failed_check_count")
    _append_if(
        errors,
        check_count != len(check_rows),
        "VOICE_FLOW_CHECK_COUNT_MISMATCH",
    )
    _append_if(
        errors,
        passed_count
        != sum(
            item.get("passed") is True
            for item in check_rows
            if isinstance(item, Mapping)
        ),
        "VOICE_FLOW_PASS_COUNT_MISMATCH",
    )
    _append_if(
        errors,
        not _is_count(check_count)
        or not _is_count(passed_count)
        or not _is_count(failed_count)
        or check_count != VOICE_FLOW_EXPECTED_CHECK_COUNT
        or passed_count != check_count
        or failed_count != 0,
        "VOICE_FLOW_STATE_GATE_FAILED",
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
        "VOICE_FLOW_DUPLICATE_CHECK_NAME",
    )
    for name, expected in REQUIRED_VOICE_FLOW_CHECKS.items():
        row = check_map.get(name)
        if (
            row is None
            or row.get("expected") != expected
            or row.get("actual") != expected
            or row.get("passed") is not True
        ):
            errors.append(f"VOICE_FLOW_REQUIRED_CHECK_FAILED:{name}")

    artifacts = report.get("input_artifacts")
    artifact_payload = artifacts if isinstance(artifacts, Mapping) else {}
    _append_if(
        errors,
        artifact_payload.get("manifest_sha256")
        != source.get("expected_input_manifest_sha256"),
        "VOICE_FLOW_INPUT_MANIFEST_SHA256_MISMATCH",
    )
    _append_if(
        errors,
        artifact_payload.get("audio_sha256") != source.get("expected_audio_sha256"),
        "VOICE_FLOW_AUDIO_SHA256_MISMATCH",
    )
    for field in ("manifest_sha256", "audio_sha256"):
        _append_if(
            errors,
            not _is_sha256(artifact_payload.get(field)),
            f"VOICE_FLOW_INPUT_SHA256_INVALID:{field}",
        )
    _append_if(
        errors,
        artifact_payload.get("raw_audio_stored_in_report") is not False
        or artifact_payload.get("raw_transcript_stored_in_report") is not False,
        "VOICE_FLOW_RAW_INPUT_SCOPE_UNSAFE",
    )
    disclosure = artifact_payload.get("selection_disclosure")
    disclosure_payload = disclosure if isinstance(disclosure, Mapping) else {}
    required_disclosure = {
        "selected_for_connectivity_not_accuracy": True,
        "prior_unspaced_trial_failed_incident_term": True,
        "performance_claim_allowed": False,
    }
    for field, expected in required_disclosure.items():
        _append_if(
            errors,
            disclosure_payload.get(field) != expected,
            f"VOICE_FLOW_SELECTION_DISCLOSURE_FAILED:{field}",
        )

    runtime = report.get("runtime")
    runtime_payload = runtime if isinstance(runtime, Mapping) else {}
    expected_runtime: dict[str, Any] = {
        "speech_model": "small",
        "speech_device": "cpu",
        "speech_compute_type": "int8",
        "speech_hotwords_used": False,
        "speech_git_commit_verified_by_api": False,
        "speech_model_artifact_verified": False,
        "model_runtime_integrity": "VERIFIED",
    }
    for field, expected in expected_runtime.items():
        _append_if(
            errors,
            runtime_payload.get(field) != expected,
            f"VOICE_FLOW_RUNTIME_FAILED:{field}",
        )
    correlation = report.get("request_correlation")
    correlation_payload = correlation if isinstance(correlation, Mapping) else {}
    expected_correlation: dict[str, Any] = {
        "workflow_key": "incident_id",
        "request_id_policy": "UNIQUE_PER_HTTP_REQUEST_PROPAGATED_WITHIN_CALL",
        "raw_incident_id_stored": False,
        "raw_analysis_id_stored": False,
        "raw_confirmation_id_stored": False,
        "raw_record_id_stored": False,
    }
    for field, expected in expected_correlation.items():
        _append_if(
            errors,
            correlation_payload.get(field) != expected,
            f"VOICE_FLOW_CORRELATION_FAILED:{field}",
        )

    provenance = report.get("provenance")
    provenance_payload = provenance if isinstance(provenance, Mapping) else {}
    runtime_digest = provenance_payload.get("runtime_manifest_sha256")
    runtime_row = check_map.get("runtime_manifest_sha256")
    _append_if(
        errors,
        not _is_sha256(runtime_digest)
        or runtime_row is None
        or runtime_row.get("expected") != runtime_digest
        or runtime_row.get("actual") != runtime_digest
        or runtime_row.get("passed") is not True,
        "VOICE_FLOW_RUNTIME_MANIFEST_UNVERIFIED",
    )
    _append_if(
        errors,
        not _is_sha256(provenance_payload.get("evaluator_source_sha256")),
        "VOICE_FLOW_EVALUATOR_SHA256_INVALID",
    )
    for field in ("backend_git_commit", "model_git_commit", "speech_git_commit"):
        value = provenance_payload.get(field)
        _append_if(
            errors,
            not isinstance(value, str)
            or len(value) != 40
            or any(char not in "0123456789abcdef" for char in value),
            f"VOICE_FLOW_PROVENANCE_INVALID:{field}",
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
    backend_cancellation_report_path: Path,
    cross_service_report_path: Path,
    voice_flow_report_path: Path,
    seoul_speech_report_path: Path,
    incheon_speech_report_path: Path,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """잠금 보고서 일곱 개의 무결성·범위·안전 Gate를 결합한다."""

    manifest_path = Path(manifest_path)
    manifest = _load_object(manifest_path)
    manifest_errors = _validate_manifest(manifest)
    sources = (
        manifest.get("sources") if isinstance(manifest.get("sources"), Mapping) else {}
    )
    paths = {
        "analysis_engine": Path(analysis_report_path),
        "backend_state": Path(backend_report_path),
        "backend_cancellation": Path(backend_cancellation_report_path),
        "cross_service_confirmation_flow": Path(cross_service_report_path),
        "cross_service_voice_to_record": Path(voice_flow_report_path),
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
    validation_errors["backend_cancellation"].extend(
        _validate_backend_cancellation(reports["backend_cancellation"])
    )
    validation_errors["cross_service_confirmation_flow"].extend(
        _validate_cross_service(reports["cross_service_confirmation_flow"])
    )
    voice_source = sources.get("cross_service_voice_to_record")
    voice_source_payload = voice_source if isinstance(voice_source, Mapping) else {}
    validation_errors["cross_service_voice_to_record"].extend(
        _validate_voice_flow(
            reports["cross_service_voice_to_record"], voice_source_payload
        )
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
    cancellation_checks = _backend_check_map(reports["backend_cancellation"])
    cross_service_checks = _backend_check_map(
        reports["cross_service_confirmation_flow"]
    )
    voice_flow_checks = _backend_check_map(reports["cross_service_voice_to_record"])
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
        "new_evidence_reanalysis_required": backend_checks.get(
            "new_evidence_reanalyze_required", {}
        ).get("actual"),
        "old_analysis_persisted_after_new_evidence_count": backend_checks.get(
            "record_count_after_stale_attempt", {}
        ).get("actual"),
        "confirmation_cancellation_reanalysis_required": cancellation_checks.get(
            "cancel_reanalyze_required", {}
        ).get("actual"),
        "cancellation_stale_attempt_persisted_record_count": cancellation_checks.get(
            "record_count_after_stale_attempt", {}
        ).get("actual"),
        "post_cancellation_rule_executed": cancellation_checks.get(
            "post_cancel_rule_executed", {}
        ).get("actual"),
        "post_cancellation_risk_display_allowed": cancellation_checks.get(
            "post_cancel_risk_display_allowed", {}
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
        "analysis_backend_http_chain_executed": gate_passed
        and reports["cross_service_confirmation_flow"].get(
            "analysis_backend_http_chain_executed"
        )
        is True,
        "voice_to_record_http_chain_executed": gate_passed
        and reports["cross_service_voice_to_record"].get(
            "voice_to_record_http_chain_executed"
        )
        is True,
        "full_voice_to_operational_handoff_validated": False,
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
            "backend_cancellation": {
                "check_count": reports["backend_cancellation"].get("check_count"),
                "passed_check_count": reports["backend_cancellation"].get(
                    "passed_check_count"
                ),
                "database_runtime": reports["backend_cancellation"].get(
                    "database_runtime"
                ),
            },
            "cross_service_confirmation_flow": {
                "check_count": reports["cross_service_confirmation_flow"].get(
                    "check_count"
                ),
                "passed_check_count": reports["cross_service_confirmation_flow"].get(
                    "passed_check_count"
                ),
                "service_boundary": reports["cross_service_confirmation_flow"].get(
                    "service_boundary"
                ),
                "database_runtime": reports["cross_service_confirmation_flow"].get(
                    "database_runtime"
                ),
                "database_runtime_verified": reports[
                    "cross_service_confirmation_flow"
                ].get("database_runtime_verified"),
            },
            "cross_service_voice_to_record": {
                "check_count": reports["cross_service_voice_to_record"].get(
                    "check_count"
                ),
                "passed_check_count": reports["cross_service_voice_to_record"].get(
                    "passed_check_count"
                ),
                "data_classification": reports["cross_service_voice_to_record"].get(
                    "data_classification"
                ),
                "database_runtime": reports["cross_service_voice_to_record"].get(
                    "database_runtime"
                ),
                "database_runtime_verified": reports[
                    "cross_service_voice_to_record"
                ].get("database_runtime_verified"),
                "human_transcript_review_performed": reports[
                    "cross_service_voice_to_record"
                ].get("human_transcript_review_performed"),
                "human_cas_confirmation_performed": reports[
                    "cross_service_voice_to_record"
                ].get("human_cas_confirmation_performed"),
            },
        },
        "safety_observations_across_separate_suites": combined_safety,
        "cross_service_http_safety_observations": {
            "zero_confirmation_rule_executed": cross_service_checks.get(
                "zero_conflict_executed", {}
            ).get("actual"),
            "zero_confirmation_risk_display_allowed": cross_service_checks.get(
                "zero_risk_display_allowed", {}
            ).get("actual"),
            "one_confirmation_rule_executed": cross_service_checks.get(
                "one_conflict_executed", {}
            ).get("actual"),
            "one_confirmation_risk_display_allowed": cross_service_checks.get(
                "one_risk_display_allowed", {}
            ).get("actual"),
            "two_confirmation_rule_executed": cross_service_checks.get(
                "two_conflict_executed", {}
            ).get("actual"),
            "two_confirmation_risk_display_allowed": cross_service_checks.get(
                "two_risk_display_allowed", {}
            ).get("actual"),
            "post_cancellation_rule_executed": cross_service_checks.get(
                "cancelled_conflict_executed", {}
            ).get("actual"),
            "post_cancellation_risk_display_allowed": cross_service_checks.get(
                "cancelled_risk_display_allowed", {}
            ).get("actual"),
        },
        "voice_to_record_http_safety_observations": {
            "zero_confirmation_rule_executed": voice_flow_checks.get(
                "zero_conflict_executed", {}
            ).get("actual"),
            "zero_confirmation_risk_display_allowed": voice_flow_checks.get(
                "zero_risk_display_allowed", {}
            ).get("actual"),
            "one_confirmation_rule_executed": voice_flow_checks.get(
                "one_conflict_executed", {}
            ).get("actual"),
            "one_confirmation_risk_display_allowed": voice_flow_checks.get(
                "one_risk_display_allowed", {}
            ).get("actual"),
            "two_confirmation_rule_executed": voice_flow_checks.get(
                "two_conflict_executed", {}
            ).get("actual"),
            "two_confirmation_risk_display_allowed": voice_flow_checks.get(
                "two_risk_display_allowed", {}
            ).get("actual"),
            "record_exact_retry_same_id": voice_flow_checks.get(
                "record_exact_retry_same_id", {}
            ).get("actual"),
        },
        "unverified_gaps": [
            "비선택 승인 음성·사람 전사 검토·실제 CAS 확인을 포함한 운영 인계 경로",
            "시설 과거 이력 없음 결과를 HTTP API부터 Backend 인계까지 연결한 경로",
            "실제 현장 무전 음성과 실제 화학사고 결과",
            "음성 물질명의 CAS 사람 정답",
            "Cloud SQL PostgreSQL 동시성·복구·가용성",
            "독립 검수된 파일럿 E2E 200건 이상",
        ],
        "claims_allowed": [
            "잠긴 일곱 보고서가 manifest SHA-256·schema와 일치함",
            "분리된 내부 회귀 suite에서 관측된 안전 계약 위반 건수",
            "Speech·Analysis·Backend 각 구현 경계의 제한된 회귀 상태",
            "공개 합성 사고 1건에서 실제 Backend→Model API HTTP 0→1→2→취소 상태 전이가 실행됨",
            "선택 공개 합성 WAV 1건에서 실제 Speech API→Backend→Model API→record HTTP 0→1→2 상태 전이가 실행됨",
            "각 분석 요청의 request ID가 Backend→Model API→응답 안에서 보존됨",
            "모의 시설명의 과거 공개 이력 NO_HISTORY_MATCH에서 시설 확인 Gate가 유지됨",
            "인증 사용자의 새 SITE_MSDS 확인 뒤 이전 confirmation과 analysis가 stale 처리됨",
            "정확한 활성 ID 취소 뒤 감사 이벤트가 보존되고 과거 analysis 저장이 차단됨",
        ],
        "claims_not_allowed": [
            "선택 합성 음성→record 연결을 사람 확인이 포함된 실제 운영 인계로 표현",
            "현장 정확도·현장 안전성·상용 운영 성능",
            "Cloud SQL PostgreSQL에서 같은 상태 전이가 검증됐다는 주장",
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
