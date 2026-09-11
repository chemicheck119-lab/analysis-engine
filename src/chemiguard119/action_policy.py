"""카드/규칙을 허용하기 전에 실행하는 결정적 보수 정책."""

from typing import Any

from chemiguard119.api_models import IncidentAnalyzeRequest
from chemiguard119.rag import _valid_official_url
from chemiguard119.retrieval import _runtime_index


class BriefHeld(Exception):
    """세부 입력·문서 내용 없이 보류 코드만 운반한다."""

    def __init__(self, code: str = "OUTPUT_REJECTED") -> None:
        super().__init__(code)
        self.code = code


def before_rule(payload: IncidentAnalyzeRequest, result: dict[str, Any]) -> None:
    """상충을 완전히 이해하는 모델이 아니다. 명시적 CAS/동일 문서 충돌만 차단한다."""
    confirmations = {
        "INCIDENT": payload.confirmed_incident_substance,
        "FACILITY": payload.confirmed_facility_substance,
    }
    if any(confirmations.values()) and result.get("parsed_report", {}).get(
        "requires_statement_clarification"
    ):
        raise BriefHeld("STATEMENT_CLARIFICATION_REQUIRED")
    for mention in result.get("substance_candidates", []):
        confirmed = confirmations.get(mention.get("role"))
        hint = mention.get("evidence_cas_hint")
        if (
            confirmed
            and hint
            and hint != confirmed.cas_number
            and mention.get("assertion") != "NEGATED"
        ):
            raise BriefHeld("CONFIRMATION_CONFLICT")
    versions: dict[tuple[str, str, str], str] = {}
    for group in result.get("evidence", []):
        for row in group.get("retrieval", {}).get("results", []):
            cas = row.get("cas_number")
            if group.get("cas_hint") and cas != group["cas_hint"]:
                raise BriefHeld("WRONG_CAS_EVIDENCE")
            key = (
                str(cas),
                str(row.get("source_record_id") or row.get("evidence_id")),
                str(row.get("title")),
            )
            content = str(row.get("body_preview") or "")
            if key in versions and versions[key] != content:
                raise BriefHeld("DOCUMENT_CONFLICT")
            versions[key] = content


def usable_source(row: dict[str, Any], cas: str | None) -> bool:
    return bool(
        cas
        and row.get("cas_number") == cas
        and row.get("evidence_id")
        and row.get("document_version")
        and row.get("cas_link_status")
        in {"SOURCE_EXACT", "APPROVED", "PUBLIC_SOURCE_VERIFIED"}
        and _valid_official_url(row.get("source_url"), str(row.get("source")))
    )


def official_cas_link(artifact: dict[str, Any], cas: str) -> list[dict[str, Any]]:
    """검색된 절의 URL이 없을 때만 쓰는 exact CAS 문서 링크. 절 관련성 정답이 아니다."""
    index = _runtime_index(artifact)
    for identity in index.get("ids_by_cas", {}).get(cas, []):
        row = index["row_by_id"][identity]
        if usable_source(row, cas):
            result = {key: value for key, value in row.items() if key != "body"}
            result["body_preview"] = str(row.get("body") or "")[:280].replace("\n", " ")
            result["brief_selection"] = "EXACT_CAS_LINK_FALLBACK_NOT_SECTION_RELEVANCE"
            return [result]
    return []
