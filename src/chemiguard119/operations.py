"""전국 현장대응 흐름을 표현하는 결정론적 에이전트 계약.

이 모듈은 LLM의 자유 생성 대신 실제 파이프라인 실행 상태와 백엔드가 제공한
위치·길찾기 결과를 조합한다. 지도 경로와 ETA를 추측하지 않으며, 좌표나
길찾기 결과가 없으면 필요한 다음 행동을 명시적으로 반환한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


POSITION_STALE_AFTER = timedelta(minutes=5)
ROUTE_ENDPOINT_TOLERANCE_M = 1_500


class OperationsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponderPosition(OperationsModel):
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

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_be_aware_and_not_future(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("현재 위치 observed_at에는 시간대가 필요합니다.")
        if value.astimezone(timezone.utc) > datetime.now(timezone.utc) + timedelta(
            minutes=5
        ):
            raise ValueError(
                "현재 위치 observed_at은 허용 오차보다 미래일 수 없습니다."
            )
        return value


class RouteGeometry(OperationsModel):
    type: Literal["LineString"] = "LineString"
    coordinates: list[tuple[float, float]] = Field(min_length=2, max_length=10_000)

    @field_validator("coordinates")
    @classmethod
    def coordinates_must_be_lon_lat(
        cls, values: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        for longitude, latitude in values:
            if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
                raise ValueError("경로 좌표는 [경도, 위도] 범위여야 합니다.")
        return values


class ServerRoute(OperationsModel):
    """BE가 비밀키로 호출한 길찾기 사업자의 결과.

    모델 API는 이 값을 재계산하거나 보정하지 않는다. 데모 경로는 반드시
    ``DEMO_SIMULATION``으로 표시해 실제 길찾기 결과와 혼동되지 않게 한다.
    """

    provider: str = Field(min_length=1, max_length=80)
    mode: Literal["LIVE_API", "CACHED_API", "DEMO_SIMULATION"]
    route_id: str = Field(min_length=1, max_length=200)
    geometry: RouteGeometry
    distance_m: int = Field(gt=0, le=5_000_000)
    duration_seconds: int = Field(gt=0, le=604_800)
    remaining_distance_m: int = Field(ge=0, le=5_000_000)
    remaining_duration_seconds: int = Field(ge=0, le=604_800)
    generated_at: datetime
    traffic_applied: bool
    attribution: str = Field(min_length=1, max_length=300)
    provider_reference: HttpUrl | None = None

    @field_validator("generated_at")
    @classmethod
    def generated_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("경로 generated_at에는 시간대가 필요합니다.")
        return value

    @model_validator(mode="after")
    def remaining_values_cannot_exceed_totals(self) -> "ServerRoute":
        if self.remaining_distance_m > self.distance_m:
            raise ValueError("남은 거리는 전체 경로 거리보다 클 수 없습니다.")
        if self.remaining_duration_seconds > self.duration_seconds:
            raise ValueError("남은 시간은 전체 예상 시간보다 클 수 없습니다.")
        return self


class OperationsContext(OperationsModel):
    dispatch_station_name: str | None = Field(default=None, max_length=160)
    responder_position: ResponderPosition | None = None
    route: ServerRoute | None = None
    journey_state: Literal["DISPATCHED", "EN_ROUTE", "ARRIVED", "ON_SCENE"] = (
        "DISPATCHED"
    )

    @model_validator(mode="after")
    def route_requires_responder_position(self) -> "OperationsContext":
        if self.route is not None and self.responder_position is None:
            raise ValueError("route를 전달하려면 responder_position이 필요합니다.")
        return self


class AgentMapPoint(OperationsModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    label: str = Field(min_length=1, max_length=200)
    source: str = Field(min_length=1, max_length=80)
    observed_at: datetime | None = None
    accuracy_m: float | None = Field(default=None, ge=0, le=5_000)
    is_simulation: bool = False


class AgentRouteState(OperationsModel):
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
    geometry: RouteGeometry | None = None
    total_distance_m: int | None = Field(default=None, ge=0)
    remaining_distance_m: int | None = Field(default=None, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0)
    progress_ratio: float | None = Field(default=None, ge=0, le=1)
    progress_ratio_is_probability: Literal[False] = False
    traffic_applied: bool | None = None
    generated_at: datetime | None = None
    attribution: str | None = Field(default=None, max_length=300)
    message: str = Field(min_length=1, max_length=500)


class AgentMapRenderingContract(OperationsModel):
    geometry_format: Literal["GEOJSON_RFC7946"] = "GEOJSON_RFC7946"
    recommended_renderer: Literal["MAPLIBRE_GL_JS"] = "MAPLIBRE_GL_JS"
    tile_provider_required: Literal[True] = True
    attribution_required: Literal[True] = True
    public_osm_standard_tiles_for_production: Literal[False] = False
    route_animation_supported: Literal[True] = True


class AgentMapContext(OperationsModel):
    coverage_scope: Literal["NATIONWIDE_KOREA"] = "NATIONWIDE_KOREA"
    incident_position: AgentMapPoint | None = None
    responder_position: AgentMapPoint | None = None
    route: AgentRouteState
    rendering: AgentMapRenderingContract = Field(
        default_factory=AgentMapRenderingContract
    )
    hazard_overlay_status: Literal["NOT_COMPUTED_NO_VALIDATED_DISPERSION_MODEL"] = (
        "NOT_COMPUTED_NO_VALIDATED_DISPERSION_MODEL"
    )


class AgentWorkflowStep(OperationsModel):
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


class AgentToolExecution(OperationsModel):
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


class OperationsAgentSnapshot(OperationsModel):
    schema_version: Literal["chemicheck119-operations-agent-v1"] = (
        "chemicheck119-operations-agent-v1"
    )
    agent_type: Literal["DETERMINISTIC_FIELD_RESPONSE_ORCHESTRATOR"] = (
        "DETERMINISTIC_FIELD_RESPONSE_ORCHESTRATOR"
    )
    phase: Literal[
        "INCIDENT_INTAKE",
        "EN_ROUTE_TRIAGE",
        "ON_SCENE_CONFIRMATION",
        "CONFLICT_SCREENING_COMPLETE",
        "EVIDENCE_REVIEW_REQUIRED",
    ]
    current_objective: str = Field(min_length=1, max_length=500)
    next_actions: list[str] = Field(min_length=1, max_length=8)
    workflow: list[AgentWorkflowStep] = Field(min_length=10, max_length=10)
    tool_executions: list[AgentToolExecution] = Field(min_length=8, max_length=8)
    map_context: AgentMapContext
    autonomous_risk_decision_allowed: Literal[False] = False
    final_decision_authority: Literal["현장 지휘관"] = "현장 지휘관"
    trace_is_chain_of_thought: Literal[False] = False


def _distance_m(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """두 WGS84 좌표의 대권거리 근사값."""

    earth_radius_m = 6_371_000
    lat_a = radians(latitude_a)
    lat_b = radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = radians(longitude_b - longitude_a)
    haversine = (
        sin(delta_lat / 2) ** 2 + cos(lat_a) * cos(lat_b) * sin(delta_lon / 2) ** 2
    )
    return 2 * earth_radius_m * asin(sqrt(min(1.0, haversine)))


def _route_endpoints_match(
    route: ServerRoute,
    responder: AgentMapPoint,
    incident: AgentMapPoint,
) -> bool:
    start_lon, start_lat = route.geometry.coordinates[0]
    end_lon, end_lat = route.geometry.coordinates[-1]
    return (
        _distance_m(
            responder.latitude,
            responder.longitude,
            start_lat,
            start_lon,
        )
        <= ROUTE_ENDPOINT_TOLERANCE_M
        and _distance_m(
            incident.latitude,
            incident.longitude,
            end_lat,
            end_lon,
        )
        <= ROUTE_ENDPOINT_TOLERANCE_M
    )


def _build_map_context(
    *,
    location: dict[str, Any] | None,
    operations: OperationsContext | None,
    processed_at: datetime,
) -> AgentMapContext:
    incident_position = None
    if location and location.get("latitude") is not None:
        incident_position = AgentMapPoint(
            latitude=location["latitude"],
            longitude=location["longitude"],
            label=(
                location.get("facility_name") or location.get("address") or "사고 위치"
            ),
            source=location.get("coordinate_source") or "REQUEST_SUPPLIED",
            observed_at=location.get("resolved_at"),
            is_simulation=(location.get("coordinate_source") == "DEMO_FIXTURE"),
        )

    responder_position = None
    if operations and operations.responder_position:
        current = operations.responder_position
        responder_position = AgentMapPoint(
            latitude=current.latitude,
            longitude=current.longitude,
            label=operations.dispatch_station_name or "출동대 현재 위치",
            source=current.source,
            observed_at=current.observed_at,
            accuracy_m=current.accuracy_m,
            is_simulation=current.source == "DEMO_SIMULATION",
        )

    route_input = operations.route if operations else None
    journey_state = operations.journey_state if operations else "DISPATCHED"
    if incident_position is None:
        status = "INCIDENT_LOCATION_REQUIRED"
        message = "사고 위치 좌표가 없어 지도 이동 경로를 계산할 수 없습니다."
    elif responder_position is None:
        status = "RESPONDER_POSITION_REQUIRED"
        message = "차량 MDT 또는 대원 단말의 현재 위치가 필요합니다."
    elif journey_state in {"ARRIVED", "ON_SCENE"}:
        status = "ARRIVED"
        message = "백엔드가 현장 도착 상태로 전달했습니다."
    elif (
        processed_at - responder_position.observed_at.astimezone(timezone.utc)
        > POSITION_STALE_AFTER
    ):
        status = "POSITION_STALE"
        message = "현재 위치가 5분 이상 갱신되지 않아 ETA를 표시하지 않습니다."
    elif route_input is None:
        status = "ROUTE_UNAVAILABLE"
        message = "서버 측 길찾기 결과가 없어 직선 경로나 ETA를 생성하지 않습니다."
    elif not _route_endpoints_match(
        route_input,
        responder_position,
        incident_position,
    ):
        status = "ROUTE_ENDPOINT_MISMATCH"
        message = (
            "도로 경로의 시작·도착점이 현재 위치·사고 위치와 달라 표시를 차단했습니다."
        )
    elif route_input.mode == "DEMO_SIMULATION":
        status = "DEMO_SIMULATION"
        message = "실제 길찾기 결과가 아닌 발표용 시뮬레이션 경로입니다."
    else:
        status = "AVAILABLE"
        message = "백엔드가 서버 측 길찾기 사업자에서 받은 경로입니다."

    route_metadata_visible = route_input is not None and incident_position is not None
    route_visible = route_metadata_visible and status != "ROUTE_ENDPOINT_MISMATCH"
    eta_allowed = status in {"AVAILABLE", "DEMO_SIMULATION"}
    progress_ratio = None
    if route_visible and route_input and route_input.distance_m:
        progress_ratio = round(
            1 - (route_input.remaining_distance_m / route_input.distance_m), 4
        )

    return AgentMapContext(
        incident_position=incident_position,
        responder_position=responder_position,
        route=AgentRouteState(
            status=status,
            provider=(
                route_input.provider if route_metadata_visible and route_input else None
            ),
            provider_mode=(
                route_input.mode if route_metadata_visible and route_input else None
            ),
            route_id=(
                route_input.route_id if route_metadata_visible and route_input else None
            ),
            geometry=route_input.geometry if route_visible and route_input else None,
            total_distance_m=(
                route_input.distance_m if route_visible and route_input else None
            ),
            remaining_distance_m=(
                route_input.remaining_distance_m
                if route_visible and route_input
                else None
            ),
            eta_seconds=(
                route_input.remaining_duration_seconds
                if eta_allowed and route_input
                else None
            ),
            progress_ratio=progress_ratio if eta_allowed else None,
            traffic_applied=(
                route_input.traffic_applied if route_visible and route_input else None
            ),
            generated_at=(
                route_input.generated_at if route_visible and route_input else None
            ),
            attribution=(
                route_input.attribution if route_visible and route_input else None
            ),
            message=message,
        ),
    )


def build_operations_agent_snapshot(
    *,
    analysis_state: str,
    location: dict[str, Any] | None,
    operations: OperationsContext | None,
    parser_output: dict[str, Any],
    substance_candidates: list[dict[str, Any]],
    facility_history: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    incident_confirmed: bool,
    facility_confirmed: bool,
    grounded_rag: dict[str, Any] | BaseModel | None,
    processed_at: datetime,
) -> OperationsAgentSnapshot:
    """실제로 실행한 도구 상태를 다음 행동으로 변환한다."""

    map_context = _build_map_context(
        location=location,
        operations=operations,
        processed_at=processed_at,
    )
    all_confirmed = incident_confirmed and facility_confirmed
    screening_completed = analysis_state in {"COMPLETED", "SCREENING_COMPLETED"}
    inconclusive = analysis_state in {
        "VERIFY_REQUIRED",
        "UNCLASSIFIED",
        "CAMEO_GROUP_SCREENING_ONLY",
    }
    journey_state = operations.journey_state if operations else "DISPATCHED"

    if screening_completed:
        phase = "CONFLICT_SCREENING_COMPLETE"
        objective = "공식 근거와 충돌 스크리닝 결과를 현장 지휘관에게 제시합니다."
    elif inconclusive:
        phase = "EVIDENCE_REVIEW_REQUIRED"
        objective = "공개 근거가 부족한 조합을 안전하게 미분류 상태로 유지합니다."
    elif journey_state in {"ARRIVED", "ON_SCENE"}:
        phase = "ON_SCENE_CONFIRMATION"
        objective = "현장에서 사고물질과 시설물질의 CAS를 각각 확인합니다."
    elif journey_state == "EN_ROUTE":
        phase = "EN_ROUTE_TRIAGE"
        objective = "이동 중 신고·시설 이력·공식 근거를 미리 정리합니다."
    else:
        phase = "INCIDENT_INTAKE"
        objective = "신고 내용과 위치를 구조화해 출동 준비 정보를 만듭니다."

    next_actions: list[str] = []
    if map_context.route.status == "INCIDENT_LOCATION_REQUIRED":
        next_actions.append("디스패치 주소를 지오코딩해 사고 위치 좌표를 확인하세요.")
    elif map_context.route.status == "RESPONDER_POSITION_REQUIRED":
        next_actions.append("차량 MDT 또는 단말 GPS 현재 위치를 연결하세요.")
    elif map_context.route.status == "ROUTE_UNAVAILABLE":
        next_actions.append("백엔드 서버에서 길찾기 API를 호출해 경로를 전달하세요.")
    elif map_context.route.status == "POSITION_STALE":
        next_actions.append("5분 이내의 최신 현재 위치로 갱신하세요.")
    elif map_context.route.status == "ROUTE_ENDPOINT_MISMATCH":
        next_actions.append("현재 위치와 사고 위치로 도로 경로를 다시 조회하세요.")

    if not incident_confirmed:
        next_actions.append("용기 라벨·현장 MSDS 등으로 사고물질 CAS를 확인하세요.")
    if not facility_confirmed:
        next_actions.append("시설물질은 과거 이력과 구분해 현장에서 CAS를 확인하세요.")
    if screening_completed:
        next_actions.append(
            "현장 지휘관이 충돌 등급과 공식 근거 링크를 함께 확인하세요."
        )
    elif inconclusive:
        next_actions.append(
            "지원 범위 밖 조합은 공식 MSDS와 추가 기관 자료를 확인하세요."
        )
    next_actions.append("대응 종료 전 전체 대화와 확인 기록을 저장하세요.")

    facility_ran = facility_history is not None
    evidence_ran = True
    rag_status = (
        grounded_rag.status
        if isinstance(grounded_rag, BaseModel)
        else (grounded_rag or {}).get("status")
    )
    route_tool_status = {
        "AVAILABLE": "COMPLETED",
        "DEMO_SIMULATION": "FALLBACK",
        "ARRIVED": "COMPLETED",
        "POSITION_STALE": "UNAVAILABLE",
        "ROUTE_ENDPOINT_MISMATCH": "UNAVAILABLE",
    }.get(map_context.route.status, "WAITING")

    workflow = [
        AgentWorkflowStep(
            step_id="INCIDENT_INGESTION",
            label="신고 접수",
            status="COMPLETED",
            detail="인증된 API 요청의 신고문을 접수했습니다.",
        ),
        AgentWorkflowStep(
            step_id="INCIDENT_PARSING",
            label="신고문 구조화",
            status="COMPLETED",
            detail=f"{parser_output.get('backend', '파서')}로 신고 필드를 구조화했습니다.",
        ),
        AgentWorkflowStep(
            step_id="INCIDENT_LOCATION",
            label="사고 위치 확인",
            status="COMPLETED" if map_context.incident_position else "WAITING",
            detail=(
                "출처가 표시된 사고 좌표를 지도 계약에 연결했습니다."
                if map_context.incident_position
                else "주소 지오코딩 또는 디스패치 좌표가 필요합니다."
            ),
        ),
        AgentWorkflowStep(
            step_id="SUBSTANCE_RESOLUTION",
            label="사고물질 후보 검색",
            status="COMPLETED",
            detail=f"후보 그룹 {len(substance_candidates)}개를 반환하고 자동 확정하지 않았습니다.",
        ),
        AgentWorkflowStep(
            step_id="FACILITY_HISTORY",
            label="시설 과거 이력 검색",
            status="COMPLETED" if facility_ran else "NOT_APPLICABLE",
            detail=(
                "전국 ICIS·PRTR 과거 이력 후보를 조회했습니다."
                if facility_ran
                else "시설명 또는 주소가 없어 시설 이력 검색을 실행하지 않았습니다."
            ),
        ),
        AgentWorkflowStep(
            step_id="EVIDENCE_RETRIEVAL",
            label="공식 근거 검색",
            status="COMPLETED",
            detail=f"근거 결과 {len(evidence)}건을 반환했습니다.",
        ),
        AgentWorkflowStep(
            step_id="ON_SITE_CONFIRMATION",
            label="현장 물질 확인",
            status="COMPLETED" if all_confirmed else "IN_PROGRESS",
            detail=(
                "사고물질과 시설물질의 인증된 확인 레코드가 모두 연결됐습니다."
                if all_confirmed
                else "확인된 CAS 두 개가 모두 모일 때까지 충돌 규칙을 차단합니다."
            ),
        ),
        AgentWorkflowStep(
            step_id="CONFLICT_SCREENING",
            label="충돌 규칙 검토",
            status=(
                "COMPLETED"
                if screening_completed
                else ("COMPLETED" if inconclusive else "BLOCKED")
            ),
            detail=(
                "CAMEO 공개 근거 기반 결정론적 규칙을 실행했습니다."
                if all_confirmed
                else "두 CAS의 현장 확인 전에는 실행하지 않습니다."
            ),
        ),
        AgentWorkflowStep(
            step_id="GROUNDED_EXPLANATION",
            label="근거 제한 설명",
            status=(
                "COMPLETED"
                if rag_status in {"COMPLETED", "FALLBACK_EXTRACTIVE"}
                else "BLOCKED"
            ),
            detail=(
                "검색된 출처 ID 안에서만 설명을 구성했습니다."
                if rag_status in {"COMPLETED", "FALLBACK_EXTRACTIVE"}
                else "충돌 규칙 완료 전에는 대응 설명을 생성하지 않습니다."
            ),
        ),
        AgentWorkflowStep(
            step_id="RESPONSE_RECORD",
            label="대응 기록 저장",
            status="WAITING",
            detail="BE가 대화·분석·현장 확인 ID를 하나의 대응 기록으로 저장해야 합니다.",
        ),
    ]

    tool_executions = [
        AgentToolExecution(
            tool_id="RULE_PARSER",
            status="COMPLETED",
            output_reference="model_outputs.parser",
            summary="규칙 기반 파서 실행 완료",
        ),
        AgentToolExecution(
            tool_id="SUBSTANCE_RESOLVER",
            status="COMPLETED",
            output_reference="model_outputs.substance_candidates",
            summary="물질 후보 검색 완료; 자동 확정 금지",
        ),
        AgentToolExecution(
            tool_id="FACILITY_HISTORY_SEARCH",
            status="COMPLETED" if facility_ran else "NOT_RUN",
            output_reference="model_outputs.facility_history_candidates",
            summary="전국 과거 공개 이력 후보 검색",
        ),
        AgentToolExecution(
            tool_id="HYBRID_EVIDENCE_RETRIEVER",
            status="COMPLETED" if evidence_ran else "NOT_RUN",
            output_reference="evidence",
            summary=f"정확 검색·BM25·TF-IDF·RRF 근거 검색 {len(evidence)}건",
        ),
        AgentToolExecution(
            tool_id="CONFIRMATION_GATE",
            status="COMPLETED" if all_confirmed else "WAITING",
            output_reference="confirmation_gate",
            summary="확인된 CAS 두 개가 있어야 규칙 실행 가능",
        ),
        AgentToolExecution(
            tool_id="CAMEO_RULE_ENGINE",
            status="COMPLETED" if all_confirmed else "BLOCKED",
            output_reference="conflict_review",
            summary="LLM이 아닌 공개 근거 기반 결정론적 충돌 스크리닝",
        ),
        AgentToolExecution(
            tool_id="GROUNDED_RAG",
            status=(
                "COMPLETED"
                if rag_status == "COMPLETED"
                else ("FALLBACK" if rag_status == "FALLBACK_EXTRACTIVE" else "BLOCKED")
            ),
            output_reference="grounded_rag",
            summary="검색된 근거 범위 안에서만 대응 설명",
        ),
        AgentToolExecution(
            tool_id="SERVER_ROUTE_PROVIDER",
            status=route_tool_status,
            output_reference="agent.map_context.route",
            summary=map_context.route.message,
        ),
    ]

    return OperationsAgentSnapshot(
        phase=phase,
        current_objective=objective,
        next_actions=next_actions[:8],
        workflow=workflow,
        tool_executions=tool_executions,
        map_context=map_context,
    )


__all__ = [
    "OperationsContext",
    "OperationsAgentSnapshot",
    "ResponderPosition",
    "RouteGeometry",
    "ServerRoute",
    "build_operations_agent_snapshot",
]
