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


def test_shared_unconfirmed_request_example_is_valid_contract_fixture() -> None:
    payload = json.loads(UNCONFIRMED_REQUEST_PATH.read_text(encoding="utf-8"))

    validated = IncidentAnalyzeRequest.model_validate(payload)

    assert validated.incident_id == "INC-EXAMPLE-0001"
    assert validated.confirmed_incident_substance is None
    assert validated.confirmed_facility_substance is None
