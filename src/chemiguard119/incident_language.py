"""제한된 한국어 신고 문맥 규칙. 진술의 의미와 사람 확인을 분리한다."""

from __future__ import annotations

import re

# Parser만 이 경계를 허용한다. Resolver의 exact/후보 승인 정책은 그대로다.
COPULA_SUFFIXES = (
    "인",
    "인지",
    "이라고",
    "이라는",
    "이란",
    "이라면",
    "일",
    "일지도",
    "입니다",
    "이었다",
    "였습니다",
)
CLAUSE_BREAK = re.compile(r"[.!?;,\n]+|(?:지만|으며|으나|는데)(?=\s|[,.;])")
UNCONFIRMED = re.compile(
    r"미확인|불명|불확실|모르|확인.{0,12}(?:못|안\s*됐|되지|안\s*했)|알\s*수\s*없|인지(?:\s|$)"
)
POSSIBLE = re.compile(r"같(?:습|아|은)|의심|일\s*수도|가능|추정")
NEGATED = re.compile(
    r"아니|아님|아닙|아닌|없(?:다|어|음|습니다|었|는|으며|으나|지만|다고|고|을)"
)
INCIDENT_TERMS = ("누출", "새고", "샌", "유출", "화재", "폭발", "탱크에서")
FACILITY_TERMS = ("옆", "저장고", "창고", "보관", "시설", "함께", "인접")
# 원문 언급은 모두 보존하지만 충돌 쌍의 직렬화는 제한한다.
MAX_STATEMENT_CONFLICTS = 64


def context_windows(text: str, spans: list[tuple[int, int]]) -> list[tuple[str, str]]:
    """다른 언급·문장/절을 넘지 않는 원문 문맥. 분류의 불확실성을 없애지는 않는다."""
    breaks = [(m.start(), m.end()) for m in CLAUSE_BREAK.finditer(text)]
    result = []
    for index, (start, end) in enumerate(spans):
        left = max(0, start - 32, spans[index - 1][1] if index else 0)
        right = min(
            len(text),
            end + 40,
            spans[index + 1][0] if index + 1 < len(spans) else len(text),
        )
        for boundary_start, boundary_end in breaks:
            if boundary_end <= start:
                left = max(left, boundary_end)
            elif boundary_start >= end:
                # "없으며"의 어미를 제거하면 "없"만 남아 부정을 잃는다.
                # 접속 어미 자체는 앞 절에 속하게 하고 뒤 절만 제외한다.
                right = min(right, boundary_end)
                break
        left_text = text[left:start]
        # "A가 아닌 B"의 부정은 A에 붙는다. 이전 언급의 조사·부정만
        # 남은 문맥을 B의 수식어로 다시 사용하지 않는다. 원문 span은 유지한다.
        if (
            index
            and left == spans[index - 1][1]
            and re.fullmatch(r"\s*(?:이|가|은|는)?\s*아닌\s*", left_text)
        ):
            left_text = ""
        result.append((left_text, text[end:right]))
    return result


def assertion_for(left: str, right: str) -> str:
    # "없다"가 아니라 "확인할 수 없다"는 정보 부족이다. 부재로 변환하지 않는다.
    if UNCONFIRMED.search(right) or re.search(r"(?:미확인|불명)\s*$", left):
        return "UNCONFIRMED"
    if POSSIBLE.search(right) or re.search(r"(?:의심되는|추정되는)\s*$", left):
        return "POSSIBLE"
    if NEGATED.search(right) or re.search(r"(?:아닌|없는)\s*$", left):
        return "NEGATED"
    return "AFFIRMED"


def context_role_for(left: str, right: str) -> str:
    if any(term in right[:20] for term in INCIDENT_TERMS):
        return "INCIDENT"
    nearest_incident = max((left.rfind(term) for term in INCIDENT_TERMS), default=-1)
    nearest_facility = max((left.rfind(term) for term in FACILITY_TERMS), default=-1)
    if nearest_facility > nearest_incident:
        return "FACILITY"
    if nearest_incident >= 0:
        return "INCIDENT"
    if any(term in right for term in FACILITY_TERMS):
        return "FACILITY"
    # "라벨에 ... 적혀 있습니다"는 물질 위치 증거가 아니다.
    if (
        "라벨" not in left
        and "적혀" not in right
        and ("있습니다" in right or "있어" in right)
    ):
        return "FACILITY"
    return "UNKNOWN"


def conflicting_mentions(mentions: list[dict]) -> list[dict]:
    """부정/비부정의 병존을 재확인 대상으로 표시. 의미 모순을 확정하지 않는다."""
    result = []
    for index, first in enumerate(mentions):
        first_cas = {
            c["cas_number"] for c in first["resolver"].get("candidates", [])[:1]
        }
        for second in mentions[index + 1 :]:
            second_cas = {
                c["cas_number"] for c in second["resolver"].get("candidates", [])[:1]
            }
            if not first_cas & second_cas or (first["assertion"] == "NEGATED") == (
                second["assertion"] == "NEGATED"
            ):
                continue
            first_role, second_role = first["context_role"], second["context_role"]
            if first_role != second_role and "UNKNOWN" not in (first_role, second_role):
                continue
            result.append(
                {
                    "mention_ids": [first["mention_id"], second["mention_id"]],
                    "status": "REQUIRES_CONTEXT_CLARIFICATION",
                    "reason_code": "NEGATED_AND_NONNEGATED_CLAIMS",
                    "not_a_verified_contradiction": True,
                }
            )
            if len(result) >= MAX_STATEMENT_CONFLICTS:
                return result
    return result
