from __future__ import annotations

import copy
import csv
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from chemiguard119.api import create_app
from chemiguard119.dashboard_contract import (
    DASHBOARD_BFF_SCHEMA_VERSION,
    FACILITY_HISTORY_LABEL,
    FACILITY_HISTORY_SEMANTICS,
    DashboardAnalysisResponse,
    DashboardAwaitingAnalysisResponse,
    DashboardCompletedAnalysisResponse,
    DashboardConfirmationRequest,
    DashboardConfirmationResponse,
    DashboardErrorResponse,
    DashboardIncidentAnalyzeRequest,
    DashboardInconclusiveAnalysisResponse,
    DashboardMaterialDiscoveryRequest,
    DashboardMaterialDiscoveryResponse,
    DashboardRecordSaveRequest,
    DashboardRecordSaveResponse,
    build_dashboard_bff_openapi,
    project_completed_model_result,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BFF_EXAMPLES = PROJECT_ROOT / "examples" / "bff"
BFF_OPENAPI = PROJECT_ROOT / "contracts" / "dashboard-bff-v1.openapi.json"
MODEL_OPENAPI = PROJECT_ROOT / "contracts" / "generated" / "model-api-v1.openapi.json"
TYPESCRIPT_CONTRACT = PROJECT_ROOT / "contracts" / "dashboard-bff-v1.types.ts"
TYPESCRIPT_CLIENT = (
    PROJECT_ROOT / "examples" / "integration" / "dashboard-bff-client.ts"
)
INTEGRATION_MANIFEST = PROJECT_ROOT / "contracts" / "model-api-integration-v1.json"
VERIFIED_PAIR_SNAPSHOT = (
    PROJECT_ROOT / "data" / "evaluation" / "verified_pair_snapshot_2024.json"
)
CAMEO_CROSSWALK = PROJECT_ROOT / "config" / "cameo_crosswalk.csv"
DASHBOARD_PAIR_CONTRACT = (
    PROJECT_ROOT / "config" / "dashboard_public_pair_contract.json"
)
MODEL_COMPLETED_RESULT = (
    PROJECT_ROOT / "examples" / "api" / "conflict_screening_completed_result.json"
)
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "release-model.yml"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _contains_key(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            key in keys or _contains_key(item, keys) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_key(item, keys) for item in value)
    return False


def test_openapi_snapshots_match_executable_contracts() -> None:
    assert _load_json(BFF_OPENAPI) == build_dashboard_bff_openapi()
    assert (
        _load_json(MODEL_OPENAPI)
        == create_app(
            runtime=None,
            allow_anonymous=False,
        ).openapi()
    )


def test_dashboard_openapi_is_backend_owned_and_never_exposes_model_key() -> None:
    schema = _load_json(BFF_OPENAPI)
    expected_paths = {
        "/api/c2guard/v1/substances/discover",
        "/api/c2guard/v1/incidents/analyze",
        "/api/c2guard/v1/incidents/{incidentId}/confirmations",
        "/api/c2guard/v1/incidents/{incidentId}/record",
    }

    assert schema["x-contract-version"] == DASHBOARD_BFF_SCHEMA_VERSION
    assert schema["x-implementation-status"] == "CONTRACT_ONLY_NOT_IMPLEMENTED_IN_LLM"
    assert schema["x-completed-result-pair-contract"] == {
        "path": "config/dashboard_public_pair_contract.json",
        "version": "dashboard-public-pair-presentation-v1",
        "matching": "EXACT_PAIR_PRESENTATION_FIELDS",
        "unsupportedPairBehavior": "REJECT_COMPLETED_RESULT",
    }
    assert set(schema["paths"]) == expected_paths
    assert set(schema["components"]["securitySchemes"]) == {"ServiceSession"}
    assert (
        "201"
        in schema["paths"]["/api/c2guard/v1/incidents/{incidentId}/confirmations"][
            "post"
        ]["responses"]
    )
    assert (
        "201"
        in schema["paths"]["/api/c2guard/v1/incidents/{incidentId}/record"]["post"][
            "responses"
        ]
    )
    completed_required = set(
        schema["components"]["schemas"]["DashboardCompletedConflictResult"]["required"]
    )
    assert {
        "ruleId",
        "ruleVersion",
        "scope",
        "policyMode",
        "mappingProvenance",
        "evidenceProvenance",
    } <= completed_required
    assert set(
        schema["components"]["schemas"]["DashboardRecordSaveRequest"]["required"]
    ) == {
        "conversationStartedAt",
        "messages",
        "analysisIds",
        "confirmationIds",
    }
    record_checks = schema["paths"]["/api/c2guard/v1/incidents/{incidentId}/record"][
        "post"
    ]["x-backend-required-checks"]
    assert any("incidentId" in item for item in record_checks)
    assert any("409" in item for item in record_checks)

    for path_item in schema["paths"].values():
        operation = path_item["post"]
        assert operation["x-implementation-owner"] == "BE_Repository"
        assert operation["x-model-api-direct-browser-call-allowed"] is False
        assert operation["security"] == [{"ServiceSession": []}]
        headers = [
            item.get("name")
            for item in operation.get("parameters", [])
            if item.get("in") == "header"
        ]
        assert "X-API-Key" not in headers


def test_cross_repository_manifest_points_to_versioned_bff_bundle() -> None:
    manifest = _load_json(INTEGRATION_MANIFEST)
    dashboard = manifest["dashboard_bff"]
    security = manifest["security"]

    assert dashboard["contract_version"] == DASHBOARD_BFF_SCHEMA_VERSION
    assert dashboard["implementation_owner"] == "BE_Repository"
    assert dashboard["implementation_status"] == "CONTRACT_ONLY_NOT_IMPLEMENTED_IN_LLM"
    assert (PROJECT_ROOT / dashboard["openapi"]).is_file()
    assert (PROJECT_ROOT / dashboard["typescript_types"]).is_file()
    assert (PROJECT_ROOT / dashboard["typescript_client_example"]).is_file()
    assert dashboard["recommended_same_origin_base_url"] == "/api/c2guard/v1"
    assert (
        dashboard["completed_result_projection"][
            "backend_must_not_generate_chemical_fields"
        ]
        is True
    )
    assert (
        PROJECT_ROOT / dashboard["completed_result_projection"]["source_fixture"]
    ).is_file()
    assert (
        PROJECT_ROOT
        / dashboard["completed_result_projection"]["pair_presentation_contract"]
    ).is_file()
    assert (
        "hasIncompatible"
        in dashboard["deprecated_frontend_shape"]["fields_not_allowed_in_v1"]
    )
    assert security["browser_calls_only_dashboard_bff"] is True
    assert security["browser_model_api_key_allowed"] is False
    assert security["model_api_cors_required"] is False

    for fixture in manifest["fixtures"]["dashboard_bff"].values():
        assert (PROJECT_ROOT / fixture).is_file()


def test_public_pair_presentation_contract_pins_all_verified_pairs() -> None:
    contract = _load_json(DASHBOARD_PAIR_CONTRACT)
    snapshot = _load_json(VERIFIED_PAIR_SNAPSHOT)
    with CAMEO_CROSSWALK.open(encoding="utf-8", newline="") as stream:
        crosswalk = {row["cas_number"]: row for row in csv.DictReader(stream)}

    assert contract["contract_version"] == "dashboard-public-pair-presentation-v1"
    assert contract["pair_count"] == snapshot["expected_unique_pair_count"] == 15
    assert contract["is_probability"] is False
    assert contract["does_not_confirm_on_site_presence"] is True
    assert {pair["pair_key"] for pair in contract["pairs"]} == {
        "|".join(sorted((pair["cas_a"], pair["cas_b"]))) for pair in snapshot["pairs"]
    }
    for pair in contract["pairs"]:
        assert len(pair["mappings"]) == 2
        for mapping in pair["mappings"]:
            source = crosswalk[mapping["cas_number"]]
            assert mapping["cameo_chemical_id"] == source["cameo_chemical_id"]
            assert mapping["selected_form"] == source["selected_form"]
            assert mapping["evidence_url"] == source["evidence_url"]


def test_generated_contract_drift_and_pair_contract_are_release_gates() -> None:
    ci_workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    release_workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "python scripts/contracts/export_contracts.py --check" in ci_workflow
    assert "config/dashboard_public_pair_contract.json" in release_workflow


def test_material_discovery_fixtures_are_candidate_only() -> None:
    request = DashboardMaterialDiscoveryRequest.model_validate(
        _load_json(BFF_EXAMPLES / "material_discovery_request.json")
    )
    candidates = DashboardMaterialDiscoveryResponse.model_validate(
        _load_json(BFF_EXAMPLES / "material_discovery_candidates_response.json")
    )
    no_match = DashboardMaterialDiscoveryResponse.model_validate(
        _load_json(BFF_EXAMPLES / "material_discovery_no_match_response.json")
    )

    assert request.top_k == 3
    assert candidates.candidates[0].cas_number == "78-93-3"
    assert candidates.candidates[0].evidence_cards == []
    assert candidates.requires_responder_confirmation is True
    assert candidates.candidate_score_is_probability is False
    assert candidates.risk_display_allowed is False
    assert no_match.status == "NO_RELIABLE_CANDIDATE"
    assert no_match.candidates == []
    assert no_match.no_reliable_candidate_means_absent is False
    assert no_match.no_reliable_candidate_means_safe is False


def test_awaiting_analysis_fixture_has_no_risk_or_reaction_payload() -> None:
    request = DashboardIncidentAnalyzeRequest.model_validate(
        _load_json(BFF_EXAMPLES / "incident_analyze_request.json")
    )
    raw_response = _load_json(
        BFF_EXAMPLES / "incident_awaiting_confirmation_response.json"
    )
    response = DashboardAwaitingAnalysisResponse.model_validate(raw_response)
    union_response = TypeAdapter(DashboardAnalysisResponse).validate_python(
        raw_response
    )

    assert request.text.startswith("차아염소산나트륨")
    assert isinstance(union_response, DashboardAwaitingAnalysisResponse)
    assert response.confirmation_gate.all_required_confirmed is False
    assert response.confirmation_gate.rule_execution_allowed is False
    assert response.conflict_review.executed is False
    assert response.risk_display_allowed is False
    assert response.facility_history.label == FACILITY_HISTORY_LABEL
    assert response.facility_history.semantics == FACILITY_HISTORY_SEMANTICS
    assert not _contains_key(
        raw_response,
        {
            "riskLevel",
            "riskLevelKo",
            "riskScale",
            "briefText",
            "reaction",
            "recommendedResponse",
            "probabilityPercent",
        },
    )


def test_completed_analysis_requires_two_confirmations_and_ordinal_risk() -> None:
    raw_response = _load_json(
        BFF_EXAMPLES / "incident_screening_completed_response.json"
    )
    response = DashboardCompletedAnalysisResponse.model_validate(raw_response)
    union_response = TypeAdapter(DashboardAnalysisResponse).validate_python(
        raw_response
    )

    assert isinstance(union_response, DashboardCompletedAnalysisResponse)
    assert response.confirmation_gate.all_required_confirmed is True
    assert response.confirmation_gate.rule_execution_allowed is True
    assert response.conflict_review.executed is True
    assert response.conflict_review.risk_display_allowed is True
    risk_scale = response.conflict_review.result.risk_scale
    assert risk_scale.is_probability is False
    assert risk_scale.probability_percent is None
    assert risk_scale.low_means_safe is False
    assert response.provenance.expert_reviewed is False
    assert response.provenance.final_decision_authority == "현장 지휘관"
    assert response.conflict_review.result.rule_version == "RUNTIME_MANIFEST_PINNED"
    assert response.conflict_review.result.scope == "PUBLIC_SOURCE_CAMEO_SCREENING"
    assert response.conflict_review.result.policy_mode == "PUBLIC_SOURCE_PILOT_V1"
    assert len(response.conflict_review.result.mapping_provenance) == 2


def test_completed_fixture_is_derived_from_versioned_pair_and_crosswalk() -> None:
    response = DashboardCompletedAnalysisResponse.model_validate(
        _load_json(BFF_EXAMPLES / "incident_screening_completed_response.json")
    )
    result = response.conflict_review.result
    snapshot = _load_json(VERIFIED_PAIR_SNAPSHOT)
    expected_pair = next(
        pair
        for pair in snapshot["pairs"]
        if {pair["cas_a"], pair["cas_b"]} == {result.incident_cas, result.facility_cas}
    )
    with CAMEO_CROSSWALK.open(encoding="utf-8", newline="") as stream:
        crosswalk = {row["cas_number"]: row for row in csv.DictReader(stream)}

    assert result.status == expected_pair["status"]
    assert result.risk_level == expected_pair["risk_level"]
    assert result.risk_level_ko == expected_pair["risk_level_ko"]
    assert result.risk_scale.raw_class_id == expected_pair["raw_class_id"]
    assert set(result.hazard_codes) == set(expected_pair["hazard_codes"])
    assert set(result.gas_products or []) == set(expected_pair["gas_products"])
    assert {str(url) for url in result.evidence_urls} == set(
        expected_pair["evidence_urls"]
    )

    for mapping in result.mapping_provenance:
        source = crosswalk[mapping.cas_number]
        assert mapping.cameo_chemical_id == source["cameo_chemical_id"]
        assert mapping.selected_form == source["selected_form"]
        assert mapping.verification_status == source["verification_status"]
        assert mapping.verification_method == source["verification_method"]
        assert str(mapping.evidence_url) == source["evidence_url"]
        assert mapping.source_product == source["source_product"]
        assert mapping.source_version == source["source_version"]


def test_completed_bff_projection_is_lossless_for_model_safety_fields() -> None:
    source = _load_json(MODEL_COMPLETED_RESULT)
    projected = project_completed_model_result(source)
    displayed = DashboardCompletedAnalysisResponse.model_validate(
        _load_json(BFF_EXAMPLES / "incident_screening_completed_response.json")
    ).conflict_review.result

    assert projected.model_dump(mode="json", by_alias=True) == displayed.model_dump(
        mode="json",
        by_alias=True,
    )


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    [
        (("conflictReview", "result", "incidentCas"), "7681-52-8"),
        (("conflictReview", "result", "riskLevel"), "LOW"),
        (("conflictReview", "result", "riskLevelKo"), "중간"),
        (("conflictReview", "result", "riskScale", "rawClassId"), 0),
        (("conflictReview", "result", "policyMode"), "APPROVED_ONLY"),
        (("conflictReview", "result", "ruleId"), "FAKE"),
        (("conflictReview", "result", "ruleVersion"), ""),
        (("conflictReview", "result", "hazardCodes"), ["SAFE"]),
        (("conflictReview", "result", "briefText"), "안전합니다."),
        (
            ("conflictReview", "result", "evidenceUrls"),
            ["https://example.com/fake"],
        ),
    ],
)
def test_completed_analysis_rejects_mapping_or_risk_corruption(
    path: tuple[str, ...],
    invalid_value: Any,
) -> None:
    payload = _load_json(BFF_EXAMPLES / "incident_screening_completed_response.json")
    cursor: dict[str, Any] = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = invalid_value

    with pytest.raises(ValidationError):
        DashboardCompletedAnalysisResponse.model_validate(payload)


def test_completed_analysis_binds_result_cas_to_mapping_and_evidence() -> None:
    payload = _load_json(BFF_EXAMPLES / "incident_screening_completed_response.json")
    wrong_mapping = copy.deepcopy(payload)
    replacement = copy.deepcopy(
        wrong_mapping["conflictReview"]["result"]["mappingProvenance"][1]
    )
    replacement["role"] = "INCIDENT"
    wrong_mapping["conflictReview"]["result"]["mappingProvenance"][0] = replacement

    wrong_evidence = copy.deepcopy(payload)
    wrong_evidence["conflictReview"]["result"]["evidenceProvenance"][
        "mappingEvidenceUrls"
    ][0] = "https://cameochemicals.noaa.gov/chemical/667"

    for invalid in (wrong_mapping, wrong_evidence):
        with pytest.raises(ValidationError):
            DashboardCompletedAnalysisResponse.model_validate(invalid)


def test_completed_analysis_rejects_internally_consistent_wrong_cameo_mapping() -> None:
    payload = _load_json(BFF_EXAMPLES / "incident_screening_completed_response.json")
    result = payload["conflictReview"]["result"]
    mapping = result["mappingProvenance"][0]
    old_url = mapping["evidenceUrl"]
    wrong_url = "https://cameochemicals.noaa.gov/chemical/667"
    mapping["cameoChemicalId"] = "667"
    mapping["selectedForm"] = "ETHANOL"
    mapping["evidenceUrl"] = wrong_url
    result["evidenceUrls"] = [
        wrong_url if value == old_url else value for value in result["evidenceUrls"]
    ]
    result["evidenceProvenance"]["mappingEvidenceUrls"] = [
        wrong_url if value == old_url else value
        for value in result["evidenceProvenance"]["mappingEvidenceUrls"]
    ]

    with pytest.raises(ValidationError):
        DashboardCompletedAnalysisResponse.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("hazardCodes", ["C"]),
        ("gasProducts", ["H2"]),
        ("requiredChecks", ["현장 확인 없이 즉시 진입"]),
        ("limitations", ["별도 한계 없음"]),
    ],
)
def test_completed_analysis_rejects_allowed_but_wrong_pair_presentation(
    field: str,
    invalid_value: Any,
) -> None:
    payload = _load_json(BFF_EXAMPLES / "incident_screening_completed_response.json")
    payload["conflictReview"]["result"][field] = invalid_value

    with pytest.raises(ValidationError):
        DashboardCompletedAnalysisResponse.model_validate(payload)


def test_awaiting_analysis_state_and_missing_roles_must_match_gate() -> None:
    payload = _load_json(BFF_EXAMPLES / "incident_awaiting_confirmation_response.json")
    wrong_state = copy.deepcopy(payload)
    wrong_state["confirmationGate"]["incidentConfirmed"] = True
    wrong_state["state"] = "AWAITING_INCIDENT_CONFIRMATION"
    wrong_state["conflictReview"]["missingConfirmations"] = ["incident_cas"]

    wrong_missing = copy.deepcopy(payload)
    wrong_missing["conflictReview"]["missingConfirmations"] = ["incident_cas"]

    for invalid in (wrong_state, wrong_missing):
        with pytest.raises(ValidationError):
            DashboardAwaitingAnalysisResponse.model_validate(invalid)


def test_inconclusive_rule_result_requires_open_confirmation_gate() -> None:
    payload = _load_json(BFF_EXAMPLES / "incident_awaiting_confirmation_response.json")
    payload["state"] = "UNCLASSIFIED"
    payload["conflictReview"] = {
        "executed": True,
        "status": "UNCLASSIFIED",
        "result": {
            "kind": "INCONCLUSIVE_RESULT",
            "status": "UNCLASSIFIED",
            "reason": "공개 근거 부족",
            "humanConfirmationRequired": True,
        },
        "riskDisplayAllowed": False,
    }

    with pytest.raises(ValidationError):
        DashboardInconclusiveAnalysisResponse.model_validate(payload)


def test_inconclusive_wrapper_and_result_status_must_match() -> None:
    payload = _load_json(BFF_EXAMPLES / "incident_screening_completed_response.json")
    payload["state"] = "UNCLASSIFIED"
    payload["conflictReview"] = {
        "executed": True,
        "status": "UNCLASSIFIED",
        "result": {
            "kind": "INCONCLUSIVE_RESULT",
            "status": "VERIFY_REQUIRED",
            "reason": "공개 근거 부족",
            "humanConfirmationRequired": True,
        },
        "riskDisplayAllowed": False,
    }
    payload["riskDisplayAllowed"] = False

    with pytest.raises(ValidationError):
        DashboardInconclusiveAnalysisResponse.model_validate(payload)


def test_confirmation_is_separate_from_record_save() -> None:
    confirmation_request = DashboardConfirmationRequest.model_validate(
        _load_json(BFF_EXAMPLES / "confirmation_request.json")
    )
    confirmation_response = DashboardConfirmationResponse.model_validate(
        _load_json(BFF_EXAMPLES / "confirmation_response.json")
    )
    save_request = DashboardRecordSaveRequest.model_validate(
        _load_json(BFF_EXAMPLES / "record_save_request.json")
    )

    assert confirmation_request.role == "INCIDENT"
    assert confirmation_request.cas_number == "7681-52-9"
    assert confirmation_response.confirmation_id.startswith("CNF-")
    assert confirmation_response.reanalyze_required is True
    assert len(save_request.messages) == 2
    assert len(save_request.analysis_ids) == 2
    assert len(save_request.confirmation_ids) == 2


def test_confirmation_rejects_future_time_and_invalid_response_cas() -> None:
    future_request = _load_json(BFF_EXAMPLES / "confirmation_request.json")
    future_request["observedAt"] = "2099-01-01T00:00:00+09:00"
    invalid_response = _load_json(BFF_EXAMPLES / "confirmation_response.json")
    invalid_response["casNumber"] = "7681-52-8"

    with pytest.raises(ValidationError):
        DashboardConfirmationRequest.model_validate(future_request)
    with pytest.raises(ValidationError):
        DashboardConfirmationResponse.model_validate(invalid_response)


def test_record_save_requires_authoritative_analysis_references() -> None:
    payload = _load_json(BFF_EXAMPLES / "record_save_request.json")
    missing_analysis = copy.deepcopy(payload)
    missing_analysis["analysisIds"] = []
    unknown_message_analysis = copy.deepcopy(payload)
    unknown_message_analysis["messages"][1]["analysisId"] = "ANL-NOT-IN-LIST"
    duplicate_confirmation = copy.deepcopy(payload)
    duplicate_confirmation["confirmationIds"] = [
        "CNF-EXAMPLE-0001",
        "CNF-EXAMPLE-0001",
    ]
    naive_conversation_time = copy.deepcopy(payload)
    naive_conversation_time["conversationStartedAt"] = "2026-07-31T14:20:00"
    naive_message_time = copy.deepcopy(payload)
    naive_message_time["messages"][0]["createdAt"] = "2026-07-31T14:20:00"

    for invalid in (
        missing_analysis,
        unknown_message_analysis,
        duplicate_confirmation,
        naive_conversation_time,
        naive_message_time,
    ):
        with pytest.raises(ValidationError):
            DashboardRecordSaveRequest.model_validate(invalid)


def test_only_successful_record_save_allows_screen_reset() -> None:
    success = DashboardRecordSaveResponse.model_validate(
        _load_json(BFF_EXAMPLES / "record_save_success_response.json")
    )
    failure = DashboardErrorResponse.model_validate(
        _load_json(BFF_EXAMPLES / "record_save_failure_response.json")
    )
    unavailable = DashboardErrorResponse.model_validate(
        _load_json(BFF_EXAMPLES / "model_unavailable_error_response.json")
    )
    naive_success = _load_json(BFF_EXAMPLES / "record_save_success_response.json")
    naive_success["savedAt"] = "2026-07-31T14:40:00"

    assert success.reset_allowed is True
    assert failure.reset_allowed is False
    assert failure.error.retryable is True
    assert unavailable.reset_allowed is False
    assert unavailable.error.code == "MODEL_SERVICE_UNAVAILABLE"
    with pytest.raises(ValidationError):
        DashboardRecordSaveResponse.model_validate(naive_success)


def test_typescript_handoff_uses_discriminated_states_and_save_then_reset() -> None:
    typescript = TYPESCRIPT_CONTRACT.read_text(encoding="utf-8")
    client = TYPESCRIPT_CLIENT.read_text(encoding="utf-8")

    assert "hasIncompatible" not in typescript
    assert "lowMeansSafe: false" in typescript
    assert "riskDisplayAllowed: false" in typescript
    assert "riskDisplayAllowed: true" in typescript
    assert "ruleVersion: 'RUNTIME_MANIFEST_PINNED'" in typescript
    assert "config/dashboard_public_pair_contract.json" in typescript
    assert "mappingProvenance" in typescript
    assert "resetAllowed: false" in typescript
    assert "resetAllowed: true" in typescript
    assert "credentials: 'include'" in client
    assert "VITE_CHEMICHECK119_BFF_BASE_URL" in client
    assert "if (saved.resetAllowed)" in client
    assert "confirmAndRefreshIncident" in client
    assert "X-API-Key" not in client
