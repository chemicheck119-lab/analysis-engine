"""`radio-sim-v1` STT 결과의 Parser·Resolver·안전 계약 실버 평가기.

같은 AIHub 신고 전화에서 절차적으로 만든 clean+17개 왜곡 조건을 대상으로 한다.
사람이 확인한 CAS 정답이나 실제 현장 무전이 아니므로 후보 보존과 결정적 안전 계약만
평가하며, CAS 정확도나 현장 안전성을 주장하지 않는다.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable
import unicodedata

from chemiguard119.stt_downstream_evaluation import (
    MAX_TEXT_LENGTH,
    MAX_WORKERS,
    Analyze,
    DownstreamEvaluationError,
    evaluate_pairs,
)


SCHEMA_VERSION = "stt-radio-sim-downstream-silver-eval-v1"
PROFILE_ID = "radio-sim-v1"
MAX_INPUT_BYTES = 512 * 1024 * 1024
MAX_PRIORITY_TERMS_BYTES = 64 * 1024
MAX_PRIORITY_TERMS = 100
MAX_RECORDS_PER_CONDITION = 200
RECORD_KEY_PATTERN = re.compile(r"^[0-9a-f]{16}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
CONTAINER_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
NON_TEXT_PATTERN = re.compile(r"[^0-9a-zA-Z가-힣\s]")
SPACE_PATTERN = re.compile(r"\s+")
CONDITIONS = frozenset(
    {
        "clean",
        "bandlimit_8khz",
        "mulaw_8khz",
        "siren_snr20",
        "siren_snr10",
        "siren_snr0",
        "vehicle_snr20",
        "vehicle_snr10",
        "vehicle_snr0",
        "wind_snr20",
        "wind_snr10",
        "wind_snr0",
        "start_cut_300ms",
        "end_cut_300ms",
        "hard_clip_minus12dbfs",
        "gain_minus18db",
        "dropout_3x120ms",
        "combined_radio_snr10",
    }
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_key_set_sha256(keys: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(keys)).encode("ascii")).hexdigest()


def load_priority_terms(path: Path) -> list[str]:
    size = path.stat().st_size
    if size <= 0 or size > MAX_PRIORITY_TERMS_BYTES:
        raise DownstreamEvaluationError(
            f"우선용어 파일 크기가 허용 범위를 벗어났습니다: {size}"
        )
    terms = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if (
        not terms
        or len(terms) > MAX_PRIORITY_TERMS
        or len(terms) != len(set(terms))
        or any(len(term) > 64 for term in terms)
    ):
        raise DownstreamEvaluationError(
            "우선용어 목록이 비었거나 중복·상한이 있습니다."
        )
    return terms


def _normalize_for_presence(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = NON_TEXT_PATTERN.sub(" ", normalized)
    return SPACE_PATTERN.sub(" ", normalized).strip().replace(" ", "")


def _priority_term_by_term(
    rows: list[dict[str, Any]], priority_terms: list[str]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for term in priority_terms:
        normalized_term = _normalize_for_presence(term)
        true_positive = false_negative = false_insertion = 0
        for row in rows:
            reference = _normalize_for_presence(str(row["reference"]))
            hypothesis = _normalize_for_presence(str(row["hypothesis"]))
            in_reference = normalized_term in reference
            in_hypothesis = normalized_term in hypothesis
            true_positive += int(in_reference and in_hypothesis)
            false_negative += int(in_reference and not in_hypothesis)
            false_insertion += int(not in_reference and in_hypothesis)
        recall_denominator = true_positive + false_negative
        precision_denominator = true_positive + false_insertion
        recall = true_positive / recall_denominator if recall_denominator else None
        precision = (
            true_positive / precision_denominator if precision_denominator else None
        )
        f1 = (
            2 * recall * precision / (recall + precision)
            if recall is not None and precision is not None and recall + precision
            else None
        )
        result.append(
            {
                "term": term,
                "reference_positive_count": recall_denominator,
                "true_positive": true_positive,
                "false_negative": false_negative,
                "false_insertion": false_insertion,
                "recall": recall,
                "precision": precision,
                "f1": f1,
            }
        )
    return result


def load_robustness_private_records(
    path: Path,
) -> dict[str, list[dict[str, Any]]]:
    """비공개 18조건 JSONL을 bounded read하고 paired 불변식을 검증한다."""

    size = path.stat().st_size
    if size <= 0 or size > MAX_INPUT_BYTES:
        raise DownstreamEvaluationError(
            f"강건성 비공개 레코드 크기가 허용 범위를 벗어났습니다: {size}"
        )
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in CONDITIONS}
    seen: set[tuple[str, str]] = set()
    maximum_rows = len(CONDITIONS) * MAX_RECORDS_PER_CONDITION
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if line_number > maximum_rows:
                raise DownstreamEvaluationError(
                    f"강건성 레코드가 상한 {maximum_rows}건을 초과했습니다."
                )
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DownstreamEvaluationError(
                    f"강건성 비공개 JSONL 형식이 잘못되었습니다: line={line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise DownstreamEvaluationError(
                    f"강건성 레코드는 JSON 객체여야 합니다: line={line_number}"
                )
            record_key = row.get("record_key")
            reference = row.get("reference")
            hypothesis = row.get("hypothesis")
            condition = row.get("channel_variant")
            if (
                not isinstance(record_key, str)
                or not RECORD_KEY_PATTERN.fullmatch(record_key)
                or not isinstance(reference, str)
                or not reference.strip()
                or not isinstance(hypothesis, str)
                or not isinstance(condition, str)
                or condition not in CONDITIONS
                or row.get("variant") != "baseline"
                or row.get("inference_variant") != "baseline"
            ):
                raise DownstreamEvaluationError(
                    f"강건성 필수 필드가 잘못되었습니다: line={line_number}"
                )
            if len(reference) > MAX_TEXT_LENGTH or len(hypothesis) > MAX_TEXT_LENGTH:
                raise DownstreamEvaluationError(
                    "Model API 입력 길이 상한을 초과한 강건성 레코드가 있습니다."
                )
            key = (condition, record_key)
            if key in seen:
                raise DownstreamEvaluationError(
                    "같은 조건에 중복 record_key가 있습니다."
                )
            seen.add(key)
            grouped[condition].append(row)

    counts = {condition: len(rows) for condition, rows in grouped.items()}
    if not counts or any(count <= 0 for count in counts.values()):
        raise DownstreamEvaluationError("clean+17개 왜곡 조건이 모두 필요합니다.")
    if len(set(counts.values())) != 1:
        raise DownstreamEvaluationError("조건별 레코드 수가 서로 다릅니다.")

    clean = {str(row["record_key"]): str(row["reference"]) for row in grouped["clean"]}
    for condition, rows in grouped.items():
        references = {str(row["record_key"]): str(row["reference"]) for row in rows}
        if references != clean:
            raise DownstreamEvaluationError(
                f"clean과 paired record/reference가 다릅니다: {condition}"
            )
        rows.sort(key=lambda row: str(row["record_key"]))
    return grouped


def load_robustness_summary(
    path: Path,
    *,
    rows_by_condition: dict[str, list[dict[str, Any]]],
    priority_terms: list[str],
    priority_terms_sha256: str,
) -> dict[str, Any]:
    """speech-service 강건성 summary와 비공개 레코드의 범위를 결합 검증한다."""

    summary = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict) or summary.get("schema_version") != "1.0.0":
        raise DownstreamEvaluationError("지원하지 않는 강건성 STT summary입니다.")
    simulation = summary.get("simulation_run")
    runtime = summary.get("runtime")
    variants = summary.get("variants")
    record_count = len(rows_by_condition.get("clean", []))
    clean_keys = [str(row["record_key"]) for row in rows_by_condition.get("clean", [])]
    variant_contract_invalid = False
    if isinstance(variants, dict) and set(variants) == CONDITIONS:
        for condition in CONDITIONS:
            variant = variants[condition]
            terms = (
                variant.get("priority_term_presence")
                if isinstance(variant, dict)
                else None
            )
            if (
                not isinstance(variant, dict)
                or variant.get("record_count") != record_count
                or not isinstance(terms, dict)
                or type(terms.get("true_positive")) is not int
                or type(terms.get("false_negative")) is not int
                or terms["true_positive"] < 0
                or terms["false_negative"] < 0
                or (
                    terms.get("recall") is not None
                    and not isinstance(terms.get("recall"), (int, float))
                )
            ):
                variant_contract_invalid = True
                break
    else:
        variant_contract_invalid = True

    if (
        summary.get("usage_role") != "evaluation"
        or "simulated communication distortion"
        not in str(summary.get("evidence_scope") or "")
        or "not field-radio" not in str(summary.get("evidence_scope") or "")
        or summary.get("record_count") != record_count
        or summary.get("record_key_set_sha256") != _record_key_set_sha256(clean_keys)
        or not isinstance(simulation, dict)
        or simulation.get("profile_id") != PROFILE_ID
        or not isinstance(simulation.get("source_manifest_sha256"), str)
        or not SHA256_PATTERN.fullmatch(simulation["source_manifest_sha256"])
        or simulation.get("priority_terms_sha256") != priority_terms_sha256
        or simulation.get("variant_count") != len(CONDITIONS)
        or not isinstance(simulation.get("selected"), dict)
        or simulation["selected"].get("total") != record_count
        or not isinstance(runtime, dict)
        or runtime.get("variants") != ["baseline"]
        or runtime.get("implementation") != "faster-whisper"
        or runtime.get("version") != "1.2.1"
        or runtime.get("model") != "small"
        or runtime.get("requested_device") != "cpu"
        or runtime.get("device") != "cpu"
        or runtime.get("compute_type") != "int8"
        or variant_contract_invalid
        or not priority_terms
    ):
        raise DownstreamEvaluationError(
            "강건성 summary와 18조건 비공개 레코드의 평가 범위가 다릅니다."
        )
    return summary


def _rate_at(metrics: dict[str, Any], *path: str) -> float | None:
    current: Any = metrics
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return float(current) if isinstance(current, (int, float)) else None


def _delta(value: float | None, baseline: float | None) -> float | None:
    return value - baseline if value is not None and baseline is not None else None


def evaluate_robustness_conditions(
    rows_by_condition: dict[str, list[dict[str, Any]]],
    analyze: Analyze,
    *,
    priority_terms: list[str],
    workers: int = 4,
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """18조건을 독립 request namespace로 평가하고 조건별 안전 지표를 보존한다."""

    if workers <= 0 or workers > MAX_WORKERS:
        raise DownstreamEvaluationError(f"workers는 1~{MAX_WORKERS} 범위여야 합니다.")
    if set(rows_by_condition) != CONDITIONS:
        raise DownstreamEvaluationError("평가 입력은 clean+17개 조건이어야 합니다.")
    if not priority_terms or len(priority_terms) != len(set(priority_terms)):
        raise DownstreamEvaluationError("중복 없는 우선용어 목록이 필요합니다.")

    by_condition: dict[str, dict[str, Any]] = {}
    private_rows: list[dict[str, Any]] = []
    for condition in sorted(CONDITIONS):
        condition_progress: Callable[[int, int], None] | None = None
        if progress:

            def report_condition_progress(
                completed: int, total: int, name: str = condition
            ) -> None:
                progress(name, completed, total)

            condition_progress = report_condition_progress
        metrics, rows = evaluate_pairs(
            rows_by_condition[condition],
            analyze,
            workers=workers,
            request_namespace=f"{PROFILE_ID}:{condition}",
            progress=condition_progress,
        )
        metrics["priority_term_by_term"] = _priority_term_by_term(
            rows_by_condition[condition], priority_terms
        )
        by_condition[condition] = metrics
        private_rows.extend({**row, "channel_variant": condition} for row in rows)

    clean = by_condition["clean"]
    clean_parser = _rate_at(
        clean, "parser_silver", "reference_parser_exact_mention_retention", "rate"
    )
    clean_top3 = _rate_at(
        clean, "resolver_silver", "reference_candidate_top3_retention", "rate"
    )
    clean_coverage = _rate_at(
        clean,
        "resolver_silver",
        "candidate_coverage_on_reference_positive_records",
        "rate",
    )
    deltas: dict[str, dict[str, Any]] = {}
    for condition, metrics in sorted(by_condition.items()):
        deltas[condition] = {
            "parser_exact_mention_retention_delta": _delta(
                _rate_at(
                    metrics,
                    "parser_silver",
                    "reference_parser_exact_mention_retention",
                    "rate",
                ),
                clean_parser,
            ),
            "resolver_top3_retention_delta": _delta(
                _rate_at(
                    metrics,
                    "resolver_silver",
                    "reference_candidate_top3_retention",
                    "rate",
                ),
                clean_top3,
            ),
            "candidate_coverage_delta": _delta(
                _rate_at(
                    metrics,
                    "resolver_silver",
                    "candidate_coverage_on_reference_positive_records",
                    "rate",
                ),
                clean_coverage,
            ),
            "comparison_scope": (
                "same record set aggregate delta versus clean; no paired confidence interval"
            ),
        }

    violation_totals: Counter[str] = Counter()
    for metrics in by_condition.values():
        for key, value in metrics["safety"].items():
            if key.endswith("_count") and key != "evaluated_hypothesis_response_count":
                violation_totals[key] += int(value)
    integrity_passed = all(
        metrics["evaluation_integrity_gate"]["passed"] is True
        for metrics in by_condition.values()
    )
    safety_passed = all(
        metrics["safety_contract_gate"]["passed"] is True
        for metrics in by_condition.values()
    ) and all(value == 0 for value in violation_totals.values())
    coverage_passed = all(
        metrics["stt_transcript_unavailable_count"] == 0
        and metrics["hypothesis_analysis_unavailable_count"] == 0
        for metrics in by_condition.values()
    )
    downstream_passed = integrity_passed and coverage_passed and safety_passed
    return (
        {
            "profile_id": PROFILE_ID,
            "condition_count": len(by_condition),
            "record_count_per_condition": len(rows_by_condition["clean"]),
            "condition_record_count": sum(
                len(rows) for rows in rows_by_condition.values()
            ),
            "by_condition": by_condition,
            "aggregate_delta_vs_clean": deltas,
            "safety_violation_totals": dict(sorted(violation_totals.items())),
            "evaluation_integrity_gate": {
                "passed": integrity_passed,
                "condition": "18조건의 참조·가설 Model API 오류가 모두 0건",
            },
            "analysis_coverage_gate": {
                "passed": coverage_passed,
                "condition": "18조건의 모든 STT 가설이 Model API 분석까지 완료",
            },
            "safety_contract_gate": {
                "passed": safety_passed,
                "condition": "평가 가능한 18조건 가설에서 확인 전 안전 위반 0건",
            },
            "downstream_evaluation_gate": {
                "passed": downstream_passed,
                "condition": "API 무결성·분석 커버리지·확인 전 안전 계약 모두 통과",
            },
            "cas_ground_truth_available": False,
            "is_cas_accuracy_evaluation": False,
            "wrong_single_cas_promotion_ground_truth_count": None,
        },
        private_rows,
    )


def _lora_signals(
    speech_summary: dict[str, Any], metrics: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    signals: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for condition in sorted(CONDITIONS):
        terms = speech_summary["variants"][condition]["priority_term_presence"]
        term_rows = metrics["by_condition"][condition]["priority_term_by_term"]
        aggregate = {
            key: sum(int(row[key]) for row in term_rows)
            for key in ("true_positive", "false_negative", "false_insertion")
        }
        if any(aggregate[key] != int(terms.get(key, -1)) for key in aggregate):
            raise DownstreamEvaluationError(
                f"우선용어 세부 집계와 STT summary가 다릅니다: {condition}"
            )
        term_denominator = int(terms.get("true_positive", 0)) + int(
            terms.get("false_negative", 0)
        )
        term_recall = terms.get("recall")
        resolver = metrics["by_condition"][condition]["resolver_silver"]
        top3 = resolver["reference_candidate_top3_retention"]
        aggregate_term_low = (
            term_denominator >= 20
            and isinstance(term_recall, (int, float))
            and float(term_recall) < 0.8
        )
        specific_terms = [
            row
            for row in term_rows
            if row["reference_positive_count"] >= 5
            and isinstance(row.get("recall"), (int, float))
            and float(row["recall"]) < 0.8
        ]
        if aggregate_term_low and specific_terms:
            for row in specific_terms:
                signals.append(
                    {
                        "condition": condition,
                        "reason": "SPECIFIC_PRIORITY_TERM_RECALL_BELOW_0_80",
                        "public_term": row["term"],
                        "priority_term_aggregate_denominator": term_denominator,
                        "priority_term_aggregate_recall": term_recall,
                        "term_denominator": row["reference_positive_count"],
                        "term_recall": row["recall"],
                        "term_false_insertion": row["false_insertion"],
                    }
                )
        elif aggregate_term_low:
            unresolved.append(
                {
                    "condition": condition,
                    "reason": "AGGREGATE_LOW_RECALL_WITHOUT_TERM_DENOMINATOR_5",
                    "denominator": term_denominator,
                    "rate": term_recall,
                }
            )
        if (
            int(top3.get("denominator") or 0) >= 20
            and isinstance(top3.get("rate"), (int, float))
            and float(top3["rate"]) < 0.9
        ):
            unresolved.append(
                {
                    "condition": condition,
                    "reason": "TOP3_LOW_WITHOUT_SHARED_CANDIDATE_ERROR_SIGNATURE",
                    "denominator": top3.get("denominator"),
                    "rate": top3.get("rate"),
                }
            )
    return signals, unresolved


def build_robustness_report(
    *,
    speech_summary: dict[str, Any],
    metrics: dict[str, Any],
    records_sha256: str,
    speech_summary_sha256: str,
    speech_image_digest: str,
    evaluator_git_commit: str,
    service_revision: str,
    service_git_commit: str,
    runtime_manifest_sha256: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    for label, value in (
        ("비공개 레코드", records_sha256),
        ("STT summary", speech_summary_sha256),
        ("runtime manifest", runtime_manifest_sha256),
    ):
        if not SHA256_PATTERN.fullmatch(value):
            raise DownstreamEvaluationError(f"{label} SHA-256이 올바르지 않습니다.")
    for label, value in (
        ("평가기", evaluator_git_commit),
        ("Model API", service_git_commit),
    ):
        if not GIT_COMMIT_PATTERN.fullmatch(value):
            raise DownstreamEvaluationError(f"{label} Git commit이 올바르지 않습니다.")
    if not service_revision.strip() or len(service_revision) > 128:
        raise DownstreamEvaluationError("Model API revision이 올바르지 않습니다.")
    if not CONTAINER_DIGEST_PATTERN.fullmatch(speech_image_digest):
        raise DownstreamEvaluationError(
            "Speech 평가 이미지 digest가 올바르지 않습니다."
        )
    if (
        metrics.get("profile_id") != PROFILE_ID
        or metrics.get("condition_count") != len(CONDITIONS)
        or metrics.get("record_count_per_condition")
        != speech_summary.get("record_count")
        or not isinstance(metrics.get("by_condition"), dict)
        or set(metrics["by_condition"]) != CONDITIONS
    ):
        raise DownstreamEvaluationError(
            "강건성 STT summary와 후단 집계의 평가 범위가 다릅니다."
        )

    signals, unresolved_signals = _lora_signals(speech_summary, metrics)
    simulation = speech_summary["simulation_run"]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fact_status": "부분 구현 또는 개발용 데모",
        "evaluation_name": "모의 통신 왜곡 STT 후단 Parser·Resolver 실버 평가",
        "evidence_scope": (
            "AIHub 신고접수 전화에서 절차적으로 생성한 radio-sim-v1 18조건의 후보 보존·"
            "안전 계약 평가; 현장 무전·CAS 정답·현장 안전성 검증 아님"
        ),
        "dataset": {
            "profile_id": PROFILE_ID,
            "source_manifest_sha256": simulation["source_manifest_sha256"],
            "record_count_per_condition": metrics["record_count_per_condition"],
            "condition_count": metrics["condition_count"],
            "derived_data": True,
        },
        "input_artifacts": {
            "speech_summary_sha256": speech_summary_sha256,
            "private_records_sha256": records_sha256,
            "private_records_committed_to_git": False,
            "priority_terms_sha256": speech_summary["simulation_run"][
                "priority_terms_sha256"
            ],
        },
        "evaluation_runtime": {
            "repository": "chemicheck119-lab/analysis-engine",
            "git_commit": evaluator_git_commit,
        },
        "stt_runtime": speech_summary.get("runtime"),
        "speech_evaluator_artifact": {
            "repository": "chemicheck119-lab/speech-service",
            "container_image_digest": speech_image_digest,
        },
        "model_api_runtime": {
            "service_revision": service_revision,
            "service_git_commit": service_git_commit,
            "runtime_manifest_sha256": runtime_manifest_sha256,
            "api_schema": "chemiguard119-api-v1",
            "deployment_scope": "개발용 Cloud Run staging; 상용 운영 아님",
        },
        "metrics": metrics,
        "whisper_lora_gate": {
            "decision": "NOT_DECIDABLE_FROM_ONE_REGION",
            "signals": signals,
            "unresolved_aggregate_signals": unresolved_signals,
            "requires_same_error_across_seoul_and_incheon": True,
            "reason": (
                "같은 원본의 여러 왜곡 조건은 독립 지역 표본이 아니므로 한 지역만으로 "
                "LoRA 실행을 결정하지 않습니다."
            ),
        },
        "claims_allowed": [
            "고정 참조 전사 분석 대비 조건별 물질 언급·Resolver 후보 보존",
            "모의 왜곡 가설 입력에서 2-CAS Gate와 후보 상태 안전 계약의 회귀 여부",
        ],
        "claims_not_allowed": [
            "STT→Resolver CAS Top-1·Top-3 정답 정확도",
            "실제 현장 무전 인식 성능 또는 통신장비 성능",
            "실제 화학사고 안전성 또는 대응 효과",
            "같은 원본의 18조건을 독립 표본 18개로 해석",
        ],
    }


def write_robustness_outputs(
    output_dir: Path,
    report: dict[str, Any],
    private_rows: Iterable[dict[str, Any]],
) -> tuple[Path, Path]:
    if output_dir.exists():
        raise FileExistsError(f"기존 출력 경로를 덮어쓰지 않습니다: {output_dir}")
    output_dir.mkdir(parents=True)
    report_path = output_dir / "report.json"
    private_path = output_dir / "records.private.jsonl"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with private_path.open("x", encoding="utf-8") as destination:
        for row in private_rows:
            destination.write(json.dumps(row, ensure_ascii=False) + "\n")
    private_path.chmod(0o600)
    return report_path, private_path


__all__ = [
    "CONDITIONS",
    "PROFILE_ID",
    "build_robustness_report",
    "evaluate_robustness_conditions",
    "load_priority_terms",
    "load_robustness_private_records",
    "load_robustness_summary",
    "sha256_file",
    "write_robustness_outputs",
]
