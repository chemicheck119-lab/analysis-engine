from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from chemiguard119.operations import (
    OperationsContext,
    ServerRoute,
    build_operations_agent_snapshot,
)


PROCESSED_AT = datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc)


def _route(*, mode: str = "LIVE_API") -> dict[str, object]:
    return {
        "provider": "KAKAO_MOBILITY",
        "mode": mode,
        "route_id": "ROUTE-001",
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [126.8311, 37.2065],
                [126.9000, 37.2100],
                [126.9417, 37.2181],
            ],
        },
        "distance_m": 10_000,
        "duration_seconds": 1_200,
        "remaining_distance_m": 4_000,
        "remaining_duration_seconds": 480,
        "generated_at": "2026-08-01T02:59:00+00:00",
        "traffic_applied": True,
        "attribution": "Kakao Mobility Directions API",
        "provider_reference": "https://developers.kakaomobility.com/",
    }


def _operations(*, route: dict[str, object] | None = None) -> OperationsContext:
    return OperationsContext.model_validate(
        {
            "dispatch_station_name": "화성소방서",
            "journey_state": "EN_ROUTE",
            "responder_position": {
                "latitude": 37.2065,
                "longitude": 126.8311,
                "observed_at": "2026-08-01T02:59:00+00:00",
                "source": "MDT_DEVICE_GPS",
                "accuracy_m": 12,
            },
            "route": route,
        }
    )


def _snapshot(
    *,
    operations: OperationsContext | None,
    state: str = "AWAITING_SUBSTANCE_CONFIRMATION",
):
    return build_operations_agent_snapshot(
        analysis_state=state,
        location={
            "facility_name": "OO전자 공장",
            "address": "경기 화성시 팔탄면",
            "latitude": 37.2181,
            "longitude": 126.9417,
            "coordinate_source": "DISPATCH_SYSTEM",
            "resolved_at": datetime(2026, 8, 1, 2, 58, tzinfo=timezone.utc),
        },
        operations=operations,
        parser_output={"backend": "RULE_BASED_V1"},
        substance_candidates=[{"surface_text": "염산"}],
        facility_history={"status": "CANDIDATES_FOUND", "results": []},
        evidence=[{"evidence_id": "EVD-001"}],
        incident_confirmed=state == "SCREENING_COMPLETED",
        facility_confirmed=state == "SCREENING_COMPLETED",
        grounded_rag=(
            {"status": "FALLBACK_EXTRACTIVE"}
            if state == "SCREENING_COMPLETED"
            else {"status": "NOT_RUN_REQUIRES_CONFIRMED_PAIR"}
        ),
        processed_at=PROCESSED_AT,
    )


def test_en_route_agent_exposes_real_provider_route_and_progress() -> None:
    snapshot = _snapshot(operations=_operations(route=_route()))

    assert snapshot.phase == "EN_ROUTE_TRIAGE"
    assert snapshot.map_context.coverage_scope == "NATIONWIDE_KOREA"
    assert snapshot.map_context.route.status == "AVAILABLE"
    assert snapshot.map_context.route.eta_seconds == 480
    assert snapshot.map_context.route.progress_ratio == 0.6
    assert snapshot.map_context.route.progress_ratio_is_probability is False
    assert snapshot.map_context.rendering.recommended_renderer == "MAPLIBRE_GL_JS"
    assert (
        snapshot.map_context.rendering.public_osm_standard_tiles_for_production is False
    )
    assert snapshot.map_context.hazard_overlay_status == (
        "NOT_COMPUTED_NO_VALIDATED_DISPERSION_MODEL"
    )
    assert snapshot.autonomous_risk_decision_allowed is False
    assert snapshot.trace_is_chain_of_thought is False


def test_missing_route_never_invents_geometry_or_eta() -> None:
    snapshot = _snapshot(operations=_operations())

    assert snapshot.map_context.route.status == "ROUTE_UNAVAILABLE"
    assert snapshot.map_context.route.geometry is None
    assert snapshot.map_context.route.eta_seconds is None
    assert snapshot.map_context.route.progress_ratio is None
    assert any("길찾기 API" in action for action in snapshot.next_actions)


def test_demo_route_is_explicitly_labeled_simulation() -> None:
    snapshot = _snapshot(operations=_operations(route=_route(mode="DEMO_SIMULATION")))

    assert snapshot.map_context.route.status == "DEMO_SIMULATION"
    assert "시뮬레이션" in snapshot.map_context.route.message
    route_tool = next(
        item
        for item in snapshot.tool_executions
        if item.tool_id == "SERVER_ROUTE_PROVIDER"
    )
    assert route_tool.status == "FALLBACK"


def test_stale_position_hides_eta_and_requests_update() -> None:
    operations = _operations(route=_route())
    stale = operations.model_copy(
        update={
            "responder_position": operations.responder_position.model_copy(
                update={"observed_at": datetime(2026, 8, 1, 2, 40, tzinfo=timezone.utc)}
            )
        }
    )
    snapshot = _snapshot(operations=stale)

    assert snapshot.map_context.route.status == "POSITION_STALE"
    assert snapshot.map_context.route.eta_seconds is None
    assert snapshot.map_context.route.progress_ratio is None
    assert any("최신 현재 위치" in action for action in snapshot.next_actions)


def test_completed_screening_changes_agent_phase_without_autonomous_decision() -> None:
    snapshot = _snapshot(
        operations=_operations(route=_route()), state="SCREENING_COMPLETED"
    )

    assert snapshot.phase == "CONFLICT_SCREENING_COMPLETE"
    assert snapshot.final_decision_authority == "현장 지휘관"
    assert snapshot.autonomous_risk_decision_allowed is False
    screening = next(
        item for item in snapshot.workflow if item.step_id == "CONFLICT_SCREENING"
    )
    assert screening.status == "COMPLETED"


def test_route_contract_rejects_invalid_geometry_and_remaining_values() -> None:
    invalid_geometry = _route()
    invalid_geometry["geometry"] = {
        "type": "LineString",
        "coordinates": [[190.0, 37.0], [126.0, 37.0]],
    }
    too_much_remaining = _route()
    too_much_remaining["remaining_distance_m"] = 10_001

    with pytest.raises(ValidationError):
        ServerRoute.model_validate(invalid_geometry)
    with pytest.raises(ValidationError):
        ServerRoute.model_validate(too_much_remaining)


def test_route_for_different_incident_is_blocked() -> None:
    wrong_route = _route()
    wrong_route["geometry"] = {
        "type": "LineString",
        "coordinates": [[129.0, 35.5], [129.1, 35.6]],
    }
    snapshot = _snapshot(operations=_operations(route=wrong_route))

    assert snapshot.map_context.route.status == "ROUTE_ENDPOINT_MISMATCH"
    assert snapshot.map_context.route.geometry is None
    assert snapshot.map_context.route.eta_seconds is None
    assert snapshot.map_context.route.provider == "KAKAO_MOBILITY"
    assert any("다시 조회" in action for action in snapshot.next_actions)


def test_route_requires_current_responder_position() -> None:
    with pytest.raises(ValidationError):
        OperationsContext.model_validate(
            {"journey_state": "EN_ROUTE", "route": _route()}
        )
