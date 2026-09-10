"""신고문 구조화를 위한 결정적 기준선과 출력 검증."""

from __future__ import annotations

import re
from typing import Any

from chemiguard119.incident_language import (
    COPULA_SUFFIXES,
    MAX_STATEMENT_CONFLICTS,
    assertion_for,
    conflicting_mentions,
    context_role_for,
    context_windows,
)
from chemiguard119.resolver import (
    AUTHORITATIVE_ALIAS_TYPES,
    find_exact_alias_spans,
    resolve_substance,
)
from chemiguard119.utils import normalize_text


ACTION_PATTERNS = {
    "WATER_APPLICATION": ("주수", "물 뿌리", "물을 뿌"),
    "WATER_FOG": ("물안개", "분무주수", "분무 주수"),
    "VENTILATION": ("환기",),
    "FOAM": ("포 소화", "포소화", "포 방사"),
    "DRAIN_BLOCK": ("배수로 차단", "배수 차단", "유입 차단"),
}
INCIDENT_PARSER_POLICY_VERSION = "incident-parser-policy-v3-span-uncertainty"

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
INCIDENT_ALIAS_CONTEXT_SUFFIXES = (
    "저장탱크",
    "탱크",
    "용기",
    "가스",
    "통",
) + COPULA_SUFFIXES
INCIDENT_WHITESPACE_TOLERANT_ALIAS_MIN_LENGTH = 6
KOREAN_ALIAS_PATTERN = re.compile(r"[가-힣\s]+")


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


def _allows_internal_whitespace(row: dict[str, Any], alias: str) -> bool:
    """긴 한글 권위 별칭에만 ASR 내부 띄어쓰기 변형을 허용한다."""

    # 이번 관측 실패는 원래 여러 단어인 권위 명칭 안에 ASR이 공백을 하나
    # 더 삽입한 경우다. 단일 단어까지 임의 분할을 허용하면 탐색 범위와
    # 오후보 가능성이 불필요하게 커지므로 관측된 범위만 복구한다.
    if not any(character.isspace() for character in alias):
        return False
    alias_type = str(row.get("alias_type") or "").strip().lower()
    if not (
        alias_type in AUTHORITATIVE_ALIAS_TYPES or alias_type.startswith("canonical")
    ):
        return False
    if not KOREAN_ALIAS_PATTERN.fullmatch(alias):
        return False
    compact_length = sum(1 for character in alias if not character.isspace())
    return compact_length >= INCIDENT_WHITESPACE_TOLERANT_ALIAS_MIN_LENGTH


def _compact_whitespace_view(text: str) -> tuple[str, tuple[int, ...]]:
    characters, positions = [], []
    for index, character in enumerate(text):
        if not character.isspace():
            folded = character.casefold()
            characters.append(folded)
            positions.extend([index] * len(folded))
    return "".join(characters), tuple(positions)


def _spans_ignoring_internal_whitespace(
    text: str,
    alias: str,
    compact_text: str,
    compact_positions: tuple[int, ...],
) -> list[tuple[int, int, str]]:
    """공백을 제외한 일치를 원문의 안전한 span으로 다시 투영한다."""

    compact_alias = "".join(
        character for character in alias if not character.isspace()
    ).casefold()
    spans: list[tuple[int, int, str]] = []
    compact_start = compact_text.find(compact_alias)
    while compact_start >= 0:
        compact_end = compact_start + len(compact_alias)
        start = compact_positions[compact_start]
        end = compact_positions[compact_end - 1] + 1
        surface = text[start:end]
        validated = find_exact_alias_spans(
            text,
            surface,
            allowed_context_suffixes=INCIDENT_ALIAS_CONTEXT_SUFFIXES,
        )
        if (start, end, surface) in validated:
            spans.append((start, end, surface))
        compact_start = compact_text.find(compact_alias, compact_start + 1)
    return spans


def _incident_alias_spans(
    text: str,
    row: dict[str, Any],
    compact_text: str,
    compact_positions: tuple[int, ...],
) -> list[tuple[int, int, str]]:
    """정확 별칭과 제한된 ``물질명+설비/물성`` 원문 span을 찾는다."""

    alias = str(row.get("alias_text") or "").strip()
    exact_spans = find_exact_alias_spans(
        text,
        alias,
        allowed_context_suffixes=INCIDENT_ALIAS_CONTEXT_SUFFIXES,
    )
    if exact_spans or not _allows_internal_whitespace(row, alias):
        return exact_spans
    return _spans_ignoring_internal_whitespace(
        text,
        alias,
        compact_text,
        compact_positions,
    )


def deterministic_parse(text: str, resolver_artifact: dict[str, Any]) -> dict[str, Any]:
    incident_types = _incident_types(text)
    fire_negative = "FIRE" not in incident_types and bool(
        INCIDENT_EVENT_PATTERNS["FIRE"].search(text)
    )

    # 학습 artifact의 검증 별칭 중 원문에 실제 등장한 표현을 찾는다. 물질명은
    # 다른 물질명을 부분 문자열로 포함할 수 있으므로(예: 차아염소산나트륨 안의
    # 나트륨), 먼저 모든 위치를 모은 뒤 가장 긴 비중첩 표현만 선택한다.
    matches: list[tuple[int, int, dict[str, Any], str]] = []
    compact_text, compact_positions = _compact_whitespace_view(text)
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
        for start, end, surface in _incident_alias_spans(
            text,
            row,
            compact_text,
            compact_positions,
        ):
            matches.append((start, end, row, surface))

    selected: list[tuple[int, int, dict[str, Any], str]] = []
    selected_spans: list[tuple[int, int]] = []
    for start, end, row, surface in sorted(
        matches,
        key=lambda item: (-(item[1] - item[0]), item[0], item[2]["cas_number"]),
    ):
        if any(
            start < selected_end and selected_start < end
            for selected_start, selected_end in selected_spans
        ):
            continue
        selected_spans.append((start, end))
        selected.append((start, end, row, surface))
    # 같은 CAS도 다른 원문 구간이면 보존한다. 길이 우선 선택 후 원문 순서로 반환한다.
    selected.sort(key=lambda item: (item[0], item[1]))
    windows = context_windows(text, [(item[0], item[1]) for item in selected])
    substances = []
    resolutions: dict[str, dict[str, Any]] = {}
    for (start, end, _row, surface), (left, right) in zip(
        selected, windows, strict=True
    ):
        assertion = assertion_for(left, right)
        context_role = context_role_for(left, right)
        if surface not in resolutions:
            resolutions[surface] = resolve_substance(
                surface, resolver_artifact, top_k=3
            )
        substances.append(
            {
                "mention_id": f"mention-{start}-{end}",
                "start": start,
                "end": end,
                "surface_text": surface,
                "role": "NEGATED" if assertion == "NEGATED" else context_role,
                "context_role": context_role,
                "assertion": assertion,
                "assertion_basis": "LOCAL_RULE_NOT_HUMAN_CONFIRMATION",
                "resolver": resolutions[surface],
            }
        )

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

    statement_conflicts = conflicting_mentions(substances)
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
        "parser_policy_version": INCIDENT_PARSER_POLICY_VERSION,
        "span_encoding": "UNICODE_CODE_POINT_START_INCLUSIVE_END_EXCLUSIVE",
        "source_text": text,
        "incident_types": incident_types,
        "fire_status": "FALSE"
        if fire_negative
        else ("TRUE" if "FIRE" in incident_types else "UNKNOWN"),
        "substance_mentions": substances,
        "statement_conflicts": statement_conflicts,
        "statement_conflict_limit_reached": len(statement_conflicts)
        >= MAX_STATEMENT_CONFLICTS,
        "requires_statement_clarification": bool(statement_conflicts),
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
        if "start" in mention or "end" in mention:
            start, end = mention.get("start"), mention.get("end")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or not 0 <= start < end <= len(source_text)
                or source_text[start:end] != surface
            ):
                errors.append("물질 표현의 원문 구간이 일치하지 않습니다.")
    allowed_incidents = {"LEAK", "FIRE", "EXPLOSION", "UNKNOWN"}
    if any(item not in allowed_incidents for item in payload.get("incident_types", [])):
        errors.append("허용되지 않은 사고유형")
    return errors
