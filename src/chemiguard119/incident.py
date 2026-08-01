"""신고문 구조화를 위한 결정적 기준선과 출력 검증."""

from __future__ import annotations

import re
from typing import Any

from chemiguard119.resolver import find_exact_alias_spans, resolve_substance
from chemiguard119.utils import normalize_text


ACTION_PATTERNS = {
    "WATER_APPLICATION": ("주수", "물 뿌리", "물을 뿌"),
    "WATER_FOG": ("물안개", "분무주수", "분무 주수"),
    "VENTILATION": ("환기",),
    "FOAM": ("포 소화", "포소화", "포 방사"),
    "DRAIN_BLOCK": ("배수로 차단", "배수 차단", "유입 차단"),
}
INCIDENT_PARSER_POLICY_VERSION = "incident-parser-policy-v2-national-dev-through-2020"

# 신고·사고 정리문에서 같은 사건이 다양한 동사로 기록된다. 아래 표현은
# 2020년까지의 전국 공식 사고 개발 구간에서 확인한 뒤, 뜻이 직접적인
# 방출·연소·폭발 표현만 보수적으로 채택했다. ``파손``처럼 결과가 누출인지
# 폭발인지 단정할 수 없는 설비 상태 표현은 포함하지 않는다.
INCIDENT_EVENT_PATTERNS = {
    "LEAK": re.compile(
        r"누출|유출|누설|분출|방출|비산|새고|샌다|새는|새어|흘러|쏟|넘쳐|넘침|튀어"
    ),
    "FIRE": re.compile(r"화재|불이|연소|발화|착화"),
    "EXPLOSION": re.compile(r"폭발|폭굉"),
}
EVENT_NEGATION_PATTERN = re.compile(r"(?:안|없|아니|아닙|아님|아닌|미발생|미확인)")

# 한국어 현장 문장에서는 물질명과 설비·물성이 붙어 쓰이는 경우가 많다.
# 이 접미사는 화학 정체성을 바꾸지 않는 문맥만 허용한다. 예를 들어
# ``염소가스``는 염소 후보로 찾되 ``염소소독제``나 ``차아염소산``은 찾지 않는다.
INCIDENT_ALIAS_CONTEXT_SUFFIXES = ("저장탱크", "탱크", "용기", "가스", "통")


def _event_is_negated(text: str, start: int, end: int) -> bool:
    left = text[max(0, start - 6) : start]
    right = text[end : min(len(text), end + 8)]
    return bool(
        re.search(r"(?:안|없는|아닌|미발생|미확인).{0,4}$", left)
        or EVENT_NEGATION_PATTERN.search(right)
    )


def _incident_types(text: str) -> list[str]:
    found: list[str] = []
    for incident_type, pattern in INCIDENT_EVENT_PATTERNS.items():
        if any(
            not _event_is_negated(text, match.start(), match.end())
            for match in pattern.finditer(text)
        ):
            found.append(incident_type)
    return found or ["UNKNOWN"]


def _incident_alias_spans(text: str, alias: str) -> list[tuple[int, int, str]]:
    """정확 별칭과 제한된 ``물질명+설비/물성`` 원문 span을 찾는다."""

    return find_exact_alias_spans(
        text,
        alias,
        allowed_context_suffixes=INCIDENT_ALIAS_CONTEXT_SUFFIXES,
    )


def _negated(text: str, surface: str) -> bool:
    escaped = re.escape(surface)
    return bool(
        re.search(
            rf"{escaped}.{{0,10}}(?:아니|아님|아닙니다|없(?:다|어|음|습니다|었(?:다|습니다)|는)|미확인)",
            text,
            re.IGNORECASE,
        )
        or re.search(rf"(?:아닌|없는).{{0,8}}{escaped}", text, re.IGNORECASE)
    )


def _role(text: str, surface: str) -> str:
    if _negated(text, surface):
        return "NEGATED"
    index = text.lower().find(surface.lower())
    start = max(0, index - 32)
    end = min(len(text), index + len(surface) + 24)
    left_context = text[start:index]
    right_context = text[index + len(surface) : end]
    incident_terms = ("누출", "새고", "샌", "유출", "화재", "폭발", "탱크에서")
    facility_terms = (
        "옆",
        "저장고",
        "창고",
        "보관",
        "시설",
        "함께",
        "인접",
        "있어",
        "있습니다",
    )
    # 물질명 뒤에 바로 사고 동사가 오는 경우를 가장 강한 사고물질 신호로 본다.
    if any(term in right_context[:20] for term in incident_terms):
        return "INCIDENT"
    # 왼쪽 문맥에 두 종류가 모두 있으면 물질명에 더 가까운 마지막 표지를 사용한다.
    nearest_incident = max(
        (left_context.rfind(term) for term in incident_terms), default=-1
    )
    nearest_facility = max(
        (left_context.rfind(term) for term in facility_terms), default=-1
    )
    if nearest_facility > nearest_incident:
        return "FACILITY"
    if nearest_incident >= 0:
        return "INCIDENT"
    if any(term in right_context for term in facility_terms):
        return "FACILITY"
    return "UNKNOWN"


def deterministic_parse(text: str, resolver_artifact: dict[str, Any]) -> dict[str, Any]:
    incident_types = _incident_types(text)
    fire_negative = "FIRE" not in incident_types and bool(
        INCIDENT_EVENT_PATTERNS["FIRE"].search(text)
    )

    # 학습 artifact의 검증 별칭 중 원문에 실제 등장한 표현을 찾는다. 물질명은
    # 다른 물질명을 부분 문자열로 포함할 수 있으므로(예: 차아염소산나트륨 안의
    # 나트륨), 먼저 모든 위치를 모은 뒤 가장 긴 비중첩 표현만 선택한다.
    matches: list[tuple[int, int, dict[str, Any], str]] = []
    for row in sorted(
        resolver_artifact["rows"],
        key=lambda item: len(item["alias_text"]),
        reverse=True,
    ):
        alias = str(row["alias_text"]).strip()
        if len(alias.strip()) < 2:
            continue
        # Resolver와 같은 문장 내 exact matcher를 사용해 ``염산염`` 안의
        # ``염산``처럼 다른 표현에 포함된 부분 문자열을 물질명으로 승격하지 않는다.
        for start, end, surface in _incident_alias_spans(text, alias):
            matches.append((start, end, row, surface))

    found_by_cas: dict[str, dict[str, Any]] = {}
    selected_spans: list[tuple[int, int]] = []
    for start, end, row, surface in sorted(
        matches,
        key=lambda item: (-(item[1] - item[0]), item[0], item[2]["cas_number"]),
    ):
        cas = row["cas_number"]
        if cas in found_by_cas:
            continue
        if any(
            start < selected_end and selected_start < end
            for selected_start, selected_end in selected_spans
        ):
            continue
        selected_spans.append((start, end))
        role = _role(text, surface)
        candidate_like = bool(
            re.search(
                rf"{re.escape(surface)}.{{0,6}}(?:같|의심|일 수도|가능)",
                text,
                re.IGNORECASE,
            )
        )
        found_by_cas[cas] = {
            "surface_text": surface,
            "role": role,
            "assertion": "NEGATED"
            if role == "NEGATED"
            else ("POSSIBLE" if candidate_like else "AFFIRMED"),
            "resolver": resolve_substance(surface, resolver_artifact, top_k=3),
        }

    planned_actions = []
    for action_code, patterns in ACTION_PATTERNS.items():
        for pattern in patterns:
            if pattern in text:
                planned_actions.append(
                    {
                        "surface_text": pattern,
                        "action_code": action_code,
                        "status": "EXACT_ACTION_DICTIONARY",
                    }
                )
                break

    substances = list(found_by_cas.values())
    needs_confirmation = any(
        item["role"] == "UNKNOWN"
        or item["assertion"] != "AFFIRMED"
        or item["resolver"]["requires_responder_confirmation"]
        for item in substances
        if item["role"] != "NEGATED"
    )
    if not substances:
        needs_confirmation = True

    return {
        "backend": "DETERMINISTIC_BASELINE",
        "source_text": text,
        "incident_types": incident_types,
        "fire_status": "FALSE"
        if fire_negative
        else ("TRUE" if "FIRE" in incident_types else "UNKNOWN"),
        "substance_mentions": substances,
        "planned_actions": planned_actions,
        "needs_substance_confirmation": needs_confirmation,
        "missing_fields": ["substance"] if not substances else [],
        "warning": "결정적 기준선도 대원 확인 전 물질을 확정하지 않습니다.",
    }


def validate_parser_output(payload: dict[str, Any], source_text: str) -> list[str]:
    errors: list[str] = []
    forbidden = {
        "risk_level",
        "severity",
        "rule_id",
        "recommended_response",
        "final_decision",
    }
    if forbidden.intersection(payload):
        errors.append("parser가 금지된 위험판정·결정 필드를 출력했습니다.")
    for mention in payload.get("substance_mentions", []):
        surface = str(mention.get("surface_text") or "")
        if surface and normalize_text(surface) not in normalize_text(source_text):
            errors.append(f"원문에 없는 물질 표현: {surface}")
    allowed_incidents = {"LEAK", "FIRE", "EXPLOSION", "UNKNOWN"}
    if any(item not in allowed_incidents for item in payload.get("incident_types", [])):
        errors.append("허용되지 않은 사고유형")
    return errors
