"""KOSHA·CAMEO 근거용 BM25 + 문자/단어 TF-IDF + RRF 검색."""

from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.pipeline import FeatureUnion
from sklearn.feature_extraction.text import TfidfVectorizer

from chemiguard119.database import connect_readonly
from chemiguard119.paths import (
    DEFAULT_RESOLVER_MODEL,
    DEFAULT_RETRIEVER_MODEL,
    EVALUATION_DIR,
)
from chemiguard119.resolver import (
    load_resolver,
    select_evidence_cas_hint_from_text,
)
from chemiguard119.utils import (
    normalize_cas,
    normalize_text,
    valid_cas_checksum,
    write_json,
)


MODEL_SCHEMA_VERSION = "evidence-hybrid-tfidf-v2"
OFFICIAL_EVIDENCE_SOURCES = frozenset({"KOSHA", "CAMEO"})
CAS_EVIDENCE_NOT_LOADED_STATUS = "CAS_EVIDENCE_NOT_LOADED"
INVALID_CAS_HINT_STATUS = "INVALID_CAS_HINT"


def _load_evidence(db_path: Path) -> list[dict[str, Any]]:
    with connect_readonly(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in connection.execute(
                """
            SELECT evidence_id, source, source_record_id, cas_number,
                   cameo_chemical_id, title, body, source_url, document_version,
                   cas_link_status
            FROM evidence
            WHERE TRIM(COALESCE(body, '')) <> ''
            ORDER BY evidence_id
            """
            )
        ]
    if not rows:
        raise RuntimeError("검색 evidence가 없습니다. 먼저 prepare를 실행하세요.")
    return rows


def train_retriever(
    db_path: Path,
    model_path: Path = DEFAULT_RETRIEVER_MODEL,
    max_features_per_branch: int = 30_000,
) -> dict[str, Any]:
    """전문가 골드 라벨 없이도 가능한 검색 기준선을 적합한다."""

    rows = _load_evidence(db_path)
    # CAMEO 한 행이 매우 길 수 있어 PoC 메모리 상한을 명시적으로 둔다.
    texts = [
        normalize_text(
            f"{row['title']} {row.get('cas_number') or ''} {row['body'][:8000]}"
        )
        for row in rows
    ]
    vectorizer = FeatureUnion(
        [
            (
                "word",
                TfidfVectorizer(
                    analyzer="word",
                    ngram_range=(1, 2),
                    token_pattern=r"(?u)[\w\-]{2,}",
                    max_features=max_features_per_branch,
                    sublinear_tf=True,
                    dtype=np.float32,
                ),
            ),
            (
                "char",
                TfidfVectorizer(
                    analyzer="char_wb",
                    ngram_range=(3, 5),
                    max_features=max_features_per_branch,
                    sublinear_tf=True,
                    dtype=np.float32,
                ),
            ),
        ]
    )
    matrix = vectorizer.fit_transform(texts)
    artifact = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task": "official_evidence_retrieval",
        "safety_note": "검색 점수는 위험등급이 아니며 Rule 근거는 rule_id로 직접 조회해야 함",
        "features": {
            "word_tfidf": "1~2 gram, max 30000",
            "char_tfidf": "3~5 gram, max 30000",
            "document_body_character_cap": 8000,
        },
        "vectorizer": vectorizer,
        "matrix": matrix,
        "rows": rows,
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, model_path, compress=3)
    return {
        "model_path": str(model_path),
        "document_count": len(rows),
        "feature_count": matrix.shape[1],
        "matrix_nonzero": int(matrix.nnz),
        "schema_version": MODEL_SCHEMA_VERSION,
    }


def load_retriever(model_path: Path = DEFAULT_RETRIEVER_MODEL) -> dict[str, Any]:
    if not model_path.exists():
        raise FileNotFoundError(
            f"retriever 모델이 없습니다: {model_path}. `train`을 먼저 실행하세요."
        )
    artifact = joblib.load(model_path)
    if artifact.get("schema_version") != MODEL_SCHEMA_VERSION:
        raise RuntimeError(
            "지원하지 않는 retriever artifact 버전입니다. 다시 학습하세요."
        )
    return artifact


def _fts_query(query: str) -> str:
    tokens = re.findall(r"[0-9A-Za-z가-힣\-]+", normalize_text(query))
    tokens = [token.replace('"', "") for token in tokens if len(token) >= 2]
    return " OR ".join(f'"{token}"' for token in tokens[:20])


def _bm25_ranks(db_path: Path, query: str, limit: int) -> list[str]:
    match = _fts_query(query)
    if not match:
        return []
    try:
        with connect_readonly(db_path) as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT evidence_id
                    FROM evidence_fts
                    WHERE evidence_fts MATCH ?
                    ORDER BY bm25(evidence_fts)
                    LIMIT ?
                    """,
                    (match, limit),
                )
            ]
    except sqlite3.OperationalError:
        return []


def _exact_ranks(
    rows: list[dict[str, Any]], query: str, cas_hint: str | None, limit: int
) -> list[str]:
    query_norm = normalize_text(query)
    ranked = []
    for row in rows:
        cas = str(row.get("cas_number") or "")
        title = normalize_text(str(row.get("title") or ""))
        if cas_hint and cas == cas_hint:
            ranked.append(str(row["evidence_id"]))
        elif cas and cas in query_norm:
            ranked.append(str(row["evidence_id"]))
        elif query_norm and query_norm == title:
            ranked.append(str(row["evidence_id"]))
        if len(ranked) >= limit:
            break
    return ranked


def _source_key(row: dict[str, Any]) -> str:
    """출처 표기의 대소문자·주변 공백 차이만 정규화한다."""

    return str(row.get("source") or "").strip().upper()


def _append_missing_cas_source_representatives(
    exact: list[str],
    rows: list[dict[str, Any]],
    cas_hint: str | None,
) -> list[str]:
    """잘린 exact 후보에 누락된 동일 CAS 출처의 첫 문서를 보충한다.

    기존 exact 후보의 순서와 순위는 그대로 둔다. ``candidate_limit`` 앞에서
    한 출처의 세부 문서가 후보를 모두 차지한 경우에만, 정확히 같은 CAS를
    가진 다른 출처의 대표 문서를 뒤에 추가한다.
    """

    if not cas_hint:
        return exact

    cas_value = str(cas_hint).strip()
    row_by_id = {str(row["evidence_id"]): row for row in rows}
    present_sources = {
        _source_key(row_by_id[evidence_id])
        for evidence_id in exact
        if evidence_id in row_by_id
        and str(row_by_id[evidence_id].get("cas_number") or "").strip() == cas_value
        and _source_key(row_by_id[evidence_id])
    }
    augmented = list(exact)
    for row in rows:
        if str(row.get("cas_number") or "").strip() != cas_value:
            continue
        source = _source_key(row)
        if not source or source in present_sources:
            continue
        augmented.append(str(row["evidence_id"]))
        present_sources.add(source)
    return augmented


def _tfidf_ranks(
    artifact: dict[str, Any], query: str, cas_hint: str | None, limit: int
) -> list[str]:
    expanded = f"{query} {cas_hint or ''}".strip()
    vector = artifact["vectorizer"].transform([normalize_text(expanded)])
    scores = (artifact["matrix"] @ vector.T).toarray().ravel()
    if not np.any(scores > 0):
        return []
    take = min(limit, scores.size)
    indices = np.argpartition(-scores, take - 1)[:take]
    indices = indices[np.argsort(-scores[indices], kind="stable")]
    return [
        str(artifact["rows"][index]["evidence_id"])
        for index in indices
        if scores[index] > 0
    ]


def _rrf(rankings: list[tuple[list[str], float]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking, weight in rankings:
        for rank, evidence_id in enumerate(ranking, 1):
            scores[evidence_id] = scores.get(evidence_id, 0.0) + weight / (k + rank)
    return scores


def _select_source_diverse_ids(
    ranked_ids: list[str],
    row_by_id: dict[str, dict[str, Any]],
    cas_hint: str | None,
    top_k: int,
) -> list[str]:
    """동일 CAS의 검색 결과가 가능한 범위에서 출처별로 노출되게 선택한다.

    전역 RRF 1위는 그대로 유지하고, 그 다음 슬롯에는 아직 노출되지 않은
    출처의 동일-CAS 대표를 전역 순위 순으로 배치한다. 나머지는 다시 전역
    순위대로 채운다. 점수 자체는 바꾸지 않지만 표시 순서는 출처 다양화로
    달라질 수 있다. 출처 수가 ``top_k``보다 많으면 전역 순위가 높은 출처를
    우선한다.
    """

    if top_k <= 0:
        return []
    if not cas_hint:
        return ranked_ids[:top_k]

    cas_value = str(cas_hint).strip()
    representatives: dict[str, str] = {}
    for evidence_id in ranked_ids:
        row = row_by_id.get(evidence_id)
        if not row or str(row.get("cas_number") or "").strip() != cas_value:
            continue
        source = _source_key(row)
        if source and source not in representatives:
            representatives[source] = evidence_id

    selected: list[str] = []
    selected_set: set[str] = set()

    def append(evidence_id: str) -> None:
        if evidence_id not in selected_set and len(selected) < top_k:
            selected.append(evidence_id)
            selected_set.add(evidence_id)

    if ranked_ids:
        append(ranked_ids[0])
    for evidence_id in representatives.values():
        append(evidence_id)
    for evidence_id in ranked_ids:
        if len(selected) >= top_k:
            break
        append(evidence_id)
    return selected


def _empty_cas_retrieval(
    query: str,
    cas_hint: str,
    *,
    status: str,
    warning: str,
    notice: str,
) -> dict[str, Any]:
    """CAS 제한 검색을 일반 유사 문서로 대체하지 않고 종료한다."""

    return {
        "status": status,
        "query": query,
        "cas_hint": cas_hint,
        "method": "strict same-CAS official evidence filter",
        "warning": warning,
        "notice": notice,
        "ranking_notice": (
            "CAS 힌트가 제공되어 다른 CAS의 문서는 검색 결과에서 제외했습니다."
        ),
        "cas_link_warning": None,
        "results": [],
    }


def search_evidence(
    query: str,
    db_path: Path,
    artifact: dict[str, Any],
    cas_hint: str | None = None,
    top_k: int = 8,
    candidate_limit: int = 40,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = artifact["rows"]
    row_by_id = {str(row["evidence_id"]): row for row in rows}

    normalized_cas_hint: str | None = None
    eligible_ids: set[str] | None = None
    if cas_hint is not None:
        normalized_cas_hint = normalize_cas(str(cas_hint))
        if not valid_cas_checksum(normalized_cas_hint):
            return _empty_cas_retrieval(
                query,
                normalized_cas_hint,
                status=INVALID_CAS_HINT_STATUS,
                warning="CAS 힌트의 형식 또는 체크디지트가 유효하지 않습니다.",
                notice=(
                    "잘못된 CAS를 일반 검색으로 대체하지 않았습니다. "
                    "용기 라벨 또는 외부 공식 MSDS에서 CAS를 다시 확인해야 합니다."
                ),
            )
        eligible_ids = {
            str(row["evidence_id"])
            for row in rows
            if normalize_cas(str(row.get("cas_number") or "")) == normalized_cas_hint
            and _source_key(row) in OFFICIAL_EVIDENCE_SOURCES
        }
        if not eligible_ids:
            return _empty_cas_retrieval(
                query,
                normalized_cas_hint,
                status=CAS_EVIDENCE_NOT_LOADED_STATUS,
                warning=(
                    f"CAS {normalized_cas_hint}의 상세 근거가 시스템에 적재되어 "
                    "있지 않습니다."
                ),
                notice=(
                    "상세 근거 미적재 — 다른 물질의 근거로 대체하지 않습니다. "
                    "외부 공식 MSDS 확인이 필요합니다."
                ),
            )

    exact = _exact_ranks(rows, query, normalized_cas_hint, candidate_limit)
    exact = _append_missing_cas_source_representatives(
        exact,
        rows,
        normalized_cas_hint,
    )
    bm25 = _bm25_ranks(
        db_path,
        f"{query} {normalized_cas_hint or ''}",
        candidate_limit,
    )
    tfidf = _tfidf_ranks(
        artifact,
        query,
        normalized_cas_hint,
        candidate_limit,
    )
    if eligible_ids is not None:
        exact = [evidence_id for evidence_id in exact if evidence_id in eligible_ids]
        bm25 = [evidence_id for evidence_id in bm25 if evidence_id in eligible_ids]
        tfidf = [evidence_id for evidence_id in tfidf if evidence_id in eligible_ids]
    scores = _rrf([(exact, 2.0), (bm25, 1.0), (tfidf, 1.0)])
    all_ranked_ids = sorted(scores, key=lambda item: (-scores[item], item))
    ranked_ids = _select_source_diverse_ids(
        all_ranked_ids,
        row_by_id,
        normalized_cas_hint,
        top_k,
    )
    results = []
    for evidence_id in ranked_ids:
        row = dict(row_by_id[evidence_id])
        row["rrf_score"] = round(scores[evidence_id], 8)
        row["body_preview"] = str(row.pop("body", ""))[:280].replace("\n", " ")
        row["rank_sources"] = {
            "exact": exact.index(evidence_id) + 1 if evidence_id in exact else None,
            "bm25": bm25.index(evidence_id) + 1 if evidence_id in bm25 else None,
            "tfidf": tfidf.index(evidence_id) + 1 if evidence_id in tfidf else None,
        }
        results.append(row)
    unapproved_link_statuses = sorted(
        {
            str(row.get("cas_link_status") or "")
            for row in results
            if str(row.get("cas_link_status") or "")
            not in {"SOURCE_EXACT", "APPROVED", "PUBLIC_SOURCE_VERIFIED"}
        }
    )
    return {
        "status": "COMPLETED" if results else "NO_EVIDENCE_FOUND",
        "query": query,
        "cas_hint": normalized_cas_hint,
        "method": (
            "exact(2x) + SQLite FTS5 BM25 + word/char TF-IDF + RRF(k=60) "
            "+ 동일 CAS 공식출처 엄격 제한 및 상위 슬롯 보존"
        ),
        "warning": "검색 순위는 위험등급이 아니며 결과 원문과 CAS를 확인해야 합니다.",
        "notice": None,
        "ranking_notice": (
            "동일 CAS의 KOSHA·CAMEO 공식 근거만 표시합니다. RRF 1위 뒤에는 "
            "공식출처별 대표 근거를 우선 표시하며, RRF 점수 자체는 변경하지 않습니다."
            if normalized_cas_hint
            else None
        ),
        "cas_link_warning": (
            "공개 근거 대조가 완료되지 않은 CAMEO–CAS 연결이 포함되어 있습니다: "
            + ", ".join(unapproved_link_statuses)
            if unapproved_link_statuses
            else None
        ),
        "results": results,
    }


def _gold_rank(results: list[dict[str, Any]], case: dict[str, str]) -> int | None:
    expected_source = case["expected_source"].upper()
    expected_cas = case.get("expected_cas", "")
    expected_cameo = case.get("expected_cameo_id", "")
    for rank, result in enumerate(results, 1):
        if str(result.get("source", "")).upper() != expected_source:
            continue
        if (
            expected_cameo
            and str(result.get("cameo_chemical_id") or "") != expected_cameo
        ):
            continue
        if expected_cas and str(result.get("cas_number") or "") != expected_cas:
            continue
        return rank
    return None


def evaluate_retriever(
    db_path: Path,
    model_path: Path = DEFAULT_RETRIEVER_MODEL,
    resolver_model_path: Path = DEFAULT_RESOLVER_MODEL,
    evaluation_path: Path = EVALUATION_DIR / "retrieval_regression_queries.csv",
    report_path: Path | None = None,
) -> dict[str, Any]:
    artifact = load_retriever(model_path)
    resolver = load_resolver(resolver_model_path)
    with evaluation_path.open(encoding="utf-8-sig", newline="") as handle:
        cases = list(csv.DictReader(handle))
    rows = []
    for case in cases:
        automatic_cas_hint = select_evidence_cas_hint_from_text(case["query"], resolver)
        end_to_end_result = search_evidence(
            case["query"],
            db_path,
            artifact,
            cas_hint=automatic_cas_hint,
            top_k=8,
        )
        end_to_end_rank = _gold_rank(end_to_end_result["results"], case)

        expected_cas = normalize_cas(case.get("expected_cas", ""))
        oracle_cas_hint = expected_cas if valid_cas_checksum(expected_cas) else None
        oracle_cas_rank = None
        if oracle_cas_hint:
            oracle_cas_result = search_evidence(
                case["query"],
                db_path,
                artifact,
                cas_hint=oracle_cas_hint,
                top_k=8,
            )
            oracle_cas_rank = _gold_rank(oracle_cas_result["results"], case)

        if not oracle_cas_hint:
            cas_hint_status = "NO_EXPECTED_CAS"
        elif automatic_cas_hint is None:
            cas_hint_status = "MISSING"
        elif automatic_cas_hint == oracle_cas_hint:
            cas_hint_status = "MATCH"
        else:
            cas_hint_status = "MISMATCH"

        rows.append(
            {
                "query": case["query"],
                "automatic_cas_hint": automatic_cas_hint,
                "expected_cas_hint": oracle_cas_hint,
                "cas_hint_status": cas_hint_status,
                "expected_source": case["expected_source"],
                "expected_cas": case.get("expected_cas"),
                "expected_cameo_id": case.get("expected_cameo_id"),
                "end_to_end_rank": end_to_end_rank,
                "oracle_cas_rank": oracle_cas_rank,
                # v1 보고서를 소비하는 내부 도구를 위한 호환 필드입니다.
                "resolver_cas_hint": automatic_cas_hint,
                "rank": end_to_end_rank,
            }
        )

    def _recall(rank_key: str, limit: int, target_rows: list[dict[str, Any]]) -> float:
        if not target_rows:
            return 0.0
        return float(
            np.mean(
                [bool(row[rank_key] and row[rank_key] <= limit) for row in target_rows]
            )
        )

    def _mrr(rank_key: str, limit: int, target_rows: list[dict[str, Any]]) -> float:
        if not target_rows:
            return 0.0
        return float(
            np.mean(
                [
                    1 / row[rank_key]
                    if row[rank_key] and row[rank_key] <= limit
                    else 0.0
                    for row in target_rows
                ]
            )
        )

    oracle_rows = [row for row in rows if row["expected_cas_hint"]]
    hinted_rows = [row for row in rows if row["automatic_cas_hint"]]
    matched_hint_count = sum(row["cas_hint_status"] == "MATCH" for row in rows)
    end_to_end_recall_at_5 = _recall("end_to_end_rank", 5, rows)
    end_to_end_recall_at_8 = _recall("end_to_end_rank", 8, rows)
    end_to_end_mrr_at_8 = _mrr("end_to_end_rank", 8, rows)
    summary = {
        "metrics_version": "retriever-evaluation-v2",
        "dataset": str(evaluation_path),
        "dataset_status": "DRAFT 내부 회귀셋으로 현장 성능 주장 금지",
        "case_count": len(rows),
        "end_to_end": {
            "description": "자동 CAS 힌트를 포함한 실제 검색 흐름",
            "recall_at_5": end_to_end_recall_at_5,
            "recall_at_8": end_to_end_recall_at_8,
            "mrr_at_8": end_to_end_mrr_at_8,
        },
        "retriever_with_oracle_cas": {
            "description": "정답 CAS를 검색 필터로 제공한 Retriever 단독 상한선",
            "eligible_case_count": len(oracle_rows),
            "recall_at_5": _recall("oracle_cas_rank", 5, oracle_rows),
            "recall_at_8": _recall("oracle_cas_rank", 8, oracle_rows),
            "mrr_at_8": _mrr("oracle_cas_rank", 8, oracle_rows),
        },
        "cas_hint": {
            "coverage": len(hinted_rows) / len(rows) if rows else 0.0,
            "exact_match_rate": matched_hint_count / len(rows) if rows else 0.0,
            "precision_when_present": (
                matched_hint_count / len(hinted_rows) if hinted_rows else 0.0
            ),
            "missing_count": sum(row["cas_hint_status"] == "MISSING" for row in rows),
            "mismatch_count": sum(row["cas_hint_status"] == "MISMATCH" for row in rows),
        },
        "metric_notice": (
            "end_to_end는 Resolver의 자동 CAS 힌트까지 포함합니다. "
            "retriever_with_oracle_cas는 평가용 정답 CAS를 주입한 검색기 단독 상한선이며 "
            "운영 성능으로 해석하면 안 됩니다."
        ),
        # v1 보고서 소비자를 위한 호환 필드입니다. 신규 코드는 end_to_end를 사용합니다.
        "recall_at_5": end_to_end_recall_at_5,
        "recall_at_8": end_to_end_recall_at_8,
        "mrr_at_8": end_to_end_mrr_at_8,
        "rows": rows,
    }
    if report_path:
        write_json(report_path, summary)
    return summary
