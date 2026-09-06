from __future__ import annotations

import json
from pathlib import Path

import pytest

from chemiguard119.stt_downstream_evaluation import DownstreamEvaluationError
from chemiguard119.stt_robustness_downstream_evaluation import CONDITIONS
from chemiguard119.stt_robustness_lora_gate import (
    build_cross_region_lora_gate,
    load_region_report,
)


def _signal(
    condition: str = "wind_snr0",
    public_term: str = "연기",
) -> dict:
    return {
        "condition": condition,
        "reason": "SPECIFIC_PRIORITY_TERM_RECALL_BELOW_0_80",
        "public_term": public_term,
        "priority_term_aggregate_denominator": 20,
        "priority_term_aggregate_recall": 0.7,
        "term_denominator": 5,
        "term_recall": 0.6,
        "term_false_insertion": 0,
    }


def _report(
    source_digest: str,
    *,
    signals: list[dict] | None = None,
    summary_digest: str = "a" * 64,
) -> dict:
    gates = {
        name: {"passed": True}
        for name in (
            "evaluation_integrity_gate",
            "analysis_coverage_gate",
            "safety_contract_gate",
            "downstream_evaluation_gate",
        )
    }
    return {
        "schema_version": "stt-radio-sim-downstream-silver-eval-v1",
        "fact_status": "부분 구현 또는 개발용 데모",
        "evidence_scope": "모의 통신 왜곡 평가; 현장 무전·CAS 정답 검증 아님",
        "dataset": {
            "profile_id": "radio-sim-v1",
            "source_manifest_sha256": source_digest,
            "record_count_per_condition": 40,
            "condition_count": len(CONDITIONS),
            "derived_data": True,
        },
        "input_artifacts": {
            "speech_summary_sha256": summary_digest,
            "private_records_sha256": "b" * 64,
            "priority_terms_sha256": "f" * 64,
            "private_records_committed_to_git": False,
        },
        "evaluation_runtime": {"git_commit": "c" * 40},
        "speech_evaluator_artifact": {"container_image_digest": "sha256:" + "3" * 64},
        "stt_runtime": {
            "implementation": "faster-whisper",
            "version": "1.2.1",
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
            "language": "ko (configured, not detected)",
            "beam_size": 5,
            "temperature": 0.0,
            "vad_filter": True,
            "condition_on_previous_text": False,
            "variants": ["baseline"],
        },
        "model_api_runtime": {
            "service_git_commit": "d" * 40,
            "runtime_manifest_sha256": "e" * 64,
        },
        "metrics": {
            "profile_id": "radio-sim-v1",
            "condition_count": len(CONDITIONS),
            "record_count_per_condition": 40,
            "by_condition": {condition: {} for condition in CONDITIONS},
            **gates,
        },
        "whisper_lora_gate": {
            "decision": "NOT_DECIDABLE_FROM_ONE_REGION",
            "signals": signals or [],
            "requires_same_error_across_seoul_and_incheon": True,
        },
    }


def _runtime_provenance(incheon: dict, seoul: dict) -> dict:
    reports = {"incheon": incheon, "seoul": seoul}
    jobs = {
        "incheon": "chemicheck119-speech-radio-sim-incheon-cpu",
        "seoul": "chemicheck119-speech-radio-sim-seoul-cpu",
    }
    return {
        "schema_version": "speech-radio-sim-runtime-provenance-v1",
        "fact_status": "구현 완료",
        "evidence_scope": "모의 통신 왜곡 실행 provenance; 현장 무전 검증 아님",
        "source": "gcloud run jobs executions describe",
        "collector": {
            "repository": "chemicheck119-lab/speech-service",
            "git_commit": "9" * 40,
        },
        "regions": {
            region: {
                "execution_name": jobs[region] + "-abcde",
                "job_name": jobs[region],
                "container_image_digest": report["speech_evaluator_artifact"][
                    "container_image_digest"
                ],
                "start_time": "2026-09-06T00:00:00Z",
                "completion_time": "2026-09-06T01:00:00Z",
                "completion_succeeded": True,
                "summary_sha256": report["input_artifacts"]["speech_summary_sha256"],
                "source_manifest_sha256": report["dataset"]["source_manifest_sha256"],
                "run_summary_sha256": ("7" if region == "incheon" else "8") * 64,
                "priority_terms_sha256": report["input_artifacts"][
                    "priority_terms_sha256"
                ],
                "record_count_per_condition": report["dataset"][
                    "record_count_per_condition"
                ],
                "stt_runtime": {
                    key: report["stt_runtime"][key]
                    for key in (
                        "implementation",
                        "version",
                        "model",
                        "device",
                        "compute_type",
                        "language",
                        "beam_size",
                        "temperature",
                        "vad_filter",
                        "condition_on_previous_text",
                        "variants",
                    )
                },
            }
            for region, report in reports.items()
        },
        "comparability_gate": {
            "passed": True,
            "final_lora_decision_made_here": False,
        },
    }


def _build(incheon: dict, seoul: dict) -> dict:
    provenance = _runtime_provenance(incheon, seoul)
    return build_cross_region_lora_gate(
        incheon_report=incheon,
        seoul_report=seoul,
        incheon_report_sha256="1" * 64,
        seoul_report_sha256="2" * 64,
        runtime_provenance=provenance,
        runtime_provenance_sha256="8" * 64,
        evaluator_git_commit="f" * 40,
        generated_at="2026-09-05T00:00:00Z",
    )


def test_loader_rejects_signal_without_preregistered_denominator(
    tmp_path: Path,
) -> None:
    report = _report("1" * 64, signals=[_signal()])
    report["whisper_lora_gate"]["signals"][0]["priority_term_aggregate_denominator"] = (
        19
    )
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(DownstreamEvaluationError, match="분모"):
        load_region_report(
            path, priority_terms=["연기"], priority_terms_sha256="f" * 64
        )


def test_loader_rejects_signal_outside_bound_priority_terms(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            _report("1" * 64, signals=[_signal(public_term="염산")]),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(DownstreamEvaluationError, match="허용 목록 밖"):
        load_region_report(
            path, priority_terms=["연기"], priority_terms_sha256="f" * 64
        )


def test_same_condition_term_and_reason_opens_experiment_design_gate() -> None:
    report = _build(
        _report("1" * 64, signals=[_signal()], summary_digest="a" * 64),
        _report("2" * 64, signals=[_signal()], summary_digest="9" * 64),
    )

    gate = report["whisper_lora_gate"]
    assert gate["decision"] == "ELIGIBLE_FOR_BOUNDED_LORA_EXPERIMENT_DESIGN"
    assert gate["passed_for_experiment_design"] is True
    assert gate["automatic_training_allowed"] is False
    assert report["repeated_signal"] == [
        {
            "condition": "wind_snr0",
            "public_term": "연기",
            "reason": "SPECIFIC_PRIORITY_TERM_RECALL_BELOW_0_80",
        }
    ]


def test_non_repeated_signal_keeps_baseline() -> None:
    report = _build(
        _report(
            "1" * 64,
            signals=[_signal("wind_snr0")],
            summary_digest="a" * 64,
        ),
        _report(
            "2" * 64,
            signals=[_signal("vehicle_snr0")],
            summary_digest="9" * 64,
        ),
    )

    assert report["whisper_lora_gate"]["decision"] == (
        "KEEP_BASELINE_NO_REPEATED_LORA_SIGNAL"
    )
    assert report["repeated_signal"] == []


def test_downstream_gate_failure_blocks_lora_even_with_repeated_signal() -> None:
    incheon = _report("1" * 64, signals=[_signal()], summary_digest="a" * 64)
    seoul = _report("2" * 64, signals=[_signal()], summary_digest="9" * 64)
    seoul["metrics"]["analysis_coverage_gate"]["passed"] = False

    report = _build(incheon, seoul)

    assert report["whisper_lora_gate"]["decision"] == (
        "BLOCK_LORA_ON_DOWNSTREAM_GATE_FAILURE"
    )
    assert report["whisper_lora_gate"]["passed_for_experiment_design"] is False


def test_same_source_manifest_cannot_prove_cross_region_repetition() -> None:
    incheon = _report("1" * 64, signals=[_signal()], summary_digest="a" * 64)
    seoul = _report("1" * 64, signals=[_signal()], summary_digest="9" * 64)

    with pytest.raises(DownstreamEvaluationError, match="서로 다른"):
        _build(incheon, seoul)


def test_runtime_provenance_must_bind_execution_to_summary() -> None:
    incheon = _report("1" * 64, summary_digest="a" * 64)
    seoul = _report("2" * 64, summary_digest="9" * 64)
    provenance = _runtime_provenance(incheon, seoul)
    provenance["regions"]["seoul"]["summary_sha256"] = "0" * 64

    with pytest.raises(DownstreamEvaluationError, match="결합되지"):
        build_cross_region_lora_gate(
            incheon_report=incheon,
            seoul_report=seoul,
            incheon_report_sha256="1" * 64,
            seoul_report_sha256="2" * 64,
            runtime_provenance=provenance,
            runtime_provenance_sha256="8" * 64,
            evaluator_git_commit="f" * 40,
        )
