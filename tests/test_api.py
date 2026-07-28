from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemiguard119 import api, pipeline
from chemiguard119.api import ModelRuntime, create_app
from chemiguard119.rules import APPROVED_ONLY_POLICY, PUBLIC_SOURCE_PILOT_POLICY


def _write_policy_config(
    config_dir: Path,
    *,
    crosswalk: str | None = None,
    pair_rules: str | None = None,
) -> None:
    """API capability 테스트에 필요한 새 운영 config 계약을 만든다."""

    (config_dir / "cameo_crosswalk.csv").write_text(
        crosswalk
        or (
            "cas_number,cameo_chemical_id,selected_form,verification_status,"
            "verification_method,evidence_url,source_product,source_version,"
            "checked_at_utc\n"
            "7647-01-0,3598,HYDROCHLORIC ACID SOLUTION,PUBLIC_SOURCE_VERIFIED,"
            "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET,"
            "https://cameochemicals.noaa.gov/chemical/3598,"
            "NOAA/EPA CAMEO Chemicals,3.1.0,2026-07-22T00:00:00+00:00\n"
            "7681-52-9,4503,SODIUM HYPOCHLORITE,PUBLIC_SOURCE_VERIFIED,"
            "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET,"
            "https://cameochemicals.noaa.gov/chemical/4503,"
            "NOAA/EPA CAMEO Chemicals,3.1.0,2026-07-22T00:00:00+00:00\n"
        ),
        encoding="utf-8",
    )
    (config_dir / "pair_rules.csv").write_text(
        pair_rules or "rule_id,cas_a,cas_b,approval_status\n",
        encoding="utf-8",
    )
    (config_dir / "conflict_policy.json").write_text(
        json.dumps(
            {
                "policy_id": PUBLIC_SOURCE_PILOT_POLICY,
                "eligible_crosswalk_statuses": ["PUBLIC_SOURCE_VERIFIED"],
                "required_verification_method": (
                    "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET"
                ),
                "required_source_product": "NOAA/EPA CAMEO Chemicals",
                "allow_direct_rules": False,
                "require_two_responder_confirmed_cas": True,
                "decision_support_only": True,
                "expert_review_required": False,
                "probability_output_allowed": False,
                "final_decision_authority": "현장 지휘관 판단",
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def runtime(tmp_path: Path) -> ModelRuntime:
    """실데이터나 직렬화 모델을 읽지 않는 API 테스트용 런타임."""

    db_path = tmp_path / "chemiguard119.sqlite"
    resolver_path = tmp_path / "substance_resolver.joblib"
    retriever_path = tmp_path / "evidence_retriever.joblib"
    config_dir = tmp_path / "config"
    for path in (db_path, resolver_path, retriever_path):
        path.touch()
    config_dir.mkdir()
    _write_policy_config(config_dir)
    return ModelRuntime(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        resolver_artifact={"schema_version": "resolver-test-v1", "rows": []},
        retriever_artifact={"schema_version": "retriever-test-v1", "rows": []},
        loaded_at_utc="2026-07-21T00:00:00+00:00",
    )


@pytest.fixture()
def stub_pipeline_boundaries(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """오케스트레이터는 실제로 실행하고 외부 모델·DB 경계만 대체한다."""

    review_calls: list[dict[str, Any]] = []

    def fake_parse(text: str, _artifact: dict[str, Any]) -> dict[str, Any]:
        return {
            "backend": "TEST_DETERMINISTIC_PARSER",
            "source_text": text,
            "incident_types": ["UNKNOWN"],
            "fire_status": "UNKNOWN",
            "substance_mentions": [],
            "planned_actions": [],
            "needs_substance_confirmation": True,
            "missing_fields": ["substance"],
        }

    def fake_search(
        query: str,
        _db_path: Path,
        _artifact: dict[str, Any],
        cas_hint: str | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        return {
            "status": ("CAS_EVIDENCE_NOT_LOADED" if cas_hint else "NO_EVIDENCE_FOUND"),
            "query": query,
            "cas_hint": cas_hint,
            "top_k": top_k,
            "results": [],
            "warning": "TEST ONLY",
            "notice": ("외부 공식 MSDS 확인이 필요합니다." if cas_hint else None),
        }

    def fake_review(
        incident_cas: str,
        facility_cas: str,
        db_path: Path,
        planned_actions: list[str] | None = None,
        allow_demo_rules: bool = False,
        policy_mode: str = APPROVED_ONLY_POLICY,
        config_dir: Path | None = None,
    ) -> dict[str, Any]:
        review_calls.append(
            {
                "incident_cas": incident_cas,
                "facility_cas": facility_cas,
                "db_path": db_path,
                "planned_actions": planned_actions,
                "allow_demo_rules": allow_demo_rules,
                "policy_mode": policy_mode,
                "config_dir": config_dir,
            }
        )
        # 분류 근거가 부족할 때 API가 그대로 전달할 수 있는 안전한 결과 형태다.
        return {
            "status": "VERIFY_REQUIRED",
            "severity": None,
            "reason": "공개 근거 추가 확인이 필요합니다.",
            "policy_mode": policy_mode,
            "expert_reviewed": False,
            "human_confirmation_required": True,
        }

    monkeypatch.setattr(pipeline, "deterministic_parse", fake_parse)
    monkeypatch.setattr(pipeline, "search_evidence", fake_search)
    monkeypatch.setattr(pipeline, "review_pair", fake_review)
    return review_calls


def _confirmed(
    *,
    role: str,
    cas_number: str,
    confirmation_id: str,
    presence_status: str = "CONFIRMED_PRESENT",
) -> dict[str, str]:
    return {
        "confirmation_id": confirmation_id,
        "cas_number": cas_number,
        "role": role,
        "presence_status": presence_status,
        "confirmation_basis": "CONTAINER_LABEL",
        "observed_at": "2025-01-01T00:00:00+09:00",
    }


def _analyze_payload(text: str = "미상 물질 냄새 신고") -> dict[str, Any]:
    return {
        "request_id": "REQ-TEST-001",
        "incident_id": "INC-TEST-001",
        "input": {"type": "MANUAL_TEXT", "text": text},
    }


def _seed_facility_history(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE facility_candidate (
                facility_name TEXT NOT NULL,
                address TEXT,
                province TEXT,
                industry TEXT,
                cas_number TEXT NOT NULL,
                chemical_name TEXT,
                survey_year INTEGER,
                fire_incident_row_count INTEGER,
                kosha_msds_exact_match INTEGER,
                prtr_company_exact_match INTEGER,
                prtr_material_exact_match INTEGER,
                source_url TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO facility_candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "OO전자 공장",
                "경기 화성시 팔탄면 테스트로 119",
                "경기도",
                "전자부품 제조업",
                "7647-01-0",
                "염산",
                2024,
                0,
                1,
                1,
                1,
                "https://example.invalid/history-record",
            ),
        )


def test_health_and_readiness_with_injected_runtime(runtime: ModelRuntime) -> None:
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "UP"
    assert live.json()["service_name"] == "케미체크119"
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "READY",
        "service": "chemicheck119-model-api",
        "service_name": "케미체크119",
        "ready": True,
        "artifacts": {
            "database": True,
            "resolver": True,
            "retriever": True,
            "config": True,
        },
        "loaded_at_utc": "2026-07-21T00:00:00+00:00",
        "resolver_schema": "resolver-test-v1",
        "retriever_schema": "retriever-test-v1",
        "conflict_review_capability": {
            "policy_mode": "PUBLIC_SOURCE_PILOT_V1",
            "public_source_verified_crosswalk_count": 2,
            "eligible_public_source_cas_count": 2,
            "approved_crosswalk_count": 0,
            "approved_direct_rule_count": 0,
            "public_source_screening_ready": True,
            "expert_approved_decision_ready": False,
            "conflict_review_ready": True,
            "expert_reviewed": False,
            "direct_rules_enabled": False,
            "configuration_valid": True,
        },
        "integrity": {
            "status": "INJECTED_OR_LEGACY_RUNTIME",
            "environment": None,
            "manifest_sha256_verified": False,
            "git_commit": None,
        },
        "rule_policy": "PUBLIC_SOURCE_PILOT_V1",
        "rule_policy_ready": True,
        "rule_policy_error": None,
        "expert_reviewed": False,
        "decision_support_only": True,
        "responder_confirmation_required": True,
        "operational_checks": {
            "runtime_ready": True,
            "authentication_ready": True,
            "authentication_mode": "EXPLICIT_ANONYMOUS",
            "rule_policy_ready": True,
            "rule_policy": "PUBLIC_SOURCE_PILOT_V1",
            "conflict_review_ready": True,
        },
    }


def test_readiness_and_metadata_report_approved_conflict_review_capability(
    runtime: ModelRuntime,
) -> None:
    _write_policy_config(
        runtime.config_dir,
        crosswalk=(
            "cas_number,cameo_chemical_id,verification_status\n"
            "7647-01-0,3598,APPROVED\n"
            "7681-52-9,4503,APPROVED\n"
            "67-64-1,8,CANDIDATE_UNVERIFIED\n"
        ),
        pair_rules=(
            "rule_id,cas_a,cas_b,approval_status\n"
            "CHEM-001,7647-01-0,7681-52-9,APPROVED\n"
            "CHEM-DRAFT,64-17-5,67-64-1,DRAFT\n"
        ),
    )
    application = create_app(
        runtime=runtime,
        allow_anonymous=True,
        rule_policy=APPROVED_ONLY_POLICY,
    )

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        metadata = client.get("/api/v1/meta")

    expected = {
        "policy_mode": "APPROVED_ONLY",
        "public_source_verified_crosswalk_count": 0,
        "eligible_public_source_cas_count": 0,
        "approved_crosswalk_count": 2,
        "approved_direct_rule_count": 1,
        "public_source_screening_ready": False,
        "expert_approved_decision_ready": True,
        "conflict_review_ready": True,
        "expert_reviewed": True,
        "direct_rules_enabled": True,
        "configuration_valid": True,
    }
    assert ready.status_code == 200
    assert ready.json()["conflict_review_capability"] == expected
    assert ready.json()["operational_checks"]["conflict_review_ready"] is True
    assert metadata.json()["conflict_review_capability"] == expected
    assert metadata.json()["runtime"]["conflict_review_capability"] == expected
    assert metadata.json()["rule_policy"] == "APPROVED_ONLY"
    assert metadata.json()["expert_reviewed"] is True


def test_runtime_uses_precomputed_conflict_capability(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = runtime.conflict_review_capability(PUBLIC_SOURCE_PILOT_POLICY)
    precomputed_runtime = ModelRuntime(
        db_path=runtime.db_path,
        resolver_model_path=runtime.resolver_model_path,
        retriever_model_path=runtime.retriever_model_path,
        config_dir=runtime.config_dir,
        resolver_artifact=runtime.resolver_artifact,
        retriever_artifact=runtime.retriever_artifact,
        loaded_at_utc=runtime.loaded_at_utc,
        conflict_capabilities={PUBLIC_SOURCE_PILOT_POLICY: cached},
    )
    monkeypatch.setattr(
        api,
        "_conflict_review_capability",
        lambda *_args, **_kwargs: pytest.fail(
            "요청 중 config를 다시 읽으면 안 됩니다."
        ),
    )

    assert (
        precomputed_runtime.readiness(PUBLIC_SOURCE_PILOT_POLICY)[
            "conflict_review_capability"
        ]
        == cached
    )


def test_approved_direct_rule_makes_conflict_review_ready_without_crosswalk(
    runtime: ModelRuntime,
) -> None:
    _write_policy_config(
        runtime.config_dir,
        crosswalk="cas_number,cameo_chemical_id,verification_status\n",
        pair_rules=(
            "rule_id,cas_a,cas_b,approval_status\n"
            "CHEM-DIRECT-001,64-17-5,67-64-1,APPROVED\n"
        ),
    )
    application = create_app(
        runtime=runtime,
        allow_anonymous=True,
        rule_policy=APPROVED_ONLY_POLICY,
    )

    with TestClient(application) as client:
        ready = client.get("/health/ready")

    assert ready.status_code == 200
    assert ready.json()["conflict_review_capability"] == {
        "policy_mode": "APPROVED_ONLY",
        "public_source_verified_crosswalk_count": 0,
        "eligible_public_source_cas_count": 0,
        "approved_crosswalk_count": 0,
        "approved_direct_rule_count": 1,
        "public_source_screening_ready": False,
        "expert_approved_decision_ready": True,
        "conflict_review_ready": True,
        "expert_reviewed": True,
        "direct_rules_enabled": True,
        "configuration_valid": True,
    }


def test_production_rejects_placeholder_or_weak_api_key(runtime: ModelRuntime) -> None:
    placeholder_api_key = "replace-me-with-a-32-character-secret"
    application = create_app(
        runtime=runtime,
        api_key=placeholder_api_key,
        deployment_environment="production",
    )

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        response = client.post(
            "/api/v1/substances/resolve",
            headers={"X-API-Key": placeholder_api_key},
            json={"query": "염산"},
        )

    assert ready.status_code == 503
    assert ready.json()["operational_checks"]["authentication_ready"] is False
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BACKEND_AUTH_CONFIGURATION_INVALID"


def test_invalid_rule_policy_fails_readiness_and_protected_requests(
    runtime: ModelRuntime,
) -> None:
    application = create_app(
        runtime=runtime,
        allow_anonymous=True,
        rule_policy="UNSUPPORTED_POLICY",
    )

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        metadata = client.get("/api/v1/meta")
        response = client.post(
            "/api/v1/substances/resolve",
            json={"query": "염산"},
        )

    assert ready.status_code == 503
    assert ready.json()["rule_policy"] == "UNSUPPORTED_POLICY"
    assert ready.json()["rule_policy_error"]
    assert ready.json()["operational_checks"]["rule_policy_ready"] is False
    assert ready.json()["operational_checks"]["rule_policy"] == "UNSUPPORTED_POLICY"
    assert metadata.status_code == 200
    assert metadata.json()["rule_policy_ready"] is False
    assert metadata.json()["rule_policy_error"]
    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "CONFLICT_POLICY_CONFIGURATION_INVALID",
        "message": "지원하지 않는 충돌 검토 정책: UNSUPPORTED_POLICY",
        "retryable": False,
        "fields": ["CHEMIGUARD119_RULE_POLICY"],
    }


def test_malformed_public_policy_config_fails_closed(runtime: ModelRuntime) -> None:
    (runtime.config_dir / "conflict_policy.json").write_text(
        json.dumps({"policy_id": PUBLIC_SOURCE_PILOT_POLICY}),
        encoding="utf-8",
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        metadata = client.get("/api/v1/meta")
        response = client.post(
            "/api/v1/substances/resolve",
            json={"query": "염산"},
        )

    assert ready.status_code == 503
    assert ready.json()["operational_checks"]["rule_policy_ready"] is False
    assert metadata.json()["rule_policy_ready"] is False
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONFLICT_POLICY_CONFIGURATION_INVALID"


def test_public_policy_without_two_eligible_mappings_is_not_ready(
    runtime: ModelRuntime,
) -> None:
    _write_policy_config(
        runtime.config_dir,
        crosswalk=(
            "cas_number,cameo_chemical_id,selected_form,verification_status,"
            "verification_method,evidence_url,source_product,source_version,"
            "checked_at_utc\n"
            "7681-52-9,4503,SODIUM HYPOCHLORITE,PUBLIC_SOURCE_VERIFIED,"
            "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET,"
            "https://cameochemicals.noaa.gov/chemical/4503,"
            "NOAA/EPA CAMEO Chemicals,3.1.0,2026-07-22T00:00:00+00:00\n"
        ),
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        metadata = client.get("/api/v1/meta")
        response = client.post(
            "/api/v1/substances/resolve",
            json={"query": "염산"},
        )

    assert ready.status_code == 503
    assert ready.json()["rule_policy_ready"] is False
    assert ready.json()["operational_checks"]["conflict_review_ready"] is False
    assert metadata.json()["rule_policy_ready"] is False
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONFLICT_POLICY_NOT_READY"


def test_readiness_reports_startup_failure_without_loading_real_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_load(**_kwargs: Any) -> ModelRuntime:
        raise FileNotFoundError("test artifacts missing")

    monkeypatch.setattr(ModelRuntime, "load", fail_load)
    application = create_app(allow_anonymous=True)

    with TestClient(application) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "NOT_READY"
    assert response.json()["reason"] == "ARTIFACT_LOAD_FAILED"


def test_protected_endpoint_requires_backend_api_key(runtime: ModelRuntime) -> None:
    application = create_app(runtime=runtime, api_key="backend-secret")

    with TestClient(application) as client:
        response = client.post("/api/v1/substances/resolve", json={"query": "염산"})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "ApiKey"
    assert response.headers["x-api-schema-version"] == "chemiguard119-api-v1"
    assert response.json()["schema_version"] == "chemiguard119-api-v1"
    assert response.json()["service_name"] == "케미체크119"
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert response.json()["occurred_at_utc"]
    assert response.json()["error"] == {
        "code": "BACKEND_AUTH_REQUIRED",
        "message": "유효한 백엔드 API 키가 필요합니다.",
        "retryable": False,
        "fields": [],
    }


def test_auth_is_fail_closed_when_key_is_not_configured(runtime: ModelRuntime) -> None:
    application = create_app(runtime=runtime, api_key="", allow_anonymous=False)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/substances/resolve",
            headers={"X-Request-Id": "REQ-AUTH-CONFIG-001"},
            json={"query": "염산"},
        )
        metadata = client.get("/api/v1/meta").json()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BACKEND_AUTH_NOT_CONFIGURED"
    assert response.json()["request_id"] == "REQ-AUTH-CONFIG-001"
    assert response.headers["x-request-id"] == "REQ-AUTH-CONFIG-001"
    assert metadata["service_name"] == "케미체크119"
    assert metadata["internal_package_name"] == "chemiguard119"
    assert metadata["authentication"] == {
        "mode": "MISCONFIGURED_FAIL_CLOSED",
        "required": True,
        "configured": False,
        "header": "X-API-Key",
        "anonymous_access_is_explicit": False,
    }


@pytest.mark.parametrize(
    "unsafe_request_id",
    ["REQ with spaces/../", "R" * 129],
)
def test_unsafe_request_id_is_replaced(
    runtime: ModelRuntime,
    unsafe_request_id: str,
) -> None:
    application = create_app(runtime=runtime, api_key="", allow_anonymous=False)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/substances/resolve",
            headers={"X-Request-Id": unsafe_request_id},
            json={"query": "염산"},
        )

    safe_request_id = response.headers["x-request-id"]
    assert response.status_code == 503
    assert safe_request_id != unsafe_request_id
    assert len(safe_request_id) <= 128
    assert re.fullmatch(r"[A-Za-z0-9_.:-]+", safe_request_id)
    assert response.json()["request_id"] == safe_request_id


def test_anonymous_mode_must_be_explicit_and_is_reported(runtime: ModelRuntime) -> None:
    application = create_app(
        runtime=runtime,
        api_key="",
        allow_anonymous=True,
        deployment_environment="test",
    )

    with TestClient(application) as client:
        response = client.get("/api/v1/meta")

    assert response.status_code == 200
    body = response.json()
    assert body["deployment_environment"] == "test"
    assert body["authentication"]["mode"] == "EXPLICIT_ANONYMOUS"
    assert body["authentication"]["required"] is False
    assert body["authentication"]["anonymous_access_is_explicit"] is True
    assert body["rule_policy"] == "PUBLIC_SOURCE_PILOT_V1"
    assert body["rule_policy_ready"] is True
    assert body["expert_reviewed"] is False
    assert body["decision_support_only"] is True
    assert body["responder_confirmation_required"] is True
    assert body["conflict_review_capability"]["policy_mode"] == body["rule_policy"]


def test_deployment_environment_uses_canonical_environment_variable(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHEMIGUARD119_ENVIRONMENT", "staging")
    application = create_app(runtime=runtime, api_key="", allow_anonymous=True)

    with TestClient(application) as client:
        response = client.get("/api/v1/meta")

    assert response.status_code == 200
    assert response.json()["deployment_environment"] == "staging"


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected": "field"},
        {
            "confirmed_incident_substance": _confirmed(
                role="INCIDENT",
                cas_number="123-45-6",
                confirmation_id="CNF-INC-001",
            )
        },
    ],
)
def test_invalid_schema_or_cas_returns_422(
    runtime: ModelRuntime,
    mutation: dict[str, Any],
) -> None:
    payload = _analyze_payload()
    payload.update(mutation)
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post("/api/v1/incidents/analyze", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_SCHEMA"
    assert response.json()["error"]["fields"]


def test_confirmation_record_requires_observation_time_and_unique_id(
    runtime: ModelRuntime,
) -> None:
    incident = _confirmed(
        role="INCIDENT",
        cas_number="7681-52-9",
        confirmation_id="CNF-SHARED-001",
    )
    missing_time = _analyze_payload()
    missing_time["confirmed_incident_substance"] = {**incident}
    missing_time["confirmed_incident_substance"].pop("observed_at")

    duplicate_ids = _analyze_payload()
    duplicate_ids["confirmed_incident_substance"] = incident
    duplicate_ids["confirmed_facility_substance"] = _confirmed(
        role="FACILITY",
        cas_number="7647-01-0",
        confirmation_id="CNF-SHARED-001",
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        missing_time_response = client.post(
            "/api/v1/incidents/analyze", json=missing_time
        )
        duplicate_response = client.post(
            "/api/v1/incidents/analyze", json=duplicate_ids
        )

    assert missing_time_response.status_code == 422
    assert missing_time_response.json()["error"]["code"] == "INVALID_SCHEMA"
    assert duplicate_response.status_code == 422
    assert duplicate_response.json()["error"]["code"] == "INVALID_SCHEMA"


def test_unconfirmed_text_awaits_confirmation_does_not_run_rule_and_hides_source(
    runtime: ModelRuntime,
    stub_pipeline_boundaries: list[dict[str, Any]],
) -> None:
    source_text = "미상 물질 냄새 신고: 탱크 주변 확인 필요"
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload(source_text),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "AWAITING_SUBSTANCE_CONFIRMATION"
    assert body["conflict_review"]["executed"] is False
    assert body["conflict_review"]["status"] == "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"
    assert body["confirmation_gate"] == {
        "policy": "TWO_AUTHENTICATED_ON_SITE_CONFIRMATIONS_REQUIRED",
        "incident_confirmed": False,
        "facility_confirmed": False,
        "all_required_confirmed": False,
        "rule_execution_allowed": False,
    }
    assert response.headers["x-request-id"] == "REQ-TEST-001"
    assert response.headers["cache-control"] == "no-store"
    assert stub_pipeline_boundaries == []
    assert (
        body["input_fingerprint"]
        == hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    )
    assert "source_text" not in body["model_outputs"]["parser"]
    assert source_text not in json.dumps(body, ensure_ascii=False)


def test_api_blocks_forged_risk_output_without_two_confirmations(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "analyze_incident",
        lambda *_args, **_kwargs: {
            "schema_version": "incident-analysis-v1",
            "status": "COMPLETED_WITH_WARNINGS",
            "parsed_report": {},
            "substance_candidates": [],
            "evidence": [],
            "rule_review": {
                "executed": True,
                "result": {
                    "status": "COMPLETED",
                    "severity": "HIGH_RISK",
                    "rule_id": "FORGED-RULE",
                },
            },
            "output_validation": {"status": "PASSED", "errors": []},
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("후보 물질만 있는 신고"),
        )

    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "UNCONFIRMED_RISK_OUTPUT_BLOCKED",
        "message": "두 물질의 현장 확인 레코드가 없어 위험도·충돌 확정 출력을 차단했습니다.",
        "retryable": False,
        "fields": [],
    }


def test_rule_requires_two_complete_confirmation_records_and_forces_demo_off(
    runtime: ModelRuntime,
    stub_pipeline_boundaries: list[dict[str, Any]],
) -> None:
    application = create_app(runtime=runtime, allow_anonymous=True)
    incident = _confirmed(
        role="INCIDENT",
        cas_number="7681-52-9",
        confirmation_id="CNF-INC-001",
    )
    facility = _confirmed(
        role="FACILITY",
        cas_number="7647-01-0",
        confirmation_id="CNF-FAC-001",
    )

    with TestClient(application) as client:
        only_one = _analyze_payload()
        only_one["confirmed_incident_substance"] = incident
        one_response = client.post("/api/v1/incidents/analyze", json=only_one)

        missing_id = _analyze_payload()
        missing_id["confirmed_incident_substance"] = incident
        missing_id["confirmed_facility_substance"] = {**facility}
        missing_id["confirmed_facility_substance"].pop("confirmation_id")
        missing_id_response = client.post("/api/v1/incidents/analyze", json=missing_id)

        not_present = _analyze_payload()
        not_present["confirmed_incident_substance"] = incident
        not_present["confirmed_facility_substance"] = {
            **facility,
            "presence_status": "NOT_CONFIRMED",
        }
        not_present_response = client.post(
            "/api/v1/incidents/analyze", json=not_present
        )

        both = _analyze_payload()
        both["confirmed_incident_substance"] = incident
        both["confirmed_facility_substance"] = facility
        both_response = client.post("/api/v1/incidents/analyze", json=both)

    assert one_response.status_code == 200
    assert one_response.json()["state"] == "AWAITING_FACILITY_CONFIRMATION"
    assert one_response.json()["conflict_review"]["executed"] is False
    assert missing_id_response.status_code == 422
    assert not_present_response.status_code == 422

    assert both_response.status_code == 200
    both_body = both_response.json()
    assert both_body["state"] == "VERIFY_REQUIRED"
    assert both_body["conflict_review"]["executed"] is True
    assert both_body["provenance"]["confirmations"] == {
        "incident": {
            "confirmation_id": "CNF-INC-001",
            "confirmation_basis": "CONTAINER_LABEL",
            "presence_status": "CONFIRMED_PRESENT",
            "observed_at": "2025-01-01T00:00:00+09:00",
        },
        "facility": {
            "confirmation_id": "CNF-FAC-001",
            "confirmation_basis": "CONTAINER_LABEL",
            "presence_status": "CONFIRMED_PRESENT",
            "observed_at": "2025-01-01T00:00:00+09:00",
        },
    }
    assert both_body["confirmation_gate"]["all_required_confirmed"] is True
    assert both_body["confirmation_gate"]["rule_execution_allowed"] is True
    assert both_body["provenance"]["rule_policy"] == "PUBLIC_SOURCE_PILOT_V1"
    assert both_body["provenance"]["expert_reviewed"] is False
    assert both_body["provenance"]["decision_support_only"] is True
    assert both_body["provenance"]["responder_confirmation_required"] is True
    assert (
        both_body["provenance"]["conflict_review_capability"][
            "public_source_screening_ready"
        ]
        is True
    )
    assert len(stub_pipeline_boundaries) == 1
    assert stub_pipeline_boundaries[0]["allow_demo_rules"] is False
    assert stub_pipeline_boundaries[0]["policy_mode"] == "PUBLIC_SOURCE_PILOT_V1"


def test_direct_conflict_review_uses_public_source_policy_and_forces_demo_rules_off(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, **kwargs})
        return {
            "status": "VERIFY_REQUIRED",
            "severity": None,
            "human_confirmation_required": True,
        }

    monkeypatch.setattr(api, "review_pair", fake_review)
    application = create_app(runtime=runtime, allow_anonymous=True)
    payload = {
        "incident": _confirmed(
            role="INCIDENT",
            cas_number="7681-52-9",
            confirmation_id="CNF-INC-001",
        ),
        "facility": _confirmed(
            role="FACILITY",
            cas_number="7647-01-0",
            confirmation_id="CNF-FAC-001",
        ),
    }

    with TestClient(application) as client:
        response = client.post("/api/v1/conflicts/review", json=payload)

    assert response.status_code == 200
    assert response.json()["rule_policy"] == "PUBLIC_SOURCE_PILOT_V1"
    assert response.json()["expert_reviewed"] is False
    assert response.json()["decision_support_only"] is True
    assert response.json()["responder_confirmation_required"] is True
    assert (
        response.json()["conflict_review_capability"]["public_source_screening_ready"]
        is True
    )
    assert response.json()["confirmation_gate"] == {
        "policy": "TWO_AUTHENTICATED_ON_SITE_CONFIRMATIONS_REQUIRED",
        "all_required_confirmed": True,
        "rule_execution_allowed": True,
    }
    assert len(calls) == 1
    assert calls[0]["allow_demo_rules"] is False
    assert calls[0]["policy_mode"] == "PUBLIC_SOURCE_PILOT_V1"


def test_direct_conflict_review_can_use_approved_only_policy(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _write_policy_config(
        runtime.config_dir,
        crosswalk="cas_number,cameo_chemical_id,verification_status\n",
        pair_rules=(
            "rule_id,cas_a,cas_b,approval_status\n"
            "CHEM-DIRECT-001,7681-52-9,7647-01-0,APPROVED\n"
        ),
    )

    def fake_review(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"args": args, **kwargs})
        return {
            "status": "VERIFY_REQUIRED",
            "severity": None,
            "human_confirmation_required": True,
        }

    monkeypatch.setattr(api, "review_pair", fake_review)
    application = create_app(
        runtime=runtime,
        allow_anonymous=True,
        rule_policy=APPROVED_ONLY_POLICY,
    )
    payload = {
        "incident": _confirmed(
            role="INCIDENT",
            cas_number="7681-52-9",
            confirmation_id="CNF-INC-001",
        ),
        "facility": _confirmed(
            role="FACILITY",
            cas_number="7647-01-0",
            confirmation_id="CNF-FAC-001",
        ),
    }

    with TestClient(application) as client:
        response = client.post("/api/v1/conflicts/review", json=payload)

    assert response.status_code == 200
    assert response.json()["rule_policy"] == "APPROVED_ONLY"
    # APPROVED_ONLY 정책이어도 실제 완료된 승인 결과가 없으면 false다.
    assert response.json()["expert_reviewed"] is False
    assert calls[0]["policy_mode"] == "APPROVED_ONLY"


def test_resolve_and_search_results_are_never_rule_eligible(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "resolve_substance",
        lambda query, _artifact, top_k: {
            "query": query,
            "status": "CANDIDATES",
            "candidates": [{"cas_number": "7647-01-0", "score": 0.99}],
            "top_k": top_k,
        },
    )
    monkeypatch.setattr(
        api,
        "search_evidence",
        lambda query, _db, _artifact, cas_hint, top_k: {
            "query": query,
            "cas_hint": cas_hint,
            "results": [],
            "top_k": top_k,
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        resolved = client.post(
            "/api/v1/substances/resolve",
            json={"query": "염산", "top_k": 3},
        )
        searched = client.post(
            "/api/v1/evidence/search",
            json={
                "query": "염산 취급 근거",
                "cas_hint": "7647-01-0",
                "cas_hint_status": "RESOLVER_CANDIDATE",
                "top_k": 5,
            },
        )

    assert resolved.status_code == 200
    assert resolved.json()["rule_eligible"] is False
    assert resolved.json()["decision_scope"] == "IDENTIFICATION_CANDIDATE_ONLY"
    assert resolved.json()["on_site_presence_confirmed"] is False
    assert resolved.json()["risk_determination_allowed"] is False
    assert "위험 확률이 아닙니다" in resolved.json()["candidate_score_notice"]
    assert searched.status_code == 200
    assert searched.json()["rule_eligible"] is False
    assert searched.json()["cas_hint_status"] == "RESOLVER_CANDIDATE"
    assert searched.json()["decision_scope"] == "EVIDENCE_ONLY"
    assert searched.json()["risk_determination_allowed"] is False


def test_facility_history_is_not_current_inventory_or_rule_input(
    runtime: ModelRuntime,
    stub_pipeline_boundaries: list[dict[str, Any]],
) -> None:
    _seed_facility_history(runtime.db_path)
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        candidates_response = client.post(
            "/api/v1/facilities/candidates",
            json={"query": "OO전자 공장", "province": "경기도", "top_k": 5},
        )
        incident = _analyze_payload("미상 물질 누출 신고")
        incident["location"] = {
            "facility_name": "OO전자 공장",
            "province": "경기도",
        }
        analysis_response = client.post("/api/v1/incidents/analyze", json=incident)

    assert candidates_response.status_code == 200
    candidates = candidates_response.json()
    assert candidates["status"] == "CANDIDATES_FOUND"
    assert candidates["decision_scope"] == "REPORTED_HANDLING_HISTORY_ONLY"
    assert candidates["risk_determination_allowed"] is False
    assert len(candidates["results"]) == 1
    facility_candidate = candidates["results"][0]
    assert facility_candidate["cas_number"] == "7647-01-0"
    assert facility_candidate["evidence_class"] == "REPORTED_HANDLING_HISTORY"
    assert facility_candidate["current_inventory_confirmed"] is False
    assert facility_candidate["rule_eligible"] is False
    assert facility_candidate["requires_on_site_confirmation"] is True

    assert analysis_response.status_code == 200
    analysis = analysis_response.json()
    embedded = analysis["model_outputs"]["facility_history_candidates"]["results"][0]
    assert embedded["cas_number"] == "7647-01-0"
    assert embedded["current_inventory_confirmed"] is False
    assert embedded["rule_eligible"] is False
    assert analysis["state"] == "AWAITING_SUBSTANCE_CONFIRMATION"
    assert analysis["conflict_review"]["executed"] is False
    # 시설 과거 이력 CAS는 confirmed_facility_cas로 자동 전달되지 않는다.
    assert stub_pipeline_boundaries == []


def test_openapi_exposes_only_documented_v1_and_health_paths(
    runtime: ModelRuntime,
) -> None:
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "케미체크119 모델 API"
    paths = response.json()["paths"]
    assert set(paths) == {
        "/health/live",
        "/health/ready",
        "/api/v1/meta",
        "/api/v1/incidents/analyze",
        "/api/v1/substances/resolve",
        "/api/v1/evidence/search",
        "/api/v1/facilities/candidates",
        "/api/v1/conflicts/review",
    }
    assert "post" in paths["/api/v1/incidents/analyze"]
    assert "post" in paths["/api/v1/conflicts/review"]
    assert "ErrorResponse" in response.json()["components"]["schemas"]
    assert not any("demo" in path for path in paths)
