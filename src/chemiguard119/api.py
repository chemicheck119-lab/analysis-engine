"""케미체크119 모델 파이프라인을 백엔드에 제공하는 FastAPI 애플리케이션."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from chemiguard119 import __version__
from chemiguard119.api_models import (
    API_SCHEMA_VERSION,
    CONFIRMATION_GATE_POLICY,
    IDENTIFIER_PATTERN,
    PUBLIC_SERVICE_NAME,
    AnalysisResponse,
    ConflictReviewRequest,
    ErrorResponse,
    ExecutedConflictReview,
    EvidenceSearchRequest,
    FacilityHistorySearchRequest,
    IncidentAnalyzeRequest,
    ResolveRequest,
    SubstanceDiscoveryRequest,
    SubstanceDiscoveryResponse,
    UnconfirmedConflictReview,
    analysis_state_for_review_status,
    contains_candidate_promotion,
    contains_unconfirmed_risk_output,
    validate_evidence_confirmation_gate,
)
from chemiguard119.agent_loop import (
    AGENT_MEMORY_SCHEMA_VERSION,
    AGENT_SCHEMA_VERSION,
    TOOL_REGISTRY,
    IncidentAgentRunner,
    IncidentAgentStepRequest,
    IncidentAgentStepResponse,
)
from chemiguard119.coverage import facility_history_coverage
from chemiguard119.paths import (
    CONFIG_DIR,
    DEFAULT_DB_PATH,
    DEFAULT_RESOLVER_MODEL,
    DEFAULT_RETRIEVER_MODEL,
)
from chemiguard119.discovery import discover_substances
from chemiguard119.database import connect_readonly
from chemiguard119.material_ranker import ranking_model_metadata
from chemiguard119.observability import configure_json_logging, emit_json_event
from chemiguard119.operations import build_operations_agent_snapshot
from chemiguard119.facility import search_facility_history
from chemiguard119.evidence_assurance import (
    reference_assurance_configuration_status,
)
from chemiguard119.pipeline import PIPELINE_SCHEMA_VERSION, analyze_incident
from chemiguard119.preprocessing import MINIMUM_ULSAN_PROFILE_COUNT
from chemiguard119.rag import GroundedRagService, RAG_SCHEMA_VERSION
from chemiguard119.resolver import load_resolver, resolve_substance
from chemiguard119.retrieval import load_retriever, search_evidence
from chemiguard119.release import (
    SUPPORTED_DEPLOYMENT_ENVIRONMENTS,
    TRUSTED_DEPLOYMENT_ENVIRONMENTS,
    verify_runtime_release,
)
from chemiguard119.rules import (
    APPROVED_ONLY_POLICY,
    PUBLIC_SOURCE_METHOD,
    PUBLIC_SOURCE_PILOT_POLICY,
    PUBLIC_SOURCE_PRODUCT,
    PUBLIC_SOURCE_VERIFIED,
    SUPPORTED_POLICY_MODES,
    review_pair,
)


SAFETY_NOTICE = (
    "이 결과는 케미체크119의 의사결정 보조 정보이며 현장 명령이 아닙니다. "
    "물질과 시설 상태를 대원이 확인하고 최종 결정은 현장 지휘관이 수행합니다."
)
SERVICE_ID = "chemicheck119-model-api"
INTERNAL_PACKAGE_NAME = "chemiguard119"
AUTH_HEADER_NAME = "X-API-Key"
REQUEST_ID_HEADER_NAME = "X-Request-Id"
DEPLOYMENT_ENVIRONMENT_ENV_VAR = "CHEMIGUARD119_ENVIRONMENT"
RULE_POLICY_ENV_VAR = "CHEMIGUARD119_RULE_POLICY"
REQUEST_ID_MAX_LENGTH = 128
REQUEST_ID_RE = re.compile(IDENTIFIER_PATTERN)
LOGGER = logging.getLogger("chemiguard119.api")
STANDARD_ERROR_RESPONSES = {
    status.HTTP_401_UNAUTHORIZED: {
        "model": ErrorResponse,
        "description": "백엔드 인증 실패",
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "model": ErrorResponse,
        "description": "요청 계약 위반",
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": ErrorResponse,
        "description": "출력 안전 검증 또는 내부 오류",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "인증 또는 모델 artifact 준비 실패",
    },
}


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _production_api_key_error(api_key: str | None, environment: str) -> str | None:
    if environment.strip().lower() not in TRUSTED_DEPLOYMENT_ENVIRONMENTS:
        return None
    if (
        not api_key
        or re.fullmatch(r"(?:[0-9a-fA-F]{64}|[A-Za-z0-9_-]{43})", api_key) is None
    ):
        return (
            "staging·production API 키는 32바이트 난수의 64자리 hex 또는 "
            "43자리 base64url 형식이어야 합니다."
        )
    lowered = api_key.lower()
    if any(
        token in lowered for token in ("교체", "change-me", "changeme", "replace-me")
    ):
        return "예시용 API 키는 운영에서 사용할 수 없습니다."
    return None


def _deployment_environment_error(environment: str) -> str | None:
    if environment not in SUPPORTED_DEPLOYMENT_ENVIRONMENTS:
        allowed = ", ".join(sorted(SUPPORTED_DEPLOYMENT_ENVIRONMENTS))
        return f"지원하지 않는 배포 환경입니다: {environment!r} (허용: {allowed})"
    return None


def _conflict_review_capability(
    config_dir: Path,
    policy_mode: str,
) -> dict[str, Any]:
    """선택한 정책에서 실제 사용할 수 있는 충돌 검토 범위를 노출한다."""

    crosswalk_path = config_dir / "cameo_crosswalk.csv"
    direct_rule_path = config_dir / "pair_rules.csv"
    policy_path = config_dir / "conflict_policy.json"
    reference_assurance = reference_assurance_configuration_status(config_dir)
    try:
        with crosswalk_path.open(encoding="utf-8-sig", newline="") as handle:
            crosswalk_rows = list(csv.DictReader(handle))
        with direct_rule_path.open(encoding="utf-8-sig", newline="") as handle:
            direct_rule_rows = list(csv.DictReader(handle))
        with policy_path.open(encoding="utf-8") as handle:
            public_policy = json.load(handle)
    except (OSError, csv.Error, json.JSONDecodeError):
        return {
            "policy_mode": policy_mode,
            "public_source_verified_crosswalk_count": 0,
            "eligible_public_source_cas_count": 0,
            "approved_crosswalk_count": 0,
            "approved_direct_rule_count": 0,
            "public_source_screening_ready": False,
            "expert_approved_decision_ready": False,
            "conflict_review_ready": False,
            "expert_reviewed": False,
            "direct_rules_enabled": False,
            "configuration_valid": False,
            "reference_assurance": reference_assurance,
        }

    public_rows = [
        row
        for row in crosswalk_rows
        if str(row.get("verification_status") or "").strip() == PUBLIC_SOURCE_VERIFIED
    ]
    eligible_public_rows: list[dict[str, str]] = []
    for row in public_rows:
        cameo_id = str(row.get("cameo_chemical_id") or "").strip()
        checked_at = str(row.get("checked_at_utc") or "").strip()
        try:
            checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            checked_is_valid = (
                checked.tzinfo is not None and checked.utcoffset() is not None
            )
        except ValueError:
            checked_is_valid = False
        if all(
            (
                cameo_id.isdigit(),
                str(row.get("selected_form") or "").strip(),
                str(row.get("verification_method") or "").strip()
                == PUBLIC_SOURCE_METHOD,
                str(row.get("source_product") or "").strip() == PUBLIC_SOURCE_PRODUCT,
                str(row.get("source_version") or "").strip(),
                str(row.get("evidence_url") or "").strip()
                == f"https://cameochemicals.noaa.gov/chemical/{cameo_id}",
                checked_is_valid,
            )
        ):
            eligible_public_rows.append(row)
    public_cas = {
        str(row.get("cas_number") or "").strip()
        for row in eligible_public_rows
        if str(row.get("cas_number") or "").strip()
    }
    approved_rows = [
        row
        for row in crosswalk_rows
        if str(row.get("verification_status") or "").strip() == "APPROVED"
    ]
    approved_cas = {
        str(row.get("cas_number") or "").strip()
        for row in approved_rows
        if str(row.get("cas_number") or "").strip()
    }
    approved_direct_rule_count = sum(
        str(row.get("approval_status") or "").strip() == "APPROVED"
        for row in direct_rule_rows
    )
    public_policy_valid = bool(
        isinstance(public_policy, dict)
        and public_policy.get("policy_id") == PUBLIC_SOURCE_PILOT_POLICY
        and public_policy.get("eligible_crosswalk_statuses") == [PUBLIC_SOURCE_VERIFIED]
        and public_policy.get("required_verification_method") == PUBLIC_SOURCE_METHOD
        and public_policy.get("required_source_product") == PUBLIC_SOURCE_PRODUCT
        and public_policy.get("allow_direct_rules") is False
        and public_policy.get("require_two_responder_confirmed_cas") is True
        and public_policy.get("decision_support_only") is True
        and public_policy.get("expert_review_required") is False
        and public_policy.get("probability_output_allowed") is False
        and public_policy.get("final_decision_authority") == "현장 지휘관 판단"
    )
    public_configuration_valid = bool(
        public_policy_valid and reference_assurance["ready"]
    )
    public_ready = public_configuration_valid and len(public_cas) >= 2
    approved_ready = approved_direct_rule_count > 0 or len(approved_cas) >= 2
    conflict_ready = (
        public_ready if policy_mode == PUBLIC_SOURCE_PILOT_POLICY else approved_ready
    )
    return {
        "policy_mode": policy_mode,
        "public_source_verified_crosswalk_count": len(public_rows),
        "eligible_public_source_cas_count": len(public_cas),
        "approved_crosswalk_count": len(approved_rows),
        "approved_direct_rule_count": approved_direct_rule_count,
        "public_source_screening_ready": public_ready,
        "expert_approved_decision_ready": approved_ready,
        "conflict_review_ready": conflict_ready,
        "expert_reviewed": (policy_mode == APPROVED_ONLY_POLICY and approved_ready),
        "direct_rules_enabled": (
            policy_mode == APPROVED_ONLY_POLICY and approved_direct_rule_count > 0
        ),
        "configuration_valid": (
            public_configuration_valid
            if policy_mode == PUBLIC_SOURCE_PILOT_POLICY
            else policy_mode == APPROVED_ONLY_POLICY
        ),
        "reference_assurance": reference_assurance,
    }


def _empty_conflict_review_capability(policy_mode: str) -> dict[str, Any]:
    """runtime이 없을 때도 metadata 계약을 동일한 형태로 유지한다."""

    return {
        "policy_mode": policy_mode,
        "public_source_verified_crosswalk_count": 0,
        "eligible_public_source_cas_count": 0,
        "approved_crosswalk_count": 0,
        "approved_direct_rule_count": 0,
        "public_source_screening_ready": False,
        "expert_approved_decision_ready": False,
        "conflict_review_ready": False,
        "expert_reviewed": False,
        "direct_rules_enabled": False,
        "configuration_valid": False,
        "reference_assurance": {
            "ready": False,
            "schema_version": None,
            "policy_id": None,
            "registry_sha256": None,
            "authority_count": 0,
            "triangulated_pair_count": 0,
            "expert_reviewed": False,
            "human_expert_substitute": False,
            "error": "runtime artifact를 불러오지 못했습니다.",
        },
    }


def _result_expert_reviewed(policy_mode: str, result: dict[str, Any]) -> bool:
    """정책 이름이 아니라 실제 완료 결과를 기준으로 전문가 승인 여부를 표시한다."""

    if "expert_reviewed" in result:
        return result.get("expert_reviewed") is True
    return bool(
        policy_mode == APPROVED_ONLY_POLICY and result.get("status") == "COMPLETED"
    )


class APIBoundaryError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        fields: list[str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.fields = fields or []
        self.headers = headers or {}


@dataclass(frozen=True)
class ModelRuntime:
    db_path: Path
    resolver_model_path: Path
    retriever_model_path: Path
    config_dir: Path
    resolver_artifact: dict[str, Any]
    retriever_artifact: dict[str, Any]
    loaded_at_utc: str
    integrity: dict[str, Any] | None = None
    conflict_capabilities: dict[str, dict[str, Any]] | None = None
    facility_coverage: dict[str, Any] | None = None

    @classmethod
    def load(
        cls,
        *,
        db_path: Path = DEFAULT_DB_PATH,
        resolver_model_path: Path = DEFAULT_RESOLVER_MODEL,
        retriever_model_path: Path = DEFAULT_RETRIEVER_MODEL,
        config_dir: Path = CONFIG_DIR,
        environment: str | None = None,
    ) -> "ModelRuntime":
        integrity = verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_model_path,
            retriever_model_path=retriever_model_path,
            config_dir=config_dir,
            environment=environment,
        )
        conflict_capabilities = {
            policy_mode: _conflict_review_capability(config_dir, policy_mode)
            for policy_mode in SUPPORTED_POLICY_MODES
        }
        return cls(
            db_path=db_path,
            resolver_model_path=resolver_model_path,
            retriever_model_path=retriever_model_path,
            config_dir=config_dir,
            resolver_artifact=load_resolver(resolver_model_path),
            retriever_artifact=load_retriever(retriever_model_path),
            loaded_at_utc=datetime.now(timezone.utc).isoformat(),
            integrity=integrity,
            conflict_capabilities=conflict_capabilities,
            facility_coverage=facility_history_coverage(db_path),
        )

    def conflict_review_capability(self, policy_mode: str) -> dict[str, Any]:
        """변경 불가능한 배포 config의 계산 결과를 요청 경로에서 재사용한다."""

        if self.conflict_capabilities is not None:
            cached = self.conflict_capabilities.get(policy_mode)
            if cached is not None:
                return cached
        # 단위 테스트처럼 runtime을 직접 주입한 경우에는 config 변경을 반영한다.
        return _conflict_review_capability(self.config_dir, policy_mode)

    def readiness(
        self,
        policy_mode: str = PUBLIC_SOURCE_PILOT_POLICY,
    ) -> dict[str, Any]:
        artifact_checks = {
            "database": self.db_path.is_file(),
            "resolver": self.resolver_model_path.is_file(),
            "retriever": self.retriever_model_path.is_file(),
            "config": self.config_dir.is_dir(),
        }
        conflict_review_capability = self.conflict_review_capability(policy_mode)
        integrity = self.integrity or {
            "status": "INJECTED_OR_LEGACY_RUNTIME",
            "manifest_sha256_verified": False,
        }
        material_discovery = {
            "ready": False,
            "profile_count": 0,
            "minimum_profile_count": MINIMUM_ULSAN_PROFILE_COUNT,
            "reason": "substance_profile·FTS 인덱스를 확인하지 못했습니다.",
        }
        if self.db_path.is_file():
            try:
                with connect_readonly(self.db_path) as connection:
                    profile_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM substance_profile"
                        ).fetchone()[0]
                    )
                    fts_profile_count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM substance_profile_fts"
                        ).fetchone()[0]
                    )
                discovery_ready = (
                    profile_count >= MINIMUM_ULSAN_PROFILE_COUNT
                    and fts_profile_count == profile_count
                )
                material_discovery = {
                    "ready": discovery_ready,
                    "profile_count": profile_count,
                    "minimum_profile_count": MINIMUM_ULSAN_PROFILE_COUNT,
                    "reason": (
                        None
                        if discovery_ready
                        else (
                            "substance_profile과 FTS 인덱스의 행 수가 다릅니다."
                            if fts_profile_count != profile_count
                            else (
                                "substance_profile 인덱스가 운영 최소 "
                                f"{MINIMUM_ULSAN_PROFILE_COUNT}건보다 적습니다."
                            )
                        )
                    ),
                }
            except sqlite3.OperationalError as error:
                if "no such table: substance_profile" not in str(error).lower():
                    raise
        return {
            "ready": all(artifact_checks.values()) and material_discovery["ready"],
            "artifacts": artifact_checks,
            "loaded_at_utc": self.loaded_at_utc,
            "resolver_schema": self.resolver_artifact.get("schema_version"),
            "resolver_training_metadata": self.resolver_artifact.get(
                "training_metadata", {}
            ),
            "retriever_schema": self.retriever_artifact.get("schema_version"),
            "material_discovery_capability": material_discovery,
            "facility_history_coverage": (
                self.facility_coverage
                if self.facility_coverage is not None
                else facility_history_coverage(self.db_path)
            ),
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
            "conflict_review_capability": conflict_review_capability,
            "integrity": {
                "status": integrity.get("status"),
                "environment": integrity.get("environment"),
                "manifest_sha256_verified": bool(
                    integrity.get("manifest_sha256_verified", False)
                ),
                "git_commit": integrity.get("git_commit"),
            },
        }


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex.upper()}"


def _valid_request_id(value: str | None) -> str | None:
    if not value or len(value) > REQUEST_ID_MAX_LENGTH:
        return None
    if REQUEST_ID_RE.fullmatch(value) is None:
        return None
    return value


def _request_id(request: Request, body_request_id: str | None = None) -> str:
    value = next(
        (
            valid
            for candidate in (
                body_request_id,
                getattr(request.state, "request_id", None),
                request.headers.get(REQUEST_ID_HEADER_NAME),
            )
            if (valid := _valid_request_id(candidate)) is not None
        ),
        _new_id("REQ"),
    )
    request.state.request_id = value
    return value


def _request_route(request: Request) -> str:
    """쿼리 문자열이나 본문 없이 저카디널리티 API route만 반환한다."""

    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return "<unmatched>"


def _request_outcome(status_code: int) -> str:
    if status_code >= 500:
        return "SERVER_ERROR"
    if status_code >= 400:
        return "CLIENT_ERROR"
    return "SUCCESS"


def _request_log_level(status_code: int) -> int:
    if status_code >= 500:
        return logging.ERROR
    if status_code >= 400:
        return logging.WARNING
    return logging.INFO


def _error_payload(
    *,
    code: str,
    message: str,
    retryable: bool,
    request_id: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "service_name": PUBLIC_SERVICE_NAME,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "fields": fields or [],
        },
        "request_id": request_id,
        "occurred_at_utc": datetime.now(timezone.utc).isoformat(),
    }


def _auth_mode(request: Request) -> str:
    if getattr(request.app.state, "auth_config_error", None):
        return "MISCONFIGURED_FAIL_CLOSED"
    if getattr(request.app.state, "allow_anonymous", False):
        if (
            str(getattr(request.app.state, "deployment_environment", "")).lower()
            == "production"
        ):
            return "MISCONFIGURED_FAIL_CLOSED"
        return "EXPLICIT_ANONYMOUS"
    if getattr(request.app.state, "api_key", None):
        return "API_KEY"
    return "MISCONFIGURED_FAIL_CLOSED"


def _runtime_or_error(request: Request) -> ModelRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        startup_error = getattr(request.app.state, "startup_error", None)
        raise APIBoundaryError(
            "ARTIFACT_NOT_READY",
            "모델 artifact가 준비되지 않았습니다. `chemiguard119 pipeline`을 먼저 실행하세요."
            + (f" ({startup_error})" if startup_error else ""),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=True,
        )
    return runtime


def _production_integrity_ready(
    runtime: ModelRuntime | None,
    environment: str,
) -> bool:
    """운영 요청은 검증된 manifest와 고정 commit을 가진 runtime만 사용한다."""

    if environment not in TRUSTED_DEPLOYMENT_ENVIRONMENTS:
        return True
    if runtime is None:
        return False
    integrity = runtime.integrity or {}
    return bool(
        integrity.get("status") == "VERIFIED"
        and integrity.get("environment") == environment
        and integrity.get("manifest_sha256_verified") is True
        and re.fullmatch(
            r"[0-9a-fA-F]{40}",
            str(integrity.get("git_commit") or ""),
        )
        is not None
    )


def _authorize(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    auth_config_error = getattr(request.app.state, "auth_config_error", None)
    if auth_config_error:
        raise APIBoundaryError(
            "BACKEND_AUTH_CONFIGURATION_INVALID",
            str(auth_config_error),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=False,
        )
    rule_policy_error = getattr(request.app.state, "rule_policy_error", None)
    if rule_policy_error:
        raise APIBoundaryError(
            "CONFLICT_POLICY_CONFIGURATION_INVALID",
            str(rule_policy_error),
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=False,
            fields=[RULE_POLICY_ENV_VAR],
        )
    active_runtime = getattr(request.app.state, "runtime", None)
    if not _production_integrity_ready(
        active_runtime,
        request.app.state.deployment_environment,
    ):
        raise APIBoundaryError(
            "MODEL_RUNTIME_INTEGRITY_NOT_READY",
            "운영 모델 artifact의 manifest, checksum 또는 commit 검증이 완료되지 않았습니다.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=False,
        )
    if active_runtime is not None:
        capability = active_runtime.conflict_review_capability(
            request.app.state.rule_policy
        )
        if capability.get("configuration_valid") is not True:
            raise APIBoundaryError(
                "CONFLICT_POLICY_CONFIGURATION_INVALID",
                "충돌 검토 정책 파일의 형식 또는 provenance 계약이 올바르지 않습니다.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
                fields=[
                    "config/conflict_policy.json",
                    "config/cameo_crosswalk.csv",
                    "config/pair_rules.csv",
                    "config/reference_assurance_registry.json",
                ],
            )
        if capability.get("conflict_review_ready") is not True:
            raise APIBoundaryError(
                "CONFLICT_POLICY_NOT_READY",
                "선택한 충돌 검토 정책에 사용할 수 있는 검증 매핑 또는 규칙이 부족합니다.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                retryable=False,
                fields=[
                    "config/cameo_crosswalk.csv",
                    "config/pair_rules.csv",
                    "config/reference_assurance_registry.json",
                ],
            )
    if getattr(request.app.state, "allow_anonymous", False):
        return
    configured = getattr(request.app.state, "api_key", None)
    if not configured:
        raise APIBoundaryError(
            "BACKEND_AUTH_NOT_CONFIGURED",
            "모델 API 인증이 구성되지 않아 요청을 차단했습니다.",
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            retryable=False,
        )
    if not x_api_key or not secrets.compare_digest(configured, x_api_key):
        raise APIBoundaryError(
            "BACKEND_AUTH_REQUIRED",
            "유효한 백엔드 API 키가 필요합니다.",
            status_code=status.HTTP_401_UNAUTHORIZED,
            retryable=False,
            headers={"WWW-Authenticate": "ApiKey"},
        )


def _state(pipeline_status: str) -> str:
    return {
        "NEEDS_SUBSTANCE_CONFIRMATION": "AWAITING_SUBSTANCE_CONFIRMATION",
        "NEEDS_INCIDENT_SUBSTANCE_CONFIRMATION": "AWAITING_INCIDENT_CONFIRMATION",
        "NEEDS_FACILITY_SUBSTANCE_CONFIRMATION": "AWAITING_FACILITY_CONFIRMATION",
        "COMPLETED_WITH_WARNINGS": "COMPLETED",
    }.get(pipeline_status, pipeline_status)


def _required_next_steps(state_value: str) -> list[str]:
    if state_value == "AWAITING_SUBSTANCE_CONFIRMATION":
        return [
            "인증된 대원이 사고물질 CAS를 확인해 백엔드 confirmation_id를 생성합니다.",
            "시설물질의 현재 존재와 CAS를 확인해 백엔드 confirmation_id를 생성합니다.",
        ]
    if state_value == "AWAITING_INCIDENT_CONFIRMATION":
        return ["인증된 대원이 사고물질 CAS를 확인해야 Rule 검토를 실행할 수 있습니다."]
    if state_value == "AWAITING_FACILITY_CONFIRMATION":
        return ["시설물질의 현재 존재와 CAS를 확인해야 Rule 검토를 실행할 수 있습니다."]
    if state_value in {"VERIFY_REQUIRED", "UNCLASSIFIED", "CAMEO_GROUP_SCREENING_ONLY"}:
        return ["교차표·공개 근거·현장 상태를 확인하고 미확인 근거를 보완해야 합니다."]
    if state_value in {"COMPLETED", "SCREENING_COMPLETED"}:
        return ["근거와 현장 상태를 확인한 뒤 현장 지휘관이 최종 판단합니다."]
    return ["출력 상태와 누락 정보를 확인한 뒤 재검토합니다."]


def _confirmation_gate(payload: IncidentAnalyzeRequest) -> dict[str, Any]:
    incident_confirmed = payload.confirmed_incident_substance is not None
    facility_confirmed = payload.confirmed_facility_substance is not None
    all_confirmed = incident_confirmed and facility_confirmed
    return {
        "policy": CONFIRMATION_GATE_POLICY,
        "incident_confirmed": incident_confirmed,
        "facility_confirmed": facility_confirmed,
        "all_required_confirmed": all_confirmed,
        "rule_execution_allowed": all_confirmed,
    }


def _enforce_confirmation_gate(
    payload: IncidentAnalyzeRequest,
    pipeline_result: dict[str, Any],
    facility_history: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """후보만 있는 결과가 위험 확정 응답으로 승격되는 것을 API에서 재차 차단한다."""

    gate = _confirmation_gate(payload)
    review = pipeline_result.get("rule_review") or {}
    candidate_outputs = {
        "parsed_report": pipeline_result.get("parsed_report"),
        "substance_candidates": pipeline_result.get("substance_candidates"),
        "facility_history_candidates": facility_history,
    }
    evidence = pipeline_result.get("evidence") or []
    evidence_errors = (
        validate_evidence_confirmation_gate(
            evidence,
            incident_confirmed=gate["incident_confirmed"],
            facility_confirmed=gate["facility_confirmed"],
            incident_cas=(
                payload.confirmed_incident_substance.cas_number
                if payload.confirmed_incident_substance
                else None
            ),
            facility_cas=(
                payload.confirmed_facility_substance.cas_number
                if payload.confirmed_facility_substance
                else None
            ),
        )
        if isinstance(evidence, list)
        and all(isinstance(item, dict) for item in evidence)
        else ["evidence는 객체 목록이어야 합니다."]
    )
    unsafe_candidate_output = contains_candidate_promotion(
        candidate_outputs
    ) or contains_unconfirmed_risk_output(candidate_outputs)

    if gate["all_required_confirmed"]:
        try:
            validated_review = ExecutedConflictReview.model_validate(review)
        except ValidationError as error:
            raise APIBoundaryError(
                "OUTPUT_VALIDATION_FAILED",
                "확인 완료 Rule 출력이 안전 계약을 만족하지 않아 차단했습니다.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                retryable=False,
            ) from error

        expected_state = analysis_state_for_review_status(validated_review.status)
        actual_state = _state(str(pipeline_result.get("status") or ""))
        reported_policy = pipeline_result.get("conflict_policy_mode")
        if (
            unsafe_candidate_output
            or evidence_errors
            or actual_state != expected_state
            or (
                reported_policy is not None
                and validated_review.policy_mode != reported_policy
            )
        ):
            raise APIBoundaryError(
                "OUTPUT_VALIDATION_FAILED",
                "확인 완료 분석 상태 또는 후보·근거 출력이 안전 계약과 다릅니다.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                retryable=False,
            )

        completed = validated_review.status in {
            "COMPLETED",
            "SCREENING_COMPLETED",
        }
        expected_cas = {
            "incident_cas": payload.confirmed_incident_substance.cas_number,
            "facility_cas": payload.confirmed_facility_substance.cas_number,
        }
        for field, confirmed_cas in expected_cas.items():
            result_cas = validated_review.result.get(field)
            if (completed and result_cas != confirmed_cas) or (
                result_cas is not None and result_cas != confirmed_cas
            ):
                raise APIBoundaryError(
                    "OUTPUT_VALIDATION_FAILED",
                    "Rule 결과 CAS가 현장 확인 CAS와 일치하지 않습니다.",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    retryable=False,
                )
        return gate

    try:
        validated_review = UnconfirmedConflictReview.model_validate(review)
    except ValidationError as error:
        raise APIBoundaryError(
            "UNCONFIRMED_RISK_OUTPUT_BLOCKED",
            "두 물질의 현장 확인 레코드가 없어 위험도·충돌 확정 출력을 차단했습니다.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            retryable=False,
        ) from error
    expected_pipeline_status = {
        (False, False): "NEEDS_SUBSTANCE_CONFIRMATION",
        (True, False): "NEEDS_FACILITY_SUBSTANCE_CONFIRMATION",
        (False, True): "NEEDS_INCIDENT_SUBSTANCE_CONFIRMATION",
    }[(gate["incident_confirmed"], gate["facility_confirmed"])]
    expected_missing = {
        role
        for role, confirmed in (
            ("incident_cas", gate["incident_confirmed"]),
            ("facility_cas", gate["facility_confirmed"]),
        )
        if not confirmed
    }
    if (
        unsafe_candidate_output
        or evidence_errors
        or pipeline_result.get("status") != expected_pipeline_status
        or set(validated_review.missing_confirmations) != expected_missing
    ):
        raise APIBoundaryError(
            "UNCONFIRMED_RISK_OUTPUT_BLOCKED",
            "두 물질의 현장 확인 레코드가 없어 위험도·충돌 확정 출력을 차단했습니다.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            retryable=False,
        )
    return gate


def _public_analysis_response(
    payload: IncidentAnalyzeRequest,
    pipeline_result: dict[str, Any],
    runtime: ModelRuntime,
    *,
    request_id: str,
    analysis_id: str,
    started_at: float,
    rule_policy: str,
    rag_service: GroundedRagService,
    facility_history: dict[str, Any] | None = None,
) -> AnalysisResponse:
    if pipeline_result.get("status") == "INVALID_INPUT":
        raise APIBoundaryError(
            "INVALID_PIPELINE_INPUT",
            "모델 파이프라인 입력 검증에 실패했습니다.",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            fields=["input"],
        )
    if pipeline_result.get("status") == "OUTPUT_VALIDATION_FAILED":
        raise APIBoundaryError(
            "OUTPUT_VALIDATION_FAILED",
            "안전 불변조건을 통과하지 못한 모델 출력이 차단되었습니다.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            retryable=False,
        )
    output_validation = pipeline_result.get("output_validation")
    if (
        not isinstance(output_validation, dict)
        or output_validation.get("status") != "PASSED"
        or output_validation.get("errors") != []
    ):
        raise APIBoundaryError(
            "OUTPUT_VALIDATION_FAILED",
            "파이프라인 출력 검증 기록이 없거나 실패 상태여서 응답을 차단했습니다.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            retryable=False,
        )
    confirmation_gate = _enforce_confirmation_gate(
        payload,
        pipeline_result,
        facility_history,
    )

    parsed = deepcopy(pipeline_result.get("parsed_report") or {})
    parsed.pop("source_text", None)
    model_outputs = {
        "pipeline_schema_version": pipeline_result.get("schema_version"),
        "parser": parsed,
        "substance_candidates": pipeline_result.get("substance_candidates", []),
        "candidate_score_notice": (
            "후보 점수는 이름 유사도·정렬값이며 사고확률, 위험확률 또는 Rule 실행 권한이 아닙니다."
        ),
        "output_validation": pipeline_result.get("output_validation"),
        "facility_history_candidates": facility_history,
    }

    confirmation_trace: dict[str, Any] = {}
    if payload.confirmed_incident_substance:
        confirmation_trace["incident"] = {
            "confirmation_id": payload.confirmed_incident_substance.confirmation_id,
            "confirmation_basis": payload.confirmed_incident_substance.confirmation_basis.value,
            "presence_status": payload.confirmed_incident_substance.presence_status,
            "observed_at": payload.confirmed_incident_substance.observed_at.isoformat(),
        }
    if payload.confirmed_facility_substance:
        confirmation_trace["facility"] = {
            "confirmation_id": payload.confirmed_facility_substance.confirmation_id,
            "confirmation_basis": payload.confirmed_facility_substance.confirmation_basis.value,
            "presence_status": payload.confirmed_facility_substance.presence_status,
            "observed_at": payload.confirmed_facility_substance.observed_at.isoformat(),
        }

    state_value = _state(str(pipeline_result.get("status") or "UNCLASSIFIED"))
    input_fingerprint = hashlib.sha256(payload.input.text.encode("utf-8")).hexdigest()
    public_evidence = deepcopy(pipeline_result.get("evidence", []))
    for item in public_evidence:
        item.pop("query", None)
        retrieval = item.get("retrieval") or {}
        retrieval.pop("query", None)
        retrieval.pop("ranking_query", None)
    reported_policy = pipeline_result.get("conflict_policy_mode")
    if reported_policy is not None and reported_policy != rule_policy:
        raise APIBoundaryError(
            "CONFLICT_POLICY_OUTPUT_MISMATCH",
            "파이프라인 출력의 충돌 검토 정책이 API 설정과 다릅니다.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            retryable=False,
        )
    policy_mode = rule_policy
    rule_wrapper = pipeline_result.get("rule_review") or {}
    rule_result = rule_wrapper.get("result") or {}
    grounded_rag = rag_service.answer(public_evidence, rule_wrapper)
    processed_at = datetime.now(timezone.utc)
    agent = build_operations_agent_snapshot(
        analysis_state=state_value,
        location=(
            payload.location.model_dump(mode="python") if payload.location else None
        ),
        operations=payload.operations_context,
        parser_output=parsed,
        substance_candidates=pipeline_result.get("substance_candidates", []),
        facility_history=facility_history,
        evidence=public_evidence,
        incident_confirmed=confirmation_gate["incident_confirmed"],
        facility_confirmed=confirmation_gate["facility_confirmed"],
        grounded_rag=grounded_rag,
        processed_at=processed_at,
    )
    elapsed_ms = (time.perf_counter() - started_at) * 1_000
    expert_reviewed = bool(
        rule_wrapper.get("executed") is True
        and _result_expert_reviewed(policy_mode, rule_result)
    )
    capability = runtime.conflict_review_capability(policy_mode)
    return AnalysisResponse(
        analysis_id=analysis_id,
        request_id=request_id,
        incident_id=payload.incident_id,
        state=state_value,
        input_fingerprint=input_fingerprint,
        model_outputs=model_outputs,
        evidence=public_evidence,
        grounded_rag=grounded_rag,
        agent=agent,
        conflict_review=pipeline_result.get("rule_review", {}),
        confirmation_gate=confirmation_gate,
        required_next_steps=_required_next_steps(state_value),
        provenance={
            "chemiguard119_version": __version__,
            "api_schema_version": API_SCHEMA_VERSION,
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "resolver_schema_version": runtime.resolver_artifact.get("schema_version"),
            "resolver_training_metadata": runtime.resolver_artifact.get(
                "training_metadata", {}
            ),
            "retriever_schema_version": runtime.retriever_artifact.get(
                "schema_version"
            ),
            "grounded_rag_schema_version": RAG_SCHEMA_VERSION,
            "grounded_rag_mode": grounded_rag["mode"],
            "runtime_loaded_at_utc": runtime.loaded_at_utc,
            "processed_at_utc": processed_at.isoformat(),
            "latency_ms": round(elapsed_ms, 3),
            "input_type": payload.input.type.value,
            "confirmations": confirmation_trace,
            "confirmation_gate_policy": CONFIRMATION_GATE_POLICY,
            "rule_policy": policy_mode,
            "expert_reviewed": expert_reviewed,
            "decision_support_only": True,
            "responder_confirmation_required": True,
            "conflict_review_capability": capability,
        },
        safety_notice=SAFETY_NOTICE,
    )


def _execute_incident_analysis(
    payload: IncidentAnalyzeRequest,
    request: Request,
    *,
    request_id: str,
    analysis_id: str,
) -> AnalysisResponse:
    """기존 사고분석 계약을 단일 도구로 재사용한다."""

    active_runtime = _runtime_or_error(request)
    started = time.perf_counter()
    result = analyze_incident(
        payload.input.text,
        db_path=active_runtime.db_path,
        resolver_artifact=active_runtime.resolver_artifact,
        retriever_artifact=active_runtime.retriever_artifact,
        confirmed_incident_cas=(
            payload.confirmed_incident_substance.cas_number
            if payload.confirmed_incident_substance
            else None
        ),
        confirmed_facility_cas=(
            payload.confirmed_facility_substance.cas_number
            if payload.confirmed_facility_substance
            else None
        ),
        planned_actions=[item.raw_text for item in payload.planned_actions],
        allow_demo_rules=False,
        policy_mode=request.app.state.rule_policy,
        config_dir=active_runtime.config_dir,
        evidence_top_k=payload.evidence_top_k,
    )
    facility_history = None
    if payload.location:
        facility_query = payload.location.facility_name or payload.location.address
        if facility_query:
            facility_history = search_facility_history(
                facility_query,
                active_runtime.db_path,
                province=payload.location.province,
                top_k=10,
            )
    return _public_analysis_response(
        payload,
        result,
        active_runtime,
        request_id=request_id,
        analysis_id=analysis_id,
        started_at=started,
        rule_policy=request.app.state.rule_policy,
        rag_service=request.app.state.rag_service,
        facility_history=facility_history,
    )


def _agent_runtime_state_fingerprint(request: Request) -> str:
    """새 artifact·정책·RAG 설정이면 대기 memory도 다시 분석하게 한다."""

    runtime = _runtime_or_error(request)
    payload = {
        "service_version": __version__,
        "runtime_loaded_at_utc": runtime.loaded_at_utc,
        "resolver_schema_version": runtime.resolver_artifact.get("schema_version"),
        "retriever_schema_version": runtime.retriever_artifact.get("schema_version"),
        "runtime_integrity": runtime.integrity,
        "rule_policy": request.app.state.rule_policy,
        "rag": request.app.state.rag_service.metadata(),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def create_app(
    *,
    runtime: ModelRuntime | None = None,
    api_key: str | None = None,
    allow_anonymous: bool | None = None,
    deployment_environment: str | None = None,
    rule_policy: str | None = None,
    rag_service: GroundedRagService | None = None,
) -> FastAPI:
    """테스트 주입과 실제 artifact 로딩을 모두 지원하는 app factory."""

    configure_json_logging()
    resolved_deployment_environment = (
        (
            deployment_environment
            or os.getenv(DEPLOYMENT_ENVIRONMENT_ENV_VAR)
            or "development"
        )
        .strip()
        .lower()
    )
    deployment_environment_error = _deployment_environment_error(
        resolved_deployment_environment
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.startup_error = None
        if runtime is not None:
            app.state.runtime = runtime
        elif deployment_environment_error is not None:
            app.state.runtime = None
            app.state.startup_error = "DEPLOYMENT_ENVIRONMENT_INVALID"
        else:
            try:
                app.state.runtime = ModelRuntime.load(
                    environment=resolved_deployment_environment
                )
            except (
                Exception
            ) as error:  # health/readiness에서 복구 가능한 상태로 노출한다.
                app.state.runtime = None
                app.state.startup_error = "ARTIFACT_LOAD_FAILED"
                emit_json_event(
                    "model_runtime_startup_failed",
                    level=logging.ERROR,
                    service_name=SERVICE_ID,
                    error_type=type(error).__name__,
                )
        yield

    application = FastAPI(
        title=f"{PUBLIC_SERVICE_NAME} 모델 API",
        version=__version__,
        description=(
            "화학사고 신고 구조화, 물질 후보 검색, 공식 근거 검색, 근거 제한형 RAG와 "
            "공개근거 CAMEO 스크리닝 또는 승인 Rule 조회를 "
            "제공하는 의사결정 지원 API입니다. 모델 출력은 현장 명령이 아닙니다."
        ),
        lifespan=lifespan,
    )
    application.state.runtime = runtime
    application.state.rag_service = rag_service or GroundedRagService()
    application.state.startup_error = None
    application.state.deployment_environment = resolved_deployment_environment
    application.state.rule_policy = (
        rule_policy or os.getenv(RULE_POLICY_ENV_VAR) or PUBLIC_SOURCE_PILOT_POLICY
    ).strip()
    application.state.rule_policy_error = (
        None
        if application.state.rule_policy in SUPPORTED_POLICY_MODES
        else f"지원하지 않는 충돌 검토 정책: {application.state.rule_policy}"
    )
    configured_key = (
        api_key if api_key is not None else os.getenv("CHEMIGUARD119_API_KEY")
    )
    application.state.api_key = (
        configured_key.strip() if configured_key and configured_key.strip() else None
    )
    application.state.allow_anonymous = (
        allow_anonymous
        if allow_anonymous is not None
        else _env_flag("CHEMIGUARD119_ALLOW_ANONYMOUS", False)
    )
    application.state.auth_config_error = (
        deployment_environment_error
        or _production_api_key_error(
            application.state.api_key,
            application.state.deployment_environment,
        )
    )
    if (
        application.state.deployment_environment.lower()
        in TRUSTED_DEPLOYMENT_ENVIRONMENTS
        and application.state.allow_anonymous
    ):
        application.state.auth_config_error = (
            "staging·production 환경에서는 익명 API 접근을 허용할 수 없습니다."
        )

    @application.middleware("http")
    async def contract_headers(request: Request, call_next: Any) -> Any:
        started = time.perf_counter()
        response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        request.state.request_id = _valid_request_id(
            request.headers.get(REQUEST_ID_HEADER_NAME)
        ) or _new_id("REQ")
        try:
            response = await call_next(request)
            response_status = response.status_code
            response.headers[REQUEST_ID_HEADER_NAME] = _request_id(request)
            response.headers["X-API-Schema-Version"] = API_SCHEMA_VERSION
            response.headers["X-Service-Id"] = SERVICE_ID
            response.headers["X-Content-Type-Options"] = "nosniff"
            if request.url.path.startswith("/api/"):
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            emit_json_event(
                "http_request_completed",
                level=_request_log_level(response_status),
                request_id=_request_id(request),
                service_name=SERVICE_ID,
                service_version=__version__,
                deployment_environment=request.app.state.deployment_environment,
                authentication_mode=_auth_mode(request),
                http_request_method=request.method,
                http_route=_request_route(request),
                http_response_status_code=response_status,
                duration_ms=round((time.perf_counter() - started) * 1_000, 3),
                outcome=_request_outcome(response_status),
            )

    @application.exception_handler(APIBoundaryError)
    async def boundary_error_handler(
        request: Request, error: APIBoundaryError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            headers=error.headers,
            content=_error_payload(
                code=error.code,
                message=error.message,
                retryable=error.retryable,
                request_id=_request_id(request),
                fields=error.fields,
            ),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        fields = [
            ".".join(str(item) for item in detail.get("loc", ()))
            for detail in error.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_error_payload(
                code="INVALID_SCHEMA",
                message="요청 JSON이 API 스키마를 만족하지 않습니다.",
                retryable=False,
                request_id=_request_id(request),
                fields=fields,
            ),
        )

    @application.exception_handler(HTTPException)
    async def http_error_handler(
        request: Request, error: HTTPException
    ) -> JSONResponse:
        detail = error.detail if isinstance(error.detail, dict) else {}
        return JSONResponse(
            status_code=error.status_code,
            content=_error_payload(
                code=str(detail.get("code") or "HTTP_ERROR"),
                message=str(detail.get("message") or "요청을 처리할 수 없습니다."),
                retryable=bool(detail.get("retryable", False)),
                request_id=_request_id(request),
                fields=list(detail.get("fields") or []),
            ),
        )

    @application.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request, error: Exception
    ) -> JSONResponse:
        emit_json_event(
            "model_api_unhandled_error",
            level=logging.ERROR,
            request_id=_request_id(request),
            service_name=SERVICE_ID,
            deployment_environment=request.app.state.deployment_environment,
            error_type=type(error).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_payload(
                code="INTERNAL_ERROR",
                message="모델 API 내부 오류가 발생했습니다.",
                retryable=True,
                request_id=_request_id(request),
            ),
        )

    @application.get("/health/live", tags=["health"])
    def live() -> dict[str, Any]:
        return {
            "status": "UP",
            "service": SERVICE_ID,
            "service_name": PUBLIC_SERVICE_NAME,
            "version": __version__,
        }

    @application.get("/health/ready", tags=["health"])
    def ready(request: Request) -> JSONResponse:
        policy_mode = request.app.state.rule_policy
        rule_policy_supported = request.app.state.rule_policy_error is None
        active_runtime = getattr(request.app.state, "runtime", None)
        if active_runtime is None:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "NOT_READY",
                    "service": SERVICE_ID,
                    "service_name": PUBLIC_SERVICE_NAME,
                    "reason": getattr(request.app.state, "startup_error", None),
                    "material_discovery_capability": {
                        "ready": False,
                        "profile_count": 0,
                        "minimum_profile_count": MINIMUM_ULSAN_PROFILE_COUNT,
                        "reason": "runtime artifact를 불러오지 못했습니다.",
                    },
                    "rule_policy": policy_mode,
                    "rule_policy_ready": False,
                    "rule_policy_error": request.app.state.rule_policy_error,
                    "expert_reviewed": False,
                    "decision_support_only": True,
                    "responder_confirmation_required": True,
                },
            )
        readiness = active_runtime.readiness(policy_mode)
        rule_policy_ready = bool(
            rule_policy_supported
            and readiness["conflict_review_capability"]["configuration_valid"]
            and readiness["conflict_review_capability"]["conflict_review_ready"]
        )
        auth_mode = _auth_mode(request)
        authentication_ready = auth_mode in {"API_KEY", "EXPLICIT_ANONYMOUS"}
        production_integrity_ready = _production_integrity_ready(
            active_runtime,
            request.app.state.deployment_environment,
        )
        overall_ready = bool(
            readiness["ready"]
            and authentication_ready
            and rule_policy_ready
            and production_integrity_ready
        )
        conflict_review_ready = bool(
            readiness["conflict_review_capability"]["conflict_review_ready"]
        )
        return JSONResponse(
            status_code=(
                status.HTTP_200_OK
                if overall_ready
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            content={
                "status": "READY" if overall_ready else "NOT_READY",
                "service": SERVICE_ID,
                "service_name": PUBLIC_SERVICE_NAME,
                **readiness,
                "ready": overall_ready,
                "rule_policy": policy_mode,
                "rule_policy_ready": rule_policy_ready,
                "rule_policy_error": request.app.state.rule_policy_error,
                "expert_reviewed": bool(
                    readiness["conflict_review_capability"]["expert_reviewed"]
                ),
                "decision_support_only": True,
                "responder_confirmation_required": True,
                "operational_checks": {
                    "runtime_ready": bool(readiness["ready"]),
                    "authentication_ready": authentication_ready,
                    "authentication_mode": auth_mode,
                    "rule_policy_ready": rule_policy_ready,
                    "rule_policy": policy_mode,
                    "conflict_review_ready": conflict_review_ready,
                    "production_integrity_ready": production_integrity_ready,
                },
            },
        )

    @application.get("/api/v1/meta", tags=["metadata"])
    def metadata(request: Request) -> dict[str, Any]:
        policy_mode = request.app.state.rule_policy
        active_runtime = getattr(request.app.state, "runtime", None)
        runtime_metadata = (
            active_runtime.readiness(policy_mode)
            if active_runtime is not None
            else {
                "ready": False,
                "startup_error": getattr(request.app.state, "startup_error", None),
                "conflict_review_capability": _empty_conflict_review_capability(
                    policy_mode
                ),
            }
        )
        auth_mode = _auth_mode(request)
        capability = runtime_metadata["conflict_review_capability"]
        return {
            "service": SERVICE_ID,
            "service_name": PUBLIC_SERVICE_NAME,
            "internal_package_name": INTERNAL_PACKAGE_NAME,
            "version": __version__,
            "deployment_environment": request.app.state.deployment_environment,
            "api_schema_version": API_SCHEMA_VERSION,
            "pipeline_schema_version": PIPELINE_SCHEMA_VERSION,
            "authentication": {
                "mode": auth_mode,
                "required": auth_mode != "EXPLICIT_ANONYMOUS",
                "configured": auth_mode == "API_KEY",
                "header": AUTH_HEADER_NAME,
                "anonymous_access_is_explicit": auth_mode == "EXPLICIT_ANONYMOUS",
            },
            "auth_enabled": auth_mode == "API_KEY",
            "runtime": runtime_metadata,
            "rule_policy": policy_mode,
            "rule_policy_ready": bool(
                request.app.state.rule_policy_error is None
                and capability["configuration_valid"]
                and capability["conflict_review_ready"]
            ),
            "rule_policy_error": request.app.state.rule_policy_error,
            "expert_reviewed": bool(capability["expert_reviewed"]),
            "decision_support_only": True,
            "responder_confirmation_required": True,
            "conflict_review_capability": capability,
            "grounded_rag_capability": request.app.state.rag_service.metadata(),
            "material_ranking_capability": ranking_model_metadata(),
            "incident_agent_capability": {
                "schema_version": AGENT_SCHEMA_VERSION,
                "memory_schema_version": AGENT_MEMORY_SCHEMA_VERSION,
                "endpoint": "/api/v1/agents/incidents/step",
                "planning_mode": "DETERMINISTIC_POLICY_PLANNER",
                "memory_mode": "BE_PERSISTED_EXTERNAL_MEMORY",
                "server_side_session_storage": False,
                "memory_can_trigger_rule": False,
                "autonomous_risk_decision_allowed": False,
                "tool_count": len(TOOL_REGISTRY),
            },
            "confirmation_gate_policy": CONFIRMATION_GATE_POLICY,
            "docs": "/docs",
            "openapi": "/openapi.json",
            "safety_notice": SAFETY_NOTICE,
        }

    @application.post(
        "/api/v1/incidents/analyze",
        response_model=AnalysisResponse,
        responses=STANDARD_ERROR_RESPONSES,
        tags=["analysis"],
        summary="사고 입력을 구조화하고 근거·Rule 검토 상태를 반환",
    )
    def analyze(
        payload: IncidentAnalyzeRequest,
        request: Request,
        _: Annotated[None, Depends(_authorize)],
    ) -> AnalysisResponse:
        return _execute_incident_analysis(
            payload,
            request_id=_request_id(request, payload.request_id),
            analysis_id=_new_id("ANL"),
            request=request,
        )

    @application.post(
        "/api/v1/agents/incidents/step",
        response_model=IncidentAgentStepResponse,
        responses=STANDARD_ERROR_RESPONSES,
        tags=["agent"],
        summary="사고 상태를 관찰해 필요한 도구를 선택하고 재계획",
    )
    def run_incident_agent_step(
        payload: IncidentAgentStepRequest,
        request: Request,
        _: Annotated[None, Depends(_authorize)],
    ) -> IncidentAgentStepResponse:
        request_id = _request_id(request, payload.analysis.request_id)
        return IncidentAgentRunner().run(
            payload,
            request_id=request_id,
            analysis_tool=lambda: _execute_incident_analysis(
                payload.analysis,
                request,
                request_id=request_id,
                analysis_id=_new_id("ANL"),
            ),
            runtime_state_fingerprint=_agent_runtime_state_fingerprint(request),
        )

    @application.post(
        "/api/v1/substances/discover",
        response_model=SubstanceDiscoveryResponse,
        tags=["models"],
        responses=STANDARD_ERROR_RESPONSES,
        summary="물질명·CAS·관찰 정보에서 확인 전 물질 후보와 공식 근거 검색",
    )
    def discover(
        payload: SubstanceDiscoveryRequest,
        request: Request,
        _: Annotated[None, Depends(_authorize)],
    ) -> SubstanceDiscoveryResponse:
        active_runtime = _runtime_or_error(request)
        result = discover_substances(
            payload.query,
            db_path=active_runtime.db_path,
            resolver_artifact=active_runtime.resolver_artifact,
            retriever_artifact=active_runtime.retriever_artifact,
            top_k=payload.top_k,
            evidence_top_k=payload.evidence_top_k,
        )
        result["schema_version"] = API_SCHEMA_VERSION
        result["safety_notice"] = SAFETY_NOTICE
        return SubstanceDiscoveryResponse.model_validate(result)

    @application.post(
        "/api/v1/substances/resolve",
        tags=["models"],
        responses=STANDARD_ERROR_RESPONSES,
    )
    def resolve(
        payload: ResolveRequest,
        request: Request,
        _: Annotated[None, Depends(_authorize)],
    ) -> dict[str, Any]:
        active_runtime = _runtime_or_error(request)
        result = resolve_substance(
            payload.query, active_runtime.resolver_artifact, top_k=payload.top_k
        )
        result["rule_eligible"] = False
        result["decision_scope"] = "IDENTIFICATION_CANDIDATE_ONLY"
        result["on_site_presence_confirmed"] = False
        result["risk_determination_allowed"] = False
        result["candidate_score_notice"] = (
            "점수는 후보 정렬값이며 사고·위험 확률이 아닙니다."
        )
        result["safety_notice"] = SAFETY_NOTICE
        return result

    @application.post(
        "/api/v1/evidence/search",
        tags=["models"],
        responses=STANDARD_ERROR_RESPONSES,
    )
    def evidence_search(
        payload: EvidenceSearchRequest,
        request: Request,
        _: Annotated[None, Depends(_authorize)],
    ) -> dict[str, Any]:
        active_runtime = _runtime_or_error(request)
        result = search_evidence(
            payload.query,
            active_runtime.db_path,
            active_runtime.retriever_artifact,
            cas_hint=payload.cas_hint,
            top_k=payload.top_k,
        )
        result["cas_hint_status"] = payload.cas_hint_status
        result["rule_eligible"] = False
        result["decision_scope"] = "EVIDENCE_ONLY"
        result["risk_determination_allowed"] = False
        result["safety_notice"] = SAFETY_NOTICE
        return result

    @application.post(
        "/api/v1/facilities/candidates",
        tags=["models"],
        responses=STANDARD_ERROR_RESPONSES,
    )
    def facility_candidates(
        payload: FacilityHistorySearchRequest,
        request: Request,
        _: Annotated[None, Depends(_authorize)],
    ) -> dict[str, Any]:
        active_runtime = _runtime_or_error(request)
        result = search_facility_history(
            payload.query,
            active_runtime.db_path,
            province=payload.province,
            top_k=payload.top_k,
        )
        result["decision_scope"] = "REPORTED_HANDLING_HISTORY_ONLY"
        result["risk_determination_allowed"] = False
        result["safety_notice"] = SAFETY_NOTICE
        return result

    @application.post(
        "/api/v1/conflicts/review",
        tags=["rules"],
        responses=STANDARD_ERROR_RESPONSES,
    )
    def conflict_review(
        payload: ConflictReviewRequest,
        request: Request,
        _: Annotated[None, Depends(_authorize)],
    ) -> dict[str, Any]:
        active_runtime = _runtime_or_error(request)
        result = review_pair(
            payload.incident.cas_number,
            payload.facility.cas_number,
            active_runtime.db_path,
            planned_actions=[item.raw_text for item in payload.planned_actions],
            allow_demo_rules=False,
            policy_mode=request.app.state.rule_policy,
            config_dir=active_runtime.config_dir,
        )
        try:
            validated_review = ExecutedConflictReview.model_validate(
                {
                    "executed": True,
                    "status": result.get("status"),
                    "gate": "BOTH_CAS_RESPONDER_CONFIRMED",
                    "policy_mode": request.app.state.rule_policy,
                    "result": result,
                }
            )
        except ValidationError as error:
            raise APIBoundaryError(
                "OUTPUT_VALIDATION_FAILED",
                "Rule 출력 안전 검증에 실패했습니다.",
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                retryable=False,
            ) from error
        completed = validated_review.status in {
            "COMPLETED",
            "SCREENING_COMPLETED",
        }
        for field, confirmed_cas in (
            ("incident_cas", payload.incident.cas_number),
            ("facility_cas", payload.facility.cas_number),
        ):
            result_cas = validated_review.result.get(field)
            if (completed and result_cas != confirmed_cas) or (
                result_cas is not None and result_cas != confirmed_cas
            ):
                raise APIBoundaryError(
                    "OUTPUT_VALIDATION_FAILED",
                    "Rule 결과 CAS가 현장 확인 CAS와 일치하지 않습니다.",
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    retryable=False,
                )
        capability = active_runtime.conflict_review_capability(
            request.app.state.rule_policy
        )
        return {
            "status": result.get("status"),
            "confirmation_ids": {
                "incident": payload.incident.confirmation_id,
                "facility": payload.facility.confirmation_id,
            },
            "confirmation_gate": {
                "policy": CONFIRMATION_GATE_POLICY,
                "all_required_confirmed": True,
                "rule_execution_allowed": True,
            },
            "result": result,
            "rule_policy": request.app.state.rule_policy,
            "expert_reviewed": _result_expert_reviewed(
                request.app.state.rule_policy,
                result,
            ),
            "decision_support_only": True,
            "responder_confirmation_required": True,
            "conflict_review_capability": capability,
            "safety_notice": SAFETY_NOTICE,
        }

    return application


app = create_app()


def run() -> None:
    """`chemiguard119-api` 콘솔 명령 진입점."""

    import uvicorn

    host = os.getenv("CHEMIGUARD119_API_HOST", "127.0.0.1")
    port = int(os.getenv("CHEMIGUARD119_API_PORT", "8000"))
    api_key = os.getenv("CHEMIGUARD119_API_KEY")
    allow_anonymous = _env_flag("CHEMIGUARD119_ALLOW_ANONYMOUS", False)
    if host not in {"127.0.0.1", "localhost", "::1"} and (
        not api_key or allow_anonymous
    ):
        raise RuntimeError(
            "로컬호스트 외 주소에서는 API 키가 필수이며 익명 접근을 허용할 수 없습니다."
        )
    uvicorn.run(
        "chemiguard119.api:app",
        host=host,
        port=port,
        workers=1,
        access_log=False,
    )


__all__ = ["ModelRuntime", "app", "create_app", "run"]
