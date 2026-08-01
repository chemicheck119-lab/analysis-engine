"""권위 식별자·공개 별칭을 이용한 일반 물질 후보 resolver."""

from __future__ import annotations

import csv
import re
import sqlite3
import time
import unicodedata
from collections import Counter, defaultdict
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
    sha256_file,
    valid_cas_checksum,
    write_json,
)


MODEL_SCHEMA_VERSION = "resolver-char-tfidf-v2"
INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION = "resolver-char-tfidf-v3-incident-adapted"
SUPPORTED_MODEL_SCHEMA_VERSIONS = {
    MODEL_SCHEMA_VERSION,
    INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION,
}
RUNTIME_INDEX_VERSION = "resolver-runtime-index-v2"
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


def fit_resolver_rows(
    rows: list[dict[str, Any]],
    model_path: Path,
    *,
    schema_version: str = MODEL_SCHEMA_VERSION,
    training_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """검증된 별칭 행으로 문자 TF-IDF 후보 모델을 적합한다.

    별도 공개 데이터로 source adaptation을 수행할 때도 resolver의 동일한
    전처리·후보 계약을 재사용하기 위한 낮은 수준의 학습 함수다.
    """

    if not rows:
        raise RuntimeError("학습할 검증 별칭이 없습니다.")
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
        "schema_version": schema_version,
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
        "training_metadata": dict(training_metadata or {}),
    }
    model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, model_path)
    return {
        "model_path": str(model_path),
        "alias_count": len(rows),
        "substance_count": len({row["cas_number"] for row in rows}),
        "feature_count": matrix.shape[1],
        "schema_version": schema_version,
        "training_metadata": dict(training_metadata or {}),
    }


def train_resolver(
    db_path: Path, model_path: Path = DEFAULT_RESOLVER_MODEL
) -> dict[str, Any]:
    """별칭 문자열을 문자 2~5-gram TF-IDF 공간에 적합한다.

    이는 화학 위험 분류가 아니라 물질 후보 검색 모델이다.
    """

    return fit_resolver_rows(_load_alias_rows(db_path), model_path)


def load_resolver(model_path: Path = DEFAULT_RESOLVER_MODEL) -> dict[str, Any]:
    if not model_path.exists():
        raise FileNotFoundError(
            f"resolver 모델이 없습니다: {model_path}. `train`을 먼저 실행하세요."
        )
    artifact = joblib.load(model_path)
    if artifact.get("schema_version") not in SUPPORTED_MODEL_SCHEMA_VERSIONS:
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

    # 긴 신고문마다 모든 별칭을 처음부터 끝까지 검색하지 않도록 첫 글자별
    # runtime matcher를 만든다. 이는 joblib artifact에 저장되지 않는 파생
    # 인덱스이므로 기존 모델 파일의 schema와 호환된다.
    eligible_matchers_by_initial: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for normalized_alias, group in alias_groups.items():
        for alias in sorted(group["eligible_surfaces"]):
            parts = re.split(r"\s+", alias)
            pattern = re.compile(
                r"\s*".join(re.escape(part) for part in parts),
                re.IGNORECASE,
            )
            initial = alias[0].casefold()
            eligible_matchers_by_initial[initial].append(
                {
                    "normalized_alias": normalized_alias,
                    "pattern": pattern,
                    "cas_numbers": group["cas_numbers"],
                }
            )

    return {
        "version": RUNTIME_INDEX_VERSION,
        "cas_rows": dict(cas_rows),
        "exact_aliases": dict(exact_aliases),
        "alias_groups": alias_groups,
        "eligible_matchers_by_initial": dict(eligible_matchers_by_initial),
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


_KOREAN_POSTPOSITIONS = (
    "에게서",
    "에서는",
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "처럼",
    "보다",
    "이나",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "과",
    "와",
    "의",
    "에",
    "도",
    "만",
    "로",
)


def _is_alias_token_char(value: str) -> bool:
    if not value:
        return False
    # 아래첨자 숫자·전각 Latin·Greek 문자도 화학식/물질명의 일부가 될 수 있다.
    # ASCII와 완성형 한글만 검사하면 ``CO₂`` 안의 ``CO``를 독립된 별칭으로
    # 오인하므로 Unicode 문자·숫자·결합문자를 모두 토큰 문자로 취급한다.
    return unicodedata.category(value)[0] in {"L", "M", "N"}


def _has_safe_alias_boundaries(
    text: str,
    start: int,
    end: int,
) -> bool:
    if start > 0 and _is_alias_token_char(text[start - 1]):
        return False
    if end >= len(text) or not _is_alias_token_char(text[end]):
        return True
    # ``HCl이``·``chlorine은``처럼 별칭의 문자 종류와 관계없이 한국어 조사는
    # 정상 문장 경계가 될 수 있다. 단, 조사 뒤에 다른 토큰 문자가 계속되면
    # ``CO이산화물`` 같은 내포 표현일 수 있으므로 허용하지 않는다.
    for particle in _KOREAN_POSTPOSITIONS:
        if not text.startswith(particle, end):
            continue
        particle_end = end + len(particle)
        if particle_end >= len(text) or not _is_alias_token_char(text[particle_end]):
            return True
    return False


def find_exact_alias_spans(
    text: str,
    alias: str,
    *,
    allowed_context_suffixes: tuple[str, ...] = (),
) -> list[tuple[int, int, str]]:
    """문장 안에서 독립된 정확 별칭의 원문 span만 반환한다.

    한국어 조사 뒤에 공백이나 문장부호가 오는 경우는 허용한다. 반면
    ``염산염`` 안의 ``염산``이나 ``톨루엔느`` 안의 ``톨루엔``처럼 다른
    한글·영숫자에 붙은 부분 문자열은 정확 일치로 승격하지 않는다.
    """

    value = str(alias or "").strip()
    if not text or len(value) < 2:
        return []
    candidates: list[tuple[int, int]]
    if any(character.isspace() for character in value):
        # 원천별 띄어쓰기 차이를 허용해야 하는 소수 별칭에만 정규식을 사용한다.
        alias_pattern = r"\s*".join(re.escape(part) for part in re.split(r"\s+", value))
        candidates = [
            (match.start(), match.end())
            for match in re.finditer(alias_pattern, text, re.IGNORECASE)
        ]
    else:
        # 대다수 별칭은 문자열 인덱스로 찾는다. casefold가 원문 길이를 바꾸는
        # 드문 Unicode 표현만 정규식 fallback을 사용해 원문 span을 보존한다.
        folded_text = text.casefold()
        folded_alias = value.casefold()
        if len(folded_text) == len(text) and len(folded_alias) == len(value):
            candidates = []
            start = folded_text.find(folded_alias)
            while start >= 0:
                candidates.append((start, start + len(value)))
                start = folded_text.find(folded_alias, start + 1)
        else:
            candidates = [
                (match.start(), match.end())
                for match in re.finditer(re.escape(value), text, re.IGNORECASE)
            ]
    safe: list[tuple[int, int, str]] = []
    for start, end in candidates:
        if _has_safe_alias_boundaries(text, start, end):
            safe.append((start, end, text[start:end]))
            continue
        for suffix in allowed_context_suffixes:
            if text.startswith(suffix, end) and _has_safe_alias_boundaries(
                text, start, end + len(suffix)
            ):
                safe.append((start, end, text[start:end]))
                break
    return safe


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
                    else (
                        "AUTHORITATIVE_ALIAS"
                        if alias_class == "AUTHORITATIVE_NAME"
                        else "REPORTED_ALIAS"
                    )
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

    runtime_index = _runtime_index(artifact)
    matchers_by_initial: dict[str, list[dict[str, Any]]] = runtime_index[
        "eligible_matchers_by_initial"
    ]

    grouped: dict[tuple[int, int, str], set[str]] = defaultdict(set)
    for start, character in enumerate(query):
        for matcher in matchers_by_initial.get(character.casefold(), []):
            match = matcher["pattern"].match(query, start)
            if not match:
                continue
            end = match.end()
            if not _has_safe_alias_boundaries(query, start, end):
                continue
            grouped[(start, end, matcher["normalized_alias"])].update(
                matcher["cas_numbers"]
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


_SAFETY_EVALUATION_BEHAVIORS = {
    "ALLOW_EXACT_HINT",
    "WITHHOLD_AUTO_HINT",
    "PRESERVE_AMBIGUITY",
}


def evaluate_resolver_hint_safety(
    model_path: Path,
    evaluation_path: Path,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """문장 내 자동 CAS 힌트의 안전 회귀셋을 별도로 평가한다.

    이 평가는 후보 검색 정확도와 분리한다. 특히 부분 문자열·복합 표현에서
    잘못된 CAS로 공식 근거 검색을 제한하지 않는지를 잠금 테스트한다.
    """

    artifact = load_resolver(model_path)
    with evaluation_path.open(encoding="utf-8-sig", newline="") as handle:
        cases = list(csv.DictReader(handle))
    required_fields = {
        "case_id",
        "query",
        "query_type",
        "expected_behavior",
        "expected_cas",
        "review_status",
        "source_type",
        "source_reference",
        "split",
        "duplicate_group",
    }
    if not cases:
        raise ValueError("Resolver 안전 평가 데이터가 비어 있습니다.")
    missing_fields = required_fields - set(cases[0])
    if missing_fields:
        raise ValueError(
            "Resolver 안전 평가 컬럼이 부족합니다: " + ", ".join(sorted(missing_fields))
        )

    rows: list[dict[str, Any]] = []
    seen_case_ids: set[str] = set()
    for case in cases:
        case_id = str(case.get("case_id") or "").strip()
        query = str(case.get("query") or "").strip()
        behavior = str(case.get("expected_behavior") or "").strip().upper()
        if not case_id or case_id in seen_case_ids:
            raise ValueError(f"비어 있거나 중복된 case_id입니다: {case_id!r}")
        if not query:
            raise ValueError(f"{case_id}: query가 비어 있습니다.")
        if behavior not in _SAFETY_EVALUATION_BEHAVIORS:
            raise ValueError(f"{case_id}: 지원하지 않는 expected_behavior={behavior}")
        seen_case_ids.add(case_id)

        expected_cas = {
            normalize_cas(value)
            for value in str(case.get("expected_cas") or "").split("|")
            if value.strip()
        }
        invalid_expected = sorted(
            value for value in expected_cas if not valid_cas_checksum(value)
        )
        if invalid_expected:
            raise ValueError(
                f"{case_id}: 유효하지 않은 expected_cas={invalid_expected}"
            )
        if behavior in {"ALLOW_EXACT_HINT", "PRESERVE_AMBIGUITY"} and not expected_cas:
            raise ValueError(f"{case_id}: expected_cas가 필요합니다.")

        started = time.perf_counter()
        resolution = resolve_substance(query, artifact, top_k=20)
        automatic_hint = select_evidence_cas_hint_from_text(query, artifact)
        latency_ms = (time.perf_counter() - started) * 1_000
        candidate_cas = {
            normalize_cas(str(item.get("cas_number") or ""))
            for item in resolution.get("candidates", [])
            if valid_cas_checksum(normalize_cas(str(item.get("cas_number") or "")))
        }
        rule_eligibility_violation = bool(resolution.get("rule_input_eligible")) or any(
            item.get("rule_eligible") is True
            for item in resolution.get("candidates", [])
        )

        if behavior == "ALLOW_EXACT_HINT":
            passed = automatic_hint in expected_cas
        elif behavior == "PRESERVE_AMBIGUITY":
            passed = (
                resolution.get("status") == "AMBIGUOUS_ALIAS"
                and automatic_hint is None
                and candidate_cas == expected_cas
            )
        else:
            passed = automatic_hint is None

        rows.append(
            {
                "case_id": case_id,
                "query": query,
                "query_type": str(case.get("query_type") or ""),
                "expected_behavior": behavior,
                "expected_cas": sorted(expected_cas),
                "resolution_status": resolution.get("status"),
                "candidate_cas": sorted(candidate_cas),
                "automatic_cas_hint": automatic_hint,
                "passed": passed,
                "rule_eligibility_violation": rule_eligibility_violation,
                "latency_ms": round(latency_ms, 6),
                "review_status": str(case.get("review_status") or ""),
                "source_type": str(case.get("source_type") or ""),
                "source_reference": str(case.get("source_reference") or ""),
                "split": str(case.get("split") or ""),
                "duplicate_group": str(case.get("duplicate_group") or ""),
            }
        )

    disallowed_rows = [
        row
        for row in rows
        if row["expected_behavior"] in {"WITHHOLD_AUTO_HINT", "PRESERVE_AMBIGUITY"}
    ]
    allowed_rows = [
        row for row in rows if row["expected_behavior"] == "ALLOW_EXACT_HINT"
    ]
    ambiguous_rows = [
        row for row in rows if row["expected_behavior"] == "PRESERVE_AMBIGUITY"
    ]
    behavior_counts = Counter(row["expected_behavior"] for row in rows)
    missing_behaviors = _SAFETY_EVALUATION_BEHAVIORS - set(behavior_counts)
    if missing_behaviors:
        raise ValueError(
            "Resolver 안전 평가에 필수 동작이 없습니다: "
            + ", ".join(sorted(missing_behaviors))
        )
    latencies = [row["latency_ms"] for row in rows]
    summary = {
        "metrics_version": "resolver-hint-safety-v1",
        "dataset": str(evaluation_path),
        "dataset_sha256": sha256_file(evaluation_path),
        "model": str(model_path),
        "model_sha256": sha256_file(model_path),
        "model_schema_version": artifact.get("schema_version"),
        "dataset_status": (
            "합성·내부 안전 회귀셋이며 현장 정확도나 전국 물질 성능 주장 금지"
        ),
        "case_count": len(rows),
        "passed_count": sum(row["passed"] for row in rows),
        "safety_pass_rate": float(np.mean([row["passed"] for row in rows])),
        "unsafe_auto_hint_count": sum(
            row["automatic_cas_hint"] is not None for row in disallowed_rows
        ),
        "wrong_cas_auto_hint_count": sum(
            row["automatic_cas_hint"] is not None
            and row["automatic_cas_hint"] not in row["expected_cas"]
            for row in allowed_rows
        ),
        "missing_expected_hint_count": sum(
            row["automatic_cas_hint"] is None for row in allowed_rows
        ),
        "resolver_rule_eligibility_violation_count": sum(
            row["rule_eligibility_violation"] for row in rows
        ),
        "ambiguous_preservation_rate": (
            float(np.mean([row["passed"] for row in ambiguous_rows]))
            if ambiguous_rows
            else None
        ),
        "latency_ms": {
            "mean": round(float(np.mean(latencies)), 6),
            "p95": round(float(np.percentile(latencies, 95)), 6),
        },
        "query_type_counts": dict(
            sorted(Counter(row["query_type"] for row in rows).items())
        ),
        "split_counts": dict(sorted(Counter(row["split"] for row in rows).items())),
        "review_status_counts": dict(
            sorted(Counter(row["review_status"] for row in rows).items())
        ),
        "metric_notice": (
            "unsafe_auto_hint_count와 resolver_rule_eligibility_violation_count는 "
            "0이어야 합니다. 후자는 Resolver 계약만 검사하며 실제 pipeline의 Rule "
            "미실행은 별도 통합 테스트로 검증합니다. 지연시간은 실행 장비의 개발용 "
            "측정치입니다."
        ),
        "rows": rows,
    }
    summary["deployment_gate"] = {
        "passed": bool(
            summary["safety_pass_rate"] == 1.0
            and summary["unsafe_auto_hint_count"] == 0
            and summary["wrong_cas_auto_hint_count"] == 0
            and summary["resolver_rule_eligibility_violation_count"] == 0
            and summary["ambiguous_preservation_rate"] == 1.0
        ),
        "required": {
            "safety_pass_rate": 1.0,
            "unsafe_auto_hint_count": 0,
            "wrong_cas_auto_hint_count": 0,
            "resolver_rule_eligibility_violation_count": 0,
            "ambiguous_preservation_rate": 1.0,
        },
    }
    if report_path:
        write_json(report_path, summary)
    return summary
