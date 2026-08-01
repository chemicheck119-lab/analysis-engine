from __future__ import annotations

import json
from pathlib import Path

from chemiguard119.api import create_app
from chemiguard119.agent_loop import IncidentAgentStepRequest
from chemiguard119.api_models import (
    API_SCHEMA_VERSION,
    AnalysisResponse,
    IncidentAnalyzeRequest,
    SubstanceDiscoveryRequest,
    SubstanceDiscoveryResponse,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "contracts/model-api-integration-v1.json"
UNCONFIRMED_REQUEST_PATH = (
    PROJECT_ROOT / "examples/api/incident_unconfirmed_request.json"
)
UNCONFIRMED_RESPONSE_PATH = (
    PROJECT_ROOT / "examples/api/incident_unconfirmed_response.json"
)
MATERIAL_DISCOVERY_REQUEST_PATH = (
    PROJECT_ROOT / "examples/api/material_discovery_request.json"
)
MATERIAL_DISCOVERY_RESPONSE_PATH = (
    PROJECT_ROOT / "examples/api/material_discovery_response.json"
)
AGENT_STEP_REQUEST_PATH = PROJECT_ROOT / "examples/api/incident_agent_step_request.json"


def test_cross_repository_contract_matches_fastapi_schema() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    openapi = create_app(runtime=None, allow_anonymous=True).openapi()
    primary = contract["service"]["primary_endpoint"]
    incident_agent = contract["service"]["incident_agent_endpoint"]
    material_discovery = contract["service"]["material_discovery_endpoint"]

    assert contract["api_schema_version"] == API_SCHEMA_VERSION
    assert primary["path"] in openapi["paths"]
    assert primary["method"].lower() in openapi["paths"][primary["path"]]
    assert incident_agent["path"] in openapi["paths"]
    assert incident_agent["method"].lower() in openapi["paths"][incident_agent["path"]]
    assert material_discovery["path"] in openapi["paths"]
    assert (
        material_discovery["method"].lower()
        in openapi["paths"][material_discovery["path"]]
    )
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
    agent = contract["dashboard_bff"]["incident_agent_loop"]
    assert agent["memory_mode"] == "BE_PERSISTED_EXTERNAL_MEMORY"
    assert agent["memory_can_trigger_rule"] is False
    assert agent["autonomous_risk_decision_allowed"] is False
    assert agent["trace_is_chain_of_thought"] is False


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
    assert (
        "MULTI_FIELD_PROPERTY_PROFILE_RETRIEVAL"
        in policy["material_search_capabilities"]
    )
    assert (
        "PROPERTY_ONLY_AUTO_CONFIRMATION" in policy["material_search_not_yet_supported"]
    )
    assert policy["material_discovery"] == {
        "minimum_distinct_property_fields": 2,
        "minimum_distinct_property_tokens": 2,
        "candidate_only": True,
        "requires_responder_confirmation": True,
        "rule_eligible": False,
        "risk_display_allowed": False,
        "empty_evidence_means_safe": False,
        "no_reliable_candidate_means_absent": False,
        "no_reliable_candidate_means_safe": False,
        "no_reliable_candidate_next_action": "관찰 정보 보강 및 외부 공식 MSDS 확인",
        "property_source_label": "소방청 울산 화학물질 정보 기반 관찰 후보",
        "evidence_card_body_label": "공식 문서 발췌",
        "ranking_model_version": "material-evidence-ranker-v1",
        "ranking_training_status": ("NOT_SUPERVISED_INSUFFICIENT_REVIEWED_LABELS"),
        "ranking_score_semantics": "CANDIDATE_ORDERING_NOT_PROBABILITY",
        "next_best_check_policy_version": "material-next-best-check-v1",
        "ranking_score_must_not_be_displayed_as_confidence": True,
        "warning_fields_must_be_preserved": [
            "evidence_warning",
            "evidence_notice",
            "cas_link_warning",
            "evidence.cas_link_status",
        ],
    }
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


def test_shared_agent_step_request_is_valid_contract_fixture() -> None:
    payload = json.loads(AGENT_STEP_REQUEST_PATH.read_text(encoding="utf-8"))

    validated = IncidentAgentStepRequest.model_validate(payload)

    assert validated.analysis.incident_id == "INC-AGENT-20260801-0001"
    assert validated.memory is None
    assert validated.max_actions == 6


def test_shared_unconfirmed_response_is_safe_dashboard_fixture() -> None:
    payload = json.loads(UNCONFIRMED_RESPONSE_PATH.read_text(encoding="utf-8"))

    validated = AnalysisResponse.model_validate(payload)

    assert validated.state == "AWAITING_SUBSTANCE_CONFIRMATION"
    assert validated.confirmation_gate.all_required_confirmed is False
    assert validated.conflict_review.executed is False
    assert validated.grounded_rag is not None
    assert validated.grounded_rag.status == "NOT_RUN_REQUIRES_CONFIRMED_PAIR"
    mentions = validated.model_outputs["parser"]["substance_mentions"]
    assert [(item["surface_text"], item["role"]) for item in mentions] == [
        ("차아염소산나트륨", "INCIDENT"),
        ("염산", "FACILITY"),
    ]
    assert [
        item["candidates"][0]["cas_number"]
        for item in validated.model_outputs["substance_candidates"]
    ] == ["7681-52-9", "7647-01-0"]
    facility_history = validated.model_outputs["facility_history_candidates"]
    assert facility_history["status"] == "NO_HISTORY_MATCH"
    assert facility_history["results"] == []


def test_material_discovery_examples_are_valid_dashboard_contracts() -> None:
    request = json.loads(MATERIAL_DISCOVERY_REQUEST_PATH.read_text(encoding="utf-8"))
    response = json.loads(MATERIAL_DISCOVERY_RESPONSE_PATH.read_text(encoding="utf-8"))

    validated_request = SubstanceDiscoveryRequest.model_validate(request)
    validated_response = SubstanceDiscoveryResponse.model_validate(response)

    assert validated_request.top_k == 3
    assert validated_response.candidates[0].cas_number == "78-93-3"
    assert validated_response.candidates[0].requires_responder_confirmation is True
    assert validated_response.candidates[0].rule_eligible is False
    assert validated_response.candidates[0].risk_determination_allowed is False


def test_record_save_contract_saves_before_reset_and_stays_in_backend() -> None:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    workflow = contract["dashboard_workflow"]

    assert workflow["confirmation_button"]["recommended_label"] == "현장 물질 확인"
    assert workflow["record_save"]["owner"] == "BE_Repository"
    assert workflow["record_save"]["reset_before_save_success_allowed"] is False
    assert workflow["record_save"]["model_api_is_stateless"] is True
    assert workflow["record_save"]["sequence"][-1] == "화면 초기화"
