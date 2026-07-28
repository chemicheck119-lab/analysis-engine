"""권위 식별자·공개 별칭을 이용한 일반 물질 후보 resolver."""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from chemiguard119.database import connect_readonly
from chemiguard119.paths import DEFAULT_RESOLVER_MODEL, EVALUATION_DIR
from chemiguard119.utils import (
    compact_text,
    normalize_cas,
    normalize_text,
    valid_cas_checksum,
    write_json,
)


MODEL_SCHEMA_VERSION = "resolver-char-tfidf-v2"
RUNTIME_INDEX_VERSION = "resolver-runtime-index-v1"
RUNTIME_INDEX_KEY = "_runtime_index"

ICIS_CANDIDATE_STATUS = "PUBLIC_CATALOG_CANDIDATE"
AUTHORITATIVE_ALIAS_TYPES = {
    "canonical_ko",
    "canonical_en",
    "canonical_name_ko",
    "canonical_name_en",
    "kosha_name",
    "search_name",
    "icis_primary_name",
    "ulsan_name_ko",
    "ulsan_name_en",
    "formula",
    "un_number",
}
COMMON_ALIAS_TYPES = {
    "alias",
    "configured_alias",
    "product_name",
    "common_name",
    "icis_reported_alias",
}
AUTHORITY_PRIORITY = {
    "PUBLIC_AUTHORITY_SOURCE": 0,
    "PROJECT_VERIFIED": 1,
    "PUBLIC_CATALOG_CANDIDATE": 2,
    "PROJECT_CONFIG_CANDIDATE": 3,
    "UNVERIFIED": 4,
}


def _load_alias_rows(db_path: Path) -> list[dict[str, Any]]:
    with connect_readonly(db_path) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(alias)")}
        required = {"cas_number", "alias_text", "normalized_text", "alias_type"}
        if not required.issubset(columns):
            raise RuntimeError(
                f"alias 테이블 컬럼이 부족합니다: {sorted(required - columns)}"
            )
        source_expression = "COALESCE(a.source, '')" if "source" in columns else "''"
        status_expression = (
            "COALESCE(a.verification_status, '')"
            if "verification_status" in columns
            else "''"
        )
        substance_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(substance)")
        }
        has_scope_columns = {
            "catalog_scope",
            "has_kosha_detail",
            "resolver_candidate_only",
        }.issubset(substance_columns)
        if has_scope_columns:
            query = f"""
                SELECT a.cas_number, a.alias_text, a.normalized_text, a.alias_type,
                       {source_expression} AS source,
                       {status_expression} AS verification_status,
                       s.catalog_scope,
                       s.has_kosha_detail,
                       s.resolver_candidate_only
                FROM alias AS a
                JOIN substance AS s ON s.cas_number = a.cas_number
                ORDER BY a.cas_number, a.alias_type, a.alias_text
            """
        else:
            query = f"""
                SELECT a.cas_number, a.alias_text, a.normalized_text, a.alias_type,
                       {source_expression} AS source,
                       {status_expression} AS verification_status
                FROM alias AS a
                ORDER BY a.cas_number, a.alias_type, a.alias_text
            """
        rows = [dict(row) for row in connection.execute(query)]
    for row in rows:
        candidate_only = row.get("verification_status") == ICIS_CANDIDATE_STATUS
        row.setdefault(
            "catalog_scope",
            "ICIS_PUBLIC_CATALOG_CANDIDATE"
            if candidate_only
            else "LEGACY_OR_TEST_REGISTRY",
        )
        row.setdefault("has_kosha_detail", 0)
        row.setdefault("resolver_candidate_only", int(candidate_only))
    if not rows:
        raise RuntimeError("학습할 검증 별칭이 없습니다. 먼저 prepare를 실행하세요.")
    return rows


def train_resolver(
    db_path: Path, model_path: Path = DEFAULT_RESOLVER_MODEL
) -> dict[str, Any]:
    """별칭 문자열을 문자 2~5-gram TF-IDF 공간에 적합한다.

    이는 화학 위험 분류가 아니라 물질 후보 검색 모델이다.
    """

    rows = _load_alias_rows(db_path)
    texts = [normalize_text(row["alias_text"]) for row in rows]
    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 5),
        lowercase=False,
        sublinear_tf=True,
        norm="l2",
    )
    matrix = vectorizer.fit_transform(texts)
    artifact = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task": "substance_candidate_retrieval",
        "safety_note": "위험등급이나 대응을 예측하지 않으며 모든 이름 기반 결과는 대원 확인이 필요함",
        "features": {
            "normalization": "Unicode NFKC + lowercase + whitespace/punctuation normalization",
            "vectorizer": "character TF-IDF",
            "ngram_range": [2, 5],
        },
        "vectorizer": vectorizer,
        "matrix": matrix,
        "rows": rows,
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, model_path)
    return {
        "model_path": str(model_path),
        "alias_count": len(rows),
        "substance_count": len({row["cas_number"] for row in rows}),
        "feature_count": matrix.shape[1],
        "schema_version": MODEL_SCHEMA_VERSION,
    }


def load_resolver(model_path: Path = DEFAULT_RESOLVER_MODEL) -> dict[str, Any]:
    if not model_path.exists():
        raise FileNotFoundError(
            f"resolver 모델이 없습니다: {model_path}. `train`을 먼저 실행하세요."
        )
    artifact = joblib.load(model_path)
    if artifact.get("schema_version") != MODEL_SCHEMA_VERSION:
        raise RuntimeError(
            "지원하지 않는 resolver artifact 버전입니다. 다시 학습하세요."
        )
    artifact[RUNTIME_INDEX_KEY] = build_resolver_runtime_index(artifact)
    return artifact


def build_resolver_runtime_index(
    artifact: dict[str, Any],
) -> dict[str, Any]:
    """배포 artifact는 바꾸지 않고 반복 정규화 결과만 메모리에 구성한다."""

    rows: list[dict[str, Any]] = artifact.get("rows", [])
    cas_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    exact_aliases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    alias_groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        cas_number = normalize_cas(str(row.get("cas_number") or ""))
        if cas_number:
            cas_rows[cas_number].append(row)

        alias = str(row.get("alias_text") or "").strip()
        normalized_alias = compact_text(alias)
        if not normalized_alias:
            continue
        exact_aliases[normalized_alias].append(row)
        if not valid_cas_checksum(cas_number):
            continue
        group = alias_groups.setdefault(
            normalized_alias,
            {"cas_numbers": set(), "eligible_surfaces": set()},
        )
        group["cas_numbers"].add(cas_number)
        if (
            len(alias) >= 2
            and _alias_class(str(row.get("alias_type") or "")) == "AUTHORITATIVE_NAME"
            and _authority_level(row)
            in {"PUBLIC_AUTHORITY_SOURCE", "PUBLIC_CATALOG_CANDIDATE"}
        ):
            group["eligible_surfaces"].add(alias)

    return {
        "version": RUNTIME_INDEX_VERSION,
        "cas_rows": dict(cas_rows),
        "exact_aliases": dict(exact_aliases),
        "alias_groups": alias_groups,
    }


def _runtime_index(artifact: dict[str, Any]) -> dict[str, Any]:
    index = artifact.get(RUNTIME_INDEX_KEY)
    if not isinstance(index, dict) or index.get("version") != RUNTIME_INDEX_VERSION:
        index = build_resolver_runtime_index(artifact)
        artifact[RUNTIME_INDEX_KEY] = index
    return index


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "y", "yes"}
    return bool(value)


def _alias_class(alias_type: str) -> str:
    normalized = alias_type.strip().lower()
    if normalized == "cas":
        return "IDENTIFIER"
    if normalized in AUTHORITATIVE_ALIAS_TYPES or normalized.startswith("canonical"):
        return "AUTHORITATIVE_NAME"
    if normalized in COMMON_ALIAS_TYPES or normalized.endswith("_alias"):
        return "PRODUCT_OR_COMMON_NAME"
    return "REPORTED_ALIAS"


def _authority_level(row: dict[str, Any]) -> str:
    status = str(row.get("verification_status") or "").strip().upper()
    if status == ICIS_CANDIDATE_STATUS:
        return "PUBLIC_CATALOG_CANDIDATE"
    if status in {"SOURCE_EXACT", "SOURCE_EXACT_VALID_CAS"}:
        return "PUBLIC_AUTHORITY_SOURCE"
    if status in {"PROJECT_CONFIG_CANDIDATE", "APPROVED_INTERNAL_DEMO"}:
        return "PROJECT_CONFIG_CANDIDATE"
    if status == "VERIFIED":
        return "PROJECT_VERIFIED"
    return "UNVERIFIED"


def _candidate(
    row: dict[str, Any],
    *,
    cas_number: str,
    score: float,
    matched_alias: str,
    match_type: str,
    matched_alias_type: str | None = None,
) -> dict[str, Any]:
    alias_type = matched_alias_type or str(row.get("alias_type") or "")
    verification_status = str(row.get("verification_status") or "")
    catalog_candidate_only = _as_bool(row.get("resolver_candidate_only")) or (
        verification_status == ICIS_CANDIDATE_STATUS
    )
    return {
        "cas_number": cas_number,
        "score": score,
        "matched_alias": matched_alias,
        "match_type": match_type,
        "matched_alias_type": alias_type,
        "matched_alias_class": _alias_class(alias_type),
        "matched_alias_source": str(row.get("source") or ""),
        "matched_alias_verification_status": verification_status,
        "authority_level": _authority_level(row),
        "catalog_scope": str(row.get("catalog_scope") or "LEGACY_OR_TEST_REGISTRY"),
        "has_kosha_detail": _as_bool(row.get("has_kosha_detail")),
        "catalog_candidate_only": catalog_candidate_only,
        # Resolver 결과는 식별 후보일 뿐이다. Rule Engine은 별도의 대원 확인을 거친
        # confirmed CAS만 입력으로 받는다.
        "rule_eligible": False,
        "current_inventory_confirmed": False,
    }


def _result(
    query: str,
    normalized_query: str,
    *,
    status: str,
    input_class: str,
    confirmation_reason: str,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "query": query,
        "normalized_query": normalized_query,
        "status": status,
        "input_class": input_class,
        "requires_responder_confirmation": True,
        "confirmation_reason": confirmation_reason,
        "rule_input_eligible": False,
        "current_inventory_confirmed": False,
        "candidates": candidates,
    }


def _looks_like_cas(value: str) -> bool:
    normalized = normalize_cas(value)
    return "-" in normalized and bool(re.fullmatch(r"[0-9-]+", normalized))


def resolve_substance(
    query: str,
    artifact: dict[str, Any],
    top_k: int = 3,
    minimum_score: float = 0.20,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = artifact["rows"]
    runtime_index = _runtime_index(artifact)
    cas_query = normalize_cas(query)
    if valid_cas_checksum(cas_query):
        exact_rows = runtime_index["cas_rows"].get(cas_query, [])
        if exact_rows:
            representative = sorted(
                exact_rows,
                key=lambda row: (
                    str(row.get("alias_type") or "").lower() != "cas",
                    not _as_bool(row.get("has_kosha_detail")),
                    str(row.get("alias_text") or ""),
                ),
            )[0]
            return _result(
                query,
                cas_query,
                status="EXACT_IDENTIFIER_MATCH",
                input_class="AUTHORITATIVE_IDENTIFIER",
                confirmation_reason="IDENTITY_EXACT_PRESENCE_UNCONFIRMED",
                candidates=[
                    _candidate(
                        representative,
                        cas_number=cas_query,
                        score=1.0,
                        matched_alias=cas_query,
                        match_type="CAS_EXACT",
                        matched_alias_type="cas",
                    )
                ],
            )
        return _result(
            query,
            cas_query,
            status="UNRESOLVED",
            input_class="UNRESOLVED",
            confirmation_reason="VALID_CAS_NOT_IN_CATALOG",
            candidates=[],
        )
    if _looks_like_cas(query):
        return _result(
            query,
            cas_query,
            status="UNRESOLVED",
            input_class="UNRESOLVED",
            confirmation_reason="INVALID_CAS_IDENTIFIER",
            candidates=[],
        )

    compact_query = compact_text(query)
    exact_aliases = (
        runtime_index["exact_aliases"].get(compact_query, []) if compact_query else []
    )
    exact_grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in exact_aliases:
        exact_grouped[row["cas_number"]].append(row)
    if exact_grouped:
        ambiguous = len(exact_grouped) > 1
        candidates = []
        for cas, grouped in sorted(exact_grouped.items()):
            representative = sorted(
                grouped,
                key=lambda row: (
                    _authority_level(row) == "UNVERIFIED",
                    str(row.get("alias_type") or ""),
                ),
            )[0]
            candidates.append(
                _candidate(
                    representative,
                    cas_number=cas,
                    score=1.0,
                    matched_alias=str(representative["alias_text"]),
                    match_type=(
                        "AMBIGUOUS_ALIAS_EXACT" if ambiguous else "UNIQUE_ALIAS_EXACT"
                    ),
                )
            )
        candidates.sort(
            key=lambda item: (
                AUTHORITY_PRIORITY.get(str(item.get("authority_level")), 99),
                not bool(item.get("has_kosha_detail")),
                str(item.get("cas_number") or ""),
            )
        )
        alias_class = candidates[0]["matched_alias_class"]
        return _result(
            query,
            normalize_text(query),
            status="AMBIGUOUS_ALIAS" if ambiguous else "EXACT_ALIAS_CANDIDATE",
            input_class=(
                "AMBIGUOUS_EXPRESSION"
                if ambiguous
                else (
                    "PRODUCT_OR_COMMON_NAME"
                    if alias_class == "PRODUCT_OR_COMMON_NAME"
                    else "AUTHORITATIVE_ALIAS"
                )
            ),
            confirmation_reason=(
                "MULTIPLE_CAS_FOR_EXPRESSION"
                if ambiguous
                else "NAME_MATCH_REQUIRES_IDENTITY_AND_PRESENCE_CONFIRMATION"
            ),
            candidates=candidates[:top_k],
        )

    if "vectorizer" not in artifact or "matrix" not in artifact:
        return _result(
            query,
            normalize_text(query),
            status="UNRESOLVED",
            input_class="UNRESOLVED",
            confirmation_reason="NO_EXACT_MATCH_AND_NO_SIMILARITY_MODEL",
            candidates=[],
        )
    query_vector = artifact["vectorizer"].transform([normalize_text(query)])
    scores = (artifact["matrix"] @ query_vector.T).toarray().ravel()
    best_by_cas: dict[str, tuple[float, int]] = {}
    for index, score in enumerate(scores):
        cas = rows[index]["cas_number"]
        if cas not in best_by_cas or score > best_by_cas[cas][0]:
            best_by_cas[cas] = (float(score), index)
    ranked = sorted(best_by_cas.items(), key=lambda item: (-item[1][0], item[0]))
    candidates = []
    for cas, (score, index) in ranked[:top_k]:
        if score < minimum_score:
            continue
        candidates.append(
            _candidate(
                rows[index],
                cas_number=cas,
                score=round(score, 6),
                matched_alias=str(rows[index]["alias_text"]),
                match_type="CHAR_TFIDF_CANDIDATE",
            )
        )
    return _result(
        query,
        normalize_text(query),
        status="FUZZY_CANDIDATE" if candidates else "UNRESOLVED",
        input_class="UNCONFIRMED_CANDIDATE" if candidates else "UNRESOLVED",
        confirmation_reason=(
            "SIMILARITY_ONLY_REQUIRES_IDENTITY_AND_PRESENCE_CONFIRMATION"
            if candidates
            else "NO_MATCH_ABOVE_THRESHOLD"
        ),
        candidates=candidates,
    )


def select_evidence_cas_hint(resolution: dict[str, Any]) -> str | None:
    """근거 검색을 좁혀도 되는 단일 CAS만 반환한다.

    이 값은 Rule 입력 승인이 아니다. 모호 표현, 제품·통칭, 유사도 후보는 첫
    후보를 임의 선택하지 않고 검색 질의 원문만 사용한다.
    """

    if resolution.get("status") not in {
        "EXACT_IDENTIFIER_MATCH",
        "EXACT_ALIAS_CANDIDATE",
    }:
        return None
    input_class = resolution.get("input_class")
    if input_class not in {
        "AUTHORITATIVE_IDENTIFIER",
        "AUTHORITATIVE_ALIAS",
    }:
        return None
    candidates = resolution.get("candidates") or []
    if len(candidates) != 1:
        return None
    if input_class == "AUTHORITATIVE_ALIAS" and candidates[0].get(
        "authority_level"
    ) not in {
        "PUBLIC_AUTHORITY_SOURCE",
        "PUBLIC_CATALOG_CANDIDATE",
    }:
        return None
    cas_number = normalize_cas(str(candidates[0].get("cas_number") or ""))
    return cas_number if valid_cas_checksum(cas_number) else None


def select_evidence_cas_hint_from_text(
    query: str,
    artifact: dict[str, Any],
) -> str | None:
    """긴 검색문 안의 단일 공공 출처 물질명만 CAS 검색 힌트로 선택한다.

    가장 긴 비중첩 표현을 먼저 선택하므로 ``차아염소산나트륨`` 안의
    ``나트륨``을 별도 물질로 오인하지 않는다. 같은 표현이 여러 CAS에 연결되면
    힌트를 반환하지 않는다.
    """

    direct = resolve_substance(query, artifact, top_k=3)
    if direct.get("status") == "AMBIGUOUS_ALIAS":
        return None
    direct_hint = select_evidence_cas_hint(direct)
    if direct_hint:
        return direct_hint

    alias_groups: dict[str, dict[str, Any]] = _runtime_index(artifact)["alias_groups"]

    grouped: dict[tuple[int, int, str], set[str]] = defaultdict(set)
    compact_query = compact_text(query)
    for normalized_alias, alias_group in alias_groups.items():
        for alias in sorted(alias_group["eligible_surfaces"]):
            if re.fullmatch(r"[A-Za-z0-9]+", alias):
                pattern = re.compile(
                    rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])",
                    re.IGNORECASE,
                )
                matches = [
                    (match.start(), match.end()) for match in pattern.finditer(query)
                ]
            else:
                matches = []
                start = compact_query.find(normalized_alias)
                while start >= 0:
                    matches.append((start, start + len(normalized_alias)))
                    start = compact_query.find(normalized_alias, start + 1)
            for start, end in matches:
                grouped[(start, end, normalized_alias)].update(
                    alias_group["cas_numbers"]
                )

    selected_cas: set[str] = set()
    selected_spans: list[tuple[int, int]] = []
    for (start, end, _alias), cas_numbers in sorted(
        grouped.items(),
        key=lambda item: (-(item[0][1] - item[0][0]), item[0][0], item[0][2]),
    ):
        if any(
            start < chosen_end and chosen_start < end
            for chosen_start, chosen_end in selected_spans
        ):
            continue
        selected_spans.append((start, end))
        selected_cas.update(cas_numbers)
    return next(iter(selected_cas)) if len(selected_cas) == 1 else None


def evaluate_resolver(
    model_path: Path = DEFAULT_RESOLVER_MODEL,
    evaluation_path: Path = EVALUATION_DIR / "resolver_regression_queries.csv",
    report_path: Path | None = None,
) -> dict[str, Any]:
    artifact = load_resolver(model_path)
    with evaluation_path.open(encoding="utf-8-sig", newline="") as handle:
        cases = list(csv.DictReader(handle))
    rows = []
    for case in cases:
        result = resolve_substance(case["query"], artifact, top_k=3)
        ranked = [item["cas_number"] for item in result["candidates"]]
        expected = case["expected_cas"]
        rank = ranked.index(expected) + 1 if expected in ranked else None
        unique_resolution_correct = bool(
            rank == 1
            and len(ranked) == 1
            and result["status"] in {"EXACT_IDENTIFIER_MATCH", "EXACT_ALIAS_CANDIDATE"}
        )
        rows.append(
            {
                "query": case["query"],
                "query_type": case["query_type"],
                "expected_cas": expected,
                "rank": rank,
                "top1": ranked[0] if ranked else None,
                "status": result["status"],
                "candidate_count": len(ranked),
                "candidate_top1_hit": rank == 1,
                "unique_resolution_correct": unique_resolution_correct,
            }
        )
    reciprocal_ranks = [1 / row["rank"] if row["rank"] else 0.0 for row in rows]
    candidate_top1_hit_rate = (
        float(np.mean([row["candidate_top1_hit"] for row in rows])) if rows else 0.0
    )
    unique_resolution_accuracy = (
        float(np.mean([row["unique_resolution_correct"] for row in rows]))
        if rows
        else 0.0
    )
    candidate_top3_recall = (
        float(np.mean([bool(row["rank"] and row["rank"] <= 3) for row in rows]))
        if rows
        else 0.0
    )
    candidate_mrr = float(np.mean(reciprocal_ranks)) if rows else 0.0
    summary = {
        "metrics_version": "resolver-evaluation-v2",
        "dataset": str(evaluation_path),
        "dataset_status": "내부 회귀셋이며 현장 성능 주장 금지",
        "case_count": len(rows),
        "top1_accuracy": unique_resolution_accuracy,
        "top3_recall": candidate_top3_recall,
        "mrr": candidate_mrr,
        "candidate_top1_hit_rate": candidate_top1_hit_rate,
        "candidate_top3_recall": candidate_top3_recall,
        "candidate_mrr": candidate_mrr,
        "unique_resolution_accuracy": unique_resolution_accuracy,
        "ambiguous_case_count": sum(row["status"] == "AMBIGUOUS_ALIAS" for row in rows),
        "metric_notice": (
            "candidate_*는 후보 목록 적중률이고, top1_accuracy와 "
            "unique_resolution_accuracy는 단일 exact 식별 성공만 계산합니다."
        ),
        "rows": rows,
    }
    if report_path:
        write_json(report_path, summary)
    return summary
