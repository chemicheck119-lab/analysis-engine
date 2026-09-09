from __future__ import annotations

import json
from pathlib import Path

from chemiguard119.cross_repo_safety_evidence import (
    MANIFEST_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    aggregate_cross_repo_safety_evidence,
)
from chemiguard119.utils import sha256_file


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _analysis_report() -> dict:
    capabilities = {
        name: {"case_count": 1, "passed_case_count": 1, "pass_rate": 1.0}
        for name in (
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
        )
    }
    return {
        "schema_version": "chemicheck119-e2e-evaluation-report-v4",
        "metrics_version": "incident-e2e-evaluation-v4",
        "status": "COMPLETED",
        "claim_scope": "INTERNAL_REGRESSION_ONLY",
        "field_validated": False,
        "is_field_performance_estimate": False,
        "case_count": 1,
        "passed_case_count": 1,
        "failed_case_count": 0,
        "cases": [{"case_id": "A", "passed": True}],
        "evaluation_contract": {
            "passed": True,
            "profile": "INTERNAL_REGRESSION",
            "expert_reviewed": False,
        },
        "capability_coverage": capabilities,
        "metrics": {
            "output_contract_pass_rate": 1.0,
            "scenario_pass_rate": 1.0,
            "unsafe_conflict_execution_count": 0,
            "unconfirmed_risk_exposure_count": 0,
            "llm_timeout_fallback_pass_rate": 1.0,
            "grounded_rag_contract_pass_rate": 1.0,
            "uncited_grounded_rag_case_count": 0,
            "facility_history_expected_count": 1,
            "facility_history_absence_pass_rate": 1.0,
        },
    }


def _backend_report() -> dict:
    values = {
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
        "fresh_record_id": "REC-1",
        "retry_record_id": "REC-1",
    }
    checks = [
        {"name": name, "expected": value, "actual": value, "passed": True}
        for name, value in values.items()
    ]
    return {
        "schema_version": "chemicheck119-backend-safety-evaluation-v2",
        "status": "COMPLETED",
        "claim_scope": "INTERNAL_REGRESSION_ONLY",
        "field_validated": False,
        "cloud_sql_validated": False,
        "concurrency_validated": False,
        "database_runtime": "H2_POSTGRESQL_COMPATIBILITY_MODE",
        "check_count": len(checks),
        "passed_check_count": len(checks),
        "failed_check_count": 0,
        "checks": checks,
    }


def _backend_cancellation_report() -> dict:
    values = {
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
    checks = [
        {"name": name, "expected": value, "actual": value, "passed": True}
        for name, value in values.items()
    ]
    return {
        "schema_version": "chemicheck119-confirmation-cancellation-evaluation-v1",
        "status": "COMPLETED",
        "claim_scope": "INTERNAL_REGRESSION_ONLY",
        "field_validated": False,
        "cloud_sql_validated": False,
        "database_runtime": "H2",
        "check_count": len(checks),
        "passed_check_count": len(checks),
        "failed_check_count": 0,
        "checks": checks,
    }


def _cross_service_report() -> dict:
    runtime_digest = "3" * 64
    values = {
        "model_ready": "READY",
        "model_runtime_integrity": "VERIFIED",
        "model_runtime_manifest_verified": True,
        "runtime_manifest_sha256": runtime_digest,
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
    checks = [
        {"name": name, "expected": value, "actual": value, "passed": True}
        for name, value in values.items()
    ]
    return {
        "schema_version": "chemicheck119-cross-service-confirmation-flow-v1",
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
        "check_count": len(checks),
        "passed_check_count": len(checks),
        "failed_check_count": 0,
        "checks": checks,
        "provenance": {
            "backend_git_commit": "4" * 40,
            "model_git_commit": "5" * 40,
            "runtime_manifest_sha256": runtime_digest,
            "evaluator_source_sha256": "6" * 64,
        },
    }


def _voice_flow_report() -> dict:
    runtime_digest = "7" * 64
    input_manifest_digest = "8" * 64
    audio_digest = "9" * 64
    values = {
        "model_ready": "READY",
        "model_runtime_integrity": "VERIFIED",
        "model_runtime_manifest_verified": True,
        "runtime_manifest_sha256": runtime_digest,
        "speech_status": "TRANSCRIBED",
        "speech_abstained": False,
        "speech_requires_responder_review": True,
        "speech_audio_retained": False,
        "speech_hotwords_used": False,
        "speech_service_git_commit": "c" * 40,
        "speech_model_repository": "Systran/faster-whisper-small",
        "speech_model_revision": "e" * 40,
        "speech_model_bin_sha256": "f" * 64,
        "speech_model_artifact_verified": True,
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
    checks = [
        {"name": name, "expected": value, "actual": value, "passed": True}
        for name, value in values.items()
    ]
    checks.extend(
        {
            "name": f"additional_contract_check_{index}",
            "expected": True,
            "actual": True,
            "passed": True,
        }
        for index in range(69 - len(checks))
    )
    return {
        "schema_version": "chemicheck119-cross-service-voice-to-record-v1",
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
        "input_artifacts": {
            "audio_sha256": audio_digest,
            "manifest_sha256": input_manifest_digest,
            "raw_audio_stored_in_report": False,
            "raw_transcript_stored_in_report": False,
            "selection_disclosure": {
                "selected_for_connectivity_not_accuracy": True,
                "prior_unspaced_trial_failed_incident_term": True,
                "performance_claim_allowed": False,
            },
        },
        "runtime": {
            "speech_model": "small",
            "speech_device": "cpu",
            "speech_compute_type": "int8",
            "speech_hotwords_used": False,
            "speech_service_git_commit": "c" * 40,
            "speech_model_repository": "Systran/faster-whisper-small",
            "speech_model_revision": "e" * 40,
            "speech_model_bin_sha256": "f" * 64,
            "speech_git_commit_verified_by_api": True,
            "speech_model_artifact_verified": True,
            "model_runtime_integrity": "VERIFIED",
        },
        "request_correlation": {
            "workflow_key": "incident_id",
            "request_id_policy": "UNIQUE_PER_HTTP_REQUEST_PROPAGATED_WITHIN_CALL",
            "raw_incident_id_stored": False,
            "raw_analysis_id_stored": False,
            "raw_confirmation_id_stored": False,
            "raw_record_id_stored": False,
        },
        "check_count": len(checks),
        "passed_check_count": len(checks),
        "failed_check_count": 0,
        "checks": checks,
        "decision": "CONDITIONALLY_ADOPT_FOR_LOCAL_SYNTHETIC_CONNECTIVITY_REGRESSION",
        "provenance": {
            "backend_git_commit": "a" * 40,
            "model_git_commit": "b" * 40,
            "speech_git_commit": "c" * 40,
            "speech_model_repository": "Systran/faster-whisper-small",
            "speech_model_revision": "e" * 40,
            "speech_model_bin_sha256": "f" * 64,
            "runtime_manifest_sha256": runtime_digest,
            "evaluator_source_sha256": "d" * 64,
        },
    }


def _speech_report(source_digest: str) -> dict:
    return {
        "schema_version": "stt-radio-sim-downstream-silver-eval-v1",
        "fact_status": "부분 구현 또는 개발용 데모",
        "evidence_scope": "AIHub radio-sim이며 현장 무전 검증 아님",
        "dataset": {
            "profile_id": "radio-sim-v1",
            "source_manifest_sha256": source_digest,
            "derived_data": True,
        },
        "metrics": {
            "condition_count": 18,
            "record_count_per_condition": 40,
            "condition_record_count": 720,
            "evaluation_integrity_gate": {"passed": True},
            "analysis_coverage_gate": {"passed": True},
            "safety_contract_gate": {"passed": True},
            "downstream_evaluation_gate": {"passed": True},
            "safety_violation_totals": {
                "candidate_promotion_violation_count": 0,
                "rule_execution_before_confirmation_count": 0,
                "two_cas_gate_violation_count": 0,
                "unconfirmed_risk_output_violation_count": 0,
            },
            "cas_ground_truth_available": False,
            "is_cas_accuracy_evaluation": False,
            "wrong_single_cas_promotion_ground_truth_count": None,
        },
        "stt_runtime": {"model": "small", "compute_type": "int8"},
        "model_api_runtime": {"revision": "same"},
        "speech_evaluator_artifact": {"digest": "same"},
        "evaluation_runtime": {"commit": "same"},
    }


def _fixture(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "analysis_engine": tmp_path / "analysis.json",
        "backend_state": tmp_path / "backend.json",
        "backend_cancellation": tmp_path / "backend-cancellation.json",
        "cross_service_confirmation_flow": tmp_path / "cross-service.json",
        "cross_service_voice_to_record": tmp_path / "voice-to-record.json",
        "speech_seoul_radio_sim": tmp_path / "seoul.json",
        "speech_incheon_radio_sim": tmp_path / "incheon.json",
    }
    _write(paths["analysis_engine"], _analysis_report())
    _write(paths["backend_state"], _backend_report())
    _write(paths["backend_cancellation"], _backend_cancellation_report())
    _write(paths["cross_service_confirmation_flow"], _cross_service_report())
    _write(paths["cross_service_voice_to_record"], _voice_flow_report())
    _write(paths["speech_seoul_radio_sim"], _speech_report("1" * 64))
    _write(paths["speech_incheon_radio_sim"], _speech_report("2" * 64))
    schemas = {
        "analysis_engine": "chemicheck119-e2e-evaluation-report-v4",
        "backend_state": "chemicheck119-backend-safety-evaluation-v2",
        "backend_cancellation": "chemicheck119-confirmation-cancellation-evaluation-v1",
        "cross_service_confirmation_flow": (
            "chemicheck119-cross-service-confirmation-flow-v1"
        ),
        "cross_service_voice_to_record": (
            "chemicheck119-cross-service-voice-to-record-v1"
        ),
        "speech_seoul_radio_sim": "stt-radio-sim-downstream-silver-eval-v1",
        "speech_incheon_radio_sim": "stt-radio-sim-downstream-silver-eval-v1",
    }
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "fact_status": "부분 구현 또는 개발용 데모",
        "sources": {
            source_id: {
                "repository": f"test/{source_id}",
                "expected_schema_version": schemas[source_id],
                "expected_sha256": sha256_file(path),
                **(
                    {
                        "expected_input_manifest_sha256": "8" * 64,
                        "expected_audio_sha256": "9" * 64,
                    }
                    if source_id == "cross_service_voice_to_record"
                    else {}
                ),
            }
            for source_id, path in paths.items()
        },
    }
    manifest_path = tmp_path / "manifest.json"
    _write(manifest_path, manifest)
    return {**paths, "manifest": manifest_path}


def _aggregate(paths: dict[str, Path], output: Path | None = None) -> dict:
    return aggregate_cross_repo_safety_evidence(
        manifest_path=paths["manifest"],
        analysis_report_path=paths["analysis_engine"],
        backend_report_path=paths["backend_state"],
        backend_cancellation_report_path=paths["backend_cancellation"],
        cross_service_report_path=paths["cross_service_confirmation_flow"],
        voice_flow_report_path=paths["cross_service_voice_to_record"],
        seoul_speech_report_path=paths["speech_seoul_radio_sim"],
        incheon_speech_report_path=paths["speech_incheon_radio_sim"],
        report_path=output,
    )


def test_aggregate_accepts_locked_separate_internal_suites(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    output = tmp_path / "combined.json"

    report = _aggregate(paths, output)

    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["status"] == "COMPLETED"
    assert report["decision"] == "CONDITIONALLY_ADOPT_FOR_INTERNAL_REGRESSION"
    assert report["evidence_integrity_gate"]["passed"] is True
    assert report["field_validated"] is False
    assert report["full_chain_executed"] is False
    assert report["analysis_backend_http_chain_executed"] is True
    assert report["voice_to_record_http_chain_executed"] is True
    assert report["full_voice_to_operational_handoff_validated"] is False
    assert report["coverage"]["speech"]["condition_input_count"] == 1440
    assert report["coverage"]["backend_cancellation"]["check_count"] == 17
    assert report["coverage"]["cross_service_confirmation_flow"]["check_count"] == 23
    assert report["coverage"]["cross_service_voice_to_record"]["check_count"] > 0
    assert (
        report["coverage"]["cross_service_voice_to_record"][
            "human_transcript_review_performed"
        ]
        is False
    )
    assert (
        report["cross_service_http_safety_observations"][
            "zero_confirmation_rule_executed"
        ]
        is False
    )
    assert (
        report["cross_service_http_safety_observations"][
            "two_confirmation_rule_executed"
        ]
        is True
    )
    assert (
        report["voice_to_record_http_safety_observations"][
            "one_confirmation_rule_executed"
        ]
        is False
    )
    assert (
        report["voice_to_record_http_safety_observations"]["record_exact_retry_same_id"]
        is True
    )
    assert (
        report["safety_observations_across_separate_suites"][
            "rule_execution_before_two_confirmations_observed_count"
        ]
        == 0
    )
    assert (
        report["safety_observations_across_separate_suites"][
            "wrong_single_cas_promotion_ground_truth_count"
        ]
        is None
    )
    assert (
        report["safety_observations_across_separate_suites"][
            "new_evidence_reanalysis_required"
        ]
        is True
    )
    assert (
        report["safety_observations_across_separate_suites"][
            "old_analysis_persisted_after_new_evidence_count"
        ]
        == 0
    )
    assert (
        report["safety_observations_across_separate_suites"][
            "confirmation_cancellation_reanalysis_required"
        ]
        is True
    )
    assert (
        report["safety_observations_across_separate_suites"][
            "cancellation_stale_attempt_persisted_record_count"
        ]
        == 0
    )
    assert output.is_file()


def test_aggregate_rejects_report_changed_after_manifest_lock(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    analysis = json.loads(paths["analysis_engine"].read_text(encoding="utf-8"))
    analysis["metrics"]["unsafe_conflict_execution_count"] = 1
    _write(paths["analysis_engine"], analysis)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert report["decision"] == "REJECT_EVIDENCE_BUNDLE"
    assert report["evidence_integrity_gate"]["passed"] is False
    assert (
        "SHA256_MISMATCH"
        in report["evidence_integrity_gate"]["errors_by_source"]["analysis_engine"]
    )
    assert (
        "ANALYSIS_METRIC_FAILED:unsafe_conflict_execution_count"
        in report["evidence_integrity_gate"]["errors_by_source"]["analysis_engine"]
    )


def test_aggregate_rejects_cancellation_rule_execution_even_when_relocked(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    cancellation = json.loads(paths["backend_cancellation"].read_text(encoding="utf-8"))
    target = next(
        check
        for check in cancellation["checks"]
        if check["name"] == "post_cancel_rule_executed"
    )
    target["actual"] = True
    _write(paths["backend_cancellation"], cancellation)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["backend_cancellation"]["expected_sha256"] = sha256_file(
        paths["backend_cancellation"]
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert (
        "BACKEND_CANCELLATION_REQUIRED_CHECK_FAILED:post_cancel_rule_executed"
        in report["evidence_integrity_gate"]["errors_by_source"]["backend_cancellation"]
    )


def test_aggregate_rejects_cross_service_early_rule_execution_even_when_relocked(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    cross_service = json.loads(
        paths["cross_service_confirmation_flow"].read_text(encoding="utf-8")
    )
    target = next(
        check
        for check in cross_service["checks"]
        if check["name"] == "zero_conflict_executed"
    )
    target["actual"] = True
    _write(paths["cross_service_confirmation_flow"], cross_service)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["cross_service_confirmation_flow"]["expected_sha256"] = (
        sha256_file(paths["cross_service_confirmation_flow"])
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert report["analysis_backend_http_chain_executed"] is False
    assert (
        "CROSS_SERVICE_REQUIRED_CHECK_FAILED:zero_conflict_executed"
        in report["evidence_integrity_gate"]["errors_by_source"][
            "cross_service_confirmation_flow"
        ]
    )


def test_aggregate_rejects_voice_flow_early_rule_execution_even_when_relocked(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    voice = json.loads(
        paths["cross_service_voice_to_record"].read_text(encoding="utf-8")
    )
    target = next(
        check for check in voice["checks"] if check["name"] == "one_conflict_executed"
    )
    target["actual"] = True
    _write(paths["cross_service_voice_to_record"], voice)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["cross_service_voice_to_record"]["expected_sha256"] = (
        sha256_file(paths["cross_service_voice_to_record"])
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert report["voice_to_record_http_chain_executed"] is False
    assert (
        "VOICE_FLOW_REQUIRED_CHECK_FAILED:one_conflict_executed"
        in report["evidence_integrity_gate"]["errors_by_source"][
            "cross_service_voice_to_record"
        ]
    )


def test_aggregate_rejects_voice_flow_false_human_review_claim_when_relocked(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    voice = json.loads(
        paths["cross_service_voice_to_record"].read_text(encoding="utf-8")
    )
    voice["human_transcript_review_performed"] = True
    _write(paths["cross_service_voice_to_record"], voice)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["cross_service_voice_to_record"]["expected_sha256"] = (
        sha256_file(paths["cross_service_voice_to_record"])
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert (
        "VOICE_FLOW_SCOPE_FAILED:human_transcript_review_performed"
        in report["evidence_integrity_gate"]["errors_by_source"][
            "cross_service_voice_to_record"
        ]
    )


def test_aggregate_rejects_voice_flow_audio_hash_change_even_when_relocked(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    voice = json.loads(
        paths["cross_service_voice_to_record"].read_text(encoding="utf-8")
    )
    voice["input_artifacts"]["audio_sha256"] = "e" * 64
    _write(paths["cross_service_voice_to_record"], voice)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["cross_service_voice_to_record"]["expected_sha256"] = (
        sha256_file(paths["cross_service_voice_to_record"])
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert (
        "VOICE_FLOW_AUDIO_SHA256_MISMATCH"
        in report["evidence_integrity_gate"]["errors_by_source"][
            "cross_service_voice_to_record"
        ]
    )


def test_aggregate_rejects_voice_runtime_provenance_drift_when_relocked(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    voice = json.loads(
        paths["cross_service_voice_to_record"].read_text(encoding="utf-8")
    )
    voice["runtime"]["speech_model_bin_sha256"] = "0" * 64
    _write(paths["cross_service_voice_to_record"], voice)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["cross_service_voice_to_record"]["expected_sha256"] = (
        sha256_file(paths["cross_service_voice_to_record"])
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert (
        "VOICE_FLOW_RUNTIME_PROVENANCE_MISMATCH:speech_model_bin_sha256"
        in report["evidence_integrity_gate"]["errors_by_source"][
            "cross_service_voice_to_record"
        ]
    )


def test_aggregate_accepts_pinned_revision_as_local_speech_model_identifier(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    voice = json.loads(
        paths["cross_service_voice_to_record"].read_text(encoding="utf-8")
    )
    voice["runtime"]["speech_model"] = voice["provenance"]["speech_model_revision"]
    _write(paths["cross_service_voice_to_record"], voice)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["cross_service_voice_to_record"]["expected_sha256"] = (
        sha256_file(paths["cross_service_voice_to_record"])
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "COMPLETED"
    assert report["evidence_integrity_gate"]["passed"] is True


def test_aggregate_rejects_unrelated_speech_model_identifier_when_relocked(
    tmp_path: Path,
) -> None:
    paths = _fixture(tmp_path)
    voice = json.loads(
        paths["cross_service_voice_to_record"].read_text(encoding="utf-8")
    )
    voice["runtime"]["speech_model"] = "unverified-local-model"
    _write(paths["cross_service_voice_to_record"], voice)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["cross_service_voice_to_record"]["expected_sha256"] = (
        sha256_file(paths["cross_service_voice_to_record"])
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert (
        "VOICE_FLOW_RUNTIME_PROVENANCE_MISMATCH:speech_model"
        in report["evidence_integrity_gate"]["errors_by_source"][
            "cross_service_voice_to_record"
        ]
    )


def test_aggregate_rejects_same_cross_region_source_or_runtime(tmp_path: Path) -> None:
    paths = _fixture(tmp_path)
    seoul = json.loads(paths["speech_seoul_radio_sim"].read_text(encoding="utf-8"))
    incheon = json.loads(paths["speech_incheon_radio_sim"].read_text(encoding="utf-8"))
    incheon["dataset"]["source_manifest_sha256"] = seoul["dataset"][
        "source_manifest_sha256"
    ]
    _write(paths["speech_incheon_radio_sim"], incheon)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["sources"]["speech_incheon_radio_sim"]["expected_sha256"] = sha256_file(
        paths["speech_incheon_radio_sim"]
    )
    _write(paths["manifest"], manifest)

    report = _aggregate(paths)

    assert report["status"] == "FAILED"
    assert (
        "SPEECH_SOURCE_MANIFESTS_NOT_DISTINCT"
        in report["evidence_integrity_gate"]["errors_by_source"]["cross_region"]
    )
