"""행동 카드 v1: 기존 분석 요청을 감싸는 추가 계약. 상태 저장/인증 증명이 아니다."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from chemiguard119.api_models import (
    IncidentAnalyzeRequest,
    StrictModel,
    contains_candidate_promotion,
)
from chemiguard119.action_catalog import CATALOG

BRIEF_SCHEMA_VERSION = "action-brief-v1"


class BriefRequest(StrictModel):
    analysis: IncidentAnalyzeRequest = Field(
        description="기존 분석 요청. 전사 원문은 수정하지 않습니다."
    )
    revision: int = Field(
        ge=0,
        description="Backend가 관리하는 사고 revision. 모델 서버가 최신 여부를 보증하지 않습니다.",
    )
    invalidated_confirmation_ids: list[str] = Field(
        default_factory=list,
        max_length=2,
        description="이번 입력에서 취소된 확인 ID. 해당 역할의 확인을 제거합니다.",
    )
    reported_evidence_conflict: bool = Field(
        default=False,
        description="새 근거 상충을 보고하면 양쪽 확인을 보류합니다. false는 상충 없음의 증명이 아닙니다.",
    )

    @model_validator(mode="after")
    def require_incident(self) -> "BriefRequest":
        if not self.analysis.incident_id:
            raise ValueError("행동 카드에는 incident_id가 필요합니다.")
        if any(
            not value or len(value) > 128 for value in self.invalidated_confirmation_ids
        ):
            raise ValueError("취소 ID는 1~128자여야 합니다.")
        return self

    def effective_analysis(self) -> IncidentAnalyzeRequest:
        result = self.analysis.model_copy(deep=True)
        for name in ("confirmed_incident_substance", "confirmed_facility_substance"):
            item = getattr(result, name)
            if item and (
                self.reported_evidence_conflict
                or item.confirmation_id in self.invalidated_confirmation_ids
            ):
                setattr(result, name, None)
        return result


class BriefSource(StrictModel):
    source_id: str
    document_id: str = Field(description="인덱스 원문 문서/항목 식별자")
    source_type: Literal["KOSHA", "CAMEO", "INTERNAL_POLICY"]
    url: str
    section: str = Field(
        description="인덱스 항목 제목 또는 내부 정책 항목. SDS section 정답 검수는 별도입니다."
    )
    cas_number: str | None = None
    role: Literal["INCIDENT", "FACILITY", "UNKNOWN"] = "UNKNOWN"
    document_version: str
    selection_method: str = "BASELINE_RETRIEVAL"
    content_sha256: str | None = Field(
        default=None, description="검색 반환 내용의 hash. 전체 원문 hash가 아닙니다."
    )
    license_status: Literal["LINK_ONLY_TERMS_REVIEW_REQUIRED", "PROJECT_AUTHORED"]
    freshness_status: Literal["INDEX_VERSION_ONLY", "VERSIONED_POLICY"]


class ActionCard(StrictModel):
    card_id: str
    phrase_id: str
    phrase_version: str
    category: Literal["확인", "대응 참고", "보류", "인계"]
    priority: int = Field(
        ge=1,
        le=4,
        description="표시 순서이며 위험등급이 아닙니다. 1부터 먼저 표시합니다.",
    )
    title: str = Field(max_length=80)
    message: str = Field(max_length=240)
    reason: str = Field(max_length=400)
    role: Literal["INCIDENT", "FACILITY", "UNKNOWN"] = "UNKNOWN"
    target_label: str = Field(
        description="화면 표시용 사고물질/시설물질/전체 안내 구분"
    )
    cas_number: str | None = None
    confirmation_status: Literal["UNCONFIRMED", "CONFIRMED_INPUT", "NOT_APPLICABLE"]
    required_conditions: list[str]
    unmet_conditions: list[str]
    source_ids: list[str] = Field(min_length=1)
    review_status: Literal["DRAFT_NOT_EXPERT_REVIEWED"] = "DRAFT_NOT_EXPERT_REVIEWED"
    tactical_authorization: Literal[False] = False
    state_fingerprint: str


class BriefResponse(StrictModel):
    schema_version: Literal["action-brief-v1"] = BRIEF_SCHEMA_VERSION
    request_id: str
    incident_id: str
    revision: int
    state_fingerprint: str = Field(
        description="입력·버전 비교용 hash. 인증·승인·최신 revision 증명이 아닙니다."
    )
    phase: Literal["initial", "final"] = Field(
        description="initial은 빠른 확인 안내, final은 현재 요청의 최종 snapshot입니다."
    )
    status: Literal["PENDING", "COMPLETED", "NEEDS_CONFIRMATION", "HELD", "TIMEOUT"] = (
        Field(
            description="PENDING 분석 중 / COMPLETED 분석 완료(현장 승인 아님) / NEEDS_CONFIRMATION 추가 확인 / HELD 근거·조건 부족 / TIMEOUT 제한시간 초과"
        )
    )
    summary: str = Field(max_length=300)
    cards: list[ActionCard]
    missing_information: list[str]
    sources: list[BriefSource]
    facts: dict[str, Any] = Field(
        default_factory=dict,
        description="Parser의 원문 span·부정·추정 그대로. 사실 검증 결과가 아닙니다.",
    )
    substance_candidates: list[dict[str, Any]] = Field(default_factory=list)
    discovery: dict[str, Any] | None = Field(
        default=None,
        description="이름 후보가 없을 때만 실행하는 보조 Discovery. 현장 확인·Rule 실행 권한이 없습니다.",
    )
    confirmation_state: dict[str, bool]
    rule_review: dict[str, Any] = Field(
        default_factory=lambda: {"executed": False},
        description="원 Rule 결과의 제한된 필드. 서수 결과이며 전술 승인이나 확률이 아닙니다.",
    )
    versions: dict[str, Any] = Field(
        description="모델·카탈로그·정책·계획 방식. LLM 사용 여부를 구분합니다."
    )
    handoff: dict[str, Any] = Field(
        default_factory=dict,
        description="확인 기록과 신고 표현·미확인 사항을 분리한 인계용 구조입니다.",
    )
    processing: dict[str, Any] = Field(default_factory=dict)
    input_provenance: dict[str, Any] = Field(
        default_factory=dict,
        description="입력 전사문 hash·문자 수. STT 시간은 분석 지연시간에서 제외합니다.",
    )
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_card_links_and_gate(self) -> "BriefResponse":
        if set(self.confirmation_state) != {"INCIDENT", "FACILITY"}:
            raise ValueError("사고/시설 역할 확인 상태가 필요합니다.")
        if contains_candidate_promotion(
            {
                "facts": self.facts,
                "candidates": self.substance_candidates,
                "discovery": self.discovery,
            }
        ):
            raise ValueError("후보의 자동 확정은 허용되지 않습니다.")
        sources = {item.source_id: item for item in self.sources}
        if len(sources) != len(self.sources):
            raise ValueError("source_id가 중복됩니다.")
        if len({card.card_id for card in self.cards}) != len(self.cards):
            raise ValueError("card_id가 중복됩니다.")
        for card in self.cards:
            phrase = CATALOG.get(card.phrase_id)
            if (
                not phrase
                or any(
                    getattr(card, key) != phrase[key]
                    for key in ("title", "message", "reason", "category", "priority")
                )
                or card.required_conditions != phrase["conditions"]
            ):
                raise ValueError("허용된 버전의 결정적 문구 목록을 벗어났습니다.")
            if (
                card.state_fingerprint != self.state_fingerprint
                or not set(card.source_ids) <= sources.keys()
            ):
                raise ValueError("카드 상태 또는 출처 연결이 유효하지 않습니다.")
            if card.category == "대응 참고":
                if (
                    card.confirmation_status != "CONFIRMED_INPUT"
                    or not self.confirmation_state.get(card.role)
                    or card.unmet_conditions
                ):
                    raise ValueError("대응 참고 조건을 충족하지 못했습니다.")
                if not card.cas_number or not any(
                    sources[key].cas_number == card.cas_number
                    and sources[key].role == card.role
                    and sources[key].source_type != "INTERNAL_POLICY"
                    for key in card.source_ids
                ):
                    raise ValueError("대응 참고의 CAS·역할 출처가 일치하지 않습니다.")
        pair = self.confirmation_state.get("INCIDENT") and self.confirmation_state.get(
            "FACILITY"
        )
        if not pair and (
            self.rule_review.get("executed")
            or any(
                self.rule_review.get(key) is not None
                for key in ("severity", "risk_level", "risk_level_ko")
            )
        ):
            raise ValueError("두 CAS 확인 전 규칙 결과 노출이 금지됩니다.")
        return self
