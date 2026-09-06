"""서울·인천 `radio-sim-v1` 결과로 제한된 Whisper LoRA 진입 신호를 판정한다."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from chemiguard119.stt_downstream_evaluation import DownstreamEvaluationError
from chemiguard119.stt_robustness_downstream_evaluation import CONDITIONS, PROFILE_ID


SCHEMA_VERSION = "stt-radio-sim-cross-region-lora-gate-v1"
INPUT_SCHEMA_VERSION = "stt-radio-sim-downstream-silver-eval-v1"
RUNTIME_PROVENANCE_SCHEMA_VERSION = "speech-radio-sim-runtime-provenance-v1"
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_RUNTIME_PROVENANCE_BYTES = 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CONTAINER_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ALLOWED_SIGNAL_REASONS = frozenset(
    {
        "SPECIFIC_PRIORITY_TERM_RECALL_BELOW_0_80",
    }
)
REGIONS = ("incheon", "seoul")
EXPECTED_JOBS = {
    "incheon": "chemicheck119-speech-radio-sim-incheon-cpu",
    "seoul": "chemicheck119-speech-radio-sim-seoul-cpu",
}
STT_RUNTIME_FIELDS = (
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_signals(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DownstreamEvaluationError("LoRA 신호 목록이 없습니다.")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise DownstreamEvaluationError("LoRA 신호 형식이 잘못되었습니다.")
        condition = item.get("condition")
        reason = item.get("reason")
        public_term = item.get("public_term")
        if (
            not isinstance(condition, str)
            or condition not in CONDITIONS
            or reason not in ALLOWED_SIGNAL_REASONS
            or not isinstance(public_term, str)
            or not public_term.strip()
            or len(public_term) > 64
        ):
            raise DownstreamEvaluationError(
                "LoRA 신호 조건 또는 사유가 잘못되었습니다."
            )
        aggregate_denominator = item.get("priority_term_aggregate_denominator")
        aggregate_recall = item.get("priority_term_aggregate_recall")
        term_denominator = item.get("term_denominator")
        term_recall = item.get("term_recall")
        term_false_insertion = item.get("term_false_insertion")
        key = (condition, public_term, str(reason))
        if key in seen:
            raise DownstreamEvaluationError("중복 LoRA 신호가 있습니다.")
        if not (
            type(aggregate_denominator) is int
            and aggregate_denominator >= 20
            and isinstance(aggregate_recall, (int, float))
            and float(aggregate_recall) < 0.8
            and type(term_denominator) is int
            and term_denominator >= 5
            and isinstance(term_recall, (int, float))
            and float(term_recall) < 0.8
            and type(term_false_insertion) is int
            and term_false_insertion >= 0
        ):
            raise DownstreamEvaluationError("우선용어 LoRA 신호 분모가 부족합니다.")
        seen.add(key)
        result.append(item)
    return result


def _stt_runtime_fingerprint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DownstreamEvaluationError("STT runtime 정보가 없습니다.")
    fingerprint = {field: value.get(field) for field in STT_RUNTIME_FIELDS}
    if (
        fingerprint["implementation"] != "faster-whisper"
        or fingerprint["model"] != "small"
        or fingerprint["device"] != "cpu"
        or fingerprint["compute_type"] != "int8"
        or fingerprint["beam_size"] != 5
        or fingerprint["temperature"] != 0.0
        or fingerprint["vad_filter"] is not True
        or fingerprint["condition_on_previous_text"] is not False
        or fingerprint["variants"] != ["baseline"]
    ):
        raise DownstreamEvaluationError("사전 고정한 faster-whisper 기준선과 다릅니다.")
    return fingerprint


def load_region_report(
    path: Path,
    *,
    priority_terms: list[str],
    priority_terms_sha256: str,
) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0 or size > MAX_REPORT_BYTES:
        raise DownstreamEvaluationError(
            f"강건성 후단 보고서 크기가 허용 범위를 벗어났습니다: {size}"
        )
    report = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != INPUT_SCHEMA_VERSION
    ):
        raise DownstreamEvaluationError("지원하지 않는 강건성 후단 보고서입니다.")
    dataset = report.get("dataset")
    artifacts = report.get("input_artifacts")
    evaluation_runtime = report.get("evaluation_runtime")
    speech_artifact = report.get("speech_evaluator_artifact")
    stt_runtime = report.get("stt_runtime")
    model_runtime = report.get("model_api_runtime")
    metrics = report.get("metrics")
    lora = report.get("whisper_lora_gate")
    if (
        report.get("fact_status") != "부분 구현 또는 개발용 데모"
        or "현장 무전" not in str(report.get("evidence_scope") or "")
        or "검증 아님" not in str(report.get("evidence_scope") or "")
        or not isinstance(dataset, dict)
        or dataset.get("profile_id") != PROFILE_ID
        or dataset.get("condition_count") != len(CONDITIONS)
        or dataset.get("derived_data") is not True
        or type(dataset.get("record_count_per_condition")) is not int
        or not 0 < dataset["record_count_per_condition"] <= 200
        or not isinstance(dataset.get("source_manifest_sha256"), str)
        or not SHA256_PATTERN.fullmatch(dataset["source_manifest_sha256"])
        or not isinstance(artifacts, dict)
        or any(
            not isinstance(artifacts.get(key), str)
            or not SHA256_PATTERN.fullmatch(artifacts[key])
            for key in (
                "speech_summary_sha256",
                "private_records_sha256",
                "priority_terms_sha256",
            )
        )
        or artifacts.get("private_records_committed_to_git") is not False
        or artifacts.get("priority_terms_sha256") != priority_terms_sha256
        or not isinstance(evaluation_runtime, dict)
        or not isinstance(evaluation_runtime.get("git_commit"), str)
        or not GIT_COMMIT_PATTERN.fullmatch(evaluation_runtime["git_commit"])
        or not isinstance(speech_artifact, dict)
        or not isinstance(speech_artifact.get("container_image_digest"), str)
        or not CONTAINER_DIGEST_PATTERN.fullmatch(
            speech_artifact["container_image_digest"]
        )
        or not isinstance(stt_runtime, dict)
        or not isinstance(model_runtime, dict)
        or not isinstance(model_runtime.get("service_git_commit"), str)
        or not GIT_COMMIT_PATTERN.fullmatch(model_runtime["service_git_commit"])
        or not isinstance(model_runtime.get("runtime_manifest_sha256"), str)
        or not SHA256_PATTERN.fullmatch(model_runtime["runtime_manifest_sha256"])
        or not isinstance(metrics, dict)
        or metrics.get("profile_id") != PROFILE_ID
        or metrics.get("condition_count") != len(CONDITIONS)
        or metrics.get("record_count_per_condition")
        != dataset["record_count_per_condition"]
        or not isinstance(metrics.get("by_condition"), dict)
        or set(metrics["by_condition"]) != CONDITIONS
        or not isinstance(lora, dict)
        or lora.get("decision") != "NOT_DECIDABLE_FROM_ONE_REGION"
        or lora.get("requires_same_error_across_seoul_and_incheon") is not True
    ):
        raise DownstreamEvaluationError(
            "강건성 후단 보고서의 provenance·범위·Gate 계약이 잘못되었습니다."
        )
    _stt_runtime_fingerprint(stt_runtime)
    signals = _validate_signals(lora.get("signals"))
    if any(signal["public_term"] not in priority_terms for signal in signals):
        raise DownstreamEvaluationError("허용 목록 밖 우선용어 LoRA 신호가 있습니다.")
    return report


def _gate_passed(report: dict[str, Any], gate: str) -> bool:
    value = report["metrics"].get(gate)
    return isinstance(value, dict) and value.get("passed") is True


def validate_runtime_provenance(
    payload: Any, *, reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    regions = payload.get("regions") if isinstance(payload, dict) else None
    comparability = (
        payload.get("comparability_gate") if isinstance(payload, dict) else None
    )
    collector = payload.get("collector") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != RUNTIME_PROVENANCE_SCHEMA_VERSION
        or payload.get("fact_status") != "구현 완료"
        or "현장 무전" not in str(payload.get("evidence_scope") or "")
        or payload.get("source") != "gcloud run jobs executions describe"
        or not isinstance(collector, dict)
        or collector.get("repository") != "chemicheck119-lab/speech-service"
        or not isinstance(collector.get("git_commit"), str)
        or not GIT_COMMIT_PATTERN.fullmatch(collector["git_commit"])
        or not isinstance(comparability, dict)
        or comparability.get("passed") is not True
        or comparability.get("final_lora_decision_made_here") is not False
        or not isinstance(regions, dict)
        or set(regions) != set(REGIONS)
    ):
        raise DownstreamEvaluationError(
            "radio-sim runtime provenance 계약 또는 비교 Gate가 잘못되었습니다."
        )
    for region in REGIONS:
        evidence = regions[region]
        report = reports[region]
        if (
            not isinstance(evidence, dict)
            or evidence.get("job_name") != EXPECTED_JOBS[region]
            or not isinstance(evidence.get("execution_name"), str)
            or not evidence["execution_name"].startswith(EXPECTED_JOBS[region] + "-")
            or evidence.get("completion_succeeded") is not True
            or not isinstance(evidence.get("start_time"), str)
            or not isinstance(evidence.get("completion_time"), str)
            or not isinstance(evidence.get("container_image_digest"), str)
            or not CONTAINER_DIGEST_PATTERN.fullmatch(
                evidence["container_image_digest"]
            )
            or not isinstance(evidence.get("summary_sha256"), str)
            or not SHA256_PATTERN.fullmatch(evidence["summary_sha256"])
            or evidence["summary_sha256"]
            != report["input_artifacts"]["speech_summary_sha256"]
            or evidence.get("source_manifest_sha256")
            != report["dataset"]["source_manifest_sha256"]
            or evidence.get("priority_terms_sha256")
            != report["input_artifacts"]["priority_terms_sha256"]
            or evidence.get("container_image_digest")
            != report["speech_evaluator_artifact"]["container_image_digest"]
            or evidence.get("record_count_per_condition")
            != report["dataset"]["record_count_per_condition"]
            or evidence.get("stt_runtime")
            != _stt_runtime_fingerprint(report["stt_runtime"])
            or not isinstance(evidence.get("run_summary_sha256"), str)
            or not SHA256_PATTERN.fullmatch(evidence["run_summary_sha256"])
        ):
            raise DownstreamEvaluationError(
                f"{region} execution provenance와 downstream 보고서가 결합되지 않습니다."
            )
    if len({regions[region]["summary_sha256"] for region in REGIONS}) != len(REGIONS):
        raise DownstreamEvaluationError(
            "서울·인천 execution에는 서로 다른 STT summary SHA-256이 필요합니다."
        )
    return payload


def load_runtime_provenance(
    path: Path, *, reports: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    size = path.stat().st_size
    if size <= 0 or size > MAX_RUNTIME_PROVENANCE_BYTES:
        raise DownstreamEvaluationError(
            "radio-sim runtime provenance 크기가 허용 범위를 벗어났습니다."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DownstreamEvaluationError(
            "radio-sim runtime provenance JSON이 잘못되었습니다."
        ) from error
    return validate_runtime_provenance(payload, reports=reports)


def build_cross_region_lora_gate(
    *,
    incheon_report: dict[str, Any],
    seoul_report: dict[str, Any],
    incheon_report_sha256: str,
    seoul_report_sha256: str,
    runtime_provenance: dict[str, Any],
    runtime_provenance_sha256: str,
    evaluator_git_commit: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    for label, value in (
        ("인천 보고서", incheon_report_sha256),
        ("서울 보고서", seoul_report_sha256),
    ):
        if not SHA256_PATTERN.fullmatch(value):
            raise DownstreamEvaluationError(f"{label} SHA-256이 올바르지 않습니다.")
    if not GIT_COMMIT_PATTERN.fullmatch(evaluator_git_commit):
        raise DownstreamEvaluationError("비교 평가기 Git commit이 올바르지 않습니다.")
    if not SHA256_PATTERN.fullmatch(runtime_provenance_sha256):
        raise DownstreamEvaluationError(
            "runtime provenance SHA-256이 올바르지 않습니다."
        )

    reports = {"incheon": incheon_report, "seoul": seoul_report}
    validate_runtime_provenance(runtime_provenance, reports=reports)
    source_manifests = {
        region: report["dataset"]["source_manifest_sha256"]
        for region, report in reports.items()
    }
    if len(set(source_manifests.values())) != len(REGIONS):
        raise DownstreamEvaluationError(
            "서울·인천 독립 자료를 증명할 서로 다른 source manifest가 필요합니다."
        )
    evaluator_commits = {
        report["evaluation_runtime"]["git_commit"] for report in reports.values()
    }
    model_commits = {
        report["model_api_runtime"]["service_git_commit"] for report in reports.values()
    }
    runtime_manifests = {
        report["model_api_runtime"]["runtime_manifest_sha256"]
        for report in reports.values()
    }
    stt_runtimes = {
        json.dumps(report.get("stt_runtime"), sort_keys=True, ensure_ascii=False)
        for report in reports.values()
    }
    priority_term_hashes = {
        report["input_artifacts"]["priority_terms_sha256"]
        for report in reports.values()
    }
    speech_image_digests = {
        report["speech_evaluator_artifact"]["container_image_digest"]
        for report in reports.values()
    }
    if (
        len(evaluator_commits) != 1
        or len(model_commits) != 1
        or len(runtime_manifests) != 1
        or len(stt_runtimes) != 1
        or len(priority_term_hashes) != 1
        or len(speech_image_digests) != 1
    ):
        raise DownstreamEvaluationError(
            "두 지역의 STT·후단 평가기·Model API runtime이 동일하지 않습니다."
        )

    signals_by_region: dict[str, set[tuple[str, str, str]]] = {}
    for region, report in reports.items():
        signals_by_region[region] = {
            (
                str(signal["condition"]),
                str(signal["public_term"]),
                str(signal["reason"]),
            )
            for signal in _validate_signals(report["whisper_lora_gate"]["signals"])
        }
    repeated = sorted(signals_by_region["incheon"] & signals_by_region["seoul"])
    gates = {
        region: {
            name: _gate_passed(report, name)
            for name in (
                "evaluation_integrity_gate",
                "analysis_coverage_gate",
                "safety_contract_gate",
                "downstream_evaluation_gate",
            )
        }
        for region, report in reports.items()
    }
    downstream_passed = all(
        all(region_gates.values()) for region_gates in gates.values()
    )
    if not downstream_passed:
        decision = "BLOCK_LORA_ON_DOWNSTREAM_GATE_FAILURE"
    elif repeated:
        decision = "ELIGIBLE_FOR_BOUNDED_LORA_EXPERIMENT_DESIGN"
    else:
        decision = "KEEP_BASELINE_NO_REPEATED_LORA_SIGNAL"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fact_status": "부분 구현 또는 개발용 데모",
        "evaluation_name": "서울·인천 모의 통신 왜곡 Whisper LoRA 진입 Gate",
        "evidence_scope": (
            "서로 다른 서울·인천 AIHub 신고접수 전화의 동일 radio-sim-v1 비교; "
            "현장 무전·CAS 정답·현장 안전성 검증 아님"
        ),
        "input_artifacts": {
            "incheon_report_sha256": incheon_report_sha256,
            "seoul_report_sha256": seoul_report_sha256,
            "runtime_provenance_sha256": runtime_provenance_sha256,
            "source_manifest_sha256_by_region": source_manifests,
            "cloud_run_execution_by_region": {
                region: runtime_provenance["regions"][region]["execution_name"]
                for region in REGIONS
            },
        },
        "evaluation_runtime": {
            "repository": "chemicheck119-lab/analysis-engine",
            "git_commit": evaluator_git_commit,
        },
        "comparability_gate": {
            "passed": True,
            "same_stt_runtime": True,
            "same_speech_image_digest": True,
            "same_priority_terms": True,
            "same_downstream_evaluator": True,
            "same_model_api_artifact": True,
            "different_region_source_manifests": True,
            "summary_hashes_bound_to_completed_cloud_run_executions": True,
        },
        "downstream_gates_by_region": gates,
        "repeated_signal": [
            {"condition": condition, "public_term": public_term, "reason": reason}
            for condition, public_term, reason in repeated
        ],
        "whisper_lora_gate": {
            "decision": decision,
            "passed_for_experiment_design": (
                decision == "ELIGIBLE_FOR_BOUNDED_LORA_EXPERIMENT_DESIGN"
            ),
            "automatic_training_allowed": False,
            "requires_preregistered_train_dev_split_and_cost_cap": True,
            "reason": (
                "동일 조건·동일 우선용어 누락 신호가 서울과 인천에서 반복되고 모든 후단 Gate가 "
                "통과한 경우에만 제한된 LoRA 실험 설계 후보가 됩니다."
            ),
        },
        "claims_allowed": [
            "서울·인천 모의 왜곡에서 사전 등록한 실패 신호의 반복 여부",
            "광주 Training 기반 제한 실험을 설계할 증거 Gate의 통과 여부",
        ],
        "claims_not_allowed": [
            "LoRA가 실제 성능을 개선한다는 주장",
            "실제 현장 무전 일반화 또는 현장 안전 보장",
            "CAS 정답 정확도 개선",
            "Gate 통과만으로 학습·배포를 자동 승인",
        ],
    }


def build_from_paths(
    *,
    incheon_path: Path,
    seoul_path: Path,
    runtime_provenance_path: Path,
    priority_terms: list[str],
    priority_terms_sha256: str,
    evaluator_git_commit: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    reports = {
        "incheon": load_region_report(
            incheon_path,
            priority_terms=priority_terms,
            priority_terms_sha256=priority_terms_sha256,
        ),
        "seoul": load_region_report(
            seoul_path,
            priority_terms=priority_terms,
            priority_terms_sha256=priority_terms_sha256,
        ),
    }
    runtime_provenance = load_runtime_provenance(
        runtime_provenance_path, reports=reports
    )
    return build_cross_region_lora_gate(
        incheon_report=reports["incheon"],
        seoul_report=reports["seoul"],
        incheon_report_sha256=sha256_file(incheon_path),
        seoul_report_sha256=sha256_file(seoul_path),
        runtime_provenance=runtime_provenance,
        runtime_provenance_sha256=sha256_file(runtime_provenance_path),
        evaluator_git_commit=evaluator_git_commit,
        generated_at=generated_at,
    )


__all__ = [
    "ALLOWED_SIGNAL_REASONS",
    "build_cross_region_lora_gate",
    "build_from_paths",
    "load_region_report",
    "load_runtime_provenance",
    "sha256_file",
    "validate_runtime_provenance",
]
