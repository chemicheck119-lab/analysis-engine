"""백엔드 연동용 REST 입력·출력 계약.

Pydantic 모델은 입력 형태와 길이를 API 경계에서 제한한다. 이름 기반 후보와
대원이 확인한 물질을 다른 타입으로 분리해, 후보가 Rule Engine 입력으로
자동 승격되지 않도록 한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chemiguard119.utils import normalize_cas, valid_cas_checksum


API_SCHEMA_VERSION = "chemiguard119-api-v1"
PUBLIC_SERVICE_NAME = "케미체크119"
CONFIRMATION_GATE_POLICY = "TWO_AUTHENTICATED_ON_SITE_CONFIRMATIONS_REQUIRED"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:-]+$"


def _contains_risk_decision(value: Any) -> bool:
    """확인 전 응답에 위험 확정 필드가 섞였는지 재귀적으로 검사한다."""

    risk_fields = {
        "severity",
        "risk_level",
        "conflict_level",
        "rule_id",
        "hazard_codes",
        "brief_text",
        "required_checks",
        "final_decision",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key in risk_fields and item not in (None, "", [], {}):
                return True
            if _contains_risk_decision(item):
                return True
    elif isinstance(value, list):
        return any(_contains_risk_decision(item) for item in value)
    return False


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IncidentInputType(str, Enum):
    MANUAL_TEXT = "MANUAL_TEXT"
    DISPATCH_TEXT = "DISPATCH_TEXT"
    VOICE_TRANSCRIPT = "VOICE_TRANSCRIPT"
    STRUCTURED_FORM = "STRUCTURED_FORM"


class ConfirmationBasis(str, Enum):
    CONTAINER_LABEL = "CONTAINER_LABEL"
    SITE_MSDS = "SITE_MSDS"
    SHIPPING_DOCUMENT = "SHIPPING_DOCUMENT"
    INSTRUMENT_READING = "INSTRUMENT_READING"
    RESPONDER_OBSERVATION = "RESPONDER_OBSERVATION"
    OTHER_VERIFIED_SOURCE = "OTHER_VERIFIED_SOURCE"


class IncidentInput(StrictModel):
    type: IncidentInputType = IncidentInputType.MANUAL_TEXT
    text: str = Field(min_length=1, max_length=4_000)
    occurred_at: datetime | None = None


class IncidentLocation(StrictModel):
    address: str | None = Field(default=None, max_length=300)
    province: str | None = Field(default=None, max_length=80)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    facility_name: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def coordinates_must_be_a_pair(self) -> "IncidentLocation":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude와 longitude는 함께 입력해야 합니다.")
        return self


class PlannedActionInput(StrictModel):
    raw_text: str = Field(min_length=1, max_length=120)


class ConfirmedSubstanceInput(StrictModel):
    """백엔드가 인증된 대원 확인 레코드에서 전달하는 물질.

    ``confirmation_id``는 사용자 자유입력 ID가 아니라 인증된 백엔드가 보관한
    현장 확인 레코드 식별자여야 한다. 모델 API는 이 레코드 참조와 확인 시각을
    필수로 받으며, 인증되지 않은 후보 입력과 타입 수준에서 분리한다.
    """

    confirmation_id: str = Field(
        min_length=1, max_length=128, pattern=IDENTIFIER_PATTERN
    )
    cas_number: str = Field(min_length=5, max_length=12)
    display_name: str | None = Field(default=None, max_length=160)
    role: Literal["INCIDENT", "FACILITY"]
    presence_status: Literal["CONFIRMED_PRESENT"]
    confirmation_basis: ConfirmationBasis
    observed_at: datetime

    @field_validator("cas_number")
    @classmethod
    def validate_cas(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("CAS 형식 또는 체크디지트가 올바르지 않습니다.")
        return normalized

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "observed_at은 시간대가 포함된 ISO 8601 시각이어야 합니다."
            )
        if value.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(
            minutes=5
        ):
            raise ValueError("observed_at은 허용된 시계 오차보다 미래일 수 없습니다.")
        return value


class IncidentAnalyzeRequest(StrictModel):
    request_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    incident_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=IDENTIFIER_PATTERN,
    )
    input: IncidentInput
    location: IncidentLocation | None = None
    planned_actions: list[PlannedActionInput] = Field(
        default_factory=list, max_length=20
    )
    confirmed_incident_substance: ConfirmedSubstanceInput | None = None
    confirmed_facility_substance: ConfirmedSubstanceInput | None = None
    evidence_top_k: int = Field(default=5, ge=1, le=10)

    @model_validator(mode="after")
    def confirmed_roles_must_match_slots(self) -> "IncidentAnalyzeRequest":
        if (
            self.confirmed_incident_substance
            and self.confirmed_incident_substance.role != "INCIDENT"
        ):
            raise ValueError("confirmed_incident_substance.role은 INCIDENT여야 합니다.")
        if (
            self.confirmed_facility_substance
            and self.confirmed_facility_substance.role != "FACILITY"
        ):
            raise ValueError("confirmed_facility_substance.role은 FACILITY여야 합니다.")
        if (
            self.confirmed_incident_substance
            and self.confirmed_facility_substance
            and self.confirmed_incident_substance.confirmation_id
            == self.confirmed_facility_substance.confirmation_id
        ):
            raise ValueError(
                "사고물질과 시설물질은 서로 다른 confirmation_id가 필요합니다."
            )
        return self

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "request_id": "REQ-20260721-0001",
                    "incident_id": "INC-20260721-0001",
                    "input": {
                        "type": "VOICE_TRANSCRIPT",
                        "text": "차아염소산나트륨 탱크가 누출됐고 옆 저장고에는 염산이 있습니다.",
                        "occurred_at": "2026-07-21T19:20:00+09:00",
                    },
                    "location": {
                        "address": "경기 화성시 팔탄면",
                        "province": "경기도",
                        "latitude": 37.2181,
                        "longitude": 126.9417,
                        "facility_name": "OO전자 공장",
                    },
                    "planned_actions": [{"raw_text": "누출구역 통제"}],
                    "evidence_top_k": 5,
                }
            ]
        },
    )


class ResolveRequest(StrictModel):
    query: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=3, ge=1, le=10)


class EvidenceSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=500)
    cas_hint: str | None = Field(default=None, min_length=5, max_length=12)
    cas_hint_status: Literal["RESPONDER_CONFIRMED", "RESOLVER_CANDIDATE"] | None = None
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("cas_hint")
    @classmethod
    def validate_optional_cas(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("CAS 형식 또는 체크디지트가 올바르지 않습니다.")
        return normalized

    @model_validator(mode="after")
    def hint_status_required_with_hint(self) -> "EvidenceSearchRequest":
        if (self.cas_hint is None) != (self.cas_hint_status is None):
            raise ValueError("cas_hint와 cas_hint_status는 함께 입력해야 합니다.")
        return self


class FacilityHistorySearchRequest(StrictModel):
    query: str = Field(min_length=2, max_length=300)
    province: str | None = Field(default=None, max_length=80)
    top_k: int = Field(default=10, ge=1, le=50)


class ConflictReviewRequest(StrictModel):
    incident: ConfirmedSubstanceInput
    facility: ConfirmedSubstanceInput
    planned_actions: list[PlannedActionInput] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def validate_roles(self) -> "ConflictReviewRequest":
        if self.incident.role != "INCIDENT" or self.facility.role != "FACILITY":
            raise ValueError(
                "incident.role=INCIDENT, facility.role=FACILITY이어야 합니다."
            )
        if self.incident.confirmation_id == self.facility.confirmation_id:
            raise ValueError(
                "사고물질과 시설물질은 서로 다른 confirmation_id가 필요합니다."
            )
        return self


class ConfirmationGateState(StrictModel):
    policy: Literal["TWO_AUTHENTICATED_ON_SITE_CONFIRMATIONS_REQUIRED"] = (
        CONFIRMATION_GATE_POLICY
    )
    incident_confirmed: bool
    facility_confirmed: bool
    all_required_confirmed: bool
    rule_execution_allowed: bool

    @model_validator(mode="after")
    def flags_must_be_consistent(self) -> "ConfirmationGateState":
        expected = self.incident_confirmed and self.facility_confirmed
        if (
            self.all_required_confirmed is not expected
            or self.rule_execution_allowed is not expected
        ):
            raise ValueError("confirmation gate 상태가 일관되지 않습니다.")
        return self


class AnalysisResponse(StrictModel):
    schema_version: Literal["chemiguard119-api-v1"] = API_SCHEMA_VERSION
    analysis_id: str
    request_id: str
    incident_id: str | None = None
    state: str
    input_fingerprint: str
    model_outputs: dict[str, Any]
    evidence: list[dict[str, Any]]
    conflict_review: dict[str, Any]
    confirmation_gate: ConfirmationGateState
    required_next_steps: list[str]
    provenance: dict[str, Any]
    safety_notice: str

    @model_validator(mode="after")
    def unconfirmed_candidates_cannot_publish_risk(self) -> "AnalysisResponse":
        if self.confirmation_gate.all_required_confirmed:
            return self
        if self.conflict_review.get("executed") is True:
            raise ValueError("두 현장 확인 레코드 없이 충돌 검토를 실행할 수 없습니다.")
        if _contains_risk_decision(
            {
                "conflict_review": self.conflict_review,
                "model_outputs": self.model_outputs,
            }
        ):
            raise ValueError(
                "현장 미확인 후보 응답에는 위험도·충돌 확정값을 포함할 수 없습니다."
            )
        if self.state == "COMPLETED":
            raise ValueError("현장 미확인 후보 응답은 COMPLETED 상태일 수 없습니다.")
        return self


class ErrorDetail(StrictModel):
    code: str
    message: str
    retryable: bool
    fields: list[str] = Field(default_factory=list)


class ErrorResponse(StrictModel):
    schema_version: Literal["chemiguard119-api-v1"] = API_SCHEMA_VERSION
    service_name: Literal["케미체크119"] = PUBLIC_SERVICE_NAME
    error: ErrorDetail
    request_id: str
    occurred_at_utc: datetime


__all__ = [
    "API_SCHEMA_VERSION",
    "AnalysisResponse",
    "CONFIRMATION_GATE_POLICY",
    "ConfirmationGateState",
    "ConflictReviewRequest",
    "ConfirmedSubstanceInput",
    "ErrorResponse",
    "EvidenceSearchRequest",
    "FacilityHistorySearchRequest",
    "IncidentAnalyzeRequest",
    "PUBLIC_SERVICE_NAME",
    "ResolveRequest",
]
