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


SECTION_METRICS_VERSION = "retriever-section-qrel-v2"


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
        gold_cas = {
            str(value).strip() for value in row.get("gold_cas_numbers", []) if value
        }
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
                evidence_cas = str(
                    evidence_by_id[evidence_id].get("cas_number") or ""
                ).strip()
                if gold_cas and evidence_cas not in gold_cas:
                    raise ValueError(
                        f"{case_id}: positive qrel의 CAS가 gold와 다릅니다: "
                        f"{evidence_id}={evidence_cas}"
                    )
        if answerable and positive_count == 0:
            raise ValueError(
                f"{case_id}: answerable 질의에는 positive qrel이 필요합니다."
            )
        if not answerable and positive_count:
            raise ValueError(
                f"{case_id}: answerable=false 질의에는 positive qrel을 둘 수 없습니다."
            )


def _dcg(grades: list[int]) -> float:
    return sum(
        (2**grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(grades, 1)
    )


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
        cas_hint = gold_cas[0] if len(gold_cas) == 1 else None
        qrel_grades = {
            str(qrel["evidence_id"]): int(qrel["relevance_grade"])
            for qrel in row["qrels"]
        }
        positive_ids = {
            evidence_id for evidence_id, grade in qrel_grades.items() if grade > 0
        }

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
        returned_positive_ids = set(returned_ids) & positive_ids
        grades = [qrel_grades.get(evidence_id, 0) for evidence_id in returned_ids]
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
                "cas_hint_mode": "ORACLE_STRICT_FILTER" if cas_hint else "NONE",
                "gold_cas_numbers": gold_cas,
                "returned_evidence_ids": returned_ids,
                "ndcg_at_k": (_dcg(grades) / ideal_dcg if ideal_dcg else 1.0),
                "recall_at_k": (
                    len(returned_positive_ids) / len(positive_ids)
                    if positive_ids
                    else 1.0
                ),
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
            "DRAFT section-title 회귀셋 결과는 현장 성능이 아닙니다. 표준 Precision@K와 "
            "nDCG는 unjudged 문서를 비관련으로 계산합니다. 불완전 qrel pool의 영향을 "
            "숨기지 않도록 judged coverage·judged relevant rate·unjudged rate를 별도로 "
            "보고합니다."
        ),
        "rows": case_reports,
    }
    if report_path:
        write_json(report_path, report)
    return report


__all__ = ["SECTION_METRICS_VERSION", "evaluate_retriever_sections"]
