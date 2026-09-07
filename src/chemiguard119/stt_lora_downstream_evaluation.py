"""LoRA B/C 전사 결과의 Parser·Resolver·확인 Gate 실버 비교기.

Speech `wind_snr0` development Gate를 통과한 동일 132건의 B/C 전사만 받는다.
AIHub label에는 사람 확인 CAS 정답이 없으므로 후보 보존과 구조적 안전 계약만
평가하며 CAS 정답 정확도나 현장 안전성을 주장하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Iterable, Mapping

from chemiguard119.stt_downstream_evaluation import (
    Analyze,
    DownstreamEvaluationError,
    evaluate_pairs,
    load_private_records,
    sha256_file,
)


SCHEMA_VERSION = "stt-lora-wind-downstream-silver-eval-v1"
SPEECH_WIND_PROTOCOL_ID = "whisper-small-lora-wind-dev-evaluation-v1"
SPEECH_ARM_PROTOCOL_ID = "whisper-small-lora-wind-dev-arm-v1"
EXPECTED_DATASET_ID = "aihub_71768_gwangju_fire_lora_dev_wind_snr0"
EXPECTED_DATASET_VERSION = (
    "dataset-71768_downloaded-2026-09-05+whisper-lora-clean-wind-snr0-v1"
)
EXPECTED_RECORDS = 132
EXPECTED_CONDITION = "wind_snr0"
EXPECTED_EVALUATION_ID = "speech_aihub119_gwangju_lora_dev_wind_snr0_132"
EXPECTED_EVIDENCE_SCOPE = (
    "AIHub emergency-call Training derivative with procedural wind; "
    "not field-radio validation"
)
ARMS = (
    "B_same_conversion_base_control",
    "C_lora_merged_candidate",
)
EXPECTED_WIND_CHECKS = {
    "clean_gate_passed",
    "all_wind_records_completed",
    "wind_cer_or_wer_improved_with_ci",
    "wind_smoke_recall_improved",
    "wind_priority_f1_improved",
    "false_insertion_nonincrease",
    "both_arms_rtf_within_service_limit",
}
MAX_JSON_BYTES = 4 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
Progress = Callable[[str, int, int], None]


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DownstreamEvaluationError(f"{name} 형식이 올바르지 않습니다.")
    return value


def _read_json(path: Path, name: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DownstreamEvaluationError(f"{name}은 일반 파일이어야 합니다.")
    size = path.stat().st_size
    if size <= 0 or size > MAX_JSON_BYTES:
        raise DownstreamEvaluationError(f"{name} 크기가 허용 범위를 벗어났습니다.")
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), name)
    except json.JSONDecodeError as exc:
        raise DownstreamEvaluationError(f"{name} JSON 형식이 잘못되었습니다.") from exc


def _require_private_file(path: Path, name: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise DownstreamEvaluationError(f"{name}은 일반 비공개 파일이어야 합니다.")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise DownstreamEvaluationError(f"{name} 권한은 owner-only여야 합니다.")


def _validate_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise DownstreamEvaluationError(f"{name} SHA-256이 올바르지 않습니다.")
    return value


def _validate_summary(
    summary: dict[str, Any],
    *,
    arm: str,
    expected_conversion_sha256: str,
) -> dict[str, Any]:
    dataset = _object(summary.get("dataset"), f"{arm} dataset")
    runtime = _object(summary.get("runtime"), f"{arm} runtime")
    bindings = _object(summary.get("input_bindings"), f"{arm} input binding")
    preflight = _object(bindings.get("data_preflight"), f"{arm} data preflight")
    if (
        summary.get("schema_version") != "1.0.0"
        or summary.get("protocol_id") != SPEECH_ARM_PROTOCOL_ID
        or summary.get("experiment_id") != EXPECTED_EVALUATION_ID
        or summary.get("usage_role") != "development"
        or summary.get("evidence_scope") != EXPECTED_EVIDENCE_SCOPE
        or summary.get("fact_status") != "부분 구현 또는 개발용 데모"
        or summary.get("model_arm") != arm
        or summary.get("automatic_adoption_allowed") is not False
        or dataset.get("dataset_id") != EXPECTED_DATASET_ID
        or dataset.get("dataset_version") != EXPECTED_DATASET_VERSION
        or dataset.get("evaluation_id") != EXPECTED_EVALUATION_ID
        or dataset.get("record_count") != EXPECTED_RECORDS
        or dataset.get("expected_record_count") != EXPECTED_RECORDS
        or dataset.get("split") != "Training internal dev"
        or dataset.get("condition") != EXPECTED_CONDITION
        or dataset.get("used_for_tuning") is not True
        or runtime.get("implementation") != "faster-whisper"
        or runtime.get("version") != "1.2.1"
        or runtime.get("requested_device") != "cpu"
        or runtime.get("device") != "cpu"
        or runtime.get("compute_type") != "int8"
        or runtime.get("initialization_fallback") is not None
        or runtime.get("language") != "ko (configured, not detected)"
        or runtime.get("beam_size") != 5
        or runtime.get("temperature") != 0.0
        or runtime.get("vad_filter") is not True
        or runtime.get("condition_on_previous_text") is not False
        or runtime.get("variants") != ["baseline"]
        or bindings.get("conversion_report_sha256") != expected_conversion_sha256
        or set(preflight) != {"execution_config", "experiment_config", "run_summary"}
    ):
        raise DownstreamEvaluationError(
            f"{arm} summary가 등록된 wind development 실행과 다릅니다."
        )
    for name, value in preflight.items():
        _validate_sha256(value, f"{arm} data preflight {name}")
    archive = _object(dataset.get("archive_sha256"), f"{arm} archive")
    fingerprint = {
        "dataset_id": dataset.get("dataset_id"),
        "dataset_version": dataset.get("dataset_version"),
        "evaluation_id": dataset.get("evaluation_id"),
        "record_count": dataset.get("record_count"),
        "manifest_sha256": _validate_sha256(
            dataset.get("manifest_sha256"), f"{arm} manifest"
        ),
        "audio_sha256": _validate_sha256(archive.get("audio"), f"{arm} audio"),
        "labels_sha256": _validate_sha256(archive.get("labels"), f"{arm} labels"),
        "data_preflight": dict(preflight),
    }
    return fingerprint


def load_bound_inputs(
    *,
    wind_report_path: Path,
    summaries: Mapping[str, Path],
    records: Mapping[str, Path],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """검증을 통과한 Speech B/C report와 private record를 hash로 결합한다."""

    if set(summaries) != set(ARMS) or set(records) != set(ARMS):
        raise DownstreamEvaluationError("B/C 입력만 정확히 필요합니다.")
    wind = _read_json(wind_report_path, "Speech wind report")
    dataset = _object(wind.get("dataset"), "Speech wind dataset")
    provenance = _object(wind.get("provenance"), "Speech wind provenance")
    source_inputs = _object(provenance.get("inputs"), "Speech wind inputs")
    checks = _object(wind.get("checks"), "Speech wind checks")
    conversion_sha256 = _validate_sha256(
        provenance.get("conversion_report_sha256"), "conversion report"
    )
    wind_preflight = _object(
        dataset.get("data_preflight"), "Speech wind data preflight"
    )
    report_fingerprint = {
        "dataset_id": dataset.get("dataset_id"),
        "dataset_version": dataset.get("dataset_version"),
        "evaluation_id": dataset.get("evaluation_id"),
        "record_count": dataset.get("record_count"),
        "manifest_sha256": _validate_sha256(
            dataset.get("manifest_sha256"), "Speech wind manifest"
        ),
        "audio_sha256": _validate_sha256(
            dataset.get("audio_sha256"), "Speech wind audio"
        ),
        "labels_sha256": _validate_sha256(
            dataset.get("labels_sha256"), "Speech wind labels"
        ),
        "data_preflight": dict(wind_preflight),
    }
    for name, value in wind_preflight.items():
        _validate_sha256(value, f"Speech wind data preflight {name}")
    for name in (
        "clean_report_sha256",
        "experiment_config_sha256",
        "priority_terms_sha256",
    ):
        _validate_sha256(provenance.get(name), f"Speech wind {name}")
    if (
        wind.get("schema_version") != "1.0.0"
        or wind.get("protocol_id") != SPEECH_WIND_PROTOCOL_ID
        or wind.get("status") != "evaluated"
        or wind.get("fact_status") != "부분 구현 또는 개발용 데모"
        or wind.get("evidence_scope") != EXPECTED_EVIDENCE_SCOPE
        or wind.get("decision") != "continue_downstream_safety_gate"
        or wind.get("automatic_adoption_allowed") is not False
        or GIT_COMMIT_PATTERN.fullmatch(str(provenance.get("evaluator_revision", "")))
        is None
        or set(checks) != EXPECTED_WIND_CHECKS
        or not all(value is True for value in checks.values())
        or set(source_inputs) != set(ARMS)
        or dataset.get("dataset_id") != EXPECTED_DATASET_ID
        or dataset.get("dataset_version") != EXPECTED_DATASET_VERSION
        or dataset.get("evaluation_id") != EXPECTED_EVALUATION_ID
        or dataset.get("record_count") != EXPECTED_RECORDS
        or dataset.get("membership_role") != "development_used_for_tuning"
        or dataset.get("condition") != EXPECTED_CONDITION
    ):
        raise DownstreamEvaluationError(
            "Speech wind Gate가 downstream 평가를 허용하지 않습니다."
        )

    rows_by_arm: dict[str, list[dict[str, Any]]] = {}
    fingerprints: dict[str, dict[str, Any]] = {}
    for arm in ARMS:
        _require_private_file(records[arm], f"{arm} records")
        _require_private_file(summaries[arm], f"{arm} summary")
        summary = _read_json(summaries[arm], f"{arm} summary")
        source = _object(source_inputs.get(arm), f"{arm} Speech input hash")
        if _validate_sha256(
            source.get("summary_sha256"), f"{arm} summary"
        ) != sha256_file(summaries[arm]) or _validate_sha256(
            source.get("records_sha256"), f"{arm} records"
        ) != sha256_file(records[arm]):
            raise DownstreamEvaluationError(f"{arm} Speech artifact hash가 다릅니다.")
        rows = load_private_records(records[arm])
        if len(rows) != EXPECTED_RECORDS:
            raise DownstreamEvaluationError(
                f"{arm}에는 정확히 {EXPECTED_RECORDS}건이 필요합니다."
            )
        if any(
            row.get("status") != "completed"
            or not str(row.get("hypothesis") or "").strip()
            for row in rows
        ):
            raise DownstreamEvaluationError(
                f"{arm}의 모든 STT 전사가 완료되어야 합니다."
            )
        rows_by_arm[arm] = rows
        fingerprints[arm] = _validate_summary(
            summary,
            arm=arm,
            expected_conversion_sha256=conversion_sha256,
        )

    if fingerprints[ARMS[0]] != fingerprints[ARMS[1]]:
        raise DownstreamEvaluationError("B/C dataset fingerprint가 다릅니다.")
    if fingerprints[ARMS[0]] != report_fingerprint:
        raise DownstreamEvaluationError(
            "Speech wind report와 B/C dataset fingerprint가 다릅니다."
        )
    left = {str(row["record_key"]): row for row in rows_by_arm[ARMS[0]]}
    right = {str(row["record_key"]): row for row in rows_by_arm[ARMS[1]]}
    if set(left) != set(right):
        raise DownstreamEvaluationError("B/C record pairing이 다릅니다.")
    for key, b_row in left.items():
        c_row = right[key]
        if b_row["reference"] != c_row["reference"] or float(
            b_row.get("audio_seconds", -1)
        ) != float(c_row.get("audio_seconds", -2)):
            raise DownstreamEvaluationError("B/C reference 또는 음성 길이가 다릅니다.")
    return wind, rows_by_arm


def _ratio_rate(metrics: dict[str, Any], section: str, name: str) -> float:
    values = _object(metrics.get(section), section)
    ratio = _object(values.get(name), name)
    rate = ratio.get("rate")
    if not isinstance(rate, (int, float)):
        raise DownstreamEvaluationError(f"{name} 분모가 없어 비교할 수 없습니다.")
    return float(rate)


def _count(metrics: dict[str, Any], section: str, name: str) -> int:
    values = _object(metrics.get(section), section)
    value = values.get(name)
    if not isinstance(value, int):
        raise DownstreamEvaluationError(f"{name} 집계가 올바르지 않습니다.")
    return value


def evaluate_lora_downstream(
    *,
    wind_report_path: Path,
    summaries: Mapping[str, Path],
    records: Mapping[str, Path],
    analyze: Analyze,
    workers: int,
    service_revision: str,
    service_git_commit: str,
    runtime_manifest_sha256: str,
    evaluator_git_commit: str,
    progress: Progress | None = None,
    generated_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    wind, rows_by_arm = load_bound_inputs(
        wind_report_path=wind_report_path,
        summaries=summaries,
        records=records,
    )
    if GIT_COMMIT_PATTERN.fullmatch(service_git_commit) is None:
        raise DownstreamEvaluationError("Model API Git commit이 올바르지 않습니다.")
    if GIT_COMMIT_PATTERN.fullmatch(evaluator_git_commit) is None:
        raise DownstreamEvaluationError("평가기 Git commit이 올바르지 않습니다.")
    _validate_sha256(runtime_manifest_sha256, "Model API runtime manifest")
    if not service_revision.strip() or len(service_revision) > 128:
        raise DownstreamEvaluationError("Model API revision이 올바르지 않습니다.")

    metrics: dict[str, dict[str, Any]] = {}
    private_rows: dict[str, list[dict[str, Any]]] = {}
    for arm in ARMS:
        arm_progress = (
            (lambda completed, total, current=arm: progress(current, completed, total))
            if progress
            else None
        )
        arm_metrics, observations = evaluate_pairs(
            rows_by_arm[arm],
            analyze,
            workers=workers,
            request_namespace=f"lora-wind-v1:{arm}",
            progress=arm_progress,
        )
        metrics[arm] = arm_metrics
        private_rows[arm] = observations

    b, c = ARMS
    b_parser = _ratio_rate(
        metrics[b], "parser_silver", "reference_parser_exact_mention_retention"
    )
    c_parser = _ratio_rate(
        metrics[c], "parser_silver", "reference_parser_exact_mention_retention"
    )
    b_top3 = _ratio_rate(
        metrics[b], "resolver_silver", "reference_candidate_top3_retention"
    )
    c_top3 = _ratio_rate(
        metrics[c], "resolver_silver", "reference_candidate_top3_retention"
    )
    b_new_candidate = _count(
        metrics[b],
        "resolver_silver",
        "reference_negative_hypothesis_candidate_record_count",
    )
    c_new_candidate = _count(
        metrics[c],
        "resolver_silver",
        "reference_negative_hypothesis_candidate_record_count",
    )
    b_inconsistent = _count(
        metrics[b], "resolver_silver", "reference_inconsistent_auto_hint_count"
    )
    c_inconsistent = _count(
        metrics[c], "resolver_silver", "reference_inconsistent_auto_hint_count"
    )
    safety_fields = (
        "two_cas_gate_violation_count",
        "rule_execution_before_confirmation_count",
        "candidate_promotion_violation_count",
        "unconfirmed_risk_output_violation_count",
    )
    checks = {
        "speech_wind_gate_passed": True,
        "all_model_api_calls_completed": all(
            value["evaluation_integrity_gate"]["passed"] is True
            and value["stt_transcript_unavailable_count"] == 0
            and value["hypothesis_analysis_unavailable_count"] == 0
            for value in metrics.values()
        ),
        "all_preconfirmation_safety_contracts_passed": all(
            value["safety_contract_gate"]["passed"] is True
            and all(value["safety"][field] == 0 for field in safety_fields)
            for value in metrics.values()
        ),
        "candidate_parser_retention_nonregression": c_parser >= b_parser,
        "candidate_resolver_top3_retention_nonregression": c_top3 >= b_top3,
        "candidate_new_candidate_signal_nonincrease": c_new_candidate
        <= b_new_candidate,
        "candidate_inconsistent_hint_signal_nonincrease": c_inconsistent
        <= b_inconsistent,
    }
    decision = (
        "pass_proxy_downstream_keep_adoption_blocked"
        if all(checks.values())
        else "reject_candidate_keep_operational_baseline"
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fact_status": "부분 구현 또는 개발용 데모",
        "evaluation_name": "LoRA wind 후단 Parser·Resolver·확인 Gate 실버 평가",
        "evidence_scope": (
            "AIHub 광주 신고전화 Training 내부 dev와 절차적 wind_snr0 파생 음성의 "
            "실버 후보 보존 평가; CAS 정답·현장 무전·현장 안전 검증 아님"
        ),
        "dataset": {
            "dataset_id": EXPECTED_DATASET_ID,
            "dataset_version": EXPECTED_DATASET_VERSION,
            "usage_role": "development",
            "used_for_tuning": True,
            "condition": EXPECTED_CONDITION,
            "record_count": EXPECTED_RECORDS,
        },
        "input_artifacts": {
            "speech_wind_report_sha256": sha256_file(wind_report_path),
            "speech_inputs": {
                arm: {
                    "summary_sha256": sha256_file(summaries[arm]),
                    "private_records_sha256": sha256_file(records[arm]),
                }
                for arm in ARMS
            },
            "private_records_committed_to_git": False,
        },
        "speech_gate": {
            "protocol_id": wind["protocol_id"],
            "decision": wind["decision"],
            "evaluator_revision": wind["provenance"].get("evaluator_revision"),
        },
        "evaluation_runtime": {
            "evaluator_git_commit": evaluator_git_commit,
            "model_api_service_revision": service_revision,
            "model_api_git_commit": service_git_commit,
            "model_api_runtime_manifest_sha256": runtime_manifest_sha256,
        },
        "metrics_by_arm": metrics,
        "comparison": {
            "C_minus_B": {
                "parser_exact_mention_retention_delta": c_parser - b_parser,
                "resolver_candidate_top3_retention_delta": c_top3 - b_top3,
                "reference_negative_candidate_record_delta": c_new_candidate
                - b_new_candidate,
                "reference_inconsistent_auto_hint_delta": c_inconsistent
                - b_inconsistent,
            },
            "cas_ground_truth_available": False,
            "wrong_single_cas_promotion_ground_truth_count": None,
        },
        "checks": checks,
        "decision": decision,
        "automatic_adoption_allowed": False,
        "claims_allowed": [
            "동일 실버 기준에서 B 대비 C의 Parser·Resolver 후보 보존 회귀 여부",
            "미확인 입력에서 후보 승격·위험 출력·Rule 실행 구조적 위반 건수",
        ],
        "claims_not_allowed": [
            "STT→Resolver CAS Top-1·Top-3 정답 정확도",
            "잘못된 단일 CAS 확정 0건이라는 정답 기반 주장",
            "실제 현장 무전 정확도 또는 현장 안전성",
            "운영 model 자동 채택 또는 상용 운영 성능",
        ],
        "remaining_gates": [
            "사람이 확인한 CAS 정답 기반 승격 오류 평가",
            "untouched-region 평가",
            "실제 현장 또는 허가된 유사 무전 자료 검증",
        ],
    }
    return report, private_rows


def write_outputs(
    output_dir: Path,
    report: dict[str, Any],
    private_rows: Mapping[str, Iterable[dict[str, Any]]],
) -> tuple[Path, dict[str, Path]]:
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError("기존 LoRA downstream 출력을 덮어쓰지 않습니다.")
    output_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_dir.parent.is_symlink() or not output_dir.parent.is_dir():
        raise DownstreamEvaluationError("출력 상위 경로는 일반 디렉터리여야 합니다.")
    output_dir.mkdir(mode=0o700)
    report_path = output_dir / "report.json"
    private_paths = {arm: output_dir / f"{arm}.private.jsonl" for arm in ARMS}
    try:
        descriptor = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        for arm in ARMS:
            descriptor = os.open(
                private_paths[arm], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
                for row in private_rows[arm]:
                    destination.write(json.dumps(row, ensure_ascii=False) + "\n")
    except BaseException:
        for path in (report_path, *private_paths.values()):
            path.unlink(missing_ok=True)
        output_dir.rmdir()
        raise
    return report_path, private_paths


__all__ = [
    "ARMS",
    "EXPECTED_RECORDS",
    "SCHEMA_VERSION",
    "evaluate_lora_downstream",
    "load_bound_inputs",
    "write_outputs",
]
