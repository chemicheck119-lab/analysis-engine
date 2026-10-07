"""Select source sections for reported situations, without deciding tactics."""

from __future__ import annotations

import re
from typing import Any

from chemiguard119.incident import _event_is_negated
from chemiguard119.rag import _valid_official_url

POLICY_VERSION = "response-section-selection-v1"
CHAPTER = re.compile(r"MSDS\s+(\d{1,2})장")
EXPOSURE = re.compile(r"노출|흡입|눈에|피부에|섭취")
EXPOSURE_ROUTES = {
    "눈": re.compile(r"눈에|눈으로"),
    "피부": re.compile(r"피부에|피부로"),
    "흡입": re.compile(r"흡입|호흡기로"),
    "먹었": re.compile(r"섭취|먹었|삼켰"),
}


def exposure_routes(parsed: dict[str, Any]) -> list[str]:
    text = str(parsed.get("source_text") or "")
    return [
        key
        for key, pattern in EXPOSURE_ROUTES.items()
        if any(
            not _event_is_negated(text, match.start(), match.end())
            for match in pattern.finditer(text)
        )
    ]


def response_chapters(parsed: dict[str, Any]) -> list[int]:
    """Use parsed events; exposure phrases remain reported, never diagnosed."""
    if parsed.get("requires_statement_clarification"):
        return []
    events = parsed.get("incident_types") or []
    chapters = []
    if "FIRE" in events:
        chapters.append(5)
    if "LEAK" in events:
        chapters.append(6)
    text = str(parsed.get("source_text") or "")
    if any(
        not _event_is_negated(text, match.start(), match.end())
        for match in EXPOSURE.finditer(text)
    ):
        chapters.insert(0, 4)
    return list(dict.fromkeys(chapters + ([8] if chapters else [])))


def select_response_evidence(
    retrieval: dict[str, Any],
    artifact: dict[str, Any],
    parsed: dict[str, Any],
    cas: str,
    top_k: int,
) -> dict[str, Any]:
    chapters = response_chapters(parsed)
    if not chapters or retrieval.get("status") == "RETRIEVAL_UNAVAILABLE":
        return retrieval
    ranking = {
        row["evidence_id"]: position
        for position, row in enumerate(retrieval.get("results") or [])
    }
    candidates = []
    routes = exposure_routes(parsed)
    for row in artifact.get("rows") or []:
        match = CHAPTER.search(str(row.get("title") or ""))
        if (
            row.get("cas_number") != cas
            or row.get("source") != "KOSHA"
            or row.get("cas_link_status")
            not in {"SOURCE_EXACT", "APPROVED", "PUBLIC_SOURCE_VERIFIED"}
            or not match
            or int(match.group(1)) not in chapters
            or not str(row.get("body") or "").strip()
            or not _valid_official_url(row.get("source_url"), "KOSHA")
        ):
            continue
        if int(match.group(1)) == 4 and not any(
            route in str(row.get("title") or "") for route in routes
        ):
            # Unknown exposure must not become an eye/skin/inhalation instruction.
            continue
        candidates.append((int(match.group(1)), row))
    candidates.sort(
        key=lambda item: (
            chapters.index(item[0]),
            ranking.get(item[1]["evidence_id"], len(ranking)),
            item[1]["evidence_id"],
        )
    )
    # Keep each requested section represented before adding more rows from one section.
    groups = [
        [item for item in candidates if item[0] == chapter] for chapter in chapters
    ]
    selected = [
        item
        for position in range(max((len(group) for group in groups), default=0))
        for group in groups
        for item in group[position : position + 1]
    ][:top_k]
    results = []
    for _, item in selected:
        row = dict(item)
        row["body_preview"] = str(row.pop("body"))[:2000]
        row["selection_method"] = POLICY_VERSION
        results.append(row)
    missing = [
        chapter
        for chapter in chapters
        if chapter not in {item[0] for item in candidates}
    ]
    return {
        **retrieval,
        "status": "COMPLETED" if results else "NO_EVIDENCE_FOUND",
        "method": POLICY_VERSION,
        "results": results,
        "requested_response_chapters": chapters,
        "missing_response_chapters": missing,
        "exposure_route_missing": 4 in chapters and not routes,
        "notice": "상황별 공식 자료 항목 조회입니다. 제품·농도·상태와 현장 적용은 별도 확인해야 합니다."
        + (
            " 미연결 항목: " + ", ".join(f"{chapter}장" for chapter in missing)
            if missing
            else ""
        ),
    }
