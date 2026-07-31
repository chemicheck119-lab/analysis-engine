"""질문에 맞는 근거 section을 평가하는 graded-qrel evaluator.

기존 회귀 평가는 같은 출처·CAS 묶음에 도달했는지만 확인한다. 이 모듈은
evidence_id별 0~3 relevance label을 사용해 제품명 section이 보호구 질문의
정답으로 계산되는 문제를 막는다. 평가 profile은 검수 범위와 성능 주장 범위를
분리하며, DRAFT 데이터 결과는 내부 회귀로만 사용할 수 있다.
"""

from __future__ import annotations

import math
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import numpy as np

from chemiguard119.database import connect_readonly
from chemiguard119.evaluation_contract import (
    EvaluationProfile,
    evaluate_dataset_contract,
    load_evaluation_rows,
)
from chemiguard119.retrieval import load_retriever, search_evidence
from chemiguard119.utils import sha256_file, valid_cas_checksum, write_json


SECTION_METRICS_VERSION = "retriever-section-qrel-v3"
HIGH_RELEVANCE_MIN_GRADE = 2


def _valid_http_url(value: object) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _evidence_index(db_path: Path) -> dict[str, dict[str, Any]]:
    with connect_readonly(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return {
            str(row["evidence_id"]): dict(row)
            for row in connection.execute(
                """
                SELECT evidence_id, cas_number, source, source_url, document_version
                FROM evidence
                """
            )
        }


def _validate_qrels(
    rows: list[Mapping[str, Any]],
    evidence_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    for row_number, row in enumerate(rows, 1):
        case_id = str(row.get("case_id") or f"<row:{row_number}>")
        query = str(row.get("query") or "").strip()
        if not query:
            raise ValueError(f"{case_id}: query가 비어 있습니다.")
        answerable = row.get("answerable")
        if not isinstance(answerable, bool):
            raise ValueError(f"{case_id}: answerable은 boolean이어야 합니다.")
        raw_gold_cas = row.get("gold_cas_numbers")
        if (
            not isinstance(raw_gold_cas, list)
            or len(raw_gold_cas) != 1
            or not str(raw_gold_cas[0]).strip()
        ):
            raise ValueError(
                f"{case_id}: Oracle-CAS section 평가는 정확히 1개의 gold CAS가 필요합니다."
            )
        gold_cas = {str(raw_gold_cas[0]).strip()}
        invalid_cas = sorted(
            value for value in gold_cas if not valid_cas_checksum(value)
        )
        if invalid_cas:
            raise ValueError(f"{case_id}: 유효하지 않은 gold CAS={invalid_cas}")
        qrels = row.get("qrels")
        if not isinstance(qrels, list) or not qrels:
            raise ValueError(f"{case_id}: qrels 배열이 필요합니다.")
        seen_ids: set[str] = set()
        positive_count = 0
        high_relevance_count = 0
        for qrel in qrels:
            if not isinstance(qrel, Mapping):
                raise ValueError(f"{case_id}: qrel은 JSON 객체여야 합니다.")
            evidence_id = str(qrel.get("evidence_id") or "").strip()
            if not evidence_id or evidence_id in seen_ids:
                raise ValueError(
                    f"{case_id}: 비어 있거나 중복된 evidence_id={evidence_id!r}"
                )
            seen_ids.add(evidence_id)
            if evidence_id not in evidence_by_id:
                raise ValueError(f"{case_id}: DB에 없는 evidence_id={evidence_id!r}")
            grade = qrel.get("relevance_grade")
            if (
                not isinstance(grade, int)
                or isinstance(grade, bool)
                or not 0 <= grade <= 3
            ):
                raise ValueError(f"{case_id}: relevance_grade는 0~3 정수여야 합니다.")
            if grade > 0:
                positive_count += 1
                if grade >= HIGH_RELEVANCE_MIN_GRADE:
                    high_relevance_count += 1
                evidence_cas = str(
                    evidence_by_id[evidence_id].get("cas_number") or ""
                ).strip()
                if gold_cas and evidence_cas not in gold_cas:
                    raise ValueError(
                        f"{case_id}: positive qrel의 CAS가 gold와 다릅니다: "
                        f"{evidence_id}={evidence_cas}"
                    )
            required_fact_ids = qrel.get("required_fact_ids")
            if not isinstance(required_fact_ids, list) or any(
                not isinstance(fact_id, str) or not fact_id.strip()
                for fact_id in required_fact_ids
            ):
                raise ValueError(
                    f"{case_id}: required_fact_ids는 문자열 배열이어야 합니다."
                )
            normalized_fact_ids = [fact_id.strip() for fact_id in required_fact_ids]
            if len(normalized_fact_ids) != len(set(normalized_fact_ids)):
                raise ValueError(f"{case_id}: required_fact_ids에 중복이 있습니다.")
            if grade > 0 and not normalized_fact_ids:
                raise ValueError(
                    f"{case_id}: 관련 qrel에는 required_fact_ids가 필요합니다."
                )
            if grade == 0 and normalized_fact_ids:
                raise ValueError(
                    f"{case_id}: 비관련 qrel에는 required_fact_ids를 둘 수 없습니다."
                )
        if answerable and positive_count == 0:
            raise ValueError(
                f"{case_id}: answerable 질의에는 positive qrel이 필요합니다."
            )
        if answerable and high_relevance_count == 0:
            raise ValueError(
                f"{case_id}: answerable 질의에는 grade "
                f"{HIGH_RELEVANCE_MIN_GRADE} 이상의 핵심 qrel이 필요합니다."
            )
        if not answerable and positive_count:
            raise ValueError(
                f"{case_id}: answerable=false 질의에는 positive qrel을 둘 수 없습니다."
            )


def _dcg(grades: list[int]) -> float:
    return sum(
        (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades, 1)
    )


def _relevance_gain(grade: int) -> int:
    return 2**grade - 1 if grade > 0 else 0


def _recall(returned_ids: set[str], expected_ids: set[str]) -> float | None:
    if not expected_ids:
        return None
    return len(returned_ids & expected_ids) / len(expected_ids)


def _wilson_interval(successes: int, total: int) -> dict[str, Any] | None:
    """작은 이항 표본의 성공률 불확실성을 정규근사보다 보수적으로 표시한다."""

    if total <= 0:
        return None
    z = 1.959963984540054
    observed = successes / total
    denominator = 1 + (z**2 / total)
    centre = observed + (z**2 / (2 * total))
    margin = z * math.sqrt(
        (observed * (1 - observed) / total) + (z**2 / (4 * total**2))
    )
    return {
        "method": "WILSON_SCORE_TWO_SIDED",
        "confidence_level": 0.95,
        "successes": successes,
        "total": total,
        "observed_rate": observed,
        "lower": max(0.0, (centre - margin) / denominator),
        "upper": min(1.0, (centre + margin) / denominator),
    }


def evaluate_retriever_sections(
    db_path: Path,
    model_path: Path,
    evaluation_path: Path,
    *,
    profile: EvaluationProfile | str = EvaluationProfile.INTERNAL_REGRESSION,
    report_path: Path | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Oracle CAS로 section 순위 품질을 측정한다.

    물질 식별 오차와 section 검색 오차를 섞지 않기 위해 gold CAS가 하나인
    질의는 그 CAS를 엄격 필터로 사용한다. 실제 end-to-end CAS 힌트 성능은
    resolver 평가에서 별도로 측정한다.
    """

    if not 1 <= top_k <= 20:
        raise ValueError("top_k는 1~20이어야 합니다.")
    evaluation_path = Path(evaluation_path)
    rows = load_evaluation_rows(evaluation_path)
    contract = evaluate_dataset_contract(rows, profile, evaluation_path)
    if not contract["passed"]:
        codes = ", ".join(item["code"] for item in contract["blockers"])
        raise ValueError(f"평가 데이터 계약 실패: {codes}")

    evidence_by_id = _evidence_index(db_path)
    _validate_qrels(rows, evidence_by_id)
    artifact = load_retriever(model_path)

    case_reports: list[dict[str, Any]] = []
    for row in rows:
        case_id = str(row["case_id"])
        gold_cas = [str(value).strip() for value in row["gold_cas_numbers"]]
        cas_hint = gold_cas[0]
        qrel_grades = {
            str(qrel["evidence_id"]): int(qrel["relevance_grade"])
            for qrel in row["qrels"]
        }
        fact_ids_by_evidence = {
            str(qrel["evidence_id"]): {
                str(fact_id).strip() for fact_id in qrel["required_fact_ids"]
            }
            for qrel in row["qrels"]
            if int(qrel["relevance_grade"]) > 0
        }
        positive_ids = {
            evidence_id for evidence_id, grade in qrel_grades.items() if grade > 0
        }
        high_relevance_ids = {
            evidence_id
            for evidence_id, grade in qrel_grades.items()
            if grade >= HIGH_RELEVANCE_MIN_GRADE
        }
        supporting_ids = {
            evidence_id for evidence_id, grade in qrel_grades.items() if grade == 1
        }
        required_fact_ids = set().union(
            *(
                fact_ids_by_evidence.get(evidence_id, set())
                for evidence_id in positive_ids
            )
        )
        high_relevance_fact_ids = set().union(
            *(
                fact_ids_by_evidence.get(evidence_id, set())
                for evidence_id in high_relevance_ids
            )
        )
        supporting_fact_ids = set().union(
            *(
                fact_ids_by_evidence.get(evidence_id, set())
                for evidence_id in supporting_ids
            )
        )

        started = time.perf_counter()
        result = search_evidence(
            str(row["query"]),
            db_path,
            artifact,
            cas_hint=cas_hint,
            top_k=top_k,
        )
        latency_ms = (time.perf_counter() - started) * 1_000
        returned = list(result.get("results") or [])
        returned_ids = [str(item.get("evidence_id") or "") for item in returned]
        returned_id_set = set(returned_ids)
        returned_positive_ids = set(returned_ids) & positive_ids
        returned_fact_ids = set().union(
            *(
                fact_ids_by_evidence.get(evidence_id, set())
                for evidence_id in returned_id_set
            )
        )
        grades = [qrel_grades.get(evidence_id, 0) for evidence_id in returned_ids]
        total_relevance_gain = sum(
            _relevance_gain(grade) for grade in qrel_grades.values()
        )
        returned_relevance_gain = sum(
            _relevance_gain(qrel_grades.get(evidence_id, 0))
            for evidence_id in returned_ids
        )
        ideal_grades = sorted(
            (grade for grade in qrel_grades.values() if grade > 0),
            reverse=True,
        )[:top_k]
        ideal_dcg = _dcg(ideal_grades)
        relevant_ranks = [
            rank
            for rank, evidence_id in enumerate(returned_ids, 1)
            if evidence_id in positive_ids
        ]
        same_cas_judged_nonrelevant = sum(
            bool(
                str(item.get("cas_number") or "") in set(gold_cas)
                and qrel_grades.get(str(item.get("evidence_id") or "")) == 0
            )
            for item in returned
        )
        judged_count = sum(evidence_id in qrel_grades for evidence_id in returned_ids)
        same_cas_judged_count = sum(
            bool(
                str(item.get("cas_number") or "") in set(gold_cas)
                and str(item.get("evidence_id") or "") in qrel_grades
            )
            for item in returned
        )
        wrong_cas_count = sum(
            bool(gold_cas and str(item.get("cas_number") or "") not in set(gold_cas))
            for item in returned
        )
        unjudged_count = sum(
            str(item.get("evidence_id") or "") not in qrel_grades for item in returned
        )
        valid_url_count = sum(
            _valid_http_url(item.get("source_url")) for item in returned
        )
        answerable = bool(row["answerable"])
        case_reports.append(
            {
                "case_id": case_id,
                "intent": row.get("intent"),
                "answerable": answerable,
                "cas_hint_mode": "ORACLE_STRICT_FILTER",
                "gold_cas_numbers": gold_cas,
                "returned_evidence_ids": returned_ids,
                "ndcg_at_k": (_dcg(grades) / ideal_dcg if ideal_dcg else 1.0),
                "recall_at_k": (
                    len(returned_positive_ids) / len(positive_ids)
                    if positive_ids
                    else 1.0
                ),
                "high_relevance_recall_at_k": _recall(
                    returned_id_set,
                    high_relevance_ids,
                ),
                "supporting_recall_at_k": _recall(
                    returned_id_set,
                    supporting_ids,
                ),
                "graded_gain_recall_at_k": (
                    returned_relevance_gain / total_relevance_gain
                    if total_relevance_gain
                    else None
                ),
                "high_relevance_complete_at_k": (
                    high_relevance_ids.issubset(returned_id_set)
                    if high_relevance_ids
                    else None
                ),
                "required_fact_coverage_at_k": _recall(
                    returned_fact_ids,
                    required_fact_ids,
                ),
                "high_relevance_fact_coverage_at_k": _recall(
                    returned_fact_ids,
                    high_relevance_fact_ids,
                ),
                "supporting_fact_coverage_at_k": _recall(
                    returned_fact_ids,
                    supporting_fact_ids,
                ),
                "high_relevance_fact_complete_at_k": (
                    high_relevance_fact_ids.issubset(returned_fact_ids)
                    if high_relevance_fact_ids
                    else None
                ),
                "missed_evidence_ids": {
                    "high_relevance": sorted(high_relevance_ids - returned_id_set),
                    "supporting": sorted(supporting_ids - returned_id_set),
                },
                "missed_fact_ids": {
                    "high_relevance": sorted(
                        high_relevance_fact_ids - returned_fact_ids
                    ),
                    "supporting": sorted(supporting_fact_ids - returned_fact_ids),
                },
                "relevance_gain": {
                    "returned": returned_relevance_gain,
                    "total": total_relevance_gain,
                },
                # 표준 Precision@K는 실제 반환 수가 아니라 K를 분모로 쓴다.
                # 미반환 슬롯과 unjudged 문서는 이 지표에서 비관련으로 계산하고,
                # qrel pool의 불완전성은 judged 지표와 unjudged 비율로 분리한다.
                "precision_at_k": len(returned_positive_ids) / top_k,
                "judged_relevant_rate_at_k": (
                    len(returned_positive_ids) / judged_count if judged_count else None
                ),
                "reciprocal_rank": (
                    1.0 / min(relevant_ranks) if relevant_ranks else 0.0
                ),
                "same_cas_judged_wrong_section_count": same_cas_judged_nonrelevant,
                "same_cas_judged_count": same_cas_judged_count,
                "same_cas_judged_wrong_section_rate_at_k": (
                    same_cas_judged_nonrelevant / same_cas_judged_count
                    if same_cas_judged_count
                    else None
                ),
                "wrong_cas_count": wrong_cas_count,
                "unjudged_count": unjudged_count,
                "judged_count": judged_count,
                "judged_relevant_count": len(returned_positive_ids),
                "judged_coverage_at_k": (
                    judged_count / len(returned_ids) if returned_ids else None
                ),
                "unjudged_rate_at_k": (
                    unjudged_count / len(returned_ids) if returned_ids else None
                ),
                "valid_source_url_count": valid_url_count,
                "returned_count": len(returned),
                "abstained": not returned,
                "latency_ms": round(latency_ms, 6),
            }
        )

    answerable_rows = [row for row in case_reports if row["answerable"]]
    unanswerable_rows = [row for row in case_reports if not row["answerable"]]
    total_returned = sum(row["returned_count"] for row in case_reports)
    total_judged = sum(row["judged_count"] for row in case_reports)
    total_same_cas_judged = sum(row["same_cas_judged_count"] for row in case_reports)
    total_judged_relevant = sum(row["judged_relevant_count"] for row in case_reports)
    latencies = [row["latency_ms"] for row in case_reports]
    high_relevance_rows = [
        row for row in answerable_rows if row["high_relevance_recall_at_k"] is not None
    ]
    supporting_rows = [
        row for row in answerable_rows if row["supporting_recall_at_k"] is not None
    ]
    high_relevance_complete_rows = [
        row
        for row in answerable_rows
        if row["high_relevance_complete_at_k"] is not None
    ]
    high_relevance_complete_count = sum(
        bool(row["high_relevance_complete_at_k"])
        for row in high_relevance_complete_rows
    )
    required_fact_rows = [
        row for row in answerable_rows if row["required_fact_coverage_at_k"] is not None
    ]
    high_relevance_fact_rows = [
        row
        for row in answerable_rows
        if row["high_relevance_fact_coverage_at_k"] is not None
    ]
    supporting_fact_rows = [
        row
        for row in answerable_rows
        if row["supporting_fact_coverage_at_k"] is not None
    ]
    high_relevance_fact_complete_rows = [
        row
        for row in answerable_rows
        if row["high_relevance_fact_complete_at_k"] is not None
    ]
    high_relevance_fact_complete_count = sum(
        bool(row["high_relevance_fact_complete_at_k"])
        for row in high_relevance_fact_complete_rows
    )
    qrel_grade_counts = Counter(
        int(qrel["relevance_grade"]) for row in rows for qrel in row["qrels"]
    )
    report = {
        "metrics_version": SECTION_METRICS_VERSION,
        "evaluation_mode": "SECTION_RELEVANCE_WITH_ORACLE_CAS",
        "evaluation_contract": contract,
        "claim_scope": contract["claim_scope"],
        "field_validated": False,
        "case_count": len(case_reports),
        "answerable_case_count": len(answerable_rows),
        "unanswerable_case_count": len(unanswerable_rows),
        "top_k": top_k,
        "metrics": {
            "ndcg_at_k": float(np.mean([row["ndcg_at_k"] for row in answerable_rows]))
            if answerable_rows
            else None,
            "recall_at_k": float(
                np.mean([row["recall_at_k"] for row in answerable_rows])
            )
            if answerable_rows
            else None,
            "high_relevance_recall_at_k": float(
                np.mean(
                    [row["high_relevance_recall_at_k"] for row in high_relevance_rows]
                )
            )
            if high_relevance_rows
            else None,
            "supporting_recall_at_k": float(
                np.mean([row["supporting_recall_at_k"] for row in supporting_rows])
            )
            if supporting_rows
            else None,
            "graded_gain_recall_at_k": float(
                np.mean([row["graded_gain_recall_at_k"] for row in answerable_rows])
            )
            if answerable_rows
            else None,
            "high_relevance_complete_case_rate_at_k": (
                high_relevance_complete_count / len(high_relevance_complete_rows)
                if high_relevance_complete_rows
                else None
            ),
            "required_fact_coverage_at_k": float(
                np.mean(
                    [row["required_fact_coverage_at_k"] for row in required_fact_rows]
                )
            )
            if required_fact_rows
            else None,
            "high_relevance_fact_coverage_at_k": float(
                np.mean(
                    [
                        row["high_relevance_fact_coverage_at_k"]
                        for row in high_relevance_fact_rows
                    ]
                )
            )
            if high_relevance_fact_rows
            else None,
            "supporting_fact_coverage_at_k": float(
                np.mean(
                    [
                        row["supporting_fact_coverage_at_k"]
                        for row in supporting_fact_rows
                    ]
                )
            )
            if supporting_fact_rows
            else None,
            "high_relevance_fact_complete_case_rate_at_k": (
                high_relevance_fact_complete_count
                / len(high_relevance_fact_complete_rows)
                if high_relevance_fact_complete_rows
                else None
            ),
            "precision_at_k": float(
                np.mean([row["precision_at_k"] for row in answerable_rows])
            )
            if answerable_rows
            else None,
            "mrr_at_k": float(
                np.mean([row["reciprocal_rank"] for row in answerable_rows])
            )
            if answerable_rows
            else None,
            "same_cas_judged_wrong_section_rate_at_k": (
                sum(row["same_cas_judged_wrong_section_count"] for row in case_reports)
                / total_same_cas_judged
                if total_same_cas_judged
                else None
            ),
            "wrong_cas_rate_at_k": (
                sum(row["wrong_cas_count"] for row in case_reports) / total_returned
                if total_returned
                else 0.0
            ),
            "unjudged_rate_at_k": (
                sum(row["unjudged_count"] for row in case_reports) / total_returned
                if total_returned
                else None
            ),
            "judged_coverage_at_k": (
                total_judged / total_returned if total_returned else None
            ),
            "judged_relevant_rate_at_k": (
                total_judged_relevant / total_judged if total_judged else None
            ),
            "unanswerable_abstention_rate": (
                float(np.mean([row["abstained"] for row in unanswerable_rows]))
                if unanswerable_rows
                else None
            ),
            "valid_source_url_coverage_at_k": (
                sum(row["valid_source_url_count"] for row in case_reports)
                / total_returned
                if total_returned
                else 0.0
            ),
        },
        "qrel_summary": {
            "relevance_grade_counts": {
                str(grade): qrel_grade_counts.get(grade, 0) for grade in range(4)
            },
            "positive_qrel_count": sum(
                count for grade, count in qrel_grade_counts.items() if grade > 0
            ),
            "high_relevance_qrel_count": sum(
                count
                for grade, count in qrel_grade_counts.items()
                if grade >= HIGH_RELEVANCE_MIN_GRADE
            ),
            "supporting_qrel_count": qrel_grade_counts.get(1, 0),
            "high_relevance_min_grade": HIGH_RELEVANCE_MIN_GRADE,
            "required_fact_assignment_count": sum(
                len(qrel["required_fact_ids"])
                for row in rows
                for qrel in row["qrels"]
                if int(qrel["relevance_grade"]) > 0
            ),
        },
        "metric_definitions": {
            "recall_at_k": {
                "relevance_grades": [1, 2, 3],
                "weighting": "BINARY_EQUAL",
                "aggregation": "MACRO_OVER_ANSWERABLE_CASES",
                "meaning": "모든 관련 qrel을 같은 1건으로 계산한 회수율",
            },
            "high_relevance_recall_at_k": {
                "relevance_grades": [2, 3],
                "weighting": "BINARY_EQUAL",
                "aggregation": "MACRO_OVER_ANSWERABLE_CASES",
                "meaning": "핵심 답변 근거의 회수율",
            },
            "supporting_recall_at_k": {
                "relevance_grades": [1],
                "weighting": "BINARY_EQUAL",
                "aggregation": "MACRO_OVER_CASES_WITH_GRADE_1_QRELS",
                "meaning": "보조 맥락 근거의 회수율",
            },
            "graded_gain_recall_at_k": {
                "relevance_grades": [1, 2, 3],
                "weighting": "TWO_POWER_GRADE_MINUS_ONE",
                "aggregation": "MACRO_OVER_ANSWERABLE_CASES",
                "meaning": "관련도 등급의 중요도를 반영한 회수율",
            },
            "required_fact_coverage_at_k": {
                "relevance_grades": [1, 2, 3],
                "weighting": "UNIQUE_FACT_IDS_PER_CASE",
                "aggregation": "MACRO_OVER_ANSWERABLE_CASES",
                "meaning": "관련 문서에 지정한 필수 사실의 회수율",
            },
            "high_relevance_fact_coverage_at_k": {
                "relevance_grades": [2, 3],
                "weighting": "UNIQUE_FACT_IDS_PER_CASE",
                "aggregation": "MACRO_OVER_ANSWERABLE_CASES",
                "meaning": "핵심 답변에 필요한 사실의 회수율",
            },
        },
        "uncertainty": {
            "high_relevance_complete_case_rate_at_k": _wilson_interval(
                high_relevance_complete_count,
                len(high_relevance_complete_rows),
            ),
            "high_relevance_fact_complete_case_rate_at_k": _wilson_interval(
                high_relevance_fact_complete_count,
                len(high_relevance_fact_complete_rows),
            ),
        },
        "latency_ms": {
            "mean": round(float(np.mean(latencies)), 6) if latencies else None,
            "p95": round(float(np.percentile(latencies, 95)), 6) if latencies else None,
        },
        "artifacts": {
            "dataset_sha256": sha256_file(evaluation_path),
            "database_sha256": sha256_file(db_path),
            "retriever_sha256": sha256_file(model_path),
        },
        "metric_notice": (
            "recall_at_k는 grade 1~3을 같은 관련 문서로 계산합니다. "
            "high_relevance_recall_at_k는 grade 2~3 핵심 근거, "
            "supporting_recall_at_k는 grade 1 보조 근거, graded_gain_recall_at_k는 "
            "2^grade-1 가중 회수율입니다. required_fact_coverage_at_k는 문서에 라벨한 "
            "필수 사실의 회수율입니다. DRAFT section-title 회귀셋 결과는 현장 성능이 "
            "아닙니다. 표준 Precision@K와 nDCG는 unjudged 문서를 비관련으로 계산하며 "
            "작은 표본의 성공률은 Wilson 95% 구간과 함께 해석해야 합니다."
        ),
        "rows": case_reports,
    }
    if report_path:
        write_json(report_path, report)
    return report


__all__ = ["SECTION_METRICS_VERSION", "evaluate_retriever_sections"]
