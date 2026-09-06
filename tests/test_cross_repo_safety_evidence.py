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
        "speech_seoul_radio_sim": tmp_path / "seoul.json",
        "speech_incheon_radio_sim": tmp_path / "incheon.json",
    }
    _write(paths["analysis_engine"], _analysis_report())
    _write(paths["backend_state"], _backend_report())
    _write(paths["speech_seoul_radio_sim"], _speech_report("1" * 64))
    _write(paths["speech_incheon_radio_sim"], _speech_report("2" * 64))
    schemas = {
        "analysis_engine": "chemicheck119-e2e-evaluation-report-v4",
        "backend_state": "chemicheck119-backend-safety-evaluation-v2",
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
    assert report["coverage"]["speech"]["condition_input_count"] == 1440
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
