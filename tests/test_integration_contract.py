from __future__ import annotations

import json
from pathlib import Path

from chemiguard119.api import create_app
from chemiguard119.api_models import (
    API_SCHEMA_VERSION,
    AnalysisResponse,
    IncidentAnalyzeRequest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "contracts/model-api-integration-v1.json"
UNCONFIRMED_REQUEST_PATH = (
    PROJECT_ROOT / "examples/api/incident_unconfirmed_request.json"
)
UNCONFIRMED_RESPONSE_PATH = (
    PROJECT_ROOT / "examples/api/incident_unconfirmed_response.json"
)


def test_cross_repository_contract_matches_fastapi_schema() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    openapi = create_app(runtime=None, allow_anonymous=True).openapi()
    primary = contract["service"]["primary_endpoint"]

    assert contract["api_schema_version"] == API_SCHEMA_VERSION
    assert primary["path"] in openapi["paths"]
    assert primary["method"].lower() in openapi["paths"][primary["path"]]
    assert contract["service"]["liveness_path"] in openapi["paths"]
    assert contract["service"]["readiness_path"] in openapi["paths"]
    assert contract["service"]["metadata_path"] in openapi["paths"]
    assert set(contract["compatibility"]["required_analysis_response_fields"]) == set(
        AnalysisResponse.model_fields
    )


def test_cross_repository_contract_keeps_model_secret_in_backend() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["security"]["model_api_public_browser_access"] is False
    assert contract["security"]["api_key_owner"].startswith("BE_Repository")
    assert contract["security"]["request_id_header"] == "X-Request-Id"
    assert contract["client_policy"]["request_id_is_idempotency_key"] is False
    assert contract["merge_order"][0].startswith("llm")
    assert contract["merge_order"][1].startswith("BE_Repository")
    assert contract["merge_order"][2].startswith("FE_Repository")


def test_dashboard_contract_never_displays_risk_before_confirmation() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    policy = contract["presentation_policy"]
    unconfirmed = policy["unconfirmed"]

    assert set(policy["unconfirmed_states"]) == {
        "AWAITING_SUBSTANCE_CONFIRMATION",
        "AWAITING_INCIDENT_CONFIRMATION",
        "AWAITING_FACILITY_CONFIRMATION",
    }
    assert unconfirmed["risk_display_allowed"] is False
    assert unconfirmed["specific_reaction_display_allowed"] is False
    assert unconfirmed["recommended_response_display_allowed"] is False
    assert unconfirmed["required_conflict_executed"] is False
    assert (
        unconfirmed["required_conflict_review_status"]
        == "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"
    )
    assert policy["confirmed"]["risk_display_requires_all_confirmations"] is True
    assert policy["confirmed"]["risk_display_requires_executed_review"] is True
    assert policy["confirmed"]["ordinal_scale_is_probability"] is False
    assert policy["confirmed"]["low_means_safe"] is False


def test_dashboard_contract_does_not_overpromise_v1_capabilities() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    policy = contract["presentation_policy"]

    assert policy["candidate_score_semantics"] == "RANKING_NOT_PROBABILITY"
    assert (
        policy["facility_history_semantics"]
        == "HISTORICAL_CANDIDATE_NOT_CURRENT_INVENTORY"
    )
    assert (
        policy["planned_actions_semantics"]
        == "UNVALIDATED_USER_INPUT_NOT_AI_RECOMMENDATION"
    )
    assert "PROPERTY_ONLY_IDENTIFICATION" in policy["material_search_not_yet_supported"]
    assert (
        policy["v1_pair_limit"][
            "multiple_executed_pair_reviews_in_one_response_allowed"
        ]
        is False
    )


def test_shared_unconfirmed_request_example_is_valid_contract_fixture() -> None:
    payload = json.loads(UNCONFIRMED_REQUEST_PATH.read_text(encoding="utf-8"))

    validated = IncidentAnalyzeRequest.model_validate(payload)

    assert validated.incident_id == "INC-EXAMPLE-0001"
    assert validated.confirmed_incident_substance is None
    assert validated.confirmed_facility_substance is None


def test_shared_unconfirmed_response_is_safe_dashboard_fixture() -> None:
    payload = json.loads(UNCONFIRMED_RESPONSE_PATH.read_text(encoding="utf-8"))

    validated = AnalysisResponse.model_validate(payload)

    assert validated.state == "AWAITING_SUBSTANCE_CONFIRMATION"
    assert validated.confirmation_gate.all_required_confirmed is False
    assert validated.conflict_review["executed"] is False
    facility = validated.model_outputs["facility_history_candidates"]["results"][0]
    assert facility["current_inventory_confirmed"] is False
    assert facility["rule_eligible"] is False
