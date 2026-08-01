"""백엔드 연동용 REST 입력·출력 계약.

Pydantic 모델은 입력 형태와 길이를 API 경계에서 제한한다. 이름 기반 후보와
대원이 확인한 물질을 다른 타입으로 분리해, 후보가 Rule Engine 입력으로
자동 승격되지 않도록 한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from chemiguard119.rules import validate_review_output
from chemiguard119.operations import OperationsAgentSnapshot, OperationsContext
from chemiguard119.utils import normalize_cas, valid_cas_checksum


API_SCHEMA_VERSION = "chemiguard119-api-v1"
PUBLIC_SERVICE_NAME = "케미체크119"
CONFIRMATION_GATE_POLICY = "TWO_AUTHENTICATED_ON_SITE_CONFIRMATIONS_REQUIRED"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9_.:-]+$"
AnalysisState = Literal[
    "AWAITING_SUBSTANCE_CONFIRMATION",
    "AWAITING_INCIDENT_CONFIRMATION",
    "AWAITING_FACILITY_CONFIRMATION",
    "COMPLETED",
    "SCREENING_COMPLETED",
    "VERIFY_REQUIRED",
    "UNCLASSIFIED",
    "CAMEO_GROUP_SCREENING_ONLY",
]
CompletedReviewStatus = Literal["COMPLETED", "SCREENING_COMPLETED"]
InconclusiveReviewStatus = Literal[
    "VERIFY_REQUIRED",
    "UNCLASSIFIED",
    "CAMEO_GROUP_SCREENING_ONLY",
]
RulePolicy = Literal["APPROVED_ONLY", "PUBLIC_SOURCE_PILOT_V1"]

_CANDIDATE_FALSE_ONLY_FIELDS = {
    "current_inventory_confirmed",
    "on_site_presence_confirmed",
    "risk_determination_allowed",
    "rule_eligible",
    "rule_input_eligible",
}
_CANDIDATE_TRUE_ONLY_FIELDS = {
    "requires_on_site_confirmation",
    "requires_responder_confirmation",
}
_CANDIDATE_CONFIRMED_MARKERS = {
    ("cas_basis", "RESPONDER_CONFIRMED"),
    ("confirmation", "RESPONDER_CONFIRMED"),
    ("presence_status", "CONFIRMED_PRESENT"),
}


def contains_unconfirmed_risk_output(value: Any) -> bool:
    """확인 전 응답에 위험 확정 필드가 섞였는지 재귀적으로 검사한다."""

    risk_fields = {
        "brief_text",
        "concrete_risk",
        "conflict_level",
        "expected_response",
        "expert_reviewed",
        "final_decision",
        "hazard_codes",
        "hazard_summary",
        "is_probability",
        "priority_checks",
        "probability",
        "probability_percent",
        "raw_class_id",
        "reaction",
        "reactions",
        "recommended_actions",
        "recommended_response",
        "required_checks",
        "response_actions",
        "risk_level_en",
        "risk_level_ko",
        "risk_scale",
        "severity",
        "risk_level",
        "scope",
        "specific_risk",
        "rule_id",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key in risk_fields:
                safe_false_marker = (
                    key
                    in {
                        "expert_reviewed",
                        "is_probability",
                    }
                    and item is False
                )
                if not safe_false_marker and item not in (None, "", [], {}):
                    return True
            if contains_unconfirmed_risk_output(item):
                return True
    elif isinstance(value, list):
        return any(contains_unconfirmed_risk_output(item) for item in value)
    return False


def contains_candidate_promotion(value: Any) -> bool:
    """검색 후보가 현장 확인·Rule 실행 가능 상태로 위조됐는지 검사한다."""

    if isinstance(value, dict):
        for key, item in value.items():
            if key in _CANDIDATE_FALSE_ONLY_FIELDS and item is not False:
                return True
            if key in _CANDIDATE_TRUE_ONLY_FIELDS and item is not True:
                return True
            if (
                isinstance(item, str)
                and (
                    key,
                    item,
                )
                in _CANDIDATE_CONFIRMED_MARKERS
            ):
                return True
            if contains_candidate_promotion(item):
                return True
    elif isinstance(value, list):
        return any(contains_candidate_promotion(item) for item in value)
    return False


def contains_probability_risk_output(value: Any) -> bool:
    """확률이 아닌 서수 위험등급에 확률 표현이 섞였는지 검사한다."""

    if isinstance(value, dict):
        for key, item in value.items():
            if key == "is_probability" and item is not False:
                return True
            if key in {"probability", "probability_percent"} and item is not None:
                return True
            if contains_probability_risk_output(item):
                return True
    elif isinstance(value, list):
        return any(contains_probability_risk_output(item) for item in value)
    return False


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UnconfirmedConflictReview(StrictModel):
    """두 현장 확인 전 공개할 수 있는 충돌 검토 보류 상태의 전부."""

    executed: Literal[False]
    status: Literal["NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"]
    gate: Literal["BOTH_CAS_RESPONDER_CONFIRMED"]
    missing_confirmations: list[Literal["incident_cas", "facility_cas"]] = Field(
        min_length=1,
        max_length=2,
    )
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("missing_confirmations")
    @classmethod
    def missing_confirmations_must_be_unique(
        cls,
        value: list[Literal["incident_cas", "facility_cas"]],
    ) -> list[Literal["incident_cas", "facility_cas"]]:
        if len(value) != len(set(value)):
            raise ValueError("missing_confirmations에는 중복 역할을 넣을 수 없습니다.")
        return value


class InconclusiveConflictResult(StrictModel):
    """Rule은 실행됐지만 위험등급을 확정하지 못한 결과의 허용 목록."""

    status: InconclusiveReviewStatus
    severity: None
    human_confirmation_required: Literal[True]
    reason: str | None = Field(default=None, max_length=1_000)
    rule_id: str | None = Field(default=None, max_length=200)
    approval_status: str | None = Field(default=None, max_length=100)
    hint: str | None = Field(default=None, max_length=1_000)
    configuration_error: str | None = Field(default=None, max_length=200)
    policy_mode: RulePolicy | None = None
    expert_reviewed: Literal[False] | None = None
    mapping_statuses: list[str] | None = None
    mapping_provenance_errors: dict[str, list[str]] | None = None
    mapping_provenance: list[dict[str, Any]] | None = None
    evidence_provenance: dict[str, Any] | None = None
    reference_assurance: dict[str, Any] | None = None
    screening: list[dict[str, Any]] | None = None
    cameo_group_screening: list[dict[str, Any]] | None = None
    unsupported_compatibility_class_ids: list[str] | None = None
    ignored_direct_rule_ids: list[str] | None = None
    scope: Literal["SIMULATED_PROTOTYPE", "APPROVED_MAPPING_ONLY"] | None = None


class OrdinalRiskScale(StrictModel):
    type: Literal[
        "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
        "ORDINAL_RULE_CLASSIFICATION",
    ]
    raw_class_id: int | None = None
    is_probability: Literal[False]
    probability_percent: None


class UnvalidatedPlannedAction(StrictModel):
    raw_text: str = Field(min_length=1, max_length=120)
    status: Literal["UNVALIDATED_ACTION_INPUT"]


class CompletedConflictResult(StrictModel):
    """운영 API에서 공개할 수 있는 완료 Rule 결과의 전체 허용 필드."""

    status: CompletedReviewStatus
    scope: Literal[
        "APPROVED",
        "APPROVED_CAMEO_GROUP_SCREENING",
        "PUBLIC_SOURCE_CAMEO_SCREENING",
    ]
    incident_cas: str
    facility_cas: str
    rule_id: str = Field(min_length=1, max_length=240)
    rule_version: str = Field(min_length=1, max_length=160)
    severity: str = Field(min_length=1, max_length=80)
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    risk_level_ko: Literal["낮음", "중간", "높음"]
    risk_scale: OrdinalRiskScale
    hazard_codes: list[str]
    gas_products: list[str] | None = None
    brief_text: str = Field(min_length=1, max_length=2_000)
    required_checks: list[str]
    evidence_urls: list[str] = Field(min_length=1)
    cameo_group_screening: list[dict[str, Any]] = Field(default_factory=list)
    planned_actions: list[UnvalidatedPlannedAction] = Field(default_factory=list)
    limitations: list[str]
    final_decision: Literal["현장 지휘관 판단"]
    human_confirmation_required: Literal[True]
    policy_mode: RulePolicy | None = None
    expert_reviewed: Literal[False] | None = None
    mapping_provenance: list[dict[str, Any]] | None = None
    evidence_provenance: dict[str, Any] | None = None
    reference_assurance: dict[str, Any] | None = None
    ignored_direct_rule_ids: list[str] | None = None


class ExecutedConflictReview(StrictModel):
    """두 CAS 확인 후 실행된 Rule 결과 wrapper."""

    executed: Literal[True]
    status: CompletedReviewStatus | InconclusiveReviewStatus
    gate: Literal["BOTH_CAS_RESPONDER_CONFIRMED"]
    policy_mode: RulePolicy
    result: dict[str, Any]

    @model_validator(mode="after")
    def result_must_match_status_and_safety_contract(
        self,
    ) -> "ExecutedConflictReview":
        if self.result.get("status") != self.status:
            raise ValueError("Rule wrapper와 result 상태가 일치하지 않습니다.")
        result_policy = self.result.get("policy_mode")
        if result_policy is not None and result_policy != self.policy_mode:
            raise ValueError("Rule wrapper와 result 정책이 일치하지 않습니다.")
        if contains_probability_risk_output(self.result):
            raise ValueError("충돌 위험은 확률 또는 퍼센트로 표시할 수 없습니다.")
        if self.status in {"COMPLETED", "SCREENING_COMPLETED"}:
            try:
                completed = CompletedConflictResult.model_validate(self.result)
            except ValidationError as error:
                raise ValueError(
                    "완료된 Rule 결과에 비허용 또는 누락 필드가 있습니다."
                ) from error
            if self.policy_mode == "PUBLIC_SOURCE_PILOT_V1":
                if (
                    completed.status != "SCREENING_COMPLETED"
                    or completed.scope != "PUBLIC_SOURCE_CAMEO_SCREENING"
                    or completed.policy_mode != "PUBLIC_SOURCE_PILOT_V1"
                    or completed.expert_reviewed is not False
                ):
                    raise ValueError(
                        "공개근거 정책은 검증된 screening 완료 결과만 공개할 수 있습니다."
                    )
            elif (
                completed.status != "COMPLETED"
                or completed.scope not in {"APPROVED", "APPROVED_CAMEO_GROUP_SCREENING"}
                or completed.policy_mode not in {None, "APPROVED_ONLY"}
            ):
                raise ValueError(
                    "승인 전용 정책은 APPROVED 완료 결과만 공개할 수 있습니다."
                )
            errors = validate_review_output(self.result)
            if errors:
                raise ValueError("완료된 Rule 결과가 안전 계약을 만족하지 않습니다.")
        else:
            try:
                InconclusiveConflictResult.model_validate(self.result)
            except ValidationError as error:
                raise ValueError(
                    "미분류 Rule 결과에는 위험등급·대응 확정값을 포함할 수 없습니다."
                ) from error
        return self


ConflictReviewContract = Annotated[
    UnconfirmedConflictReview | ExecutedConflictReview,
    Field(discriminator="executed"),
]


def analysis_state_for_review_status(status: str) -> AnalysisState:
    """Rule 결과 상태를 공개 분석 상태로 정규화한다."""

    if status == "COMPLETED":
        return "COMPLETED"
    allowed: set[str] = {
        "SCREENING_COMPLETED",
        "VERIFY_REQUIRED",
        "UNCLASSIFIED",
        "CAMEO_GROUP_SCREENING_ONLY",
    }
    if status not in allowed:
        raise ValueError("지원하지 않는 Rule 결과 상태입니다.")
    return status  # type: ignore[return-value]


def validate_evidence_confirmation_gate(
    evidence: list[dict[str, Any]],
    *,
    incident_confirmed: bool,
    facility_confirmed: bool,
    incident_cas: str | None = None,
    facility_cas: str | None = None,
) -> list[str]:
    """근거 검색의 CAS 출처가 실제 confirmation gate와 일치하는지 검사한다."""

    errors: list[str] = []
    confirmed_by_role = {
        "INCIDENT": incident_confirmed,
        "FACILITY": facility_confirmed,
    }
    confirmed_cas_by_role = {
        "INCIDENT": normalize_cas(incident_cas) if incident_cas else None,
        "FACILITY": normalize_cas(facility_cas) if facility_cas else None,
    }
    seen_roles: set[str] = set()
    for index, item in enumerate(evidence):
        prefix = f"evidence[{index}]"
        if contains_unconfirmed_risk_output(item):
            errors.append(f"{prefix}에 충돌 위험 또는 대응 확정 필드가 있습니다.")
        basis = item.get("cas_basis")
        role = item.get("role")
        requires_confirmation = item.get("requires_responder_confirmation")
        if basis == "RESPONDER_CONFIRMED":
            if role not in confirmed_by_role or not confirmed_by_role[role]:
                errors.append(f"{prefix}의 확인 CAS 역할이 gate와 일치하지 않습니다.")
            if requires_confirmation is not False:
                errors.append(f"{prefix}의 확인 완료 표시가 일치하지 않습니다.")
        elif basis == "PARSER_CANDIDATE":
            if role not in confirmed_by_role:
                errors.append(f"{prefix}의 Parser 후보 역할이 올바르지 않습니다.")
            elif confirmed_by_role[role]:
                errors.append(
                    f"{prefix}의 확인 완료 역할을 Parser 후보로 낮출 수 없습니다."
                )
            if requires_confirmation is not True:
                errors.append(f"{prefix}의 Parser 후보 확인 필요 표시가 없습니다.")
        elif basis == "NO_CAS_HINT":
            if (
                role != "UNKNOWN"
                or incident_confirmed
                or facility_confirmed
                or item.get("cas_hint") is not None
                or requires_confirmation is not True
            ):
                errors.append(f"{prefix}의 CAS 미지정 상태가 올바르지 않습니다.")
        else:
            errors.append(f"{prefix}의 cas_basis가 지원되지 않습니다.")

        if role in confirmed_by_role:
            if role in seen_roles:
                errors.append(f"{prefix}에 역할별 근거 검색이 중복되었습니다.")
            seen_roles.add(role)

        cas_hint = item.get("cas_hint")
        retrieval = item.get("retrieval")
        if basis in {"RESPONDER_CONFIRMED", "PARSER_CANDIDATE"}:
            normalized_hint = normalize_cas(str(cas_hint or ""))
            if not valid_cas_checksum(normalized_hint):
                errors.append(f"{prefix}의 CAS hint가 유효하지 않습니다.")
                continue
            if (
                basis == "RESPONDER_CONFIRMED"
                and role in confirmed_cas_by_role
                and confirmed_cas_by_role[role] is not None
                and normalized_hint != confirmed_cas_by_role[role]
            ):
                errors.append(f"{prefix}의 CAS hint가 현장 확인 CAS와 다릅니다.")
            if not isinstance(retrieval, dict):
                errors.append(f"{prefix}의 retrieval 객체가 없습니다.")
                continue
            retrieval_hint = normalize_cas(str(retrieval.get("cas_hint") or ""))
            if retrieval_hint != normalized_hint:
                errors.append(f"{prefix}의 retrieval CAS가 hint와 다릅니다.")
            results = retrieval.get("results")
            if not isinstance(results, list):
                errors.append(f"{prefix}의 retrieval.results 형식이 올바르지 않습니다.")
            elif any(
                not isinstance(result, dict)
                or normalize_cas(str(result.get("cas_number") or "")) != normalized_hint
                for result in results
            ):
                errors.append(f"{prefix}에 다른 CAS 근거가 포함되었습니다.")
        elif basis == "NO_CAS_HINT" and isinstance(retrieval, dict):
            if retrieval.get("cas_hint") is not None:
                errors.append(f"{prefix}의 CAS 미지정 retrieval에 hint가 있습니다.")
    return errors


def validate_provenance_confirmation_gate(
    provenance: dict[str, Any],
    *,
    incident_confirmed: bool,
    facility_confirmed: bool,
) -> list[str]:
    """provenance가 확인·의사결정 보조 상태를 거짓으로 승격하지 않는지 검사한다."""

    errors: list[str] = []
    if (
        "decision_support_only" in provenance
        and provenance.get("decision_support_only") is not True
    ):
        errors.append("provenance.decision_support_only는 true여야 합니다.")
    if (
        "responder_confirmation_required" in provenance
        and provenance.get("responder_confirmation_required") is not True
    ):
        errors.append("provenance.responder_confirmation_required는 true여야 합니다.")
    if (
        not (incident_confirmed and facility_confirmed)
        and provenance.get("expert_reviewed") is not False
    ):
        errors.append("미확인 응답의 expert_reviewed는 false여야 합니다.")

    confirmations = provenance.get("confirmations")
    if confirmations is not None:
        if not isinstance(confirmations, dict):
            errors.append("provenance.confirmations는 객체여야 합니다.")
        else:
            if ("incident" in confirmations) != incident_confirmed:
                errors.append("사고물질 confirmation provenance가 gate와 다릅니다.")
            if ("facility" in confirmations) != facility_confirmed:
                errors.append("시설물질 confirmation provenance가 gate와 다릅니다.")
    return errors


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
    coordinate_source: (
        Literal[
            "DISPATCH_SYSTEM",
            "GEOCODING_PROVIDER",
            "RESPONDER_OBSERVATION",
            "MANUAL_ENTRY",
            "DEMO_FIXTURE",
        ]
        | None
    ) = None
    geocoding_provider: str | None = Field(default=None, max_length=80)
    resolved_at: datetime | None = None

    @model_validator(mode="after")
    def coordinates_must_be_a_pair(self) -> "IncidentLocation":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude와 longitude는 함께 입력해야 합니다.")
        if self.coordinate_source is not None and self.latitude is None:
            raise ValueError("coordinate_source에는 위도·경도가 필요합니다.")
        if self.geocoding_provider is not None and self.coordinate_source != (
            "GEOCODING_PROVIDER"
        ):
            raise ValueError(
                "geocoding_provider는 GEOCODING_PROVIDER 좌표에만 사용할 수 있습니다."
            )
        if self.resolved_at is not None:
            if self.latitude is None:
                raise ValueError("resolved_at에는 위도·경도가 필요합니다.")
            if self.resolved_at.tzinfo is None or self.resolved_at.utcoffset() is None:
                raise ValueError("resolved_at에는 시간대가 필요합니다.")
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
    operations_context: OperationsContext | None = None

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
                        "coordinate_source": "DISPATCH_SYSTEM",
                        "resolved_at": "2026-07-21T19:20:00+09:00",
                    },
                    "operations_context": {
                        "dispatch_station_name": "화성소방서",
                        "journey_state": "EN_ROUTE",
                        "responder_position": {
                            "latitude": 37.2065,
                            "longitude": 126.8311,
                            "observed_at": "2026-07-21T19:22:00+09:00",
                            "source": "MDT_DEVICE_GPS",
                            "accuracy_m": 12.0,
                        },
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


class SubstanceDiscoveryRequest(StrictModel):
    """물질명·CAS·관찰 정보를 후보와 공식 근거 카드로 변환하는 요청."""

    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=5)
    evidence_top_k: int = Field(default=3, ge=1, le=5)


class MatchedProperty(StrictModel):
    field: Literal["physical_state", "color", "odor", "use_description"]
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=2_000)


class SubstancePropertyProfile(StrictModel):
    physical_state: str = Field(max_length=2_000)
    color: str = Field(max_length=2_000)
    odor: str = Field(max_length=2_000)
    use_description: str = Field(max_length=8_000)
    source_id: Literal["NFA_ULSAN_CHEMICAL_INFORMATION"]
    source_url: str = Field(min_length=1, max_length=2_000)
    document_version: str = Field(max_length=500)


class SubstanceEvidenceCard(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=500)
    cas_number: str = Field(min_length=5, max_length=12)
    source: Literal["KOSHA", "CAMEO"]
    title: str = Field(min_length=1, max_length=2_000)
    body_preview: str = Field(max_length=2_000)
    source_url: str = Field(min_length=1, max_length=2_000)
    document_version: str = Field(max_length=500)
    cas_link_status: str | None = Field(default=None, max_length=120)

    @field_validator("cas_number")
    @classmethod
    def validate_evidence_cas(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("근거 카드의 CAS가 올바르지 않습니다.")
        return normalized


class SubstanceDiscoveryCandidate(StrictModel):
    rank: int = Field(ge=1, le=5)
    cas_number: str = Field(min_length=5, max_length=12)
    display_name: str = Field(min_length=1, max_length=500)
    match_basis: Literal[
        "IDENTITY_EXPRESSION",
        "PUBLIC_PROPERTY_PROFILE",
        "IDENTITY_AND_PUBLIC_PROPERTY_PROFILE",
    ]
    matched_expression: str | None = Field(default=None, max_length=500)
    matched_properties: list[MatchedProperty] = Field(default_factory=list)
    property_profile: SubstancePropertyProfile | None = None
    evidence_status: str = Field(min_length=1, max_length=120)
    evidence_warning: str | None = Field(default=None, max_length=2_000)
    evidence_notice: str | None = Field(default=None, max_length=2_000)
    cas_link_warning: str | None = Field(default=None, max_length=2_000)
    evidence: list[SubstanceEvidenceCard] = Field(default_factory=list, max_length=5)
    requires_responder_confirmation: Literal[True]
    rule_eligible: Literal[False]
    risk_determination_allowed: Literal[False]

    @field_validator("cas_number")
    @classmethod
    def validate_candidate_cas(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("물질 후보의 CAS가 올바르지 않습니다.")
        return normalized

    @model_validator(mode="after")
    def evidence_must_match_candidate_cas(self) -> "SubstanceDiscoveryCandidate":
        if any(item.cas_number != self.cas_number for item in self.evidence):
            raise ValueError("근거 카드의 CAS는 물질 후보 CAS와 같아야 합니다.")
        return self


class SubstanceDiscoveryResponse(StrictModel):
    schema_version: Literal["chemiguard119-api-v1"] = API_SCHEMA_VERSION
    query: str = Field(min_length=2, max_length=500)
    status: Literal[
        "CANDIDATES_FOUND",
        "NO_RELIABLE_CANDIDATE",
        "PROFILE_INDEX_NOT_AVAILABLE",
    ]
    search_mode: Literal[
        "IDENTITY_AND_PROPERTY_RETRIEVAL",
        "IDENTITY_RETRIEVAL",
        "PROPERTY_PROFILE_RETRIEVAL",
        "ABSTAINED",
    ]
    method: str = Field(min_length=1, max_length=1_000)
    profile_index_available: bool
    candidates: list[SubstanceDiscoveryCandidate] = Field(
        default_factory=list,
        max_length=5,
    )
    requires_responder_confirmation: Literal[True]
    rule_eligible: Literal[False]
    risk_determination_allowed: Literal[False]
    candidate_score_is_probability: Literal[False]
    notice: str = Field(min_length=1, max_length=2_000)
    safety_notice: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def candidates_must_remain_unconfirmed(self) -> "SubstanceDiscoveryResponse":
        if self.status == "CANDIDATES_FOUND" and not self.candidates:
            raise ValueError("CANDIDATES_FOUND에는 후보가 필요합니다.")
        if self.status != "CANDIDATES_FOUND" and self.candidates:
            raise ValueError("후보 없음 상태에는 candidates가 비어 있어야 합니다.")
        if contains_candidate_promotion(self.model_dump()):
            raise ValueError("물질 탐색 후보를 현장 확인 상태로 승격할 수 없습니다.")
        if contains_unconfirmed_risk_output(self.model_dump()):
            raise ValueError("물질 탐색 응답에 위험도·충돌 판정을 포함할 수 없습니다.")
        return self


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


RagStatus = Literal[
    "COMPLETED",
    "FALLBACK_EXTRACTIVE",
    "DISABLED",
    "NO_GROUNDED_EVIDENCE",
    "NOT_RUN_REQUIRES_CONFIRMED_PAIR",
    "NOT_RUN_RULE_NOT_COMPLETED",
]


class GroundedRagStatement(StrictModel):
    text: str = Field(min_length=1, max_length=600)
    source_ids: list[str] = Field(min_length=1, max_length=3)


class GroundedRagCitation(StrictModel):
    source_id: str = Field(min_length=1, max_length=500)
    source_type: Literal["KOSHA", "CAMEO", "CAMEO_RULE_ENGINE"]
    title: str = Field(min_length=1, max_length=500)
    cas_number: str | None = Field(default=None, max_length=12)
    source_urls: list[str] = Field(min_length=1, max_length=5)


class GroundedRagCitationValidation(StrictModel):
    passed: Literal[True]
    unknown_source_ids: list[str] = Field(default_factory=list, max_length=0)


class GroundedRagAnswer(StrictModel):
    """위험 판단과 분리된, 인용 ID 검증형 RAG 설명 계약."""

    schema_version: Literal["chemicheck119-grounded-rag-v1"]
    status: RagStatus
    mode: Literal["off", "extractive", "llm"]
    used_llm: bool
    model: str | None = Field(default=None, max_length=300)
    statements: list[GroundedRagStatement] = Field(default_factory=list, max_length=5)
    citations: list[GroundedRagCitation] = Field(default_factory=list, max_length=7)
    citation_validation: GroundedRagCitationValidation
    risk_decision_source: Literal["DETERMINISTIC_CAMEO_RULE_ENGINE"]
    semantic_grounding_verified: Literal[False]
    fallback_reason: str | None = Field(default=None, max_length=120)
    latency_ms: float = Field(ge=0)
    limitations: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def citations_and_execution_state_must_be_consistent(self) -> "GroundedRagAnswer":
        citation_ids = {item.source_id for item in self.citations}
        statement_ids = {
            source_id for item in self.statements for source_id in item.source_ids
        }
        if statement_ids - citation_ids:
            raise ValueError("RAG 문장이 응답에 없는 source_id를 인용했습니다.")
        if self.status == "COMPLETED":
            if not self.used_llm or not self.model or not self.statements:
                raise ValueError("COMPLETED RAG에는 LLM·모델·문장이 필요합니다.")
        elif self.status == "FALLBACK_EXTRACTIVE":
            if self.used_llm or not self.statements or not self.fallback_reason:
                raise ValueError("extractive fallback 상태가 일관되지 않습니다.")
        elif self.used_llm or self.model or self.statements or self.citations:
            raise ValueError("RAG 미실행 상태에는 모델·문장·인용을 포함할 수 없습니다.")
        return self


class AnalysisResponse(StrictModel):
    schema_version: Literal["chemiguard119-api-v1"] = API_SCHEMA_VERSION
    analysis_id: str
    request_id: str
    incident_id: str | None = None
    state: AnalysisState
    input_fingerprint: str
    model_outputs: dict[str, Any]
    evidence: list[dict[str, Any]]
    grounded_rag: GroundedRagAnswer | None = None
    # 기존 v1 저장 응답도 검증할 수 있도록 계약상 선택 필드로 유지한다.
    # 현재 FastAPI 구현은 모든 신규 분석에 snapshot을 항상 채운다.
    agent: OperationsAgentSnapshot | None = None
    conflict_review: ConflictReviewContract
    confirmation_gate: ConfirmationGateState
    required_next_steps: list[str]
    provenance: dict[str, Any]
    safety_notice: str

    @model_validator(mode="after")
    def output_must_match_confirmation_and_rule_gate(self) -> "AnalysisResponse":
        incident_confirmed = self.confirmation_gate.incident_confirmed
        facility_confirmed = self.confirmation_gate.facility_confirmed
        if self.grounded_rag is not None:
            if not self.confirmation_gate.all_required_confirmed:
                if self.grounded_rag.status != "NOT_RUN_REQUIRES_CONFIRMED_PAIR":
                    raise ValueError(
                        "현장 확인 전에는 RAG 대응 요약을 실행할 수 없습니다."
                    )
            elif isinstance(self.conflict_review, ExecutedConflictReview):
                completed = self.conflict_review.status in {
                    "COMPLETED",
                    "SCREENING_COMPLETED",
                }
                if (
                    completed
                    and self.grounded_rag.status == "NOT_RUN_RULE_NOT_COMPLETED"
                ) or (
                    not completed
                    and self.grounded_rag.status != "NOT_RUN_RULE_NOT_COMPLETED"
                ):
                    raise ValueError("RAG 상태가 Rule 완료 상태와 일치하지 않습니다.")
        evidence_errors = validate_evidence_confirmation_gate(
            self.evidence,
            incident_confirmed=incident_confirmed,
            facility_confirmed=facility_confirmed,
        )
        provenance_errors = validate_provenance_confirmation_gate(
            self.provenance,
            incident_confirmed=incident_confirmed,
            facility_confirmed=facility_confirmed,
        )
        if evidence_errors or provenance_errors:
            raise ValueError(
                "근거·provenance가 confirmation gate 안전 계약과 일치하지 않습니다."
            )
        if contains_candidate_promotion(self.model_outputs):
            raise ValueError(
                "검색 후보를 현장 확인 또는 Rule 실행 가능 상태로 승격할 수 없습니다."
            )
        if contains_unconfirmed_risk_output(self.model_outputs):
            raise ValueError(
                "위험도·충돌 판정은 conflict_review 밖에 포함할 수 없습니다."
            )

        if self.confirmation_gate.all_required_confirmed:
            if not isinstance(self.conflict_review, ExecutedConflictReview):
                raise ValueError("두 CAS 확인 후에는 Rule 실행 결과가 필요합니다.")
            expected_state = analysis_state_for_review_status(
                self.conflict_review.status
            )
            if self.state != expected_state:
                raise ValueError(
                    "확인 완료 응답 상태가 Rule 결과 상태와 일치하지 않습니다."
                )
            return self

        expected_state = {
            (False, False): "AWAITING_SUBSTANCE_CONFIRMATION",
            (True, False): "AWAITING_FACILITY_CONFIRMATION",
            (False, True): "AWAITING_INCIDENT_CONFIRMATION",
        }[
            (
                incident_confirmed,
                facility_confirmed,
            )
        ]
        if self.state != expected_state:
            raise ValueError(
                "현장 미확인 응답 상태가 confirmation gate와 일치하지 않습니다."
            )
        if not isinstance(self.conflict_review, UnconfirmedConflictReview):
            raise ValueError(
                "현장 미확인 conflict_review는 엄격한 보류 상태만 포함할 수 있습니다."
            )
        expected_missing = {
            role
            for role, confirmed in (
                ("incident_cas", incident_confirmed),
                ("facility_cas", facility_confirmed),
            )
            if not confirmed
        }
        if set(self.conflict_review.missing_confirmations) != expected_missing:
            raise ValueError(
                "conflict_review의 누락 확인 역할이 confirmation gate와 일치하지 않습니다."
            )
        if contains_unconfirmed_risk_output(
            {
                "conflict_review": self.conflict_review.model_dump(),
                "evidence": self.evidence,
                "provenance": self.provenance,
            }
        ):
            raise ValueError(
                "현장 미확인 후보 응답에는 위험도·충돌 확정값을 포함할 수 없습니다."
            )
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
    "AnalysisState",
    "AnalysisResponse",
    "CompletedConflictResult",
    "ConflictReviewContract",
    "CONFIRMATION_GATE_POLICY",
    "ConfirmationGateState",
    "ConflictReviewRequest",
    "ConfirmedSubstanceInput",
    "ErrorResponse",
    "ExecutedConflictReview",
    "GroundedRagAnswer",
    "GroundedRagCitation",
    "GroundedRagStatement",
    "EvidenceSearchRequest",
    "FacilityHistorySearchRequest",
    "InconclusiveConflictResult",
    "IncidentAnalyzeRequest",
    "OrdinalRiskScale",
    "PUBLIC_SERVICE_NAME",
    "ResolveRequest",
    "SubstanceDiscoveryRequest",
    "SubstanceDiscoveryResponse",
    "UnconfirmedConflictReview",
    "UnvalidatedPlannedAction",
    "analysis_state_for_review_status",
    "contains_candidate_promotion",
    "contains_probability_risk_output",
    "contains_unconfirmed_risk_output",
    "validate_evidence_confirmation_gate",
    "validate_provenance_confirmation_gate",
]
