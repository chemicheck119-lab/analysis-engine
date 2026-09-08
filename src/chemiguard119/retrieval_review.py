"""Retriever section qrel 후보 생성과 독립 검수 도구.

기계적으로 만든 질문과 현재 Retriever의 pool을 정답으로 취급하지 않는다.
후보 생성, 두 사람의 독립 라벨링, 완전 일치 병합을 분리하고 검수 전에는
어떤 성능 수치도 만들지 않는다.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from chemiguard119.database import connect_readonly
from chemiguard119.evaluation_contract import (
    EvaluationProfile,
    evaluate_dataset_contract,
    load_evaluation_rows,
)
from chemiguard119.retrieval import load_retriever, search_evidence
from chemiguard119.utils import sha256_file, valid_cas_checksum, write_json


CANDIDATE_SCHEMA_VERSION = "chemicheck119-retriever-qrel-candidate-v1"
REVIEW_SHEET_SCHEMA_VERSION = "chemicheck119-retriever-qrel-review-sheet-v1"
MERGE_SCHEMA_VERSION = "chemicheck119-retriever-qrel-review-merge-v1"
REVIEW_AUDIT_SCHEMA_VERSION = "chemicheck119-retriever-qrel-review-audit-v1"
POOL_RUN_SCHEMA_VERSION = "chemicheck119-retriever-pool-run-v1"
POOL_AUDIT_SCHEMA_VERSION = "chemicheck119-retriever-pool-audit-v1"
REVIEW_ROLES = frozenset({"LABELER", "REVIEWER"})
ACTOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]{2,64}$")
KOSHA_API_REFERENCE = "https://www.data.go.kr/data/15157612/openapi.do"
TARGET_CANDIDATE_COUNT_MIN = 100
TARGET_CANDIDATE_COUNT_MAX = 200


# target_sections는 gold label이 아니라 qrel 누락을 줄이기 위한 pool 확장 힌트다.
# 검수 시트에는 노출하지 않고, 최종 relevance는 사람이 원문을 읽고 결정한다.
QUERY_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "template_id": "PPE_LEAK",
        "intent": "PPE",
        "query": "{name} 누출 현장에 필요한 개인보호구는 무엇인가요?",
        "target_sections": (6, 8),
    },
    {
        "template_id": "RESPIRATORY_PROTECTION",
        "intent": "PPE",
        "query": "{name} 노출을 막기 위한 호흡기 보호구는 무엇인가요?",
        "target_sections": (8,),
    },
    {
        "template_id": "SPILL_RESPONSE",
        "intent": "SPILL_RESPONSE",
        "query": "{name}이 누출됐을 때 처음 취해야 할 조치는 무엇인가요?",
        "target_sections": (6,),
    },
    {
        "template_id": "CLEANUP_METHOD",
        "intent": "SPILL_RESPONSE",
        "query": "누출된 {name}을 안전하게 정화하거나 제거하는 방법은 무엇인가요?",
        "target_sections": (6,),
    },
    {
        "template_id": "FIRE_EXTINGUISHING",
        "intent": "FIRE_RESPONSE",
        "query": "{name} 화재에 적절한 소화 방법과 소화제는 무엇인가요?",
        "target_sections": (5,),
    },
    {
        "template_id": "FIRE_HAZARDS",
        "intent": "FIRE_RESPONSE",
        "query": "{name} 화재 때 특별히 주의할 위험은 무엇인가요?",
        "target_sections": (5,),
    },
    {
        "template_id": "FIRST_AID_INHALATION",
        "intent": "FIRST_AID",
        "query": "{name}을 흡입했을 때 응급조치는 무엇인가요?",
        "target_sections": (4,),
    },
    {
        "template_id": "FIRST_AID_SKIN",
        "intent": "FIRST_AID",
        "query": "{name}이 피부에 닿았을 때 응급조치는 무엇인가요?",
        "target_sections": (4,),
    },
    {
        "template_id": "FIRST_AID_EYE",
        "intent": "FIRST_AID",
        "query": "{name}이 눈에 들어갔을 때 응급조치는 무엇인가요?",
        "target_sections": (4,),
    },
    {
        "template_id": "HANDLING",
        "intent": "STORAGE_HANDLING",
        "query": "{name}을 안전하게 취급할 때 주의사항은 무엇인가요?",
        "target_sections": (7,),
    },
    {
        "template_id": "STORAGE",
        "intent": "STORAGE_HANDLING",
        "query": "{name}의 안전한 저장 조건은 무엇인가요?",
        "target_sections": (7,),
    },
    {
        "template_id": "STABILITY",
        "intent": "STABILITY_REACTIVITY",
        "query": "{name}의 안정성과 피해야 할 조건은 무엇인가요?",
        "target_sections": (10,),
    },
    {
        "template_id": "INCOMPATIBILITY",
        "intent": "STABILITY_REACTIVITY",
        "query": "{name}과 함께 두면 안 되는 물질은 무엇인가요?",
        "target_sections": (10,),
    },
    {
        "template_id": "IDENTIFICATION",
        "intent": "IDENTIFICATION",
        "query": "{name}의 제품 식별 정보와 성분 정보는 무엇인가요?",
        "target_sections": (1, 3),
    },
    {
        "template_id": "PHYSICAL_PROPERTIES",
        "intent": "IDENTIFICATION",
        "query": "{name}의 색상, 냄새, 물리적 상태는 무엇인가요?",
        "target_sections": (9,),
    },
    {
        "template_id": "CURRENT_INVENTORY",
        "intent": "UNANSWERABLE",
        "query": "현재 사고 현장에 {name}이 몇 리터 저장되어 있나요?",
        "target_sections": (),
    },
    {
        "template_id": "CURRENT_LEAK_RATE",
        "intent": "UNANSWERABLE",
        "query": "현재 사고 현장에서 {name}이 분당 몇 리터씩 누출되고 있나요?",
        "target_sections": (),
    },
    {
        "template_id": "CURRENT_WIND",
        "intent": "UNANSWERABLE",
        "query": "현재 {name} 사고 현장의 풍향과 풍속은 얼마인가요?",
        "target_sections": (),
    },
    {
        "template_id": "CURRENT_EXPOSED_PEOPLE",
        "intent": "UNANSWERABLE",
        "query": "현재 {name} 사고로 실제 노출된 사람은 몇 명인가요?",
        "target_sections": (),
    },
)

REVIEW_COLUMNS = (
    "sheet_schema_version",
    "case_id",
    "actor_role",
    "actor_id",
    "query",
    "intent",
    "cas_number",
    "evidence_id",
    "source",
    "source_url",
    "document_version",
    "title",
    "body",
    "body_sha256",
    "review_decision",
    "answerable",
    "relevance_grade",
    "required_fact_ids_json",
    "supporting_sentence",
    "review_notes",
)

REQUIRED_REVIEW_INPUT_COLUMNS = (
    "review_decision",
    "answerable",
    "relevance_grade",
    "required_fact_ids_json",
)
REVIEW_JUDGMENT_COLUMNS = REQUIRED_REVIEW_INPUT_COLUMNS + (
    "supporting_sentence",
    "review_notes",
)

Search = Callable[..., dict[str, Any]]


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    if path.exists():
        raise FileExistsError(f"기존 파일을 덮어쓰지 않습니다: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(_json_compact(dict(row)) + "\n" for row in rows),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _body_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_http_url(value: object) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _section_number(title: str) -> int | None:
    match = re.search(r"\bMSDS\s+(\d{1,2})장\b", title)
    return int(match.group(1)) if match else None


def _load_kosha_materials(db_path: Path) -> list[dict[str, str]]:
    with connect_readonly(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT DISTINCT s.cas_number, s.canonical_name_ko
                FROM substance AS s
                JOIN evidence AS e ON e.cas_number = s.cas_number
                WHERE e.source = 'KOSHA'
                  AND s.has_kosha_detail = 1
                  AND TRIM(e.body) <> ''
                ORDER BY s.cas_number
                """
            )
        ]
    return [
        {
            "cas_number": str(row["cas_number"]),
            "canonical_name_ko": str(row["canonical_name_ko"]),
        }
        for row in rows
    ]


def _load_official_evidence_by_cas(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    with connect_readonly(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT evidence_id, source, cas_number, title, body,
                       source_url, document_version
                FROM evidence
                WHERE source IN ('KOSHA', 'CAMEO')
                  AND cas_number IS NOT NULL
                  AND TRIM(body) <> ''
                ORDER BY evidence_id
                """
            )
        ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["cas_number"]), []).append(row)
    return grouped


def _candidate_evidence(
    row: Mapping[str, Any], pool_sources: set[str]
) -> dict[str, Any]:
    body = str(row.get("body") or "")
    return {
        "evidence_id": str(row["evidence_id"]),
        "cas_number": str(row.get("cas_number") or ""),
        "source": str(row.get("source") or ""),
        "source_url": str(row.get("source_url") or ""),
        "document_version": str(row.get("document_version") or ""),
        "title": str(row.get("title") or ""),
        "body": body,
        "body_sha256": _body_sha256(body),
        "pool_sources": sorted(pool_sources),
    }


def validate_candidate_rows(rows: list[Mapping[str, Any]]) -> None:
    if not rows:
        raise ValueError("Retriever qrel 후보가 비어 있습니다.")
    seen_cases: set[str] = set()
    for index, raw_row in enumerate(rows, 1):
        row = dict(raw_row)
        case_id = str(row.get("case_id") or f"<row:{index}>").strip()
        if not case_id or case_id in seen_cases:
            raise ValueError(f"비어 있거나 중복된 case_id={case_id!r}")
        seen_cases.add(case_id)
        if row.get("candidate_schema_version") != CANDIDATE_SCHEMA_VERSION:
            raise ValueError(f"{case_id}: 지원하지 않는 candidate schema")
        forbidden = {"answerable", "qrels", "relevance_grade", "required_fact_ids"}
        leaked = sorted(forbidden.intersection(row))
        if leaked:
            raise ValueError(f"{case_id}: 검수 전 정답 필드가 포함됐습니다: {leaked}")
        for field in (
            "query",
            "intent",
            "template_id",
            "source_type",
            "source_reference",
            "duplicate_group",
            "database_sha256",
            "retriever_sha256",
        ):
            if not isinstance(row.get(field), str) or not str(row[field]).strip():
                raise ValueError(f"{case_id}: {field}가 필요합니다.")
        for field in ("database_sha256", "retriever_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(row[field])):
                raise ValueError(f"{case_id}: {field} 형식이 올바르지 않습니다.")
        cas_number = str(row.get("cas_number") or "").strip()
        if not valid_cas_checksum(cas_number):
            raise ValueError(f"{case_id}: 유효하지 않은 CAS={cas_number!r}")
        evidence_candidates = row.get("evidence_candidates")
        if not isinstance(evidence_candidates, list) or not evidence_candidates:
            raise ValueError(f"{case_id}: evidence_candidates가 필요합니다.")
        seen_evidence: set[str] = set()
        for evidence in evidence_candidates:
            if not isinstance(evidence, Mapping):
                raise ValueError(f"{case_id}: evidence 후보는 객체여야 합니다.")
            evidence_id = str(evidence.get("evidence_id") or "").strip()
            if not evidence_id or evidence_id in seen_evidence:
                raise ValueError(
                    f"{case_id}: 비어 있거나 중복된 evidence_id={evidence_id!r}"
                )
            seen_evidence.add(evidence_id)
            if str(evidence.get("cas_number") or "").strip() != cas_number:
                raise ValueError(f"{case_id}: 다른 CAS evidence가 pool에 포함됐습니다.")
            if str(evidence.get("source") or "") not in {"KOSHA", "CAMEO"}:
                raise ValueError(f"{case_id}:{evidence_id}: 공식 출처가 아닙니다.")
            if not _valid_http_url(evidence.get("source_url")):
                raise ValueError(
                    f"{case_id}:{evidence_id}: 공식 source URL이 필요합니다."
                )
            if not str(evidence.get("document_version") or "").strip():
                raise ValueError(f"{case_id}:{evidence_id}: 문서 버전이 필요합니다.")
            if not str(evidence.get("title") or "").strip():
                raise ValueError(f"{case_id}:{evidence_id}: 근거 제목이 필요합니다.")
            body = str(evidence.get("body") or "")
            if not body.strip() or _body_sha256(body) != evidence.get("body_sha256"):
                raise ValueError(
                    f"{case_id}:{evidence_id}: body hash가 일치하지 않습니다."
                )


def load_candidate_rows(candidate_path: Path) -> list[dict[str, Any]]:
    rows = load_evaluation_rows(Path(candidate_path))
    validate_candidate_rows(rows)
    return rows


def generate_qrel_candidate_pool(
    db_path: Path,
    retriever_model_path: Path,
    output_path: Path | None = None,
    *,
    top_k: int = 12,
    max_substances: int | None = None,
    retriever_artifact: dict[str, Any] | None = None,
    searcher: Search | None = None,
) -> dict[str, Any]:
    """KOSHA 물질별 19개 질문과 검수용 evidence pool을 생성한다."""

    db_path = Path(db_path)
    retriever_model_path = Path(retriever_model_path)
    if not 1 <= top_k <= 50:
        raise ValueError("top_k는 1~50이어야 합니다.")
    materials = _load_kosha_materials(db_path)
    if max_substances is not None:
        if max_substances <= 0:
            raise ValueError("max_substances는 1 이상이어야 합니다.")
        materials = materials[:max_substances]
    if not materials:
        raise ValueError("KOSHA 상세가 적재된 물질이 없습니다.")
    artifact = (
        retriever_artifact
        if retriever_artifact is not None
        else load_retriever(retriever_model_path)
    )
    search = searcher or search_evidence
    evidence_by_cas = _load_official_evidence_by_cas(db_path)
    database_hash = sha256_file(db_path)
    retriever_hash = sha256_file(retriever_model_path)
    created_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []

    for material in materials:
        cas_number = material["cas_number"]
        name = material["canonical_name_ko"]
        cas_evidence = evidence_by_cas.get(cas_number, [])
        evidence_index = {str(row["evidence_id"]): row for row in cas_evidence}
        for template in QUERY_TEMPLATES:
            query = str(template["query"]).format(name=name)
            retrieval = search(
                query,
                db_path,
                artifact,
                cas_hint=cas_number,
                top_k=top_k,
                candidate_limit=max(40, top_k),
            )
            selected: dict[str, set[str]] = {}
            for result in retrieval.get("results") or []:
                evidence_id = str(result.get("evidence_id") or "")
                if evidence_id in evidence_index:
                    selected.setdefault(evidence_id, set()).add(
                        "CURRENT_RETRIEVER_TOP_K"
                    )
            target_sections = set(template["target_sections"])
            for evidence in cas_evidence:
                section_number = _section_number(str(evidence.get("title") or ""))
                if section_number in target_sections:
                    selected.setdefault(str(evidence["evidence_id"]), set()).add(
                        "MECHANICAL_SECTION_POOL"
                    )
            # 정상 기권한 답변 불가 질의도 최소한의 같은-CAS negative control을
            # 사람이 직접 0으로 확인해야 evaluator용 qrel 배열을 만들 수 있다.
            if not selected and template["intent"] == "UNANSWERABLE":
                for evidence in cas_evidence[:top_k]:
                    selected.setdefault(str(evidence["evidence_id"]), set()).add(
                        "NO_RESULT_NEGATIVE_CONTROL_POOL"
                    )
            if not selected:
                raise ValueError(
                    f"{cas_number}:{template['template_id']}: evidence pool이 비었습니다."
                )
            evidence_candidates = [
                _candidate_evidence(evidence_index[evidence_id], selected[evidence_id])
                for evidence_id in sorted(selected)
            ]
            safe_cas = cas_number.replace("-", "")
            rows.append(
                {
                    "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
                    "case_id": f"RET-QREL-{safe_cas}-{template['template_id']}",
                    "query": query,
                    "intent": template["intent"],
                    "template_id": template["template_id"],
                    "cas_number": cas_number,
                    "source_type": "MECHANICAL_KOSHA_QUERY_TEMPLATE",
                    "source_reference": KOSHA_API_REFERENCE,
                    "scenario_origin": "SYNTHETIC_QUERY_FOR_HUMAN_REVIEW",
                    "data_use_scope": "QREL_REVIEW_CANDIDATE_ONLY",
                    "duplicate_group": f"{cas_number}:{template['intent']}",
                    "created_at": created_at,
                    "database_sha256": database_hash,
                    "retriever_sha256": retriever_hash,
                    "pool_strategy": (
                        "current retriever top-k union mechanical SDS section pool; "
                        "pool membership is not a relevance label"
                    ),
                    "evidence_candidates": evidence_candidates,
                }
            )

    validate_candidate_rows(rows)
    if output_path is not None:
        _write_jsonl(Path(output_path), rows)
    count = len(rows)
    pool_sizes = [len(row["evidence_candidates"]) for row in rows]
    source_counts = Counter(
        str(evidence["source"])
        for row in rows
        for evidence in row["evidence_candidates"]
    )
    return {
        "status": "COMPLETED",
        "action": "GENERATE_QREL_CANDIDATE_POOL",
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "candidate_count": count,
        "substance_count": len(materials),
        "query_template_count": len(QUERY_TEMPLATES),
        "intent_counts": dict(sorted(Counter(row["intent"] for row in rows).items())),
        "evidence_judgment_count": sum(pool_sizes),
        "pool_size": {
            "minimum": min(pool_sizes),
            "maximum": max(pool_sizes),
            "average": round(sum(pool_sizes) / len(pool_sizes), 4),
        },
        "evidence_source_counts": dict(sorted(source_counts.items())),
        "target_range": {
            "minimum": TARGET_CANDIDATE_COUNT_MIN,
            "maximum": TARGET_CANDIDATE_COUNT_MAX,
            "within_range": TARGET_CANDIDATE_COUNT_MIN
            <= count
            <= TARGET_CANDIDATE_COUNT_MAX,
        },
        "database_sha256": database_hash,
        "retriever_sha256": retriever_hash,
        "output_path": str(output_path) if output_path is not None else None,
        "claim_scope": "REVIEW_CANDIDATE_ONLY",
        "warning": (
            "기계 생성 질문과 pool이며 정답·검색 성능·현장 성능으로 사용할 수 없습니다."
        ),
    }


def _candidate_context(
    candidate: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, str]:
    return {
        "query": str(candidate["query"]),
        "intent": str(candidate["intent"]),
        "cas_number": str(candidate["cas_number"]),
        "evidence_id": str(evidence["evidence_id"]),
        "source": str(evidence["source"]),
        "source_url": str(evidence["source_url"]),
        "document_version": str(evidence["document_version"]),
        "title": str(evidence["title"]),
        "body": str(evidence["body"]),
        "body_sha256": str(evidence["body_sha256"]),
    }


def export_review_sheet(
    candidate_path: Path,
    output_path: Path,
    *,
    actor_role: str,
    actor_id: str,
) -> dict[str, Any]:
    role = str(actor_role).strip().upper()
    normalized_actor = str(actor_id).strip()
    if role not in REVIEW_ROLES:
        raise ValueError(f"actor_role은 {sorted(REVIEW_ROLES)} 중 하나여야 합니다.")
    if not ACTOR_ID_PATTERN.fullmatch(normalized_actor):
        raise ValueError("actor_id는 2~64자의 영문·숫자·_.:@-만 사용할 수 있습니다.")
    candidates = load_candidate_rows(Path(candidate_path))
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError(f"기존 파일을 덮어쓰지 않습니다: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for candidate in candidates:
            for evidence in candidate["evidence_candidates"]:
                writer.writerow(
                    {
                        "sheet_schema_version": REVIEW_SHEET_SCHEMA_VERSION,
                        "case_id": candidate["case_id"],
                        "actor_role": role,
                        "actor_id": normalized_actor,
                        **_candidate_context(candidate, evidence),
                        "review_decision": "",
                        "answerable": "",
                        "relevance_grade": "",
                        "required_fact_ids_json": "",
                        "supporting_sentence": "",
                        "review_notes": "",
                    }
                )
                row_count += 1
    output_path.chmod(0o600)
    return {
        "status": "COMPLETED",
        "action": "EXPORT_QREL_REVIEW_SHEET",
        "actor_role": role,
        "actor_id": normalized_actor,
        "case_count": len(candidates),
        "evidence_judgment_count": row_count,
        "candidate_sha256": sha256_file(Path(candidate_path)),
        "output_path": str(output_path),
        "instruction": (
            "다른 검수자의 시트를 보지 말고 모든 행의 answerable·relevance·필수 사실·"
            "근거 문장을 작성하세요. pool 포함 여부는 relevance 정답이 아닙니다."
        ),
    }


def _read_review_sheet(
    path: Path,
    expected_role: str,
) -> tuple[str, dict[tuple[str, str], dict[str, str]]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = sorted(set(REVIEW_COLUMNS) - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"{path}: 누락된 검수 열={missing}")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"{path}: 검수 행이 없습니다.")
    actor_ids = {str(row.get("actor_id") or "").strip() for row in rows}
    roles = {str(row.get("actor_role") or "").strip().upper() for row in rows}
    if len(actor_ids) != 1 or not next(iter(actor_ids)):
        raise ValueError(f"{path}: 하나의 actor_id만 허용합니다.")
    actor_id = next(iter(actor_ids))
    if not ACTOR_ID_PATTERN.fullmatch(actor_id):
        raise ValueError(f"{path}: actor_id 형식이 올바르지 않습니다.")
    if roles != {expected_role}:
        raise ValueError(f"{path}: actor_role은 {expected_role}이어야 합니다.")
    indexed: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (
            str(row.get("case_id") or "").strip(),
            str(row.get("evidence_id") or "").strip(),
        )
        if not all(key) or key in indexed:
            raise ValueError(f"{path}: 비어 있거나 중복된 검수 키={key!r}")
        if row.get("sheet_schema_version") != REVIEW_SHEET_SCHEMA_VERSION:
            raise ValueError(f"{path}:{key}: 지원하지 않는 sheet schema")
        indexed[key] = row
    return actor_id, indexed


def _parse_bool(value: str, field: str, case_id: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{case_id}: {field}는 true 또는 false여야 합니다.")


def _parse_review_case(
    candidate: Mapping[str, Any],
    rows: list[Mapping[str, str]],
) -> dict[str, Any]:
    case_id = str(candidate["case_id"])
    if any(
        str(row.get("review_decision") or "").strip().upper() != "APPROVE"
        for row in rows
    ):
        raise ValueError(f"{case_id}: 모든 행에 review_decision=APPROVE가 필요합니다.")
    answerable_values = {
        _parse_bool(str(row.get("answerable") or ""), "answerable", case_id)
        for row in rows
    }
    if len(answerable_values) != 1:
        raise ValueError(f"{case_id}: evidence 행마다 answerable이 다릅니다.")
    answerable = next(iter(answerable_values))
    qrels: list[dict[str, Any]] = []
    evidence_text: dict[str, str] = {}
    high_relevance_count = 0
    positive_count = 0
    for row in rows:
        evidence_id = str(row["evidence_id"])
        grade_text = str(row.get("relevance_grade") or "").strip()
        try:
            grade = int(grade_text)
        except ValueError as error:
            raise ValueError(
                f"{case_id}:{evidence_id}: relevance_grade는 0~3 정수여야 합니다."
            ) from error
        if not 0 <= grade <= 3:
            raise ValueError(
                f"{case_id}:{evidence_id}: relevance_grade는 0~3 정수여야 합니다."
            )
        try:
            fact_ids = json.loads(str(row.get("required_fact_ids_json") or ""))
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{case_id}:{evidence_id}: required_fact_ids_json이 유효하지 않습니다."
            ) from error
        if not isinstance(fact_ids, list) or any(
            not isinstance(value, str) or not value.strip() for value in fact_ids
        ):
            raise ValueError(
                f"{case_id}:{evidence_id}: required_fact_ids_json은 문자열 배열이어야 합니다."
            )
        fact_ids = [value.strip() for value in fact_ids]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError(f"{case_id}:{evidence_id}: required fact가 중복됐습니다.")
        supporting_sentence = str(row.get("supporting_sentence") or "").strip()
        body = str(row.get("body") or "")
        if grade > 0:
            positive_count += 1
            high_relevance_count += int(grade >= 2)
            if not fact_ids or not supporting_sentence:
                raise ValueError(
                    f"{case_id}:{evidence_id}: 관련 근거에는 fact ID와 근거 문장이 필요합니다."
                )
            if supporting_sentence not in body:
                raise ValueError(
                    f"{case_id}:{evidence_id}: 근거 문장이 evidence 원문에 없습니다."
                )
            evidence_text[evidence_id] = supporting_sentence
        elif fact_ids or supporting_sentence:
            raise ValueError(
                f"{case_id}:{evidence_id}: relevance 0에는 fact ID·근거 문장을 둘 수 없습니다."
            )
        qrels.append(
            {
                "evidence_id": evidence_id,
                "relevance_grade": grade,
                "required_fact_ids": fact_ids,
            }
        )
    if answerable and (positive_count == 0 or high_relevance_count == 0):
        raise ValueError(
            f"{case_id}: 답변 가능 질의에는 grade 2 이상의 핵심 근거가 필요합니다."
        )
    if not answerable and positive_count:
        raise ValueError(f"{case_id}: 답변 불가 질의에는 관련 근거를 둘 수 없습니다.")
    return {
        "answerable": answerable,
        "qrels": qrels,
        "supporting_sentences": evidence_text,
    }


def audit_review_sheet(
    candidate_path: Path,
    review_sheet_path: Path,
    *,
    actor_role: str,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """정답을 추론하지 않고 한 사람의 검수 진행률과 무결성만 감사한다."""

    role = str(actor_role).strip().upper()
    if role not in REVIEW_ROLES:
        raise ValueError(f"actor_role은 {sorted(REVIEW_ROLES)} 중 하나여야 합니다.")

    candidate_path = Path(candidate_path)
    review_sheet_path = Path(review_sheet_path)
    candidates = load_candidate_rows(candidate_path)
    expected_keys = {
        (str(candidate["case_id"]), str(evidence["evidence_id"]))
        for candidate in candidates
        for evidence in candidate["evidence_candidates"]
    }
    blockers: list[dict[str, Any]] = []
    try:
        actor_id, review_rows = _read_review_sheet(review_sheet_path, role)
    except ValueError as error:
        actor_id = None
        review_rows = {}
        blockers.append({"code": "REVIEW_SHEET_INVALID", "message": str(error)})

    if review_rows and set(review_rows) != expected_keys:
        blockers.append(
            {
                "code": "REVIEW_CASE_SET_MISMATCH",
                "missing_count": len(expected_keys - set(review_rows)),
                "unexpected_count": len(set(review_rows) - expected_keys),
            }
        )

    changed_context_count = 0
    if review_rows:
        for candidate in candidates:
            for evidence in candidate["evidence_candidates"]:
                key = (str(candidate["case_id"]), str(evidence["evidence_id"]))
                sheet_row = review_rows.get(key)
                if sheet_row is None:
                    continue
                context = _candidate_context(candidate, evidence)
                changed = [
                    field
                    for field, expected in context.items()
                    if str(sheet_row.get(field) or "") != expected
                ]
                if changed:
                    changed_context_count += 1
        if changed_context_count:
            blockers.append(
                {
                    "code": "CANDIDATE_CONTEXT_CHANGED",
                    "evidence_row_count": changed_context_count,
                }
            )

    started_row_count = sum(
        any(str(row.get(field) or "").strip() for field in REVIEW_JUDGMENT_COLUMNS)
        for row in review_rows.values()
    )
    approved_row_count = sum(
        str(row.get("review_decision") or "").strip().upper() == "APPROVE"
        for row in review_rows.values()
    )
    untouched_case_count = 0
    in_progress_case_count = 0
    valid_case_count = 0
    invalid_cases: list[dict[str, str]] = []

    if review_rows and not blockers:
        for candidate in candidates:
            case_id = str(candidate["case_id"])
            case_rows = [
                review_rows[(case_id, str(evidence["evidence_id"]))]
                for evidence in candidate["evidence_candidates"]
            ]
            started = any(
                str(row.get(field) or "").strip()
                for row in case_rows
                for field in REVIEW_JUDGMENT_COLUMNS
            )
            if not started:
                untouched_case_count += 1
                continue
            complete = all(
                str(row.get(field) or "").strip()
                for row in case_rows
                for field in REQUIRED_REVIEW_INPUT_COLUMNS
            )
            if not complete:
                in_progress_case_count += 1
                continue
            try:
                _parse_review_case(candidate, case_rows)
            except ValueError as error:
                invalid_cases.append({"case_id": case_id, "message": str(error)})
            else:
                valid_case_count += 1

    candidate_count = len(candidates)
    if blockers:
        status = "BLOCKED_REVIEW_GATE"
    elif invalid_cases:
        status = "NEEDS_CORRECTION"
    elif valid_case_count == candidate_count:
        status = "READY_FOR_INDEPENDENT_MERGE"
    elif started_row_count:
        status = "IN_PROGRESS"
    else:
        status = "NOT_STARTED"

    report = {
        "schema_version": REVIEW_AUDIT_SCHEMA_VERSION,
        "status": status,
        "action": "AUDIT_QREL_REVIEW_PROGRESS",
        "actor_role": role,
        "actor_id": actor_id,
        "candidate_count": candidate_count,
        "evidence_judgment_count": len(expected_keys),
        "progress": {
            "started_evidence_row_count": started_row_count,
            "approved_evidence_row_count": approved_row_count,
            "valid_completed_case_count": valid_case_count,
            "in_progress_case_count": in_progress_case_count,
            "invalid_case_count": len(invalid_cases),
            "untouched_case_count": untouched_case_count,
            "valid_case_completion_ratio": round(valid_case_count / candidate_count, 6),
        },
        "invalid_cases": invalid_cases,
        "blockers": blockers,
        "candidate_sha256": sha256_file(candidate_path),
        "review_sheet_sha256": sha256_file(review_sheet_path),
        "ready_for_independent_merge": (status == "READY_FOR_INDEPENDENT_MERGE"),
        "is_performance_result": False,
        "claim_limit": (
            "한 사람의 검수 진행률과 파일 무결성 감사이며 Retriever 정확도나 "
            "독립 이중 검수 완료를 뜻하지 않습니다."
        ),
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


def _query_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_pool_run(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: 유효한 JSON이 아닙니다.") from error
    if not isinstance(payload, Mapping):
        raise ValueError(f"{path}: pool run은 JSON 객체여야 합니다.")
    run = dict(payload)
    if run.get("schema_version") != POOL_RUN_SCHEMA_VERSION:
        raise ValueError(f"{path}: 지원하지 않는 pool run schema입니다.")
    system_id = str(run.get("system_id") or "").strip()
    if not ACTOR_ID_PATTERN.fullmatch(system_id):
        raise ValueError(f"{path}: system_id 형식이 올바르지 않습니다.")
    if not str(run.get("system_version") or "").strip():
        raise ValueError(f"{path}: system_version이 필요합니다.")
    for field in ("system_artifact_sha256", "database_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(run.get(field) or "")):
            raise ValueError(f"{path}: {field} 형식이 올바르지 않습니다.")
    top_k = run.get("top_k")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= 50:
        raise ValueError(f"{path}: top_k는 1~50 정수여야 합니다.")
    results = run.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError(f"{path}: results 배열이 필요합니다.")
    seen_cases: set[str] = set()
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError(f"{path}: result는 JSON 객체여야 합니다.")
        case_id = str(result.get("case_id") or "").strip()
        if not case_id or case_id in seen_cases:
            raise ValueError(f"{path}: 비어 있거나 중복된 case_id={case_id!r}")
        seen_cases.add(case_id)
        if not re.fullmatch(r"[0-9a-f]{64}", str(result.get("query_sha256") or "")):
            raise ValueError(
                f"{path}:{case_id}: query_sha256 형식이 올바르지 않습니다."
            )
        returned_ids = result.get("returned_evidence_ids")
        if not isinstance(returned_ids, list) or any(
            not isinstance(value, str) or not value.strip() for value in returned_ids
        ):
            raise ValueError(
                f"{path}:{case_id}: returned_evidence_ids는 문자열 배열이어야 합니다."
            )
        normalized_ids = [value.strip() for value in returned_ids]
        if len(normalized_ids) > top_k or len(normalized_ids) != len(
            set(normalized_ids)
        ):
            raise ValueError(
                f"{path}:{case_id}: 반환 ID가 top_k를 넘거나 중복됐습니다."
            )
    return run


def audit_candidate_pool_coverage(
    candidate_path: Path,
    db_path: Path,
    system_run_paths: list[Path],
    *,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """선언한 검색 시스템의 Top-K가 사람 검수 pool에 모두 포함됐는지 검사한다."""

    if not system_run_paths:
        raise ValueError("최소 1개의 system run이 필요합니다.")
    candidate_path = Path(candidate_path)
    db_path = Path(db_path)
    candidates = load_candidate_rows(candidate_path)
    candidate_by_case = {str(row["case_id"]): row for row in candidates}
    expected_case_ids = set(candidate_by_case)
    candidate_db_hashes = {str(row["database_sha256"]) for row in candidates}
    actual_db_hash = sha256_file(db_path)
    blockers: list[dict[str, Any]] = []
    if candidate_db_hashes != {actual_db_hash}:
        blockers.append(
            {
                "code": "DATABASE_ARTIFACT_CHANGED",
                "candidate_database_sha256": sorted(candidate_db_hashes),
                "actual_database_sha256": actual_db_hash,
            }
        )

    evidence_by_cas = _load_official_evidence_by_cas(db_path)
    evidence_index = {
        str(evidence["evidence_id"]): (cas_number, evidence)
        for cas_number, evidence_rows in evidence_by_cas.items()
        for evidence in evidence_rows
    }
    runs: list[tuple[Path, dict[str, Any]]] = []
    system_ids: set[str] = set()
    for raw_path in system_run_paths:
        path = Path(raw_path)
        run = _load_pool_run(path)
        system_id = str(run["system_id"])
        if system_id in system_ids:
            blockers.append({"code": "DUPLICATE_SYSTEM_ID", "system_id": system_id})
        system_ids.add(system_id)
        if str(run["database_sha256"]) != actual_db_hash:
            blockers.append(
                {
                    "code": "POOL_RUN_DATABASE_MISMATCH",
                    "system_id": system_id,
                }
            )
        result_case_ids = {str(row["case_id"]) for row in run["results"]}
        if result_case_ids != expected_case_ids:
            blockers.append(
                {
                    "code": "POOL_RUN_CASE_SET_MISMATCH",
                    "system_id": system_id,
                    "missing_count": len(expected_case_ids - result_case_ids),
                    "unexpected_count": len(result_case_ids - expected_case_ids),
                }
            )
        runs.append((path, run))

    missing_pairs: set[tuple[str, str]] = set()
    affected_cases: set[str] = set()
    system_summaries: list[dict[str, Any]] = []
    if not blockers:
        for path, run in runs:
            system_id = str(run["system_id"])
            returned_count = 0
            missing_count = 0
            abstained_case_count = 0
            for result in run["results"]:
                case_id = str(result["case_id"])
                candidate = candidate_by_case[case_id]
                if str(result["query_sha256"]) != _query_sha256(
                    str(candidate["query"])
                ):
                    blockers.append(
                        {
                            "code": "POOL_RUN_QUERY_MISMATCH",
                            "system_id": system_id,
                            "case_id": case_id,
                        }
                    )
                    continue
                cas_number = str(candidate["cas_number"])
                pool_ids = {
                    str(evidence["evidence_id"])
                    for evidence in candidate["evidence_candidates"]
                }
                returned_ids = [
                    str(value).strip() for value in result["returned_evidence_ids"]
                ]
                returned_count += len(returned_ids)
                abstained_case_count += int(not returned_ids)
                for evidence_id in returned_ids:
                    indexed = evidence_index.get(evidence_id)
                    if indexed is None:
                        blockers.append(
                            {
                                "code": "POOL_RUN_UNKNOWN_EVIDENCE",
                                "system_id": system_id,
                                "case_id": case_id,
                                "evidence_id": evidence_id,
                            }
                        )
                        continue
                    evidence_cas, _evidence = indexed
                    if evidence_cas != cas_number:
                        blockers.append(
                            {
                                "code": "POOL_RUN_WRONG_CAS_EVIDENCE",
                                "system_id": system_id,
                                "case_id": case_id,
                                "evidence_id": evidence_id,
                            }
                        )
                        continue
                    if evidence_id not in pool_ids:
                        missing_pairs.add((case_id, evidence_id))
                        affected_cases.add(case_id)
                        missing_count += 1
            system_summaries.append(
                {
                    "system_id": system_id,
                    "system_version": run["system_version"],
                    "system_artifact_sha256": run["system_artifact_sha256"],
                    "top_k": run["top_k"],
                    "returned_occurrence_count": returned_count,
                    "abstained_case_count": abstained_case_count,
                    "missing_pool_occurrence_count": missing_count,
                    "run_sha256": sha256_file(path),
                }
            )

    if blockers:
        status = "BLOCKED_POOL_AUDIT"
    elif missing_pairs:
        status = "POOL_EXPANSION_REQUIRED"
    else:
        status = "COMPLETE_FOR_DECLARED_SYSTEMS"
    report = {
        "schema_version": POOL_AUDIT_SCHEMA_VERSION,
        "status": status,
        "action": "AUDIT_QREL_POOL_COVERAGE",
        "candidate_count": len(candidates),
        "declared_system_count": len(runs),
        "systems": system_summaries,
        "missing_unique_case_evidence_pair_count": len(missing_pairs),
        "affected_case_count": len(affected_cases),
        "blockers": blockers,
        "candidate_sha256": sha256_file(candidate_path),
        "database_sha256": actual_db_hash,
        "is_performance_result": False,
        "claim_limit": (
            "선언한 검색 시스템의 Top-K가 검수 pool에 포함됐는지만 확인합니다. "
            "pool 완전성, 문서 관련성, Retriever 정확도를 증명하지 않습니다."
        ),
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


def merge_review_sheets(
    candidate_path: Path,
    labeler_sheet_path: Path,
    reviewer_sheet_path: Path,
    db_path: Path,
    output_path: Path,
    *,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """독립 검수 결과가 완전히 일치할 때만 qrel 평가셋을 생성한다."""

    candidates = load_candidate_rows(Path(candidate_path))
    candidate_by_case = {str(row["case_id"]): row for row in candidates}
    labeler_id, labeler_rows = _read_review_sheet(Path(labeler_sheet_path), "LABELER")
    reviewer_id, reviewer_rows = _read_review_sheet(
        Path(reviewer_sheet_path), "REVIEWER"
    )
    expected_keys = {
        (str(candidate["case_id"]), str(evidence["evidence_id"]))
        for candidate in candidates
        for evidence in candidate["evidence_candidates"]
    }
    blockers: list[dict[str, Any]] = []
    if labeler_id == reviewer_id:
        blockers.append({"code": "REVIEWERS_NOT_INDEPENDENT", "actor_id": labeler_id})
    for role, rows in (("LABELER", labeler_rows), ("REVIEWER", reviewer_rows)):
        if set(rows) != expected_keys:
            blockers.append(
                {
                    "code": "REVIEW_CASE_SET_MISMATCH",
                    "role": role,
                    "missing_count": len(expected_keys - set(rows)),
                    "unexpected_count": len(set(rows) - expected_keys),
                }
            )
    db_path = Path(db_path)
    database_hash = sha256_file(db_path)
    expected_db_hashes = {str(row["database_sha256"]) for row in candidates}
    if expected_db_hashes != {database_hash}:
        blockers.append(
            {
                "code": "DATABASE_ARTIFACT_CHANGED",
                "candidate_sha256": sorted(expected_db_hashes),
                "actual_sha256": database_hash,
            }
        )

    merged_rows: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []
    invalid_labels: list[dict[str, str]] = []
    if not blockers:
        for case_id, candidate in candidate_by_case.items():
            evidence_by_id = {
                str(row["evidence_id"]): row for row in candidate["evidence_candidates"]
            }
            labeler_case: list[dict[str, str]] = []
            reviewer_case: list[dict[str, str]] = []
            for evidence_id, evidence in evidence_by_id.items():
                key = (case_id, evidence_id)
                context = _candidate_context(candidate, evidence)
                for role, sheet_row in (
                    ("LABELER", labeler_rows[key]),
                    ("REVIEWER", reviewer_rows[key]),
                ):
                    changed = [
                        field
                        for field, expected in context.items()
                        if str(sheet_row.get(field) or "") != expected
                    ]
                    if changed:
                        blockers.append(
                            {
                                "code": "CANDIDATE_CONTEXT_CHANGED",
                                "case_id": case_id,
                                "evidence_id": evidence_id,
                                "role": role,
                                "fields": changed,
                            }
                        )
                labeler_case.append(labeler_rows[key])
                reviewer_case.append(reviewer_rows[key])
            if blockers:
                continue
            try:
                labeler_label = _parse_review_case(candidate, labeler_case)
                reviewer_label = _parse_review_case(candidate, reviewer_case)
            except ValueError as error:
                invalid_labels.append({"case_id": case_id, "message": str(error)})
                continue
            if labeler_label != reviewer_label:
                differing_evidence = sorted(
                    {
                        qrel["evidence_id"]
                        for qrel in labeler_label["qrels"]
                        if qrel not in reviewer_label["qrels"]
                    }
                    | {
                        qrel["evidence_id"]
                        for qrel in reviewer_label["qrels"]
                        if qrel not in labeler_label["qrels"]
                    }
                )
                disagreements.append(
                    {
                        "case_id": case_id,
                        "answerable_disagrees": (
                            labeler_label["answerable"] != reviewer_label["answerable"]
                        ),
                        "evidence_ids": differing_evidence,
                    }
                )
                continue
            merged_rows.append(
                {
                    "case_id": case_id,
                    "query": candidate["query"],
                    "intent": candidate["intent"],
                    "answerable": labeler_label["answerable"],
                    "gold_cas_numbers": [candidate["cas_number"]],
                    "qrels": labeler_label["qrels"],
                    "supporting_sentences": labeler_label["supporting_sentences"],
                    "review_status": "DOUBLE_REVIEWED_NON_EXPERT",
                    "source_type": candidate["source_type"],
                    "source_reference": candidate["source_reference"],
                    "labeler_id": labeler_id,
                    "reviewer_id": reviewer_id,
                    "expert_reviewed": False,
                    "split": "locked_test",
                    "duplicate_group": candidate["duplicate_group"],
                    "scenario_origin": candidate["scenario_origin"],
                    "data_use_scope": "COMPETITION_REVIEWED_EVALUATION_ONLY",
                    "database_sha256": database_hash,
                }
            )

    if invalid_labels:
        blockers.append({"code": "INVALID_REVIEW_LABEL", "errors": invalid_labels})
    if disagreements:
        blockers.append(
            {
                "code": "INDEPENDENT_REVIEW_DISAGREEMENT",
                "case_count": len(disagreements),
                "cases": disagreements,
            }
        )

    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError(f"기존 파일을 덮어쓰지 않습니다: {output_path}")
    contract: dict[str, Any] | None = None
    if not blockers and len(merged_rows) == len(candidates):
        temporary_path = output_path.with_name(f".{output_path.name}.tmp")
        _write_jsonl(temporary_path, merged_rows)
        contract = evaluate_dataset_contract(
            merged_rows,
            EvaluationProfile.COMPETITION_REVIEWED,
            temporary_path,
        )
        if not contract["passed"]:
            temporary_path.unlink(missing_ok=True)
            blockers.append(
                {
                    "code": "MERGED_DATASET_CONTRACT_FAILED",
                    "contract_blockers": contract["blockers"],
                }
            )
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.replace(output_path)
            contract["dataset"] = str(output_path)

    report = {
        "schema_version": MERGE_SCHEMA_VERSION,
        "status": "COMPLETED" if not blockers else "BLOCKED_REVIEW_GATE",
        "candidate_count": len(candidates),
        "evidence_judgment_count": len(expected_keys),
        "merged_case_count": len(merged_rows) if not blockers else 0,
        "labeler_id": labeler_id,
        "reviewer_id": reviewer_id,
        "independent_review": labeler_id != reviewer_id,
        "disagreement_count": len(disagreements),
        "blockers": blockers,
        "database_sha256": database_hash,
        "output_path": str(output_path) if not blockers else None,
        "evaluation_contract": contract,
        "claim_limit": (
            "기계 생성 질문을 이중 비전문가가 검수한 KOSHA section 평가셋이며 "
            "현장 질문 분포·현장 안전성 검증이 아닙니다."
        ),
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


__all__ = [
    "CANDIDATE_SCHEMA_VERSION",
    "POOL_AUDIT_SCHEMA_VERSION",
    "POOL_RUN_SCHEMA_VERSION",
    "QUERY_TEMPLATES",
    "audit_candidate_pool_coverage",
    "audit_review_sheet",
    "export_review_sheet",
    "generate_qrel_candidate_pool",
    "load_candidate_rows",
    "merge_review_sheets",
    "validate_candidate_rows",
]
