from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chemiguard119 import api, pipeline
from chemiguard119.api import ModelRuntime, create_app
from chemiguard119.api_models import AnalysisResponse, SubstanceDiscoveryCandidate
from chemiguard119.rules import APPROVED_ONLY_POLICY, PUBLIC_SOURCE_PILOT_POLICY


DEPLOYED_API_KEY = "0123456789abcdef" * 4
REFERENCE_REGISTRY_SOURCE = (
    Path(__file__).resolve().parents[1] / "config" / "reference_assurance_registry.json"
)


def _expected_reference_assurance(config_dir: Path) -> dict[str, Any]:
    registry_path = config_dir / "reference_assurance_registry.json"
    return {
        "ready": True,
        "schema_version": "chemicheck119-reference-assurance-registry-v1",
        "policy_id": "OFFICIAL_REFERENCE_TRIANGULATION_V1",
        "registry_sha256": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        "authority_count": 5,
        "triangulated_pair_count": 2,
        "expert_reviewed": False,
        "human_expert_substitute": False,
        "error": None,
    }


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
    shutil.copy2(
        REFERENCE_REGISTRY_SOURCE,
        config_dir / "reference_assurance_registry.json",
    )


@pytest.fixture()
def runtime(tmp_path: Path) -> ModelRuntime:
    """실데이터나 직렬화 모델을 읽지 않는 API 테스트용 런타임."""

    db_path = tmp_path / "chemiguard119.sqlite"
    resolver_path = tmp_path / "substance_resolver.joblib"
    retriever_path = tmp_path / "evidence_retriever.joblib"
    config_dir = tmp_path / "config"
    for path in (resolver_path, retriever_path):
        path.touch()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE substance_profile(cas_number TEXT PRIMARY KEY)"
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE substance_profile_fts USING fts5(
                cas_number UNINDEXED
            )
            """
        )
        profile_rows = [("78-93-3",)] + [(f"TEST-{index:04d}",) for index in range(699)]
        connection.executemany(
            "INSERT INTO substance_profile VALUES (?)",
            profile_rows,
        )
        connection.executemany(
            "INSERT INTO substance_profile_fts VALUES (?)",
            profile_rows,
        )
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


def _analyze_payload_with_confirmed_pair() -> dict[str, Any]:
    payload = _analyze_payload("차아염소산나트륨과 염산 현장 확인")
    payload["confirmed_incident_substance"] = _confirmed(
        role="INCIDENT",
        cas_number="7681-52-9",
        confirmation_id="CNF-INC-001",
    )
    payload["confirmed_facility_substance"] = _confirmed(
        role="FACILITY",
        cas_number="7647-01-0",
        confirmation_id="CNF-FAC-001",
    )
    return payload


def _safe_unconfirmed_pipeline_result() -> dict[str, Any]:
    return {
        "schema_version": "incident-analysis-v1",
        "status": "NEEDS_SUBSTANCE_CONFIRMATION",
        "parsed_report": {},
        "substance_candidates": [],
        "evidence": [],
        "rule_review": {
            "executed": False,
            "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
            "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
            "missing_confirmations": ["incident_cas", "facility_cas"],
            "reason": "현장 확인이 필요합니다.",
        },
        "output_validation": {"status": "PASSED", "errors": []},
    }


def _safe_confirmed_pipeline_result() -> dict[str, Any]:
    return {
        "schema_version": "incident-analysis-v1",
        "status": "VERIFY_REQUIRED",
        "conflict_policy_mode": PUBLIC_SOURCE_PILOT_POLICY,
        "parsed_report": {},
        "substance_candidates": [],
        "evidence": [],
        "rule_review": {
            "executed": True,
            "status": "VERIFY_REQUIRED",
            "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
            "policy_mode": PUBLIC_SOURCE_PILOT_POLICY,
            "result": {
                "status": "VERIFY_REQUIRED",
                "severity": None,
                "reason": "공개 근거 추가 확인이 필요합니다.",
                "policy_mode": PUBLIC_SOURCE_PILOT_POLICY,
                "expert_reviewed": False,
                "human_confirmation_required": True,
            },
        },
        "output_validation": {"status": "PASSED", "errors": []},
    }


def _completed_rule_result() -> dict[str, Any]:
    return {
        "status": "COMPLETED",
        "scope": "APPROVED",
        "incident_cas": "7681-52-9",
        "facility_cas": "7647-01-0",
        "rule_id": "TEST-APPROVED-RULE",
        "rule_version": "test-v1",
        "severity": "HIGH_RISK",
        "risk_level": "HIGH",
        "risk_level_ko": "높음",
        "risk_scale": {
            "type": "ORDINAL_RULE_CLASSIFICATION",
            "is_probability": False,
            "probability_percent": None,
        },
        "hazard_codes": ["TEST"],
        "brief_text": "테스트용 충돌 위험",
        "required_checks": ["현장 상태 확인"],
        "evidence_urls": ["https://example.test/evidence"],
        "planned_actions": [],
        "limitations": ["테스트 fixture"],
        "final_decision": "현장 지휘관 판단",
        "human_confirmation_required": True,
    }


def _public_source_completed_rule_result() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "api"
        / "conflict_screening_completed_result.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _enable_approved_policy_for_test(runtime: ModelRuntime) -> None:
    """APPROVED_ONLY 출력 계약 테스트가 readiness 게이트를 통과하게 한다."""

    _write_policy_config(
        runtime.config_dir,
        pair_rules=(
            "rule_id,cas_a,cas_b,approval_status\n"
            "CHEM-DIRECT-001,7681-52-9,7647-01-0,APPROVED\n"
        ),
    )


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
        "material_discovery_capability": {
            "ready": True,
            "profile_count": 700,
            "minimum_profile_count": 700,
            "reason": None,
        },
        "facility_history_coverage": {
            "ready": False,
            "scope": "UNAVAILABLE",
            "evidence_class": "REPORTED_HANDLING_HISTORY",
            "current_inventory_confirmed": False,
            "candidate_row_count": 0,
            "distinct_facility_count": 0,
            "distinct_cas_count": 0,
            "covered_province_count": 0,
            "covered_provinces": [],
            "province_breakdown": [],
            "unknown_location_facility_count": 0,
            "warning": "시설 이력 후보는 현재 재고·수량·저장 위치의 확정 정보가 아닙니다.",
            "reason": "facility_candidate 테이블을 확인하지 못했습니다.",
        },
        "data_scope_semantics": {
            "substance_identity_search": "NATIONAL_CATALOG_NO_REGION_FILTER",
            "facility_history": "NATIONWIDE_HISTORICAL_CANDIDATES_WHEN_READY",
            "property_profile": "ULSAN_PUBLIC_PROPERTY_PROFILE_SUPPLEMENT",
            "property_profile_is_nationwide": False,
        },
        "operations_agent_capability": {
            "ready": True,
            "schema_version": "chemicheck119-operations-agent-v1",
            "coverage_scope": "NATIONWIDE_KOREA",
            "map_contract": "PROVIDER_NEUTRAL_GEOJSON",
            "recommended_renderer": "MAPLIBRE_GL_JS",
            "route_provider_owner": "BACKEND_SERVER_SIDE",
            "route_or_eta_inference_allowed": False,
            "hazard_dispersion_model_available": False,
        },
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
            "reference_assurance": _expected_reference_assurance(runtime.config_dir),
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
            "production_integrity_ready": True,
        },
    }


def test_readiness_fails_when_material_discovery_index_is_missing(
    runtime: ModelRuntime,
) -> None:
    with sqlite3.connect(runtime.db_path) as connection:
        connection.execute("DROP TABLE substance_profile")
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        ready = client.get("/health/ready")

    body = ready.json()
    assert ready.status_code == 503
    assert body["status"] == "NOT_READY"
    assert body["ready"] is False
    assert body["material_discovery_capability"] == {
        "ready": False,
        "profile_count": 0,
        "minimum_profile_count": 700,
        "reason": "substance_profile·FTS 인덱스를 확인하지 못했습니다.",
    }
    assert body["operational_checks"]["runtime_ready"] is False


def test_readiness_fails_when_material_discovery_fts_index_is_missing(
    runtime: ModelRuntime,
) -> None:
    with sqlite3.connect(runtime.db_path) as connection:
        connection.execute("DROP TABLE substance_profile_fts")
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        ready = client.get("/health/ready")

    body = ready.json()
    assert ready.status_code == 503
    assert body["ready"] is False
    assert body["material_discovery_capability"] == {
        "ready": False,
        "profile_count": 0,
        "minimum_profile_count": 700,
        "reason": "substance_profile·FTS 인덱스를 확인하지 못했습니다.",
    }


def test_readiness_fails_when_material_discovery_indexes_have_different_counts(
    runtime: ModelRuntime,
) -> None:
    with sqlite3.connect(runtime.db_path) as connection:
        connection.execute("DELETE FROM substance_profile_fts")
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        ready = client.get("/health/ready")

    assert ready.status_code == 503
    assert ready.json()["material_discovery_capability"] == {
        "ready": False,
        "profile_count": 700,
        "minimum_profile_count": 700,
        "reason": "substance_profile과 FTS 인덱스의 행 수가 다릅니다.",
    }


def test_readiness_fails_when_material_discovery_profile_count_is_below_minimum(
    runtime: ModelRuntime,
) -> None:
    with sqlite3.connect(runtime.db_path) as connection:
        connection.execute(
            "DELETE FROM substance_profile WHERE cas_number = ?",
            ("TEST-0698",),
        )
        connection.execute(
            "DELETE FROM substance_profile_fts WHERE cas_number = ?",
            ("TEST-0698",),
        )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        ready = client.get("/health/ready")

    assert ready.status_code == 503
    assert ready.json()["material_discovery_capability"] == {
        "ready": False,
        "profile_count": 699,
        "minimum_profile_count": 700,
        "reason": "substance_profile 인덱스가 운영 최소 700건보다 적습니다.",
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
        "reference_assurance": _expected_reference_assurance(runtime.config_dir),
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
        "reference_assurance": _expected_reference_assurance(runtime.config_dir),
    }


def test_public_source_readiness_fails_closed_for_invalid_reference_registry(
    runtime: ModelRuntime,
) -> None:
    (runtime.config_dir / "reference_assurance_registry.json").write_text(
        '{"schema_version":"forged"}',
        encoding="utf-8",
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        response = client.post(
            "/api/v1/substances/resolve",
            json={"query": "염산"},
        )

    assert ready.status_code == 503
    capability = ready.json()["conflict_review_capability"]
    assert capability["configuration_valid"] is False
    assert capability["reference_assurance"]["ready"] is False
    assert capability["reference_assurance"]["expert_reviewed"] is False
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CONFLICT_POLICY_CONFIGURATION_INVALID"
    assert (
        "config/reference_assurance_registry.json" in response.json()["error"]["fields"]
    )


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


def test_production_readiness_rejects_injected_runtime_without_verified_manifest(
    runtime: ModelRuntime,
) -> None:
    application = create_app(
        runtime=runtime,
        api_key=DEPLOYED_API_KEY,
        deployment_environment="production",
    )

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        response = client.post(
            "/api/v1/substances/resolve",
            headers={"X-API-Key": DEPLOYED_API_KEY},
            json={"query": "염산"},
        )

    assert ready.status_code == 503
    assert ready.json()["operational_checks"]["production_integrity_ready"] is False
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MODEL_RUNTIME_INTEGRITY_NOT_READY"


def test_unknown_deployment_environment_fails_closed(runtime: ModelRuntime) -> None:
    application = create_app(
        runtime=runtime,
        api_key=DEPLOYED_API_KEY,
        deployment_environment="prod",
    )

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        response = client.post(
            "/api/v1/substances/resolve",
            headers={"X-API-Key": DEPLOYED_API_KEY},
            json={"query": "염산"},
        )

    assert ready.status_code == 503
    assert (
        ready.json()["operational_checks"]["authentication_mode"]
        == "MISCONFIGURED_FAIL_CLOSED"
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BACKEND_AUTH_CONFIGURATION_INVALID"


def test_unknown_deployment_environment_does_not_load_serialized_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_calls: list[dict[str, Any]] = []

    def unexpected_load(**kwargs: Any) -> ModelRuntime:
        load_calls.append(kwargs)
        raise AssertionError("잘못된 환경에서는 artifact를 역직렬화하면 안 됩니다.")

    monkeypatch.setattr(ModelRuntime, "load", unexpected_load)
    application = create_app(
        api_key=DEPLOYED_API_KEY,
        deployment_environment="prod",
    )

    with TestClient(application) as client:
        ready = client.get("/health/ready")

    assert ready.status_code == 503
    assert ready.json()["reason"] == "DEPLOYMENT_ENVIRONMENT_INVALID"
    assert load_calls == []


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
    assert response.json()["material_discovery_capability"] == {
        "ready": False,
        "profile_count": 0,
        "minimum_profile_count": 700,
        "reason": "runtime artifact를 불러오지 못했습니다.",
    }


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
    application = create_app(
        runtime=runtime,
        api_key=DEPLOYED_API_KEY,
        allow_anonymous=False,
    )

    with TestClient(application) as client:
        metadata = client.get("/api/v1/meta")
        ready = client.get("/health/ready")
        response = client.post(
            "/api/v1/substances/resolve",
            headers={"X-API-Key": DEPLOYED_API_KEY},
            json={"query": "염산"},
        )

    assert metadata.status_code == 200
    assert metadata.json()["deployment_environment"] == "staging"
    assert ready.status_code == 503
    assert ready.json()["operational_checks"]["production_integrity_ready"] is False
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MODEL_RUNTIME_INTEGRITY_NOT_READY"


def test_staging_rejects_anonymous_and_weak_api_key(runtime: ModelRuntime) -> None:
    application = create_app(
        runtime=runtime,
        api_key="a" * 32,
        allow_anonymous=True,
        deployment_environment="staging",
    )

    with TestClient(application) as client:
        ready = client.get("/health/ready")
        response = client.post(
            "/api/v1/substances/resolve",
            json={"query": "염산"},
        )

    assert ready.status_code == 503
    assert ready.json()["operational_checks"]["authentication_ready"] is False
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BACKEND_AUTH_CONFIGURATION_INVALID"


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


def test_incident_agent_step_selects_confirmation_tools_and_resumes_from_memory(
    runtime: ModelRuntime,
    stub_pipeline_boundaries: list[dict[str, Any]],
) -> None:
    application = create_app(runtime=runtime, allow_anonymous=True)
    payload = {"analysis": _analyze_payload("미상 물질 냄새 신고"), "max_actions": 6}

    with TestClient(application) as client:
        metadata_response = client.get("/api/v1/meta")
        first_response = client.post(
            "/api/v1/agents/incidents/step",
            json=payload,
        )
        first = first_response.json()
        second_response = client.post(
            "/api/v1/agents/incidents/step",
            json={**payload, "memory": first["memory"]},
        )

    assert metadata_response.status_code == 200
    capability = metadata_response.json()["incident_agent_capability"]
    assert capability == {
        "schema_version": "chemicheck119-incident-agent-v1",
        "memory_schema_version": "chemicheck119-incident-agent-memory-v1",
        "endpoint": "/api/v1/agents/incidents/step",
        "planning_mode": "DETERMINISTIC_POLICY_PLANNER",
        "memory_mode": "BE_PERSISTED_EXTERNAL_MEMORY",
        "server_side_session_storage": False,
        "memory_can_trigger_rule": False,
        "autonomous_risk_decision_allowed": False,
        "tool_count": 6,
    }
    assert first_response.status_code == 200
    assert first["status"] == "WAITING_FOR_HUMAN"
    assert first["selected_tool_count"] == 4
    assert [item["tool_id"] for item in first["memory"]["history"]] == [
        "RUN_INCIDENT_ANALYSIS",
        "VERIFY_SAFETY_CONTRACT",
        "REQUEST_INCIDENT_CONFIRMATION",
        "REQUEST_FACILITY_CONFIRMATION",
    ]
    assert first["analysis"]["conflict_review"]["executed"] is False
    assert first["memory_can_trigger_rule"] is False
    assert first["trace_is_chain_of_thought"] is False
    assert second_response.status_code == 200
    assert second_response.json()["selected_tool_count"] == 0
    assert second_response.json()["events"][0]["decision_code"] == (
        "NO_NEW_OBSERVATION"
    )
    assert stub_pipeline_boundaries == []


def test_incident_analysis_returns_nationwide_operations_agent_and_route_contract(
    runtime: ModelRuntime,
    stub_pipeline_boundaries: list[dict[str, Any]],
) -> None:
    observed_at = datetime.now(timezone.utc).isoformat()
    payload = _analyze_payload("화성 공장 저장탱크 누출 신고")
    payload["location"] = {
        "latitude": 37.2181,
        "longitude": 126.9417,
        "coordinate_source": "DISPATCH_SYSTEM",
        "resolved_at": observed_at,
    }
    payload["operations_context"] = {
        "dispatch_station_name": "화성소방서",
        "journey_state": "EN_ROUTE",
        "responder_position": {
            "latitude": 37.2065,
            "longitude": 126.8311,
            "observed_at": observed_at,
            "source": "MDT_DEVICE_GPS",
            "accuracy_m": 12,
        },
        "route": {
            "provider": "TEST_SERVER_ROUTE",
            "mode": "LIVE_API",
            "route_id": "ROUTE-TEST-001",
            "geometry": {
                "type": "LineString",
                "coordinates": [
                    [126.8311, 37.2065],
                    [126.9417, 37.2181],
                ],
            },
            "distance_m": 10_000,
            "duration_seconds": 1_200,
            "remaining_distance_m": 4_000,
            "remaining_duration_seconds": 480,
            "generated_at": observed_at,
            "traffic_applied": True,
            "attribution": "테스트 서버 길찾기",
        },
    }
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post("/api/v1/incidents/analyze", json=payload)

    assert response.status_code == 200
    agent = response.json()["agent"]
    assert agent["phase"] == "EN_ROUTE_TRIAGE"
    assert agent["map_context"]["coverage_scope"] == "NATIONWIDE_KOREA"
    assert agent["map_context"]["route"]["status"] == "AVAILABLE"
    assert agent["map_context"]["route"]["eta_seconds"] == 480
    assert agent["map_context"]["route"]["progress_ratio"] == 0.6
    assert agent["map_context"]["route"]["progress_ratio_is_probability"] is False
    assert agent["map_context"]["hazard_overlay_status"] == (
        "NOT_COMPUTED_NO_VALIDATED_DISPERSION_MODEL"
    )
    assert agent["autonomous_risk_decision_allowed"] is False
    assert len(agent["workflow"]) == 10
    assert len(agent["tool_executions"]) == 8


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


@pytest.mark.parametrize(
    ("target", "forged_value"),
    [
        (
            "rule_review",
            {
                "risk_scale": {
                    "type": "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
                    "raw_class_id": 2,
                    "is_probability": False,
                }
            },
        ),
        ("rule_review", {"risk_level_ko": "중간"}),
        ("parsed_report", {"specific_risk": "유독성 가스 생성"}),
        ("substance_candidates", {"reaction": "독성 가스 발생"}),
    ],
)
def test_api_blocks_every_unconfirmed_risk_output_location(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    forged_value: dict[str, Any],
) -> None:
    safe_result: dict[str, Any] = {
        "schema_version": "incident-analysis-v1",
        "status": "NEEDS_SUBSTANCE_CONFIRMATION",
        "parsed_report": {},
        "substance_candidates": [],
        "evidence": [],
        "rule_review": {
            "executed": False,
            "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
            "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
            "missing_confirmations": ["incident_cas", "facility_cas"],
            "reason": "현장 확인이 필요합니다.",
        },
        "output_validation": {"status": "PASSED", "errors": []},
    }
    if target == "substance_candidates":
        safe_result[target] = [forged_value]
    else:
        safe_result[target].update(forged_value)
    monkeypatch.setattr(
        api,
        "analyze_incident",
        lambda *_args, **_kwargs: safe_result,
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("현장 미확인 물질 신고"),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "UNCONFIRMED_RISK_OUTPUT_BLOCKED"


def test_api_blocks_risk_in_unconfirmed_facility_history(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "analyze_incident",
        lambda *_args, **_kwargs: {
            "schema_version": "incident-analysis-v1",
            "status": "NEEDS_SUBSTANCE_CONFIRMATION",
            "parsed_report": {},
            "substance_candidates": [],
            "evidence": [],
            "rule_review": {
                "executed": False,
                "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
                "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
                "missing_confirmations": ["incident_cas", "facility_cas"],
                "reason": "현장 확인이 필요합니다.",
            },
            "output_validation": {"status": "PASSED", "errors": []},
        },
    )
    monkeypatch.setattr(
        api,
        "search_facility_history",
        lambda *_args, **_kwargs: {
            "status": "CANDIDATES_FOUND",
            "results": [
                {
                    "cas_number": "7647-01-0",
                    "current_inventory_confirmed": False,
                    "risk_level_ko": "중간",
                }
            ],
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)
    request = _analyze_payload("미상 물질 신고")
    request["location"] = {"facility_name": "OO전자 공장", "province": "경기도"}

    with TestClient(application) as client:
        response = client.post("/api/v1/incidents/analyze", json=request)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "UNCONFIRMED_RISK_OUTPUT_BLOCKED"


@pytest.mark.parametrize(
    "forged_status",
    [
        "SCREENING_COMPLETED",
        "VERIFY_REQUIRED",
        "UNCLASSIFIED",
        "CAMEO_GROUP_SCREENING_ONLY",
    ],
)
def test_api_blocks_completed_or_uncertain_state_without_two_confirmations(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
    forged_status: str,
) -> None:
    monkeypatch.setattr(
        api,
        "analyze_incident",
        lambda *_args, **_kwargs: {
            "schema_version": "incident-analysis-v1",
            "status": forged_status,
            "parsed_report": {},
            "substance_candidates": [],
            "evidence": [],
            "rule_review": {
                "executed": False,
                "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
                "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
                "missing_confirmations": ["incident_cas", "facility_cas"],
                "reason": "현장 확인이 필요합니다.",
            },
            "output_validation": {"status": "PASSED", "errors": []},
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("현장 미확인 물질 신고"),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "UNCONFIRMED_RISK_OUTPUT_BLOCKED"


def test_api_blocks_missing_confirmation_role_mismatch(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "analyze_incident",
        lambda *_args, **_kwargs: {
            "schema_version": "incident-analysis-v1",
            "status": "NEEDS_SUBSTANCE_CONFIRMATION",
            "parsed_report": {},
            "substance_candidates": [],
            "evidence": [],
            "rule_review": {
                "executed": False,
                "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
                "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
                "missing_confirmations": ["incident_cas"],
                "reason": "현장 확인이 필요합니다.",
            },
            "output_validation": {"status": "PASSED", "errors": []},
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("현장 미확인 물질 신고"),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "UNCONFIRMED_RISK_OUTPUT_BLOCKED"


def test_api_blocks_risk_nested_in_unconfirmed_evidence(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline_result = _safe_unconfirmed_pipeline_result()
    pipeline_result["evidence"] = [
        {
            "role": "UNKNOWN",
            "cas_hint": None,
            "cas_basis": "NO_CAS_HINT",
            "requires_responder_confirmation": True,
            "risk_level_ko": "높음",
            "recommended_actions": ["위험구역 통제"],
            "retrieval": {"status": "NO_EVIDENCE_FOUND", "results": []},
        }
    ]
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("미확인 물질 신고"),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "UNCONFIRMED_RISK_OUTPUT_BLOCKED"


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("rule_eligible", True),
        ("current_inventory_confirmed", True),
        ("requires_responder_confirmation", False),
        ("presence_status", "CONFIRMED_PRESENT"),
    ],
)
def test_api_blocks_candidate_promotion_without_confirmation(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    forged_value: Any,
) -> None:
    pipeline_result = _safe_unconfirmed_pipeline_result()
    pipeline_result["substance_candidates"] = [
        {
            "cas_number": "7681-52-9",
            "requires_responder_confirmation": True,
            field: forged_value,
        }
    ]
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("미확인 물질 신고"),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "UNCONFIRMED_RISK_OUTPUT_BLOCKED"


def test_api_blocks_facility_history_promotion_to_current_inventory(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "analyze_incident",
        lambda *_args, **_kwargs: _safe_unconfirmed_pipeline_result(),
    )
    monkeypatch.setattr(
        api,
        "search_facility_history",
        lambda *_args, **_kwargs: {
            "status": "CANDIDATES_FOUND",
            "results": [
                {
                    "cas_number": "7647-01-0",
                    "current_inventory_confirmed": True,
                    "rule_eligible": False,
                    "requires_on_site_confirmation": True,
                }
            ],
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)
    payload = _analyze_payload("미확인 물질 신고")
    payload["location"] = {"facility_name": "OO전자 공장"}

    with TestClient(application) as client:
        response = client.post("/api/v1/incidents/analyze", json=payload)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "UNCONFIRMED_RISK_OUTPUT_BLOCKED"


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("expert_reviewed", True),
        ("decision_support_only", False),
        ("responder_confirmation_required", False),
        ("confirmations", {"incident": {"confirmation_id": "FORGED"}}),
    ],
)
def test_analysis_contract_blocks_unconfirmed_provenance_promotion(
    runtime: ModelRuntime,
    stub_pipeline_boundaries: list[dict[str, Any]],
    field: str,
    forged_value: Any,
) -> None:
    application = create_app(runtime=runtime, allow_anonymous=True)
    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("미확인 물질 신고"),
        )
    assert response.status_code == 200
    forged_response = response.json()
    forged_response["provenance"][field] = forged_value

    with pytest.raises(ValidationError):
        AnalysisResponse.model_validate(forged_response)


@pytest.mark.parametrize(
    "review",
    [
        {},
        {
            "executed": False,
            "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
            "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
            "missing_confirmations": ["incident_cas"],
            "reason": "현장 확인이 필요합니다.",
        },
    ],
)
def test_api_requires_executed_review_after_two_confirmations(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
    review: dict[str, Any],
) -> None:
    pipeline_result = _safe_confirmed_pipeline_result()
    pipeline_result["rule_review"] = review
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload_with_confirmed_pair(),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


def test_api_blocks_confirmed_state_that_disagrees_with_rule_result(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline_result = _safe_confirmed_pipeline_result()
    pipeline_result["status"] = "NEEDS_SUBSTANCE_CONFIRMATION"
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload_with_confirmed_pair(),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


def test_completed_analysis_exposes_grounded_rag_without_new_endpoint(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _public_source_completed_rule_result()
    pipeline_result = _safe_confirmed_pipeline_result()
    pipeline_result["status"] = "SCREENING_COMPLETED"
    pipeline_result["rule_review"] = {
        "executed": True,
        "status": "SCREENING_COMPLETED",
        "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
        "policy_mode": PUBLIC_SOURCE_PILOT_POLICY,
        "result": result,
    }
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload_with_confirmed_pair(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "SCREENING_COMPLETED"
    assert body["grounded_rag"]["status"] == "FALLBACK_EXTRACTIVE"
    assert body["grounded_rag"]["used_llm"] is False
    assert {item["source_id"] for item in body["grounded_rag"]["citations"]} == {
        "RULE_RESULT"
    }
    assert body["conflict_review"]["result"]["risk_level_ko"] == "높음"


def test_incident_agent_step_completes_only_after_confirmed_safe_analysis(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _public_source_completed_rule_result()
    pipeline_result = _safe_confirmed_pipeline_result()
    pipeline_result["status"] = "SCREENING_COMPLETED"
    pipeline_result["rule_review"] = {
        "executed": True,
        "status": "SCREENING_COMPLETED",
        "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
        "policy_mode": PUBLIC_SOURCE_PILOT_POLICY,
        "result": result,
    }
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/agents/incidents/step",
            json={"analysis": _analyze_payload_with_confirmed_pair()},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "GOAL_COMPLETED"
    assert body["selected_tool_count"] == 3
    assert [item["tool_id"] for item in body["memory"]["history"]] == [
        "RUN_INCIDENT_ANALYSIS",
        "VERIFY_SAFETY_CONTRACT",
        "PRESENT_DECISION_SUPPORT",
    ]
    assert body["analysis"]["state"] == "SCREENING_COMPLETED"
    assert body["analysis"]["conflict_review"]["result"]["risk_level_ko"] == "높음"
    assert body["autonomous_risk_decision_allowed"] is False


@pytest.mark.parametrize(
    ("review_status", "scope", "policy_mode", "extra_fields"),
    [
        ("COMPLETED_DEMO", "DRAFT", APPROVED_ONLY_POLICY, {}),
        ("COMPLETED", "DRAFT", APPROVED_ONLY_POLICY, {}),
        ("COMPLETED", "APPROVED", PUBLIC_SOURCE_PILOT_POLICY, {}),
        (
            "COMPLETED",
            "APPROVED",
            APPROVED_ONLY_POLICY,
            {
                "expected_response": ["근거 계약 밖의 임의 대응"],
                "recommended_actions": ["근거 계약 밖의 임의 대응"],
            },
        ),
    ],
)
def test_api_blocks_demo_draft_policy_mismatch_and_unknown_action_fields(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
    review_status: str,
    scope: str,
    policy_mode: str,
    extra_fields: dict[str, Any],
) -> None:
    if policy_mode == APPROVED_ONLY_POLICY:
        _enable_approved_policy_for_test(runtime)
    result = {
        **_completed_rule_result(),
        "status": review_status,
        "scope": scope,
        **extra_fields,
    }
    pipeline_result = _safe_confirmed_pipeline_result()
    pipeline_result["status"] = "COMPLETED_WITH_WARNINGS"
    pipeline_result["conflict_policy_mode"] = policy_mode
    pipeline_result["rule_review"] = {
        "executed": True,
        "status": review_status,
        "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
        "policy_mode": policy_mode,
        "result": result,
    }
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(
        runtime=runtime,
        allow_anonymous=True,
        rule_policy=policy_mode,
    )

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload_with_confirmed_pair(),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


@pytest.mark.parametrize(
    "forged_evidence",
    [
        {
            "role": "INCIDENT",
            "cas_hint": "7664-93-9",
            "cas_basis": "RESPONDER_CONFIRMED",
            "requires_responder_confirmation": False,
            "retrieval": {
                "status": "CAS_EVIDENCE_NOT_LOADED",
                "cas_hint": "7664-93-9",
                "results": [],
            },
        },
        {
            "role": "INCIDENT",
            "cas_hint": "7664-93-9",
            "cas_basis": "PARSER_CANDIDATE",
            "requires_responder_confirmation": True,
            "retrieval": {
                "status": "CAS_EVIDENCE_NOT_LOADED",
                "cas_hint": "7664-93-9",
                "results": [],
            },
        },
        {
            "role": "UNKNOWN",
            "cas_hint": None,
            "cas_basis": "NO_CAS_HINT",
            "requires_responder_confirmation": True,
            "retrieval": {
                "status": "NO_EVIDENCE_FOUND",
                "cas_hint": None,
                "results": [],
            },
        },
    ],
)
def test_api_blocks_evidence_that_disagrees_with_confirmation_gate(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
    forged_evidence: dict[str, Any],
) -> None:
    pipeline_result = _safe_confirmed_pipeline_result()
    pipeline_result["evidence"] = [forged_evidence]
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload_with_confirmed_pair(),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


def test_api_blocks_probability_disguised_as_completed_risk(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_approved_policy_for_test(runtime)
    result = _completed_rule_result()
    result["risk_scale"] = {
        "type": "ORDINAL_RULE_CLASSIFICATION",
        "is_probability": True,
        "probability_percent": 99,
    }
    pipeline_result = _safe_confirmed_pipeline_result()
    pipeline_result["status"] = "COMPLETED_WITH_WARNINGS"
    pipeline_result["conflict_policy_mode"] = APPROVED_ONLY_POLICY
    pipeline_result["rule_review"] = {
        "executed": True,
        "status": "COMPLETED",
        "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
        "policy_mode": APPROVED_ONLY_POLICY,
        "result": result,
    }
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(
        runtime=runtime,
        allow_anonymous=True,
        rule_policy=APPROVED_ONLY_POLICY,
    )

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload_with_confirmed_pair(),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


def test_api_blocks_completed_rule_cas_mismatch(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_approved_policy_for_test(runtime)
    result = _completed_rule_result()
    result["incident_cas"] = "7664-93-9"
    pipeline_result = _safe_confirmed_pipeline_result()
    pipeline_result["status"] = "COMPLETED_WITH_WARNINGS"
    pipeline_result["conflict_policy_mode"] = APPROVED_ONLY_POLICY
    pipeline_result["rule_review"] = {
        "executed": True,
        "status": "COMPLETED",
        "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
        "policy_mode": APPROVED_ONLY_POLICY,
        "result": result,
    }
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(
        runtime=runtime,
        allow_anonymous=True,
        rule_policy=APPROVED_ONLY_POLICY,
    )

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload_with_confirmed_pair(),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


def test_direct_conflict_review_blocks_risk_fields_on_inconclusive_result(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "review_pair",
        lambda *_args, **_kwargs: {
            "status": "VERIFY_REQUIRED",
            "severity": None,
            "reason": "근거 추가 확인이 필요합니다.",
            "human_confirmation_required": True,
            "risk_level": "HIGH",
            "risk_level_ko": "높음",
            "recommended_actions": ["위험구역 통제"],
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)
    payload = {
        "incident": _analyze_payload_with_confirmed_pair()[
            "confirmed_incident_substance"
        ],
        "facility": _analyze_payload_with_confirmed_pair()[
            "confirmed_facility_substance"
        ],
    }

    with TestClient(application) as client:
        response = client.post("/api/v1/conflicts/review", json=payload)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


@pytest.mark.parametrize(
    "validation_record",
    [
        None,
        {"status": "FAILED", "errors": ["검증 실패"]},
        {"status": "PASSED", "errors": ["숨겨진 오류"]},
    ],
)
def test_api_requires_clean_pipeline_output_validation_record(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
    validation_record: dict[str, Any] | None,
) -> None:
    pipeline_result = _safe_unconfirmed_pipeline_result()
    if validation_record is None:
        pipeline_result.pop("output_validation")
    else:
        pipeline_result["output_validation"] = validation_record
    monkeypatch.setattr(
        api, "analyze_incident", lambda *_args, **_kwargs: pipeline_result
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("미확인 물질 신고"),
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OUTPUT_VALIDATION_FAILED"


@pytest.mark.parametrize(
    ("pipeline_status", "expected_http_status", "expected_error_code"),
    [
        ("INVALID_INPUT", 422, "INVALID_PIPELINE_INPUT"),
        ("OUTPUT_VALIDATION_FAILED", 500, "OUTPUT_VALIDATION_FAILED"),
    ],
)
def test_pipeline_failure_state_is_reported_before_confirmation_gate(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
    pipeline_status: str,
    expected_http_status: int,
    expected_error_code: str,
) -> None:
    monkeypatch.setattr(
        api,
        "analyze_incident",
        lambda *_args, **_kwargs: {
            "schema_version": "incident-analysis-v1",
            "status": pipeline_status,
            "parsed_report": {},
            "substance_candidates": [],
            "evidence": [],
            "rule_review": {},
            "output_validation": {"status": "FAILED", "errors": ["test"]},
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload("파이프라인 실패 상태 시험"),
        )

    assert response.status_code == expected_http_status
    assert response.json()["error"]["code"] == expected_error_code


def test_confirmed_canonical_request_allows_same_cas_without_query_hit(
    runtime: ModelRuntime,
    stub_pipeline_boundaries: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def search_with_loaded_but_unmatched_facility(
        query: str,
        _db_path: Path,
        _artifact: dict[str, Any],
        cas_hint: str | None = None,
        top_k: int = 5,
    ) -> dict[str, Any]:
        results = (
            []
            if cas_hint == "7647-01-0"
            else [
                {
                    "evidence_id": "KOSHA:TEST",
                    "source": "KOSHA",
                    "cas_number": cas_hint,
                }
            ]
        )
        return {
            "status": "COMPLETED" if results else "NO_EVIDENCE_FOUND",
            "query": query,
            "cas_hint": cas_hint,
            "top_k": top_k,
            "results": results,
        }

    monkeypatch.setattr(
        pipeline,
        "search_evidence",
        search_with_loaded_but_unmatched_facility,
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/incidents/analyze",
            json=_analyze_payload_with_confirmed_pair()
            | {
                "input": {
                    "type": "MANUAL_TEXT",
                    "text": "○○전자 공장, 차아염소산나트륨 저장탱크 누출",
                }
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "VERIFY_REQUIRED"
    assert body["confirmation_gate"]["all_required_confirmed"] is True
    facility_evidence = next(
        item for item in body["evidence"] if item["role"] == "FACILITY"
    )
    assert facility_evidence["cas_hint"] == "7647-01-0"
    assert facility_evidence["retrieval"]["status"] == "NO_EVIDENCE_FOUND"
    assert facility_evidence["retrieval"]["results"] == []
    assert len(stub_pipeline_boundaries) == 1


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
    assert (
        one_response.json()["grounded_rag"]["status"]
        == "NOT_RUN_REQUIRES_CONFIRMED_PAIR"
    )
    assert missing_id_response.status_code == 422
    assert not_present_response.status_code == 422

    assert both_response.status_code == 200
    both_body = both_response.json()
    assert both_body["state"] == "VERIFY_REQUIRED"
    assert both_body["conflict_review"]["executed"] is True
    assert both_body["grounded_rag"]["status"] == "NOT_RUN_RULE_NOT_COMPLETED"
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


def test_material_discovery_exposes_safe_dashboard_contract(
    runtime: ModelRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api,
        "discover_substances",
        lambda query, **kwargs: {
            "query": query,
            "status": "CANDIDATES_FOUND",
            "search_mode": "PROPERTY_PROFILE_RETRIEVAL",
            "method": "test property retrieval",
            "profile_index_available": True,
            "candidates": [
                {
                    "rank": 1,
                    "cas_number": "78-93-3",
                    "display_name": "메틸 에틸 케톤",
                    "match_basis": "PUBLIC_PROPERTY_PROFILE",
                    "matched_expression": None,
                    "matched_properties": [
                        {
                            "field": "odor",
                            "label": "냄새",
                            "value": "박하 및 달콤한 냄새",
                        }
                    ],
                    "property_profile": {
                        "physical_state": "액체(휘발성)",
                        "color": "무색 투명",
                        "odor": "박하 및 달콤한 냄새",
                        "use_description": "용제",
                        "source_id": "NFA_ULSAN_CHEMICAL_INFORMATION",
                        "source_url": (
                            "https://www.data.go.kr/data/15081005/fileData.do"
                        ),
                        "document_version": "2021-01-15 기준",
                    },
                    "evidence_status": "COMPLETED",
                    "evidence_warning": (
                        "검색 순위는 위험등급이 아니며 원문을 확인해야 합니다."
                    ),
                    "evidence_notice": None,
                    "cas_link_warning": None,
                    "evidence": [
                        {
                            "evidence_id": "KOSHA:MEK-1",
                            "cas_number": "78-93-3",
                            "source": "KOSHA",
                            "title": "메틸 에틸 케톤 MSDS",
                            "body_preview": "공식 문서 발췌",
                            "source_url": "https://example.test/kosha/mek",
                            "document_version": "2026-01-01",
                            "cas_link_status": "SOURCE_EXACT",
                        }
                    ],
                    "requires_responder_confirmation": True,
                    "rule_eligible": False,
                    "risk_determination_allowed": False,
                }
            ],
            "requires_responder_confirmation": True,
            "rule_eligible": False,
            "risk_determination_allowed": False,
            "candidate_score_is_probability": False,
            "notice": "용기 라벨·현장 MSDS로 확인해야 합니다.",
        },
    )
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/substances/discover",
            json={
                "query": "무색 투명하고 박하 냄새가 나는 휘발성 액체",
                "top_k": 5,
                "evidence_top_k": 3,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "chemiguard119-api-v1"
    assert body["status"] == "CANDIDATES_FOUND"
    assert body["candidates"][0]["cas_number"] == "78-93-3"
    assert body["candidates"][0]["requires_responder_confirmation"] is True
    assert body["candidates"][0]["rule_eligible"] is False
    assert body["candidates"][0]["risk_determination_allowed"] is False
    assert "위험등급이 아니며" in body["candidates"][0]["evidence_warning"]
    assert body["candidates"][0]["evidence"][0]["cas_link_status"] == "SOURCE_EXACT"
    assert body["candidate_score_is_probability"] is False
    assert "현장 지휘관" in body["safety_notice"]


def test_material_discovery_rejects_too_short_observation(
    runtime: ModelRuntime,
) -> None:
    application = create_app(runtime=runtime, allow_anonymous=True)

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/substances/discover",
            json={"query": "물"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_SCHEMA"


def test_material_discovery_candidate_rejects_mismatched_evidence_cas() -> None:
    with pytest.raises(ValidationError, match="근거 카드의 CAS"):
        SubstanceDiscoveryCandidate.model_validate(
            {
                "rank": 1,
                "cas_number": "78-93-3",
                "display_name": "메틸 에틸 케톤",
                "match_basis": "IDENTITY_EXPRESSION",
                "matched_expression": "메틸 에틸 케톤",
                "matched_properties": [],
                "property_profile": None,
                "evidence_status": "COMPLETED",
                "evidence": [
                    {
                        "evidence_id": "KOSHA:OTHER",
                        "cas_number": "67-64-1",
                        "source": "KOSHA",
                        "title": "다른 물질 문서",
                        "body_preview": "다른 CAS 근거",
                        "source_url": "https://example.test/kosha/other",
                        "document_version": "2026-01-01",
                        "cas_link_status": "SOURCE_EXACT",
                    }
                ],
                "requires_responder_confirmation": True,
                "rule_eligible": False,
                "risk_determination_allowed": False,
            }
        )


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
        "/api/v1/agents/incidents/step",
        "/api/v1/substances/discover",
        "/api/v1/substances/resolve",
        "/api/v1/evidence/search",
        "/api/v1/facilities/candidates",
        "/api/v1/conflicts/review",
    }
    assert "post" in paths["/api/v1/incidents/analyze"]
    assert "post" in paths["/api/v1/agents/incidents/step"]
    assert "post" in paths["/api/v1/substances/discover"]
    assert "post" in paths["/api/v1/conflicts/review"]
    assert (
        paths["/api/v1/substances/discover"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/SubstanceDiscoveryResponse"
    )
    assert "ErrorResponse" in response.json()["components"]["schemas"]
    assert not any("demo" in path for path in paths)
