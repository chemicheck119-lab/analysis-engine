"""FE가 소비할 서비스 BE/BFF 계약.

이 모듈은 모델 API에 새 라우트를 노출하지 않는다. ``FE_Repository``가
``BE_Repository``를 통해 모델 결과를 안전하게 소비하도록, camelCase DTO와
계약 전용 OpenAPI를 실행 가능한 Pydantic 모델로 고정한다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import FastAPI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from chemiguard119.api_models import API_SCHEMA_VERSION, CompletedConflictResult
from chemiguard119.evidence_assurance import build_reference_assurance
from chemiguard119.operations import OperationsAgentSnapshot, OperationsContext
from chemiguard119.paths import CONFIG_DIR
from chemiguard119.rules import validate_review_output
from chemiguard119.utils import normalize_cas, sha256_file, valid_cas_checksum


DASHBOARD_BFF_SCHEMA_VERSION = "chemicheck119-dashboard-bff-v1"
DASHBOARD_PUBLIC_PAIR_CONTRACT_VERSION = "dashboard-public-pair-presentation-v1"
FACILITY_HISTORY_LABEL = "과거 공개 이력 기반 시설물질 후보"
FACILITY_HISTORY_SEMANTICS = "HISTORICAL_CANDIDATE_NOT_CURRENT_INVENTORY"
PUBLIC_CAMEO_RULE_ID = "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX"
PUBLIC_CAMEO_RULE_VERSION = "RUNTIME_MANIFEST_PINNED"
PUBLIC_CAMEO_REACTIVITY_URL = "https://cameochemicals.noaa.gov/reactivity"
PublicHazardCode = Literal["C", "E", "F", "G", "NR", "R1", "R2", "R3", "T"]
PublicGasProduct = Literal[
    "Acid Fumes",
    "BR2",
    "CO",
    "CO2",
    "Cl2",
    "ClO2",
    "H2",
    "HX",
    "Halocarbons",
    "Hydrocarbons",
    "NOx",
    "O2",
    "X2",
    "X2O",
    "XO2",
]


@lru_cache(maxsize=1)
def _dashboard_public_pair_index() -> dict[str, dict[str, Any]]:
    """공개검증 15쌍의 표시 계약을 읽고 중복·버전 오류에는 fail-closed 한다."""

    path = CONFIG_DIR / "dashboard_public_pair_contract.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("대시보드 공개검증 pair 계약을 읽을 수 없습니다.") from exc

    if payload.get("contract_version") != DASHBOARD_PUBLIC_PAIR_CONTRACT_VERSION:
        raise ValueError("대시보드 공개검증 pair 계약 버전이 올바르지 않습니다.")
    if payload.get("is_probability") is not False:
        raise ValueError("대시보드 공개검증 pair 계약은 확률 계약일 수 없습니다.")

    pairs = payload.get("pairs")
    if not isinstance(pairs, list) or payload.get("pair_count") != len(pairs):
        raise ValueError("대시보드 공개검증 pair 계약의 pair 수가 올바르지 않습니다.")

    index: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("대시보드 공개검증 pair 항목 형식이 올바르지 않습니다.")
        cas_numbers = pair.get("cas_numbers")
        if not isinstance(cas_numbers, list) or len(cas_numbers) != 2:
            raise ValueError("대시보드 공개검증 pair CAS 형식이 올바르지 않습니다.")
        pair_key = "|".join(sorted(str(item) for item in cas_numbers))
        if pair.get("pair_key") != pair_key or pair_key in index:
            raise ValueError("대시보드 공개검증 pair key가 중복되거나 잘못되었습니다.")
        index[pair_key] = pair
    return index


def _to_camel(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(item.capitalize() for item in rest)


class DashboardModel(BaseModel):
    """BFF DTO 공통 설정."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        alias_generator=_to_camel,
        populate_by_name=True,
    )


class DashboardErrorDetail(DashboardModel):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1_000)
    retryable: bool


class DashboardErrorResponse(DashboardModel):
    schema_version: Literal[DASHBOARD_BFF_SCHEMA_VERSION] = DASHBOARD_BFF_SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    error: DashboardErrorDetail
    reset_allowed: Literal[False] = False


class DashboardMatchedProperty(DashboardModel):
    field: Literal["physical_state", "color", "odor", "use_description"]
    label: str = Field(min_length=1, max_length=40)
    value: str = Field(min_length=1, max_length=2_000)


class DashboardPropertySource(DashboardModel):
    label: Literal["소방청 울산 화학물질 정보 기반 관찰 후보"]
    source_id: Literal["NFA_ULSAN_CHEMICAL_INFORMATION"]
    source_url: HttpUrl
    document_version: str = Field(min_length=1, max_length=500)


class DashboardEvidenceCard(DashboardModel):
    evidence_id: str = Field(min_length=1, max_length=500)
    cas_number: str = Field(min_length=5, max_length=12)
    source: Literal["KOSHA", "CAMEO"]
    title: str = Field(min_length=1, max_length=2_000)
    body_label: Literal["공식 문서 발췌"] = "공식 문서 발췌"
    body_preview: str = Field(max_length=2_000)
    source_url: HttpUrl
    document_version: str = Field(max_length=500)
    cas_link_status: str | None = Field(default=None, max_length=120)

    @field_validator("cas_number")
    @classmethod
    def cas_number_must_be_valid(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("근거 카드 CAS 형식 또는 체크디지트가 올바르지 않습니다.")
        return normalized


class DashboardMaterialCandidate(DashboardModel):
    rank: int = Field(ge=1, le=5)
    cas_number: str = Field(min_length=5, max_length=12)
    display_name: str = Field(min_length=1, max_length=500)
    match_basis: Literal[
        "IDENTITY_EXPRESSION",
        "PUBLIC_PROPERTY_PROFILE",
        "IDENTITY_AND_PUBLIC_PROPERTY_PROFILE",
    ]
    matched_expression: str | None = Field(default=None, max_length=500)
    matched_properties: list[DashboardMatchedProperty] = Field(
        default_factory=list,
        max_length=4,
    )
    property_source: DashboardPropertySource | None = None
    evidence_status: str = Field(min_length=1, max_length=120)
    evidence_warning: str | None = Field(default=None, max_length=2_000)
    evidence_notice: str | None = Field(default=None, max_length=2_000)
    cas_link_warning: str | None = Field(default=None, max_length=2_000)
    evidence_cards: list[DashboardEvidenceCard] = Field(
        default_factory=list,
        max_length=5,
    )
    requires_responder_confirmation: Literal[True] = True
    rule_eligible: Literal[False] = False
    risk_determination_allowed: Literal[False] = False

    @field_validator("cas_number")
    @classmethod
    def candidate_cas_must_be_valid(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("물질 후보 CAS 형식 또는 체크디지트가 올바르지 않습니다.")
        return normalized

    @model_validator(mode="after")
    def evidence_cards_must_match_candidate(self) -> "DashboardMaterialCandidate":
        if any(card.cas_number != self.cas_number for card in self.evidence_cards):
            raise ValueError("후보 카드와 공식 근거 카드의 CAS가 다릅니다.")
        return self


class DashboardMaterialDiscoveryRequest(DashboardModel):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=5)
    evidence_top_k: int = Field(default=3, ge=1, le=5)


class DashboardMaterialDiscoveryResponse(DashboardModel):
    schema_version: Literal[DASHBOARD_BFF_SCHEMA_VERSION] = DASHBOARD_BFF_SCHEMA_VERSION
    source_model_schema_version: Literal[API_SCHEMA_VERSION] = API_SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=128)
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
    candidates: list[DashboardMaterialCandidate] = Field(
        default_factory=list,
        max_length=5,
    )
    requires_responder_confirmation: Literal[True] = True
    candidate_score_is_probability: Literal[False] = False
    risk_display_allowed: Literal[False] = False
    no_reliable_candidate_means_absent: Literal[False] = False
    no_reliable_candidate_means_safe: Literal[False] = False
    notice: str = Field(min_length=1, max_length=2_000)
    safety_notice: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def candidate_state_must_be_consistent(
        self,
    ) -> "DashboardMaterialDiscoveryResponse":
        if self.status == "CANDIDATES_FOUND" and not self.candidates:
            raise ValueError("CANDIDATES_FOUND에는 하나 이상의 후보가 필요합니다.")
        if self.status != "CANDIDATES_FOUND" and self.candidates:
            raise ValueError("후보 없음 상태의 candidates는 비어 있어야 합니다.")
        return self


class DashboardIncidentLocation(DashboardModel):
    facility_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    province: str | None = Field(default=None, max_length=80)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
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
    def coordinates_must_be_a_pair(self) -> "DashboardIncidentLocation":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude와 longitude는 함께 입력해야 합니다.")
        if self.coordinate_source is not None and self.latitude is None:
            raise ValueError("coordinateSource에는 위도·경도가 필요합니다.")
        if self.geocoding_provider is not None and self.coordinate_source != (
            "GEOCODING_PROVIDER"
        ):
            raise ValueError(
                "geocodingProvider는 GEOCODING_PROVIDER 좌표에만 사용할 수 있습니다."
            )
        if self.resolved_at is not None:
            if self.latitude is None:
                raise ValueError("resolvedAt에는 위도·경도가 필요합니다.")
            if self.resolved_at.tzinfo is None or self.resolved_at.utcoffset() is None:
                raise ValueError("resolvedAt에는 시간대가 필요합니다.")
        return self


class DashboardResponderPosition(DashboardModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    observed_at: datetime
    source: Literal[
        "VEHICLE_GPS",
        "MDT_DEVICE_GPS",
        "MANUAL_DISPATCH",
        "DEMO_SIMULATION",
    ]
    accuracy_m: float | None = Field(default=None, ge=0, le=5_000)


class DashboardRouteGeometry(DashboardModel):
    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]] = Field(min_length=2, max_length=10_000)


class DashboardServerRoute(DashboardModel):
    provider: str = Field(min_length=1, max_length=80)
    mode: Literal["LIVE_API", "CACHED_API", "DEMO_SIMULATION"]
    route_id: str = Field(min_length=1, max_length=200)
    geometry: DashboardRouteGeometry
    distance_m: int = Field(gt=0, le=5_000_000)
    duration_seconds: int = Field(gt=0, le=604_800)
    remaining_distance_m: int = Field(ge=0, le=5_000_000)
    remaining_duration_seconds: int = Field(ge=0, le=604_800)
    generated_at: datetime
    traffic_applied: bool
    attribution: str = Field(min_length=1, max_length=300)
    provider_reference: HttpUrl | None = None


class DashboardOperationsContext(DashboardModel):
    dispatch_station_name: str | None = Field(default=None, max_length=160)
    responder_position: DashboardResponderPosition | None = None
    route: DashboardServerRoute | None = None
    journey_state: Literal["DISPATCHED", "EN_ROUTE", "ARRIVED", "ON_SCENE"] = (
        "DISPATCHED"
    )

    @model_validator(mode="after")
    def must_match_model_api_operations_contract(self) -> "DashboardOperationsContext":
        OperationsContext.model_validate(self.model_dump(mode="python"))
        return self


class DashboardIncidentAnalyzeRequest(DashboardModel):
    incident_id: str | None = Field(default=None, min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4_000)
    input_type: Literal[
        "MANUAL_TEXT",
        "DISPATCH_TEXT",
        "VOICE_TRANSCRIPT",
        "STRUCTURED_FORM",
    ] = "MANUAL_TEXT"
    occurred_at: datetime | None = None
    location: DashboardIncidentLocation | None = None
    planned_actions: list[str] = Field(default_factory=list, max_length=20)
    evidence_top_k: int = Field(default=5, ge=1, le=10)
    operations_context: DashboardOperationsContext | None = None

    @field_validator("planned_actions")
    @classmethod
    def planned_action_lengths(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 120 for value in values):
            raise ValueError("plannedActions 각 항목은 1~120자여야 합니다.")
        return values


class DashboardSubstanceMention(DashboardModel):
    surface_text: str = Field(min_length=1, max_length=500)
    role: Literal["INCIDENT", "FACILITY", "UNKNOWN"]
    assertion: Literal["AFFIRMED", "NEGATED", "SUSPECTED", "UNKNOWN"]


class DashboardParserSummary(DashboardModel):
    backend: str = Field(min_length=1, max_length=120)
    incident_types: list[str] = Field(default_factory=list)
    substance_mentions: list[DashboardSubstanceMention] = Field(default_factory=list)
    warning: str = Field(min_length=1, max_length=2_000)


class DashboardResolverCandidate(DashboardModel):
    cas_number: str = Field(min_length=5, max_length=12)
    ranking_score: float | None = None
    ranking_score_is_probability: Literal[False] = False
    rule_eligible: Literal[False] = False
    current_inventory_confirmed: Literal[False] = False

    @field_validator("cas_number")
    @classmethod
    def resolver_cas_must_be_valid(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("물질 후보 CAS 형식 또는 체크디지트가 올바르지 않습니다.")
        return normalized


class DashboardSubstanceCandidateGroup(DashboardModel):
    surface_text: str = Field(min_length=1, max_length=500)
    role: Literal["INCIDENT", "FACILITY", "UNKNOWN"]
    resolver_status: str = Field(min_length=1, max_length=120)
    candidates: list[DashboardResolverCandidate] = Field(default_factory=list)
    requires_responder_confirmation: Literal[True] = True


class DashboardFacilityCandidate(DashboardModel):
    facility_name: str = Field(min_length=1, max_length=500)
    address: str | None = Field(default=None, max_length=1_000)
    province: str | None = Field(default=None, max_length=80)
    cas_number: str = Field(min_length=5, max_length=12)
    chemical_names: str | None = Field(default=None, max_length=2_000)
    latest_survey_year: str | None = Field(default=None, max_length=20)
    source_url: HttpUrl | None = None
    evidence_class: Literal["REPORTED_HANDLING_HISTORY"]
    current_inventory_confirmed: Literal[False] = False
    rule_eligible: Literal[False] = False
    requires_on_site_confirmation: Literal[True] = True

    @field_validator("cas_number")
    @classmethod
    def facility_candidate_cas_must_be_valid(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("시설물질 후보 CAS가 올바르지 않습니다.")
        return normalized


class DashboardFacilityHistory(DashboardModel):
    status: Literal["CANDIDATES_FOUND", "NO_HISTORY_MATCH", "NOT_QUERIED"]
    label: Literal[FACILITY_HISTORY_LABEL] = FACILITY_HISTORY_LABEL
    semantics: Literal[FACILITY_HISTORY_SEMANTICS] = FACILITY_HISTORY_SEMANTICS
    warning: str = Field(min_length=1, max_length=2_000)
    candidates: list[DashboardFacilityCandidate] = Field(default_factory=list)


class DashboardConfirmationGate(DashboardModel):
    policy: Literal["TWO_AUTHENTICATED_ON_SITE_CONFIRMATIONS_REQUIRED"]
    incident_confirmed: bool
    facility_confirmed: bool
    all_required_confirmed: bool
    rule_execution_allowed: bool

    @model_validator(mode="after")
    def aggregate_flags_must_match_roles(self) -> "DashboardConfirmationGate":
        both = self.incident_confirmed and self.facility_confirmed
        if self.all_required_confirmed != both:
            raise ValueError("allRequiredConfirmed가 역할별 확인 상태와 다릅니다.")
        if self.rule_execution_allowed != both:
            raise ValueError("ruleExecutionAllowed가 역할별 확인 상태와 다릅니다.")
        return self


class DashboardPendingConflictReview(DashboardModel):
    executed: Literal[False]
    status: Literal["NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"]
    missing_confirmations: list[Literal["incident_cas", "facility_cas"]] = Field(
        min_length=1,
        max_length=2,
    )
    reason: str = Field(min_length=1, max_length=500)
    risk_display_allowed: Literal[False] = False


class DashboardOrdinalRiskScale(DashboardModel):
    type: Literal["ORDINAL_CAMEO_COMPATIBILITY_CLASS"]
    raw_class_id: int | None = None
    is_probability: Literal[False]
    probability_percent: None
    low_means_safe: Literal[False] = False


class DashboardMappingProvenance(DashboardModel):
    role: Literal["INCIDENT", "FACILITY"]
    cas_number: str = Field(min_length=5, max_length=12)
    cameo_chemical_id: str = Field(pattern=r"^[0-9]+$")
    selected_form: str = Field(min_length=1, max_length=500)
    verification_status: Literal["PUBLIC_SOURCE_VERIFIED"]
    verification_method: Literal["EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET"]
    evidence_url: HttpUrl
    source_product: Literal["NOAA/EPA CAMEO Chemicals"]
    source_version: Literal["3.1.0 rev 1"]
    checked_at_utc: datetime

    @field_validator("cas_number")
    @classmethod
    def mapping_cas_must_be_valid(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("CAMEO mapping provenance CAS가 올바르지 않습니다.")
        return normalized

    @field_validator("checked_at_utc")
    @classmethod
    def mapping_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checkedAtUtc는 시간대가 포함되어야 합니다.")
        return value

    @model_validator(mode="after")
    def evidence_url_must_match_cameo_id(self) -> "DashboardMappingProvenance":
        expected = f"https://cameochemicals.noaa.gov/chemical/{self.cameo_chemical_id}"
        if str(self.evidence_url).rstrip("/") != expected:
            raise ValueError("CAMEO mapping provenance URL과 물질 ID가 다릅니다.")
        return self


class DashboardEvidenceProvenance(DashboardModel):
    basis: Literal["PUBLIC_OFFICIAL_SOURCE"]
    source_product: Literal["NOAA/EPA CAMEO Chemicals"]
    source_versions: list[str] = Field(min_length=1)
    mapping_evidence_urls: list[HttpUrl] = Field(min_length=2)
    compatibility_evidence_urls: list[HttpUrl] = Field(min_length=1)


class DashboardReferenceSource(DashboardModel):
    source_id: str = Field(min_length=1, max_length=160)
    authority_id: str = Field(min_length=1, max_length=120)
    organization: str = Field(min_length=1, max_length=300)
    independence_group: str = Field(min_length=1, max_length=120)
    authority_kind: str = Field(min_length=1, max_length=120)
    source_role: Literal[
        "PRIMARY_REACTIVITY_DATASHEET",
        "INCIDENT_OR_PUBLIC_HEALTH_CORROBORATION",
        "INTERNATIONAL_CHEMICAL_SAFETY_CARD",
    ]
    title: str = Field(min_length=1, max_length=500)
    source_url: HttpUrl
    locator: str = Field(min_length=1, max_length=500)
    published_or_updated: str = Field(min_length=1, max_length=200)
    relation: Literal[
        "SUPPORTS",
        "SUPPORTS_WITH_INCIDENT_AND_MECHANISM",
        "SUPPORTS_SCREENING_ONLY",
    ]


class DashboardReferenceClaimCheck(DashboardModel):
    claim: Literal[
        "SUBSTANCE_IDENTITY_AND_FORM",
        "PAIR_REACTIVITY_SCREENING",
        "CURRENT_SITE_INVENTORY",
        "ACTUAL_MIXING_AND_FIELD_CONDITIONS",
        "HUMAN_CHEMICAL_EXPERT_REVIEW",
    ]
    status: Literal["PASSED", "LIMITED", "NOT_PROVEN", "NOT_PERFORMED"]
    basis: str = Field(min_length=1, max_length=1_000)


class DashboardReferenceAssurance(DashboardModel):
    schema_version: Literal["chemicheck119-reference-assurance-v1"]
    policy_id: Literal["OFFICIAL_REFERENCE_TRIANGULATION_V1"]
    status: Literal["REFERENCE_TRIANGULATED", "PRIMARY_AUTHORITY_ONLY"]
    claim_id: str | None = Field(default=None, max_length=200)
    claim_type: str | None = Field(default=None, max_length=120)
    cas_pair: list[str] = Field(min_length=2, max_length=2)
    claim_text_ko: str | None = Field(default=None, max_length=2_000)
    expected_gas_products: list[str] | None = None
    scope_conditions: list[str] | None = None
    not_proven_by_claim: list[str] | None = None
    reference_count: int = Field(ge=1)
    independent_authority_count: int = Field(ge=1)
    sources: list[DashboardReferenceSource] = Field(min_length=1)
    claim_checks: list[DashboardReferenceClaimCheck] = Field(min_length=1)
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_at_utc: datetime
    machine_checked: Literal[True]
    expert_reviewed: Literal[False]
    human_expert_substitute: Literal[False]
    decision_support_only: Literal[True]
    limitations: list[str] = Field(min_length=1)


class DashboardCompletedConflictResult(DashboardModel):
    kind: Literal["ORDINAL_SCREENING_RESULT"] = "ORDINAL_SCREENING_RESULT"
    status: Literal["SCREENING_COMPLETED"]
    scope: Literal["PUBLIC_SOURCE_CAMEO_SCREENING"]
    policy_mode: Literal["PUBLIC_SOURCE_PILOT_V1"]
    incident_cas: str = Field(min_length=5, max_length=12)
    facility_cas: str = Field(min_length=5, max_length=12)
    rule_id: Literal[PUBLIC_CAMEO_RULE_ID]
    rule_version: Literal[PUBLIC_CAMEO_RULE_VERSION]
    severity: Literal[
        "NO_KNOWN_HAZARDOUS_REACTION",
        "CAUTION",
        "HIGH_RISK",
    ]
    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    risk_level_ko: Literal["낮음", "중간", "높음"]
    risk_scale: DashboardOrdinalRiskScale
    hazard_codes: list[PublicHazardCode]
    gas_products: list[PublicGasProduct] | None = None
    brief_text: str = Field(min_length=1, max_length=2_000)
    required_checks: list[str]
    evidence_urls: list[HttpUrl] = Field(min_length=1)
    limitations: list[str]
    final_decision: Literal["현장 지휘관 판단"]
    expert_reviewed: Literal[False]
    human_confirmation_required: Literal[True]
    mapping_provenance: list[DashboardMappingProvenance] = Field(
        min_length=2,
        max_length=2,
    )
    evidence_provenance: DashboardEvidenceProvenance
    reference_assurance: DashboardReferenceAssurance

    @field_validator("incident_cas", "facility_cas")
    @classmethod
    def completed_cas_must_be_valid(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("완료 결과 CAS 형식 또는 체크디지트가 올바르지 않습니다.")
        return normalized

    @model_validator(mode="after")
    def completed_result_must_preserve_model_safety(
        self,
    ) -> "DashboardCompletedConflictResult":
        mapping_by_role = {item.role: item for item in self.mapping_provenance}
        if set(mapping_by_role) != {"INCIDENT", "FACILITY"}:
            raise ValueError("CAMEO mapping provenance 역할은 두 종류여야 합니다.")
        if (
            mapping_by_role["INCIDENT"].cas_number != self.incident_cas
            or mapping_by_role["FACILITY"].cas_number != self.facility_cas
        ):
            raise ValueError("완료 결과 CAS와 역할별 CAMEO mapping CAS가 다릅니다.")

        mapping_urls = {
            str(item.evidence_url).rstrip("/") for item in self.mapping_provenance
        }
        provenance_mapping_urls = {
            str(url).rstrip("/")
            for url in self.evidence_provenance.mapping_evidence_urls
        }
        if provenance_mapping_urls != mapping_urls:
            raise ValueError("mapping evidence URL이 역할별 provenance와 다릅니다.")
        source_versions = {item.source_version for item in self.mapping_provenance}
        if set(self.evidence_provenance.source_versions) != source_versions:
            raise ValueError("evidence source version이 mapping provenance와 다릅니다.")

        compatibility_urls = {
            str(url).rstrip("/")
            for url in self.evidence_provenance.compatibility_evidence_urls
        }
        if compatibility_urls != {PUBLIC_CAMEO_REACTIVITY_URL}:
            raise ValueError("공개근거 compatibility URL이 CAMEO 원문과 다릅니다.")
        result_evidence_urls = {str(url).rstrip("/") for url in self.evidence_urls}
        if result_evidence_urls != mapping_urls | compatibility_urls:
            raise ValueError("완료 결과 evidence URL이 검증 provenance와 다릅니다.")
        expected_registry_sha = sha256_file(
            CONFIG_DIR / "reference_assurance_registry.json"
        )
        if self.reference_assurance.registry_sha256 != expected_registry_sha:
            raise ValueError("공식근거 보증 registry checksum이 배포 설정과 다릅니다.")
        expected_reference_assurance = DashboardReferenceAssurance.model_validate(
            build_reference_assurance(
                {
                    "incident_cas": self.incident_cas,
                    "facility_cas": self.facility_cas,
                    "gas_products": self.gas_products or [],
                },
                CONFIG_DIR,
            )
        )
        if self.reference_assurance != expected_reference_assurance:
            raise ValueError(
                "공식근거 보증 내용이 배포된 주장 registry와 정확히 일치하지 않습니다."
            )

        expected_brief = (
            "NOAA/EPA CAMEO 공개 원자료로 대조한 반응성 그룹 조합 중 "
            f"가장 보수적인 등급은 {self.risk_level_ko}입니다."
        )
        if self.brief_text != expected_brief:
            raise ValueError(
                "완료 결과 설명은 Rule Engine 원문 템플릿을 보존해야 합니다."
            )

        pair_key = "|".join(sorted((self.incident_cas, self.facility_cas)))
        expected_pair = _dashboard_public_pair_index().get(pair_key)
        if expected_pair is None:
            raise ValueError(
                "현재 대시보드 계약이 지원하지 않는 공개검증 물질쌍입니다."
            )

        exact_scalar_fields = {
            "status": self.status,
            "scope": self.scope,
            "policy_mode": self.policy_mode,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "severity": self.severity,
            "risk_level": self.risk_level,
            "risk_level_ko": self.risk_level_ko,
            "raw_class_id": self.risk_scale.raw_class_id,
            "brief_text": self.brief_text,
            "final_decision": self.final_decision,
            "expert_reviewed": self.expert_reviewed,
            "human_confirmation_required": self.human_confirmation_required,
        }
        for field, actual in exact_scalar_fields.items():
            if expected_pair.get(field) != actual:
                raise ValueError(f"완료 결과 {field}가 검증된 물질쌍 계약과 다릅니다.")

        exact_list_fields = {
            "hazard_codes": self.hazard_codes,
            "gas_products": self.gas_products,
            "required_checks": self.required_checks,
            "limitations": self.limitations,
        }
        for field, actual in exact_list_fields.items():
            if expected_pair.get(field) != actual:
                raise ValueError(f"완료 결과 {field}가 검증된 물질쌍 계약과 다릅니다.")

        expected_evidence_urls = {
            str(value).rstrip("/") for value in expected_pair["evidence_urls"]
        }
        if result_evidence_urls != expected_evidence_urls:
            raise ValueError("완료 결과 evidence URL이 검증된 물질쌍 계약과 다릅니다.")

        expected_compatibility_urls = {
            str(value).rstrip("/")
            for value in expected_pair["compatibility_evidence_urls"]
        }
        if compatibility_urls != expected_compatibility_urls:
            raise ValueError(
                "완료 결과 compatibility URL이 검증된 물질쌍 계약과 다릅니다."
            )

        expected_mapping_by_cas = {
            item["cas_number"]: item for item in expected_pair["mappings"]
        }
        if set(expected_mapping_by_cas) != {
            item.cas_number for item in self.mapping_provenance
        }:
            raise ValueError("CAMEO mapping CAS 집합이 검증된 물질쌍과 다릅니다.")
        mapping_fields = (
            "cameo_chemical_id",
            "selected_form",
            "verification_status",
            "verification_method",
            "source_product",
            "source_version",
        )
        for item in self.mapping_provenance:
            expected_mapping = expected_mapping_by_cas[item.cas_number]
            for field in mapping_fields:
                if getattr(item, field) != expected_mapping[field]:
                    raise ValueError(
                        f"CAMEO mapping {field}가 검증 crosswalk와 다릅니다."
                    )
            if str(item.evidence_url).rstrip("/") != str(
                expected_mapping["evidence_url"]
            ).rstrip("/"):
                raise ValueError(
                    "CAMEO mapping evidence URL이 검증 crosswalk와 다릅니다."
                )
            expected_checked_at = datetime.fromisoformat(
                expected_mapping["checked_at_utc"].replace("Z", "+00:00")
            )
            if item.checked_at_utc != expected_checked_at:
                raise ValueError("CAMEO mapping 확인 시각이 검증 crosswalk와 다릅니다.")

        source_payload = {
            "status": self.status,
            "scope": self.scope,
            "policy_mode": self.policy_mode,
            "incident_cas": self.incident_cas,
            "facility_cas": self.facility_cas,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "severity": self.severity,
            "risk_level": self.risk_level,
            "risk_level_ko": self.risk_level_ko,
            "risk_scale": {
                "type": self.risk_scale.type,
                "raw_class_id": self.risk_scale.raw_class_id,
                "is_probability": self.risk_scale.is_probability,
                "probability_percent": self.risk_scale.probability_percent,
            },
            "hazard_codes": self.hazard_codes,
            "gas_products": self.gas_products,
            "brief_text": self.brief_text,
            "required_checks": self.required_checks,
            "evidence_urls": [str(url) for url in self.evidence_urls],
            "limitations": self.limitations,
            "final_decision": self.final_decision,
            "expert_reviewed": self.expert_reviewed,
            "human_confirmation_required": self.human_confirmation_required,
            "mapping_provenance": [
                item.model_dump(mode="json") for item in self.mapping_provenance
            ],
            "evidence_provenance": self.evidence_provenance.model_dump(mode="json"),
            "reference_assurance": self.reference_assurance.model_dump(mode="json"),
        }
        CompletedConflictResult.model_validate(source_payload)
        errors = validate_review_output(source_payload)
        if errors:
            raise ValueError(
                "BFF 완료 결과가 모델 안전 계약과 다릅니다: " + "; ".join(errors)
            )
        return self


def project_completed_model_result(
    source: dict[str, Any],
) -> DashboardCompletedConflictResult:
    """모델 Rule 결과의 안전 필드를 재생성 없이 BFF 표시 DTO로 투영한다."""

    validated = CompletedConflictResult.model_validate(source)
    normalized = validated.model_dump(mode="json")
    errors = validate_review_output(normalized)
    if errors:
        raise ValueError("모델 완료 결과 안전 검증 실패: " + "; ".join(errors))
    if (
        validated.status != "SCREENING_COMPLETED"
        or validated.scope != "PUBLIC_SOURCE_CAMEO_SCREENING"
        or validated.policy_mode != "PUBLIC_SOURCE_PILOT_V1"
    ):
        raise ValueError(
            "대시보드 BFF v1은 PUBLIC_SOURCE_PILOT_V1 완료 결과만 지원합니다."
        )

    risk_scale = dict(normalized["risk_scale"])
    risk_scale["low_means_safe"] = False
    return DashboardCompletedConflictResult.model_validate(
        {
            "kind": "ORDINAL_SCREENING_RESULT",
            "status": normalized["status"],
            "scope": normalized["scope"],
            "policy_mode": normalized["policy_mode"],
            "incident_cas": normalized["incident_cas"],
            "facility_cas": normalized["facility_cas"],
            "rule_id": normalized["rule_id"],
            "rule_version": normalized["rule_version"],
            "severity": normalized["severity"],
            "risk_level": normalized["risk_level"],
            "risk_level_ko": normalized["risk_level_ko"],
            "risk_scale": risk_scale,
            "hazard_codes": normalized["hazard_codes"],
            "gas_products": normalized["gas_products"],
            "brief_text": normalized["brief_text"],
            "required_checks": normalized["required_checks"],
            "evidence_urls": normalized["evidence_urls"],
            "limitations": normalized["limitations"],
            "final_decision": normalized["final_decision"],
            "expert_reviewed": normalized["expert_reviewed"],
            "human_confirmation_required": normalized["human_confirmation_required"],
            "mapping_provenance": normalized["mapping_provenance"],
            "evidence_provenance": normalized["evidence_provenance"],
            "reference_assurance": normalized["reference_assurance"],
        }
    )


class DashboardInconclusiveConflictResult(DashboardModel):
    kind: Literal["INCONCLUSIVE_RESULT"] = "INCONCLUSIVE_RESULT"
    status: Literal[
        "VERIFY_REQUIRED",
        "UNCLASSIFIED",
        "CAMEO_GROUP_SCREENING_ONLY",
    ]
    reason: str = Field(min_length=1, max_length=2_000)
    human_confirmation_required: Literal[True] = True


class DashboardCompletedConflictReview(DashboardModel):
    executed: Literal[True]
    status: Literal["SCREENING_COMPLETED"]
    result: DashboardCompletedConflictResult
    risk_display_allowed: Literal[True] = True

    @model_validator(mode="after")
    def wrapper_status_must_match_result(self) -> "DashboardCompletedConflictReview":
        if self.status != self.result.status:
            raise ValueError("충돌 검토 wrapper와 결과 상태가 다릅니다.")
        return self


class DashboardInconclusiveConflictReview(DashboardModel):
    executed: Literal[True]
    status: Literal[
        "VERIFY_REQUIRED",
        "UNCLASSIFIED",
        "CAMEO_GROUP_SCREENING_ONLY",
    ]
    result: DashboardInconclusiveConflictResult
    risk_display_allowed: Literal[False] = False

    @model_validator(mode="after")
    def wrapper_status_must_match_result(
        self,
    ) -> "DashboardInconclusiveConflictReview":
        if self.status != self.result.status:
            raise ValueError("충돌 검토 wrapper와 미분류 결과 상태가 다릅니다.")
        return self


class DashboardProvenance(DashboardModel):
    model_version: str = Field(min_length=1, max_length=500)
    data_version: str = Field(min_length=1, max_length=500)
    rule_policy: str = Field(min_length=1, max_length=120)
    expert_reviewed: Literal[False]
    final_decision_authority: Literal["현장 지휘관"]


class DashboardGroundedRagStatement(DashboardModel):
    text: str = Field(min_length=1, max_length=600)
    source_ids: list[str] = Field(min_length=1, max_length=3)


class DashboardGroundedRagCitation(DashboardModel):
    source_id: str = Field(min_length=1, max_length=500)
    source_type: Literal["KOSHA", "CAMEO", "CAMEO_RULE_ENGINE"]
    title: str = Field(min_length=1, max_length=500)
    cas_number: str | None = Field(default=None, max_length=12)
    source_urls: list[HttpUrl] = Field(min_length=1, max_length=5)


class DashboardGroundedRag(DashboardModel):
    """대시보드가 표시할 짧은 근거 요약과 출처 링크."""

    schema_version: Literal["chemicheck119-grounded-rag-v1"]
    status: Literal[
        "COMPLETED",
        "FALLBACK_EXTRACTIVE",
        "DISABLED",
        "NO_GROUNDED_EVIDENCE",
        "NOT_RUN_REQUIRES_CONFIRMED_PAIR",
        "NOT_RUN_RULE_NOT_COMPLETED",
    ]
    used_llm: bool
    model: str | None = Field(default=None, max_length=300)
    statements: list[DashboardGroundedRagStatement] = Field(
        default_factory=list,
        max_length=5,
    )
    citations: list[DashboardGroundedRagCitation] = Field(
        default_factory=list,
        max_length=7,
    )
    risk_decision_source: Literal["DETERMINISTIC_CAMEO_RULE_ENGINE"]
    semantic_grounding_verified: Literal[False]
    fallback_reason: str | None = Field(default=None, max_length=120)
    limitations: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def statement_citations_must_exist(self) -> "DashboardGroundedRag":
        citation_ids = {item.source_id for item in self.citations}
        statement_ids = {
            source_id for item in self.statements for source_id in item.source_ids
        }
        if statement_ids - citation_ids:
            raise ValueError("RAG 문장이 응답에 없는 sourceId를 인용했습니다.")
        return self


class DashboardAgentMapPoint(DashboardModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    label: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=80)
    observed_at: datetime | None = None
    accuracy_m: float | None = Field(default=None, ge=0, le=5_000)
    is_simulation: bool = False


class DashboardAgentRouteState(DashboardModel):
    status: Literal[
        "AVAILABLE",
        "DEMO_SIMULATION",
        "ROUTE_UNAVAILABLE",
        "INCIDENT_LOCATION_REQUIRED",
        "RESPONDER_POSITION_REQUIRED",
        "POSITION_STALE",
        "ROUTE_ENDPOINT_MISMATCH",
        "ARRIVED",
    ]
    provider: str | None = Field(default=None, max_length=80)
    provider_mode: Literal["LIVE_API", "CACHED_API", "DEMO_SIMULATION"] | None = None
    route_id: str | None = Field(default=None, max_length=200)
    geometry: DashboardRouteGeometry | None = None
    total_distance_m: int | None = Field(default=None, ge=0)
    remaining_distance_m: int | None = Field(default=None, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    progress_ratio: float | None = Field(default=None, ge=0, le=1)
    progress_ratio_is_probability: Literal[False] = False
    traffic_applied: bool | None = None
    generated_at: datetime | None = None
    attribution: str | None = Field(default=None, max_length=300)
    message: str = Field(min_length=1, max_length=500)


class DashboardAgentMapRenderingContract(DashboardModel):
    geometry_format: Literal["GEOJSON_RFC7946"] = "GEOJSON_RFC7946"
    recommended_renderer: Literal["MAPLIBRE_GL_JS"] = "MAPLIBRE_GL_JS"
    tile_provider_required: Literal[True] = True
    attribution_required: Literal[True] = True
    public_osm_standard_tiles_for_production: Literal[False] = False
    route_animation_supported: Literal[True] = True


class DashboardAgentMapContext(DashboardModel):
    coverage_scope: Literal["NATIONWIDE_KOREA"] = "NATIONWIDE_KOREA"
    incident_position: DashboardAgentMapPoint | None = None
    responder_position: DashboardAgentMapPoint | None = None
    route: DashboardAgentRouteState
    rendering: DashboardAgentMapRenderingContract
    hazard_overlay_status: Literal["NOT_COMPUTED_NO_VALIDATED_DISPERSION_MODEL"]


class DashboardAgentWorkflowStep(DashboardModel):
    step_id: Literal[
        "INCIDENT_INGESTION",
        "INCIDENT_PARSING",
        "INCIDENT_LOCATION",
        "SUBSTANCE_RESOLUTION",
        "FACILITY_HISTORY",
        "EVIDENCE_RETRIEVAL",
        "ON_SITE_CONFIRMATION",
        "CONFLICT_SCREENING",
        "GROUNDED_EXPLANATION",
        "RESPONSE_RECORD",
    ]
    label: str = Field(min_length=1, max_length=120)
    status: Literal["COMPLETED", "IN_PROGRESS", "WAITING", "BLOCKED", "NOT_APPLICABLE"]
    detail: str = Field(min_length=1, max_length=500)


class DashboardAgentToolExecution(DashboardModel):
    tool_id: Literal[
        "RULE_PARSER",
        "SUBSTANCE_RESOLVER",
        "FACILITY_HISTORY_SEARCH",
        "HYBRID_EVIDENCE_RETRIEVER",
        "CONFIRMATION_GATE",
        "CAMEO_RULE_ENGINE",
        "GROUNDED_RAG",
        "SERVER_ROUTE_PROVIDER",
    ]
    status: Literal[
        "COMPLETED", "WAITING", "BLOCKED", "FALLBACK", "NOT_RUN", "UNAVAILABLE"
    ]
    output_reference: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=500)


class DashboardOperationsAgentSnapshot(DashboardModel):
    schema_version: Literal["chemicheck119-operations-agent-v1"]
    agent_type: Literal["DETERMINISTIC_FIELD_RESPONSE_ORCHESTRATOR"]
    phase: Literal[
        "INCIDENT_INTAKE",
        "EN_ROUTE_TRIAGE",
        "ON_SCENE_CONFIRMATION",
        "CONFLICT_SCREENING_COMPLETE",
        "EVIDENCE_REVIEW_REQUIRED",
    ]
    current_objective: str = Field(min_length=1, max_length=500)
    next_actions: list[str] = Field(min_length=1, max_length=8)
    workflow: list[DashboardAgentWorkflowStep] = Field(min_length=10, max_length=10)
    tool_executions: list[DashboardAgentToolExecution] = Field(
        min_length=8, max_length=8
    )
    map_context: DashboardAgentMapContext
    autonomous_risk_decision_allowed: Literal[False]
    final_decision_authority: Literal["현장 지휘관"]
    trace_is_chain_of_thought: Literal[False]

    @model_validator(mode="after")
    def must_match_model_api_agent_contract(
        self,
    ) -> "DashboardOperationsAgentSnapshot":
        OperationsAgentSnapshot.model_validate(self.model_dump(mode="python"))
        return self


def project_operations_agent(
    source: dict[str, Any],
) -> DashboardOperationsAgentSnapshot:
    """모델 API 에이전트 snapshot을 재생성 없이 BFF camelCase DTO로 투영한다."""

    validated = OperationsAgentSnapshot.model_validate(source)
    return DashboardOperationsAgentSnapshot.model_validate(
        validated.model_dump(mode="python")
    )


class DashboardAnalysisBase(DashboardModel):
    schema_version: Literal[DASHBOARD_BFF_SCHEMA_VERSION] = DASHBOARD_BFF_SCHEMA_VERSION
    source_model_schema_version: Literal[API_SCHEMA_VERSION] = API_SCHEMA_VERSION
    analysis_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    incident_id: str = Field(min_length=1, max_length=128)
    parser: DashboardParserSummary
    substance_candidates: list[DashboardSubstanceCandidateGroup] = Field(
        default_factory=list
    )
    facility_history: DashboardFacilityHistory
    evidence_cards: list[DashboardEvidenceCard] = Field(default_factory=list)
    grounded_rag: DashboardGroundedRag | None = None
    # BFF 도입 전 기존 화면도 유지할 수 있도록 전환 기간에는 선택 필드다.
    # 새 BE는 모델 API의 필수 agent snapshot을 그대로 투영해야 한다.
    agent: DashboardOperationsAgentSnapshot | None = None
    confirmation_gate: DashboardConfirmationGate
    required_next_steps: list[str]
    provenance: DashboardProvenance
    safety_notice: str = Field(min_length=1, max_length=2_000)


class DashboardAwaitingAnalysisResponse(DashboardAnalysisBase):
    state: Literal[
        "AWAITING_SUBSTANCE_CONFIRMATION",
        "AWAITING_INCIDENT_CONFIRMATION",
        "AWAITING_FACILITY_CONFIRMATION",
    ]
    conflict_review: DashboardPendingConflictReview
    risk_display_allowed: Literal[False] = False

    @model_validator(mode="after")
    def awaiting_state_must_keep_gate_closed(
        self,
    ) -> "DashboardAwaitingAnalysisResponse":
        if (
            self.confirmation_gate.all_required_confirmed
            or self.confirmation_gate.rule_execution_allowed
        ):
            raise ValueError("확인 대기 상태에서 Rule 실행 gate를 열 수 없습니다.")
        incident_confirmed = self.confirmation_gate.incident_confirmed
        facility_confirmed = self.confirmation_gate.facility_confirmed
        expected = {
            (False, False): (
                "AWAITING_SUBSTANCE_CONFIRMATION",
                {"incident_cas", "facility_cas"},
            ),
            (False, True): (
                "AWAITING_INCIDENT_CONFIRMATION",
                {"incident_cas"},
            ),
            (True, False): (
                "AWAITING_FACILITY_CONFIRMATION",
                {"facility_cas"},
            ),
        }.get((incident_confirmed, facility_confirmed))
        if expected is None:
            raise ValueError("두 물질이 확인된 상태는 확인 대기 상태가 될 수 없습니다.")
        expected_state, expected_missing = expected
        if self.state != expected_state:
            raise ValueError("확인 대기 state가 역할별 확인 상태와 다릅니다.")
        if set(self.conflict_review.missing_confirmations) != expected_missing:
            raise ValueError("missingConfirmations가 역할별 확인 상태와 다릅니다.")
        if (
            self.grounded_rag is not None
            and self.grounded_rag.status != "NOT_RUN_REQUIRES_CONFIRMED_PAIR"
        ):
            raise ValueError("확인 대기 상태에서는 RAG 요약을 실행할 수 없습니다.")
        return self


class DashboardCompletedAnalysisResponse(DashboardAnalysisBase):
    state: Literal["SCREENING_COMPLETED"]
    conflict_review: DashboardCompletedConflictReview
    risk_display_allowed: Literal[True] = True

    @model_validator(mode="after")
    def completed_state_requires_two_confirmations(
        self,
    ) -> "DashboardCompletedAnalysisResponse":
        if not (
            self.confirmation_gate.all_required_confirmed
            and self.confirmation_gate.rule_execution_allowed
        ):
            raise ValueError("위험도 표시는 두 물질 확인과 Rule 실행이 필요합니다.")
        if self.state != self.conflict_review.status:
            raise ValueError("분석 상태와 충돌 검토 상태가 다릅니다.")
        if self.provenance.rule_policy != self.conflict_review.result.policy_mode:
            raise ValueError("분석 provenance와 충돌 결과 정책이 다릅니다.")
        if (
            self.provenance.expert_reviewed
            is not self.conflict_review.result.expert_reviewed
        ):
            raise ValueError("분석 provenance와 충돌 결과 검토 상태가 다릅니다.")
        if self.grounded_rag is not None and self.grounded_rag.status in {
            "NOT_RUN_REQUIRES_CONFIRMED_PAIR",
            "NOT_RUN_RULE_NOT_COMPLETED",
        }:
            raise ValueError(
                "완료된 Rule 결과에는 완료·fallback RAG 상태가 필요합니다."
            )
        return self


class DashboardInconclusiveAnalysisResponse(DashboardAnalysisBase):
    state: Literal[
        "VERIFY_REQUIRED",
        "UNCLASSIFIED",
        "CAMEO_GROUP_SCREENING_ONLY",
    ]
    conflict_review: DashboardInconclusiveConflictReview
    risk_display_allowed: Literal[False] = False

    @model_validator(mode="after")
    def inconclusive_state_must_not_show_risk(
        self,
    ) -> "DashboardInconclusiveAnalysisResponse":
        if self.state != self.conflict_review.status:
            raise ValueError("분석 상태와 충돌 검토 상태가 다릅니다.")
        if not (
            self.confirmation_gate.all_required_confirmed
            and self.confirmation_gate.rule_execution_allowed
        ):
            raise ValueError("Rule 실행 결과에는 두 물질의 현장 확인이 필요합니다.")
        if (
            self.grounded_rag is not None
            and self.grounded_rag.status != "NOT_RUN_RULE_NOT_COMPLETED"
        ):
            raise ValueError("미분류 Rule 결과에서는 RAG 요약을 실행할 수 없습니다.")
        return self


DashboardAnalysisResponse = Annotated[
    DashboardAwaitingAnalysisResponse
    | DashboardCompletedAnalysisResponse
    | DashboardInconclusiveAnalysisResponse,
    Field(union_mode="left_to_right"),
]


class DashboardConfirmationRequest(DashboardModel):
    role: Literal["INCIDENT", "FACILITY"]
    cas_number: str = Field(min_length=5, max_length=12)
    display_name: str | None = Field(default=None, max_length=160)
    confirmation_basis: Literal[
        "CONTAINER_LABEL",
        "SITE_MSDS",
        "SHIPPING_DOCUMENT",
        "INSTRUMENT_READING",
        "RESPONDER_OBSERVATION",
        "OTHER_VERIFIED_SOURCE",
    ]
    observed_at: datetime

    @field_validator("cas_number")
    @classmethod
    def confirmed_cas_must_be_valid(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("현장 확인 CAS가 올바르지 않습니다.")
        return normalized

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observedAt은 시간대가 포함된 ISO 8601이어야 합니다.")
        if value.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(
            minutes=5
        ):
            raise ValueError("observedAt은 허용된 시계 오차보다 미래일 수 없습니다.")
        return value


class DashboardMovementUpdateRequest(DashboardModel):
    """FE가 새 GPS 관측값만 BE에 전달하는 경량 이동 갱신 계약."""

    responder_position: DashboardResponderPosition
    journey_state: Literal["DISPATCHED", "EN_ROUTE", "ARRIVED", "ON_SCENE"]
    client_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def responder_position_must_match_model_contract(
        self,
    ) -> "DashboardMovementUpdateRequest":
        OperationsContext.model_validate(
            {
                "responder_position": self.responder_position.model_dump(mode="python"),
                "journey_state": self.journey_state,
            }
        )
        return self


class DashboardMovementUpdateResponse(DashboardModel):
    schema_version: Literal[DASHBOARD_BFF_SCHEMA_VERSION] = DASHBOARD_BFF_SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    incident_id: str = Field(min_length=1, max_length=128)
    accepted_at: datetime
    client_sequence: int = Field(ge=1)
    map_context: DashboardAgentMapContext
    next_refresh_seconds: int = Field(ge=3, le=60)
    route_recalculated: bool

    @field_validator("accepted_at")
    @classmethod
    def accepted_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acceptedAt은 시간대가 포함되어야 합니다.")
        return value


class DashboardConfirmationResponse(DashboardModel):
    schema_version: Literal[DASHBOARD_BFF_SCHEMA_VERSION] = DASHBOARD_BFF_SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    incident_id: str = Field(min_length=1, max_length=128)
    confirmation_id: str = Field(min_length=1, max_length=128)
    role: Literal["INCIDENT", "FACILITY"]
    cas_number: str = Field(min_length=5, max_length=12)
    created_at: datetime
    reanalyze_required: Literal[True] = True

    @field_validator("cas_number")
    @classmethod
    def confirmation_response_cas_must_be_valid(cls, value: str) -> str:
        normalized = normalize_cas(value)
        if not valid_cas_checksum(normalized):
            raise ValueError("현장 확인 응답 CAS가 올바르지 않습니다.")
        return normalized

    @field_validator("created_at")
    @classmethod
    def created_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("createdAt은 시간대가 포함되어야 합니다.")
        return value


class DashboardConversationMessage(DashboardModel):
    message_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=1)
    role: Literal["USER", "ASSISTANT", "SYSTEM"]
    text: str = Field(min_length=1, max_length=10_000)
    created_at: datetime
    analysis_id: str | None = Field(default=None, max_length=128)

    @field_validator("created_at")
    @classmethod
    def message_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("메시지 createdAt은 시간대가 포함되어야 합니다.")
        return value


class DashboardRecordSaveRequest(DashboardModel):
    conversation_started_at: datetime
    messages: list[DashboardConversationMessage] = Field(min_length=1, max_length=500)
    analysis_ids: list[str] = Field(min_length=1, max_length=100)
    confirmation_ids: list[str] = Field(max_length=20)

    @field_validator("conversation_started_at")
    @classmethod
    def conversation_time_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("conversationStartedAt은 시간대가 포함되어야 합니다.")
        return value

    @model_validator(mode="after")
    def message_sequence_must_be_unique_and_ordered(
        self,
    ) -> "DashboardRecordSaveRequest":
        sequences = [message.sequence for message in self.messages]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("대화 sequence는 중복 없이 오름차순이어야 합니다.")
        message_ids = [message.message_id for message in self.messages]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("대화 messageId에는 중복을 넣을 수 없습니다.")
        if any(
            message.created_at < self.conversation_started_at
            for message in self.messages
        ):
            raise ValueError("메시지 시각은 대화 시작 시각보다 빠를 수 없습니다.")
        if len(self.analysis_ids) != len(set(self.analysis_ids)):
            raise ValueError("analysisIds에는 중복을 넣을 수 없습니다.")
        if len(self.confirmation_ids) != len(set(self.confirmation_ids)):
            raise ValueError("confirmationIds에는 중복을 넣을 수 없습니다.")
        message_analysis_ids = {
            message.analysis_id for message in self.messages if message.analysis_id
        }
        if not message_analysis_ids.issubset(set(self.analysis_ids)):
            raise ValueError("메시지 analysisId가 저장 요청 analysisIds에 없습니다.")
        return self


class DashboardRecordSaveResponse(DashboardModel):
    schema_version: Literal[DASHBOARD_BFF_SCHEMA_VERSION] = DASHBOARD_BFF_SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    incident_id: str = Field(min_length=1, max_length=128)
    record_id: str = Field(min_length=1, max_length=128)
    saved_at: datetime
    reset_allowed: Literal[True] = True

    @field_validator("saved_at")
    @classmethod
    def saved_at_must_include_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("savedAt은 시간대가 포함되어야 합니다.")
        return value


def build_dashboard_bff_openapi() -> dict[str, Any]:
    """BE 구현용 계약 전용 OpenAPI를 생성한다.

    반환된 라우트는 모델 API에 마운트되지 않는다. OpenAPI의 구현 소유자는
    ``BE_Repository``다.
    """

    application = FastAPI(
        title="케미체크119 대시보드 BFF 계약",
        version="1.0.0",
        description=(
            "FE_Repository가 BE_Repository를 통해 모델 API를 안전하게 사용하는 "
            "계약입니다. 이 명세의 라우트는 llm FastAPI가 제공하지 않습니다."
        ),
        openapi_version="3.1.0",
    )

    async def discover_substances(
        payload: DashboardMaterialDiscoveryRequest,
    ) -> DashboardMaterialDiscoveryResponse:
        raise NotImplementedError(payload)

    async def analyze_incident(
        payload: DashboardIncidentAnalyzeRequest,
    ) -> DashboardAnalysisResponse:
        raise NotImplementedError(payload)

    async def create_confirmation(
        incidentId: str,
        payload: DashboardConfirmationRequest,
    ) -> DashboardConfirmationResponse:
        raise NotImplementedError(incidentId, payload)

    async def update_movement(
        incidentId: str,
        payload: DashboardMovementUpdateRequest,
    ) -> DashboardMovementUpdateResponse:
        raise NotImplementedError(incidentId, payload)

    async def save_record(
        incidentId: str,
        payload: DashboardRecordSaveRequest,
    ) -> DashboardRecordSaveResponse:
        raise NotImplementedError(incidentId, payload)

    common_errors = {
        400: {"model": DashboardErrorResponse},
        401: {"model": DashboardErrorResponse},
        403: {"model": DashboardErrorResponse},
        409: {"model": DashboardErrorResponse},
        422: {"model": DashboardErrorResponse},
        500: {"model": DashboardErrorResponse},
        503: {"model": DashboardErrorResponse},
    }
    application.add_api_route(
        "/api/c2guard/v1/substances/discover",
        discover_substances,
        methods=["POST"],
        response_model=DashboardMaterialDiscoveryResponse,
        responses=common_errors,
        tags=["dashboard-bff"],
        summary="물질명·CAS·성상 관찰에서 확인 전 후보 탐색",
    )
    application.add_api_route(
        "/api/c2guard/v1/incidents/analyze",
        analyze_incident,
        methods=["POST"],
        response_model=DashboardAnalysisResponse,
        responses=common_errors,
        tags=["dashboard-bff"],
        summary="사고 분석과 확인 gate 상태 조회",
    )
    application.add_api_route(
        "/api/c2guard/v1/incidents/{incidentId}/confirmations",
        create_confirmation,
        methods=["POST"],
        response_model=DashboardConfirmationResponse,
        status_code=201,
        responses=common_errors,
        tags=["dashboard-bff"],
        summary="인증 사용자 기준 현장 물질 확인 레코드 생성",
    )
    application.add_api_route(
        "/api/c2guard/v1/incidents/{incidentId}/movement",
        update_movement,
        methods=["POST"],
        response_model=DashboardMovementUpdateResponse,
        responses=common_errors,
        tags=["dashboard-bff"],
        summary="차량·단말 현재 위치 갱신과 서버 길찾기 상태 조회",
    )
    application.add_api_route(
        "/api/c2guard/v1/incidents/{incidentId}/record",
        save_record,
        methods=["POST"],
        response_model=DashboardRecordSaveResponse,
        status_code=201,
        responses=common_errors,
        tags=["dashboard-bff"],
        summary="전체 대화·분석·확인 기록 저장",
    )

    schema = application.openapi()
    schema["servers"] = [{"url": "/"}]
    schema["components"]["securitySchemes"] = {
        "ServiceSession": {
            "type": "apiKey",
            "in": "cookie",
            "name": "CHEMICHECK119_SESSION",
            "description": (
                "BE가 발급·검증하는 HttpOnly·Secure·SameSite 세션의 계약 예시입니다. "
                "모델 API X-API-Key를 브라우저에 전달하지 않습니다."
            ),
        }
    }
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            operation["security"] = [{"ServiceSession": []}]
            operation["x-implementation-owner"] = "BE_Repository"
            operation["x-model-api-direct-browser-call-allowed"] = False
    schema["paths"]["/api/c2guard/v1/incidents/{incidentId}/record"]["post"][
        "x-backend-required-checks"
    ] = [
        "analysisIds가 모두 존재하며 path incidentId에 속하는지 확인",
        "confirmationIds가 모두 존재하며 path incidentId에 속하는지 확인",
        "권위 있는 모델 snapshot·confirmation·provenance를 BE 저장소에서 결합",
        "알 수 없거나 다른 사고의 ID는 409로 거부",
    ]
    schema["paths"]["/api/c2guard/v1/incidents/{incidentId}/movement"]["post"][
        "x-backend-required-checks"
    ] = [
        "GPS 관측 시각·좌표 범위·clientSequence 단조 증가 검증",
        "길찾기 API Key는 BE Secret에서만 로드하고 브라우저에 반환하지 않음",
        "재탐색은 provider rate limit과 이동 거리 임계값으로 제한",
        "길찾기 실패 시 직선 경로·가짜 ETA 대신 ROUTE_UNAVAILABLE 반환",
        "DEMO_SIMULATION 경로는 실제 경로와 명확히 구분",
    ]
    schema["x-contract-version"] = DASHBOARD_BFF_SCHEMA_VERSION
    schema["x-implementation-status"] = "CONTRACT_ONLY_NOT_IMPLEMENTED_IN_LLM"
    schema["x-model-api-schema-version"] = API_SCHEMA_VERSION
    schema["x-completed-result-pair-contract"] = {
        "path": "config/dashboard_public_pair_contract.json",
        "version": DASHBOARD_PUBLIC_PAIR_CONTRACT_VERSION,
        "matching": "EXACT_PAIR_PRESENTATION_FIELDS",
        "unsupportedPairBehavior": "REJECT_COMPLETED_RESULT",
    }
    return schema


__all__ = [
    "DASHBOARD_BFF_SCHEMA_VERSION",
    "FACILITY_HISTORY_LABEL",
    "FACILITY_HISTORY_SEMANTICS",
    "DashboardAnalysisResponse",
    "DashboardAwaitingAnalysisResponse",
    "DashboardCompletedAnalysisResponse",
    "DashboardConfirmationRequest",
    "DashboardConfirmationResponse",
    "DashboardErrorResponse",
    "DashboardGroundedRag",
    "DashboardIncidentAnalyzeRequest",
    "DashboardOperationsAgentSnapshot",
    "DashboardOperationsContext",
    "DashboardInconclusiveAnalysisResponse",
    "DashboardMaterialDiscoveryRequest",
    "DashboardMaterialDiscoveryResponse",
    "DashboardMovementUpdateRequest",
    "DashboardMovementUpdateResponse",
    "DashboardRecordSaveRequest",
    "DashboardRecordSaveResponse",
    "build_dashboard_bff_openapi",
    "project_completed_model_result",
    "project_operations_agent",
]
