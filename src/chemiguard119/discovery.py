"""관찰 정보에서 물질 후보와 공식 근거 카드를 함께 찾는다.

정확한 CAS·별칭은 Resolver로 찾고, 색상·냄새·상태·용도 같은 관찰 표현은
소방청 공개 물성 프로필 FTS5 인덱스로 찾는다. 어떤 경로의 결과도 현장 확인
전에는 물질 확정이나 Rule Engine 입력으로 승격하지 않는다.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from chemiguard119.database import connect_readonly
from chemiguard119.resolver import resolve_substance
from chemiguard119.retrieval import search_evidence


DISCOVERY_METHOD = (
    "exact CAS/alias Resolver + NFA Ulsan property-profile FTS5 BM25 "
    "+ KOSHA/CAMEO evidence retrieval"
)
PROPERTY_SOURCE_ID = "NFA_ULSAN_CHEMICAL_INFORMATION"
PROPERTY_SOURCE_URL = "https://www.data.go.kr/data/15081005/fileData.do"
_DIRECT_RESOLUTION_STATUSES = {
    "EXACT_IDENTIFIER_MATCH",
    "EXACT_ALIAS_CANDIDATE",
    "AMBIGUOUS_ALIAS",
}
_PROPERTY_FIELDS = (
    ("physical_state", "상온 상태"),
    ("color", "색상"),
    ("odor", "냄새"),
    ("use_description", "용도"),
)
_KOREAN_SUFFIXES = (
    "으로",
    "처럼",
    "하고",
    "이며",
    "이고",
    "에서",
    "에는",
    "가",
    "이",
    "은",
    "는",
    "을",
    "를",
    "의",
    "와",
    "과",
    "도",
)
_PROPERTY_STOPWORDS = {
    "그리고",
    "고체",
    "기체",
    "나는",
    "냄새",
    "또는",
    "물질",
    "보이는",
    "분말",
    "상태",
    "색상",
    "액체",
    "용도",
    "이나",
    "이며",
    "있고",
    "있음",
    "혹은",
    "화학물질",
}


def _query_tokens(query: str) -> list[str]:
    unique_tokens: list[str] = []
    for raw_token in re.findall(r"[0-9A-Za-z가-힣\-]+", query.lower()):
        normalized = raw_token.strip().replace('"', "")
        for suffix in _KOREAN_SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 2:
                normalized = normalized[: -len(suffix)]
                break
        if (
            len(normalized) < 2
            or normalized in _PROPERTY_STOPWORDS
            or normalized in unique_tokens
        ):
            continue
        unique_tokens.append(normalized)
    return unique_tokens[:24]


def _fts_query(query: str) -> str:
    tokens = _query_tokens(query)
    if not tokens:
        return ""
    token_query = " OR ".join(f'"{token}"' for token in tokens)
    return f"{{physical_state color odor use_description}} : ({token_query})"


def _property_candidates(
    query: str,
    db_path: Path,
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    if len(_query_tokens(query)) < 2:
        return [], True
    match = _fts_query(query)
    if not match:
        return [], True
    try:
        with connect_readonly(db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        profile.cas_number,
                        profile.canonical_name_ko,
                        profile.canonical_name_en,
                        profile.physical_state,
                        profile.color,
                        profile.odor,
                        profile.use_description,
                        profile.source_url,
                        profile.document_version,
                        bm25(
                            substance_profile_fts,
                            0.0, 0.0, 0.0, 2.0, 2.5, 2.5, 1.0
                        ) AS bm25_score
                    FROM substance_profile_fts
                    JOIN substance_profile AS profile
                      ON profile.cas_number = substance_profile_fts.cas_number
                    WHERE substance_profile_fts MATCH ?
                    ORDER BY bm25_score, profile.cas_number
                    LIMIT ?
                    """,
                    (match, limit),
                )
            ]
    except sqlite3.OperationalError as error:
        if "no such table: substance_profile" in str(error).lower():
            return [], False
        raise
    reliable_rows = [
        row
        for row in rows
        if len(_matched_properties(query, row)) >= 2
        and len(_matched_property_tokens(query, row)) >= 2
    ]
    return reliable_rows, True


def _substance_identity(db_path: Path, cas_number: str) -> dict[str, str]:
    with connect_readonly(db_path) as connection:
        row = connection.execute(
            """
            SELECT canonical_name_ko, canonical_name_en
            FROM substance
            WHERE cas_number = ?
            """,
            (cas_number,),
        ).fetchone()
    return {
        "canonical_name_ko": str(row[0] or "") if row else "",
        "canonical_name_en": str(row[1] or "") if row else "",
    }


def _profile_for_cas(db_path: Path, cas_number: str) -> dict[str, Any]:
    with connect_readonly(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT cas_number, canonical_name_ko, canonical_name_en,
                   physical_state, color, odor, use_description,
                   source_url, document_version
            FROM substance_profile
            WHERE cas_number = ?
            """,
            (cas_number,),
        ).fetchone()
    return dict(row) if row else {}


def _matched_properties(query: str, profile: dict[str, Any]) -> list[dict[str, str]]:
    query_tokens = _query_tokens(query)
    matches: list[dict[str, str]] = []
    for field, label in _PROPERTY_FIELDS:
        value = str(profile.get(field) or "").strip()
        compact_value = re.sub(r"\s+", "", value.lower())
        if value and any(
            token in value.lower() or re.sub(r"\s+", "", token) in compact_value
            for token in query_tokens
        ):
            matches.append({"field": field, "label": label, "value": value})
    return matches


def _matched_property_tokens(query: str, profile: dict[str, Any]) -> set[str]:
    matched_tokens: set[str] = set()
    for field, _label in _PROPERTY_FIELDS:
        value = str(profile.get(field) or "").strip().lower()
        compact_value = re.sub(r"\s+", "", value)
        for token in _query_tokens(query):
            compact_token = re.sub(r"\s+", "", token)
            if value and (token in value or compact_token in compact_value):
                matched_tokens.add(token)
    return matched_tokens


def _evidence_cards(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": str(item.get("evidence_id") or ""),
            "cas_number": str(item.get("cas_number") or ""),
            "source": str(item.get("source") or ""),
            "title": str(item.get("title") or ""),
            "body_preview": str(item.get("body_preview") or ""),
            "source_url": str(item.get("source_url") or ""),
            "document_version": str(item.get("document_version") or ""),
            "cas_link_status": (
                str(item.get("cas_link_status"))
                if item.get("cas_link_status") is not None
                else None
            ),
        }
        for item in results
    ]


def discover_substances(
    query: str,
    *,
    db_path: Path,
    resolver_artifact: dict[str, Any],
    retriever_artifact: dict[str, Any],
    top_k: int = 5,
    evidence_top_k: int = 3,
) -> dict[str, Any]:
    """정확 식별과 물성 검색을 합쳐 확인이 필요한 물질 후보를 반환한다."""

    resolution = resolve_substance(query, resolver_artifact, top_k=top_k)
    direct_candidates = (
        list(resolution.get("candidates") or [])
        if resolution.get("status") in _DIRECT_RESOLUTION_STATUSES
        else []
    )

    property_rows, profile_index_available = _property_candidates(
        query,
        db_path,
        limit=max(10, top_k * 4),
    )
    property_by_cas = {
        str(row["cas_number"]): row for row in property_rows if row.get("cas_number")
    }

    ordered_cas: list[str] = []
    direct_by_cas: dict[str, dict[str, Any]] = {}
    for candidate in direct_candidates:
        cas_number = str(candidate.get("cas_number") or "")
        if not cas_number:
            continue
        direct_by_cas.setdefault(cas_number, candidate)
        if cas_number not in ordered_cas:
            ordered_cas.append(cas_number)
    for row in property_rows:
        cas_number = str(row.get("cas_number") or "")
        if cas_number and cas_number not in ordered_cas:
            ordered_cas.append(cas_number)
    ordered_cas = ordered_cas[:top_k]

    candidates: list[dict[str, Any]] = []
    for rank, cas_number in enumerate(ordered_cas, 1):
        direct = direct_by_cas.get(cas_number)
        profile = property_by_cas.get(cas_number, {})
        if direct and profile_index_available and not profile:
            profile = _profile_for_cas(db_path, cas_number)
        identity = _substance_identity(db_path, cas_number)
        display_name = (
            str(profile.get("canonical_name_ko") or "")
            or identity["canonical_name_ko"]
            or (str(direct.get("matched_alias") or "") if direct else "")
            or str(profile.get("canonical_name_en") or "")
            or identity["canonical_name_en"]
            or cas_number
        )
        evidence_result = search_evidence(
            f"{display_name} {query} 누출 대응 반응성 보호구",
            db_path,
            retriever_artifact,
            cas_hint=cas_number,
            top_k=evidence_top_k,
        )
        has_direct = direct is not None
        has_profile = bool(profile)
        match_basis = (
            "IDENTITY_AND_PUBLIC_PROPERTY_PROFILE"
            if has_direct and has_profile
            else ("IDENTITY_EXPRESSION" if has_direct else "PUBLIC_PROPERTY_PROFILE")
        )
        candidates.append(
            {
                "rank": rank,
                "cas_number": cas_number,
                "display_name": display_name,
                "match_basis": match_basis,
                "matched_expression": (
                    str(direct.get("matched_alias") or "") if direct else None
                ),
                "matched_properties": _matched_properties(query, profile),
                "property_profile": (
                    {
                        "physical_state": str(profile.get("physical_state") or ""),
                        "color": str(profile.get("color") or ""),
                        "odor": str(profile.get("odor") or ""),
                        "use_description": str(profile.get("use_description") or ""),
                        "source_id": PROPERTY_SOURCE_ID,
                        "source_url": str(
                            profile.get("source_url") or PROPERTY_SOURCE_URL
                        ),
                        "document_version": str(profile.get("document_version") or ""),
                    }
                    if has_profile
                    else None
                ),
                "evidence_status": str(
                    evidence_result.get("status") or "NO_EVIDENCE_FOUND"
                ),
                "evidence_warning": (
                    str(evidence_result.get("warning"))
                    if evidence_result.get("warning") is not None
                    else None
                ),
                "evidence_notice": (
                    str(evidence_result.get("notice"))
                    if evidence_result.get("notice") is not None
                    else None
                ),
                "cas_link_warning": (
                    str(evidence_result.get("cas_link_warning"))
                    if evidence_result.get("cas_link_warning") is not None
                    else None
                ),
                "evidence": _evidence_cards(list(evidence_result.get("results") or [])),
                "requires_responder_confirmation": True,
                "rule_eligible": False,
                "risk_determination_allowed": False,
            }
        )

    status = (
        "CANDIDATES_FOUND"
        if candidates
        else (
            "NO_RELIABLE_CANDIDATE"
            if profile_index_available
            else "PROFILE_INDEX_NOT_AVAILABLE"
        )
    )
    search_mode = (
        "IDENTITY_AND_PROPERTY_RETRIEVAL"
        if direct_candidates and property_rows
        else (
            "IDENTITY_RETRIEVAL"
            if direct_candidates
            else ("PROPERTY_PROFILE_RETRIEVAL" if property_rows else "ABSTAINED")
        )
    )
    notice = (
        "색상·냄새·상태는 여러 물질이 공유하므로 후보를 하나로 확정하지 않습니다. "
        "용기 라벨·현장 MSDS 등으로 CAS와 현장 존재를 확인해야 합니다."
        if candidates
        else (
            "현재 적재된 공개 성상 프로필에서 신뢰할 후보를 찾지 못했습니다. "
            "해당 물질이 없거나 안전하다는 뜻이 아니므로 관찰 정보를 보강하고 외부 공식 "
            "MSDS를 확인해야 합니다."
            if profile_index_available
            else (
                "물질 성상 프로필 인덱스가 준비되지 않았습니다. 후보 없음이나 안전으로 "
                "해석하지 말고 외부 공식 MSDS를 확인해야 합니다."
            )
        )
    )
    return {
        "query": query,
        "status": status,
        "search_mode": search_mode,
        "method": DISCOVERY_METHOD,
        "profile_index_available": profile_index_available,
        "candidates": candidates,
        "requires_responder_confirmation": True,
        "rule_eligible": False,
        "risk_determination_allowed": False,
        "candidate_score_is_probability": False,
        "notice": notice,
    }


__all__ = ["DISCOVERY_METHOD", "discover_substances"]
