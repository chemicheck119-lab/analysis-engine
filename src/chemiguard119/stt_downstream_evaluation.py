"""STT 전사문이 Parser·Resolver 후보를 보존하는지 평가하는 실버 평가기.

AIHub 신고접수 음성 라벨에는 물질 CAS 정답이 없다. 따라서 이 모듈은 참조
전사문을 같은 고정 Parser·Resolver에 넣은 결과를 실버 기준으로 삼아, STT
가설에서 물질 언급과 후보가 얼마나 보존되는지만 측정한다. 이 결과를 CAS
정확도나 현장 안전성으로 해석하면 안 된다.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable
import unicodedata
from urllib import error, parse, request

from chemiguard119.api_models import (
    contains_candidate_promotion,
    contains_unconfirmed_risk_output,
)


SCHEMA_VERSION = "stt-downstream-silver-eval-v1"
MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 2_500
MAX_TEXT_LENGTH = 4_000
MAX_WORKERS = 8
CAS_PATTERN = re.compile(r"^\d{2,7}-\d{2}-\d$")
RECORD_KEY_PATTERN = re.compile(r"^[0-9a-f]{16}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
Analyze = Callable[[str, str], dict[str, Any]]


class DownstreamEvaluationError(RuntimeError):
    """민감 원문을 포함하지 않는 평가 오류."""


@dataclass(frozen=True)
class MentionObservation:
    surface: str
    role: str
    assertion: str
    candidates: tuple[str, ...]
    automatic_hint: str | None


@dataclass(frozen=True)
class AnalysisObservation:
    mentions: tuple[MentionObservation, ...]
    state: str | None
    safety: dict[str, bool]
    elapsed_seconds: float
    error_type: str | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_surface(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _surface_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "count": numerator,
        "denominator": denominator,
        "rate": _rate(numerator, denominator),
    }


def load_private_records(path: Path) -> list[dict[str, Any]]:
    """비공개 STT 레코드를 bounded JSONL로 읽고 원문은 출력하지 않는다."""

    size = path.stat().st_size
    if size <= 0 or size > MAX_INPUT_BYTES:
        raise DownstreamEvaluationError(
            f"비공개 레코드 파일 크기가 허용 범위를 벗어났습니다: {size}"
        )

    rows: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if len(rows) >= MAX_RECORDS:
                raise DownstreamEvaluationError(
                    f"평가 레코드가 상한 {MAX_RECORDS}건을 초과했습니다."
                )
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DownstreamEvaluationError(
                    f"비공개 레코드 JSONL 형식이 잘못되었습니다: line={line_number}"
                ) from exc
            if not isinstance(row, dict):
                raise DownstreamEvaluationError(
                    f"비공개 레코드는 JSON 객체여야 합니다: line={line_number}"
                )
            record_key = row.get("record_key")
            reference = row.get("reference")
            hypothesis = row.get("hypothesis")
            if (
                not isinstance(record_key, str)
                or not RECORD_KEY_PATTERN.fullmatch(record_key)
                or not isinstance(reference, str)
                or not reference.strip()
                or not isinstance(hypothesis, str)
            ):
                raise DownstreamEvaluationError(
                    f"필수 비공개 레코드 필드가 잘못되었습니다: line={line_number}"
                )
            if record_key in seen_keys:
                raise DownstreamEvaluationError("중복 record_key가 있습니다.")
            if row.get("variant") != "baseline":
                raise DownstreamEvaluationError(
                    "교차지역 후단 평가는 baseline variant만 허용합니다."
                )
            if len(reference) > MAX_TEXT_LENGTH or len(hypothesis) > MAX_TEXT_LENGTH:
                raise DownstreamEvaluationError(
                    "Model API 입력 길이 상한을 초과한 레코드가 있습니다."
                )
            seen_keys.add(record_key)
            rows.append(row)
    if not rows:
        raise DownstreamEvaluationError("평가할 비공개 레코드가 없습니다.")
    return rows


def load_speech_summary(path: Path, *, expected_records: int) -> dict[str, Any]:
    summary = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict) or summary.get("schema_version") != "1.0.0":
        raise DownstreamEvaluationError("지원하지 않는 STT summary 스키마입니다.")
    dataset = summary.get("dataset")
    runtime = summary.get("runtime")
    variants = summary.get("variants")
    if (
        not isinstance(dataset, dict)
        or dataset.get("record_count") != expected_records
        or summary.get("usage_role") != "evaluation"
        or "not field-radio" not in str(summary.get("evidence_scope") or "")
        or not isinstance(runtime, dict)
        or runtime.get("variants") != ["baseline"]
        or runtime.get("implementation") != "faster-whisper"
        or runtime.get("version") != "1.2.1"
        or runtime.get("model") != "small"
        or runtime.get("requested_device") != "cpu"
        or runtime.get("device") != "cpu"
        or runtime.get("compute_type") != "int8"
        or not isinstance(variants, dict)
        or set(variants) != {"baseline"}
    ):
        raise DownstreamEvaluationError(
            "STT summary와 baseline 비공개 레코드의 평가 범위가 다릅니다."
        )
    return summary


class ModelApiClient:
    """원문을 로그에 남기지 않는 최소 Model API 클라이언트."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        bearer_token: str | None,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
    ) -> None:
        parsed = parse.urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise DownstreamEvaluationError("Model API URL이 올바르지 않습니다.")
        if parsed.scheme != "https" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
        }:
            raise DownstreamEvaluationError("원격 Model API는 HTTPS만 허용합니다.")
        if not api_key:
            raise DownstreamEvaluationError("Model API Key가 필요합니다.")
        if timeout_seconds <= 0 or max_retries < 0 or max_retries > 5:
            raise DownstreamEvaluationError(
                "timeout 또는 retry 설정이 범위를 벗어났습니다."
            )
        self._url = base_url.rstrip("/") + "/api/v1/incidents/analyze"
        self._api_key = api_key
        self._bearer_token = bearer_token
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    def analyze(self, text: str, request_id: str) -> dict[str, Any]:
        payload = json.dumps(
            {
                "request_id": request_id,
                "input": {"type": "MANUAL_TEXT", "text": text},
                "evidence_top_k": 1,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self._api_key,
            "X-Request-Id": request_id,
            "User-Agent": "chemicheck119-stt-downstream-eval/1",
        }
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"

        for attempt in range(self._max_retries + 1):
            try:
                api_request = request.Request(
                    self._url,
                    data=payload,
                    headers=headers,
                    method="POST",
                )
                with request.urlopen(
                    api_request, timeout=self._timeout_seconds
                ) as response:
                    response_bytes = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(response_bytes) > MAX_API_RESPONSE_BYTES:
                    raise DownstreamEvaluationError(
                        "Model API 응답이 허용된 크기 상한을 초과했습니다."
                    )
                result = json.loads(response_bytes)
                if not isinstance(result, dict):
                    raise DownstreamEvaluationError(
                        "Model API가 JSON 객체가 아닌 응답을 반환했습니다."
                    )
                return result
            except error.HTTPError as exc:
                if exc.code < 500 or attempt >= self._max_retries:
                    raise DownstreamEvaluationError(
                        f"Model API HTTP 오류가 발생했습니다: status={exc.code}"
                    ) from exc
            except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                if attempt >= self._max_retries:
                    raise DownstreamEvaluationError(
                        f"Model API 호출이 실패했습니다: type={type(exc).__name__}"
                    ) from exc
            time.sleep(0.25 * (2**attempt))
        raise AssertionError("unreachable")


def _safe_cas_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    result: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        cas_number = item.get("cas_number")
        if isinstance(cas_number, str) and CAS_PATTERN.fullmatch(cas_number):
            result.append(cas_number)
    return tuple(dict.fromkeys(result))


def _observe(response: dict[str, Any], elapsed_seconds: float) -> AnalysisObservation:
    model_outputs = response.get("model_outputs")
    parser_output = (
        model_outputs.get("parser") if isinstance(model_outputs, dict) else None
    )
    raw_mentions = (
        parser_output.get("substance_mentions")
        if isinstance(parser_output, dict)
        else []
    )
    mentions: list[MentionObservation] = []
    if isinstance(raw_mentions, list):
        for raw in raw_mentions:
            if not isinstance(raw, dict):
                continue
            resolver = raw.get("resolver")
            resolver = resolver if isinstance(resolver, dict) else {}
            surface = raw.get("surface_text")
            hint = None
            candidates = (
                model_outputs.get("substance_candidates", [])
                if isinstance(model_outputs, dict)
                else []
            )
            for candidate in candidates:
                if (
                    isinstance(candidate, dict)
                    and candidate.get("surface_text") == surface
                    and candidate.get("role") == raw.get("role")
                    and isinstance(candidate.get("evidence_cas_hint"), str)
                ):
                    hint = str(candidate["evidence_cas_hint"])
                    break
            mentions.append(
                MentionObservation(
                    surface=_normalize_surface(str(surface or "")),
                    role=str(raw.get("role") or "UNKNOWN"),
                    assertion=str(raw.get("assertion") or "UNKNOWN"),
                    candidates=_safe_cas_list(resolver.get("candidates")),
                    automatic_hint=hint,
                )
            )

    gate = response.get("confirmation_gate")
    review = response.get("conflict_review")
    gate = gate if isinstance(gate, dict) else {}
    review = review if isinstance(review, dict) else {}
    candidates = (
        model_outputs.get("substance_candidates", [])
        if isinstance(model_outputs, dict)
        else []
    )
    safety = {
        "two_cas_gate_closed": (
            gate.get("incident_confirmed") is False
            and gate.get("facility_confirmed") is False
            and gate.get("all_required_confirmed") is False
            and gate.get("rule_execution_allowed") is False
        ),
        "rule_not_executed": (
            review.get("executed") is False
            and review.get("status") == "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"
        ),
        "candidates_not_promoted": not contains_candidate_promotion(candidates),
        "no_unconfirmed_risk_output": not contains_unconfirmed_risk_output(
            model_outputs
        ),
    }
    return AnalysisObservation(
        mentions=tuple(mentions),
        state=str(response.get("state")) if response.get("state") else None,
        safety=safety,
        elapsed_seconds=elapsed_seconds,
    )


def _failed_observation(error_type: str) -> AnalysisObservation:
    return AnalysisObservation(
        mentions=(),
        state=None,
        safety={},
        elapsed_seconds=0.0,
        error_type=error_type,
    )


def _analyze_one(
    analyze: Analyze,
    *,
    text: str,
    request_id: str,
) -> AnalysisObservation:
    started = time.perf_counter()
    try:
        response = analyze(text, request_id)
        return _observe(response, time.perf_counter() - started)
    except Exception as exc:  # 평가 실패를 분모에서 숨기지 않는다.
        return _failed_observation(type(exc).__name__)


def _request_id(record_key: str, side: str, namespace: str | None = None) -> str:
    material = f"{record_key}:{side}"
    if namespace:
        material = f"{namespace}:{material}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"stt-silver-{side}-{digest}"


def _private_observation(observation: AnalysisObservation) -> dict[str, Any]:
    return {
        "analysis_error_type": observation.error_type,
        "state": observation.state,
        "elapsed_seconds": observation.elapsed_seconds,
        "safety": observation.safety,
        "mentions": [
            {
                "surface_sha256_prefix": _surface_hash(mention.surface),
                "role": mention.role,
                "assertion": mention.assertion,
                "candidate_cas": list(mention.candidates),
                "automatic_cas_hint": mention.automatic_hint,
            }
            for mention in observation.mentions
        ],
    }


def evaluate_pairs(
    rows: list[dict[str, Any]],
    analyze: Analyze,
    *,
    workers: int = 4,
    request_namespace: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if workers <= 0 or workers > MAX_WORKERS:
        raise DownstreamEvaluationError(f"workers는 1~{MAX_WORKERS} 범위여야 합니다.")

    observations: dict[tuple[int, str], AnalysisObservation] = {}
    futures: dict[Future[AnalysisObservation], tuple[int, str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index, row in enumerate(rows):
            futures[
                executor.submit(
                    _analyze_one,
                    analyze,
                    text=str(row["reference"]),
                    request_id=_request_id(
                        str(row["record_key"]), "reference", request_namespace
                    ),
                )
            ] = (index, "reference")
            if row.get("status") == "completed" and str(row["hypothesis"]).strip():
                futures[
                    executor.submit(
                        _analyze_one,
                        analyze,
                        text=str(row["hypothesis"]),
                        request_id=_request_id(
                            str(row["record_key"]), "hypothesis", request_namespace
                        ),
                    )
                ] = (index, "hypothesis")
            else:
                observations[(index, "hypothesis")] = _failed_observation(
                    "STT_TRANSCRIPT_NOT_AVAILABLE"
                )

        completed = 0
        total = len(futures)
        for future in as_completed(futures):
            observations[futures[future]] = future.result()
            completed += 1
            if progress:
                progress(completed, total)

    exact_surface_retained = 0
    reference_mention_count = 0
    hypothesis_mention_count = 0
    candidate_reference_mentions = 0
    top1_retained = 0
    top3_retained = 0
    reference_hint_count = 0
    reference_hint_retained = 0
    hypothesis_hint_count = 0
    inconsistent_hint_count = 0
    unassessable_hint_count = 0
    reference_candidate_records = 0
    hypothesis_candidate_on_reference_positive = 0
    reference_negative_hypothesis_candidate_records = 0
    reference_api_errors = 0
    hypothesis_api_errors = 0
    stt_transcript_unavailable = 0
    hypothesis_safety_denominator = 0
    safety_violations: Counter[str] = Counter()
    private_rows: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        reference = observations[(index, "reference")]
        hypothesis = observations[(index, "hypothesis")]
        reference_api_errors += reference.error_type is not None
        stt_transcript_unavailable += (
            hypothesis.error_type == "STT_TRANSCRIPT_NOT_AVAILABLE"
        )
        hypothesis_api_errors += hypothesis.error_type not in {
            None,
            "STT_TRANSCRIPT_NOT_AVAILABLE",
        }

        reference_surfaces = Counter(
            mention.surface for mention in reference.mentions if mention.surface
        )
        hypothesis_surfaces = Counter(
            mention.surface for mention in hypothesis.mentions if mention.surface
        )
        reference_mention_count += sum(reference_surfaces.values())
        hypothesis_mention_count += sum(hypothesis_surfaces.values())
        exact_surface_retained += sum(
            min(count, hypothesis_surfaces.get(surface, 0))
            for surface, count in reference_surfaces.items()
        )

        hypothesis_top1 = {
            mention.candidates[0]
            for mention in hypothesis.mentions
            if mention.candidates
        }
        hypothesis_top3 = {
            cas for mention in hypothesis.mentions for cas in mention.candidates[:3]
        }
        hypothesis_hints = {
            mention.automatic_hint
            for mention in hypothesis.mentions
            if mention.automatic_hint
        }
        reference_top3 = {
            cas for mention in reference.mentions for cas in mention.candidates[:3]
        }
        reference_has_candidate = any(
            mention.candidates for mention in reference.mentions
        )
        hypothesis_has_candidate = any(
            mention.candidates for mention in hypothesis.mentions
        )
        if reference_has_candidate:
            reference_candidate_records += 1
            hypothesis_candidate_on_reference_positive += hypothesis_has_candidate
        elif hypothesis_has_candidate:
            reference_negative_hypothesis_candidate_records += 1

        for mention in reference.mentions:
            if mention.candidates:
                candidate_reference_mentions += 1
                top1_retained += mention.candidates[0] in hypothesis_top1
                top3_retained += bool(set(mention.candidates[:3]) & hypothesis_top3)
            if mention.automatic_hint:
                reference_hint_count += 1
                reference_hint_retained += mention.automatic_hint in hypothesis_hints

        hypothesis_hint_count += len(hypothesis_hints)
        for hint in hypothesis_hints:
            if not reference_top3:
                unassessable_hint_count += 1
            elif hint not in reference_top3:
                inconsistent_hint_count += 1

        if hypothesis.error_type is None:
            hypothesis_safety_denominator += 1
            for key, passed in hypothesis.safety.items():
                if not passed:
                    safety_violations[key] += 1

        private_rows.append(
            {
                "record_key": row["record_key"],
                "stt_status": row.get("status"),
                "reference": _private_observation(reference),
                "hypothesis": _private_observation(hypothesis),
            }
        )

    parser_metrics = {
        "reference_mention_count": reference_mention_count,
        "hypothesis_mention_count": hypothesis_mention_count,
        "reference_parser_exact_mention_retention": _ratio(
            exact_surface_retained, reference_mention_count
        ),
        "interpretation": (
            "참조 전사문에서 결정적 Parser가 찾은 표면 물질명이 STT 가설의 "
            "Parser 출력에도 동일 정규화 표현으로 남았는지 측정합니다. 사람 검수 NER "
            "정답 Recall이 아닙니다."
        ),
    }
    resolver_metrics = {
        "reference_candidate_mention_count": candidate_reference_mentions,
        "reference_candidate_top1_retention": _ratio(
            top1_retained, candidate_reference_mentions
        ),
        "reference_candidate_top3_retention": _ratio(
            top3_retained, candidate_reference_mentions
        ),
        "candidate_coverage_on_reference_positive_records": _ratio(
            hypothesis_candidate_on_reference_positive, reference_candidate_records
        ),
        "reference_auto_hint_retention": _ratio(
            reference_hint_retained, reference_hint_count
        ),
        "hypothesis_auto_hint_count": hypothesis_hint_count,
        "reference_inconsistent_auto_hint_count": inconsistent_hint_count,
        "unassessable_hypothesis_auto_hint_count": unassessable_hint_count,
        "reference_negative_hypothesis_candidate_record_count": (
            reference_negative_hypothesis_candidate_records
        ),
        "cas_ground_truth_available": False,
        "is_cas_accuracy_evaluation": False,
        "wrong_single_cas_promotion_ground_truth_count": None,
        "interpretation": (
            "Top-1·Top-3는 참조 전사문의 Resolver 후보 보존율이며 CAS 정답 정확도가 "
            "아닙니다. reference_inconsistent도 실제 오답 확정이 아니라 추가 검수 신호입니다."
        ),
    }
    safety_metrics = {
        "evaluated_hypothesis_response_count": hypothesis_safety_denominator,
        "two_cas_gate_violation_count": safety_violations["two_cas_gate_closed"],
        "rule_execution_before_confirmation_count": safety_violations[
            "rule_not_executed"
        ],
        "candidate_promotion_violation_count": safety_violations[
            "candidates_not_promoted"
        ],
        "unconfirmed_risk_output_violation_count": safety_violations[
            "no_unconfirmed_risk_output"
        ],
    }
    service_integrity_passed = reference_api_errors == 0 and hypothesis_api_errors == 0
    safety_contract_passed = hypothesis_safety_denominator == len(
        rows
    ) - stt_transcript_unavailable - hypothesis_api_errors and all(
        value == 0
        for key, value in safety_metrics.items()
        if key.endswith("_violation_count")
    )
    metrics = {
        "record_count": len(rows),
        "stt_transcript_unavailable_count": stt_transcript_unavailable,
        "reference_model_api_error_count": reference_api_errors,
        "hypothesis_model_api_error_count": hypothesis_api_errors,
        "hypothesis_analysis_unavailable_count": (
            stt_transcript_unavailable + hypothesis_api_errors
        ),
        "parser_silver": parser_metrics,
        "resolver_silver": resolver_metrics,
        "safety": safety_metrics,
        "evaluation_integrity_gate": {
            "passed": service_integrity_passed,
            "condition": "참조·가설 Model API 오류가 모두 0건",
        },
        "safety_contract_gate": {
            "passed": safety_contract_passed,
            "condition": "평가 가능한 모든 가설 응답에서 확인 전 안전 위반 0건",
        },
    }
    return metrics, private_rows


def build_report(
    *,
    speech_summary: dict[str, Any],
    metrics: dict[str, Any],
    records_sha256: str,
    speech_summary_sha256: str,
    service_revision: str,
    service_git_commit: str,
    runtime_manifest_sha256: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if not SHA256_PATTERN.fullmatch(records_sha256):
        raise DownstreamEvaluationError("비공개 레코드 SHA-256이 올바르지 않습니다.")
    if not SHA256_PATTERN.fullmatch(speech_summary_sha256):
        raise DownstreamEvaluationError("STT summary SHA-256이 올바르지 않습니다.")
    if not SHA256_PATTERN.fullmatch(runtime_manifest_sha256):
        raise DownstreamEvaluationError("runtime manifest SHA-256이 올바르지 않습니다.")
    if not GIT_COMMIT_PATTERN.fullmatch(service_git_commit):
        raise DownstreamEvaluationError("Model API Git commit이 올바르지 않습니다.")
    if not service_revision.strip() or len(service_revision) > 128:
        raise DownstreamEvaluationError("Model API revision이 올바르지 않습니다.")
    dataset = speech_summary["dataset"]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fact_status": "부분 구현 또는 개발용 데모",
        "evaluation_name": "교차지역 STT 후단 Parser·Resolver 실버 평가",
        "evidence_scope": (
            "AIHub 신고접수 전화 음성의 참조 전사문 대비 후보 보존 평가; "
            "현장 무전·CAS 정답·현장 안전성 검증 아님"
        ),
        "dataset": {
            "dataset_id": dataset.get("dataset_id") or dataset.get("id"),
            "dataset_version": dataset.get("dataset_version"),
            "evaluation_id": dataset.get("evaluation_id"),
            "split": dataset.get("split"),
            "record_count": dataset.get("record_count"),
        },
        "input_artifacts": {
            "speech_summary_sha256": speech_summary_sha256,
            "private_records_sha256": records_sha256,
            "private_records_committed_to_git": False,
        },
        "stt_runtime": speech_summary.get("runtime"),
        "model_api_runtime": {
            "service_revision": service_revision,
            "service_git_commit": service_git_commit,
            "runtime_manifest_sha256": runtime_manifest_sha256,
            "api_schema": "chemiguard119-api-v1",
            "deployment_scope": "개발용 Cloud Run staging; 상용 운영 아님",
        },
        "metrics": metrics,
        "claims_allowed": [
            "고정 참조 전사 분석 결과 대비 물질 언급·후보 보존율",
            "미확인 전사 입력에서 2-CAS Gate와 후보 상태 안전 계약의 회귀 여부",
        ],
        "claims_not_allowed": [
            "STT→Resolver CAS Top-1·Top-3 정답 정확도",
            "실제 현장 무전 인식 성능",
            "실제 화학사고 안전성 또는 대응 효과",
            "reference_inconsistent_auto_hint를 실제 오답 CAS 확정 건수로 해석",
        ],
    }


def write_outputs(
    output_dir: Path,
    report: dict[str, Any],
    private_rows: Iterable[dict[str, Any]],
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    private_path = output_dir / "records.private.jsonl"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with private_path.open("w", encoding="utf-8") as destination:
        for row in private_rows:
            destination.write(json.dumps(row, ensure_ascii=False) + "\n")
    private_path.chmod(0o600)
    return report_path, private_path


def required_secret_from_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise DownstreamEvaluationError(f"필수 인증 환경변수가 없습니다: {name}")
    return value


__all__ = [
    "DownstreamEvaluationError",
    "MAX_WORKERS",
    "ModelApiClient",
    "build_report",
    "evaluate_pairs",
    "load_private_records",
    "load_speech_summary",
    "required_secret_from_env",
    "sha256_file",
    "write_outputs",
]
