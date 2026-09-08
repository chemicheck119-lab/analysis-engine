"""실제 Backend→Model API HTTP 경계의 합성 확인 상태 전이를 평가한다.

이 평가는 공개 합성 replay만 사용하며 신고 음성·현장 무전·실제 대원 확인을 다루지 않는다.
각 HTTP 요청은 고유 request ID를 사용하고 하나의 사고 흐름은 incident ID로 연결한다.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, parse, request

from chemiguard119.utils import sha256_file, write_json


REPORT_SCHEMA_VERSION = "chemicheck119-cross-service-confirmation-flow-v1"
FACT_STATUS = "부분 구현 또는 개발용 데모"
CLAIM_SCOPE = "LOCAL_CROSS_SERVICE_SYNTHETIC_REGRESSION_ONLY"
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
REQUEST_IDS = {
    "zero": "REQ-CROSS-SERVICE-ZERO-001",
    "confirm_incident": "REQ-CROSS-SERVICE-CONFIRM-INCIDENT-001",
    "one": "REQ-CROSS-SERVICE-ONE-001",
    "confirm_facility": "REQ-CROSS-SERVICE-CONFIRM-FACILITY-001",
    "two": "REQ-CROSS-SERVICE-TWO-001",
    "cancel_facility": "REQ-CROSS-SERVICE-CANCEL-FACILITY-001",
    "cancelled": "REQ-CROSS-SERVICE-CANCELLED-001",
}


class CrossServiceFlowClient(Protocol):
    def model_ready(self) -> Mapping[str, Any]: ...

    def model_metadata(self) -> Mapping[str, Any]: ...

    def backend_info(self) -> Mapping[str, Any]: ...

    def open_pilot_session(self, station_id: str) -> None: ...

    def replay(self, scenario_id: str) -> Mapping[str, Any]: ...

    def analyze(
        self, payload: Mapping[str, Any], request_id: str
    ) -> Mapping[str, Any]: ...

    def confirm(
        self, incident_id: str, role: str, request_id: str
    ) -> Mapping[str, Any]: ...

    def cancel(
        self,
        incident_id: str,
        role: str,
        confirmation_id: str,
        request_id: str,
    ) -> Mapping[str, Any]: ...


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class HttpCrossServiceFlowClient:
    """로컬 또는 명시 승인된 환경의 합성 E2E HTTP client."""

    def __init__(
        self,
        *,
        bff_base_url: str,
        model_base_url: str,
        origin: str,
        timeout_seconds: float = 20.0,
        allow_non_loopback: bool = False,
    ) -> None:
        self.bff_base_url = _validate_base_url(
            bff_base_url, "Backend", allow_non_loopback
        )
        self.model_base_url = _validate_base_url(
            model_base_url, "Model API", allow_non_loopback
        )
        self.origin = _validate_origin(origin)
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("HTTP timeout은 0초 초과 120초 이하여야 합니다.")
        self.timeout_seconds = timeout_seconds
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = request.build_opener(
            request.HTTPCookieProcessor(self.cookie_jar), _NoRedirect()
        )

    def model_ready(self) -> Mapping[str, Any]:
        return self._json("GET", self.model_base_url + "/health/ready")

    def model_metadata(self) -> Mapping[str, Any]:
        return self._json("GET", self.model_base_url + "/api/v1/meta")

    def backend_info(self) -> Mapping[str, Any]:
        return self._json("GET", self.bff_base_url + "/actuator/info")

    def open_pilot_session(self, station_id: str) -> None:
        body = parse.urlencode({"stationId": station_id}).encode("utf-8")
        response = self._exchange(
            "POST",
            self.bff_base_url + "/auth/staging/pilot",
            body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": self.origin,
            },
            allowed_statuses={303},
        )
        if response[0] != 303 or not any(
            cookie.name in {"CHEMICHECK119_SESSION", "__session"}
            for cookie in self.cookie_jar
        ):
            raise RuntimeError("합성 파일럿 session을 발급받지 못했습니다.")

    def replay(self, scenario_id: str) -> Mapping[str, Any]:
        _, body = self._exchange(
            "GET",
            self.bff_base_url
            + "/api/c2guard/v1/intake/replay-stream/"
            + parse.quote(scenario_id, safe=""),
            headers={"Accept": "text/event-stream"},
            allowed_statuses={200},
        )
        for line in body.decode("utf-8").splitlines():
            if line.startswith("data:"):
                payload = json.loads(line.removeprefix("data:"))
                if isinstance(payload, dict):
                    return payload
        raise RuntimeError("합성 replay 응답에 JSON data event가 없습니다.")

    def analyze(self, payload: Mapping[str, Any], request_id: str) -> Mapping[str, Any]:
        return self._json(
            "POST",
            self.bff_base_url + "/api/c2guard/v1/incidents/analyze",
            payload=payload,
            headers={"X-Request-Id": request_id},
        )

    def confirm(
        self, incident_id: str, role: str, request_id: str
    ) -> Mapping[str, Any]:
        return self._json(
            "POST",
            self.bff_base_url
            + "/api/c2guard/v1/intake/replays/"
            + parse.quote(incident_id, safe="")
            + "/confirmations/"
            + parse.quote(role, safe=""),
            headers={"X-Request-Id": request_id},
        )

    def cancel(
        self,
        incident_id: str,
        role: str,
        confirmation_id: str,
        request_id: str,
    ) -> Mapping[str, Any]:
        return self._json(
            "DELETE",
            self.bff_base_url
            + "/api/c2guard/v1/incidents/"
            + parse.quote(incident_id, safe="")
            + "/confirmations/"
            + parse.quote(role, safe="")
            + "/"
            + parse.quote(confirmation_id, safe=""),
            headers={"X-Request-Id": request_id},
        )

    def _json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        body = None
        merged_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            merged_headers["Content-Type"] = "application/json"
        _, response_body = self._exchange(
            method,
            url,
            body=body,
            headers=merged_headers,
            allowed_statuses={200, 201},
        )
        decoded = json.loads(response_body.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise RuntimeError("HTTP JSON 응답의 최상위 값이 객체가 아닙니다.")
        return decoded

    def _exchange(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        allowed_statuses: set[int],
    ) -> tuple[int, bytes]:
        http_request = request.Request(
            url, data=body, headers=dict(headers or {}), method=method
        )
        try:
            with self.opener.open(
                http_request, timeout=self.timeout_seconds
            ) as response:
                status = response.status
                response_body = response.read()
        except error.HTTPError as http_error:
            status = http_error.code
            response_body = http_error.read()
        except (error.URLError, TimeoutError) as network_error:
            raise RuntimeError(
                "서비스 HTTP 경계에 연결할 수 없습니다."
            ) from network_error
        if status not in allowed_statuses:
            raise RuntimeError(
                f"서비스 HTTP 경계가 허용되지 않은 상태 {status}를 반환했습니다."
            )
        return status, response_body


def _validate_base_url(value: str, label: str, allow_non_loopback: bool) -> str:
    parsed = parse.urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{label} base URL 형식이 올바르지 않습니다.")
    if not allow_non_loopback and parsed.hostname not in LOCAL_HOSTS:
        raise ValueError(f"{label}는 기본적으로 loopback 주소만 허용합니다.")
    return value.strip().rstrip("/")


def _validate_origin(value: str) -> str:
    parsed = parse.urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("파일럿 Origin 형식이 올바르지 않습니다.")
    return value.strip().rstrip("/")


def _analysis_request(envelope: Mapping[str, Any]) -> dict[str, Any]:
    location = envelope.get("location")
    location_payload = location if isinstance(location, Mapping) else {}
    return {
        "incidentId": envelope.get("incidentId"),
        "text": envelope.get("reportText"),
        "inputType": "DISPATCH_TEXT",
        "occurredAt": envelope.get("occurredAt"),
        "location": {
            "facilityName": envelope.get("facilityName"),
            "address": envelope.get("addressText"),
            "latitude": location_payload.get("latitude"),
            "longitude": location_payload.get("longitude"),
            "coordinateSource": "DISPATCH_SYSTEM",
            "resolvedAt": envelope.get("receivedAt"),
        },
        "operationsContext": {
            "dispatchStationName": envelope.get("stationDisplayName"),
            "journeyState": "EN_ROUTE",
        },
        "evidenceTopK": 5,
    }


def _gate(response: Mapping[str, Any]) -> Mapping[str, Any]:
    value = response.get("confirmationGate")
    return value if isinstance(value, Mapping) else {}


def _conflict(response: Mapping[str, Any]) -> Mapping[str, Any]:
    value = response.get("conflictReview")
    return value if isinstance(value, Mapping) else {}


def _add_check(
    checks: list[dict[str, Any]], name: str, expected: Any, actual: Any
) -> None:
    checks.append(
        {
            "name": name,
            "expected": expected,
            "actual": actual,
            "passed": actual == expected,
        }
    )


def evaluate_cross_service_confirmation_flow(
    client: CrossServiceFlowClient,
    *,
    station_id: str,
    scenario_id: str,
    backend_git_commit: str,
    model_git_commit: str,
    runtime_manifest_sha256: str,
    runtime_manifest_actual_sha256: str,
    database_runtime: str,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """공개 합성 사고의 0→1→2→취소 확인 상태를 실제 HTTP 경계로 평가한다."""

    if GIT_COMMIT_PATTERN.fullmatch(backend_git_commit) is None:
        raise ValueError("Backend Git commit은 40자리 소문자 SHA여야 합니다.")
    if GIT_COMMIT_PATTERN.fullmatch(model_git_commit) is None:
        raise ValueError("Model API Git commit은 40자리 소문자 SHA여야 합니다.")
    if SHA256_PATTERN.fullmatch(runtime_manifest_sha256) is None:
        raise ValueError("Runtime manifest SHA-256은 64자리 소문자 hex여야 합니다.")
    if SHA256_PATTERN.fullmatch(runtime_manifest_actual_sha256) is None:
        raise ValueError("계산된 Runtime manifest SHA-256 형식이 올바르지 않습니다.")
    if database_runtime not in {
        "H2_POSTGRESQL_COMPATIBILITY_MODE",
        "POSTGRESQL_TESTCONTAINER",
        "CLOUD_SQL_POSTGRESQL",
    }:
        raise ValueError("지원하지 않는 database runtime입니다.")

    model_ready = client.model_ready()
    model_metadata = client.model_metadata()
    backend_info = client.backend_info()
    client.open_pilot_session(station_id)
    envelope = client.replay(scenario_id)
    if (
        envelope.get("sourceType") != "SYNTHETIC_DISPATCH_REPLAY"
        or envelope.get("dataClassification") != "PUBLIC_SYNTHETIC"
        or envelope.get("containsPersonalInformation") is not False
    ):
        raise RuntimeError(
            "PUBLIC_SYNTHETIC·비개인정보 replay가 아니므로 후단 전송을 중단합니다."
        )
    incident_id = str(envelope.get("incidentId") or "")
    request_payload = _analysis_request(envelope)

    zero = client.analyze(request_payload, REQUEST_IDS["zero"])
    incident_confirmation = client.confirm(
        incident_id, "INCIDENT", REQUEST_IDS["confirm_incident"]
    )
    one = client.analyze(request_payload, REQUEST_IDS["one"])
    facility_confirmation = client.confirm(
        incident_id, "FACILITY", REQUEST_IDS["confirm_facility"]
    )
    two = client.analyze(request_payload, REQUEST_IDS["two"])
    facility_confirmation_id = str(facility_confirmation.get("confirmationId") or "")
    cancelled_confirmation = client.cancel(
        incident_id,
        "FACILITY",
        facility_confirmation_id,
        REQUEST_IDS["cancel_facility"],
    )
    cancelled = client.analyze(request_payload, REQUEST_IDS["cancelled"])

    checks: list[dict[str, Any]] = []
    model_runtime = model_metadata.get("runtime")
    model_runtime_payload = model_runtime if isinstance(model_runtime, Mapping) else {}
    integrity = model_runtime_payload.get("integrity")
    integrity_payload = integrity if isinstance(integrity, Mapping) else {}
    backend_release = backend_info.get("release")
    backend_release_payload = (
        backend_release if isinstance(backend_release, Mapping) else {}
    )
    _add_check(checks, "model_ready", "READY", model_ready.get("status"))
    _add_check(
        checks,
        "model_runtime_integrity",
        "VERIFIED",
        integrity_payload.get("status"),
    )
    _add_check(
        checks,
        "model_git_commit",
        model_git_commit,
        integrity_payload.get("git_commit"),
    )
    _add_check(
        checks,
        "model_runtime_manifest_verified",
        True,
        integrity_payload.get("manifest_sha256_verified"),
    )
    _add_check(
        checks,
        "runtime_manifest_sha256",
        runtime_manifest_sha256,
        runtime_manifest_actual_sha256,
    )
    _add_check(
        checks,
        "backend_git_commit",
        backend_git_commit,
        backend_release_payload.get("gitCommit"),
    )
    _add_check(
        checks,
        "synthetic_source_type",
        "SYNTHETIC_DISPATCH_REPLAY",
        envelope.get("sourceType"),
    )
    _add_check(
        checks,
        "synthetic_data_classification",
        "PUBLIC_SYNTHETIC",
        envelope.get("dataClassification"),
    )
    _add_check(
        checks,
        "synthetic_contains_personal_information",
        False,
        envelope.get("containsPersonalInformation"),
    )
    _add_check(
        checks,
        "incident_id_present",
        True,
        bool(incident_id),
    )

    stages = {
        "zero": (zero, False, False, "AWAITING_SUBSTANCE_CONFIRMATION", False),
        "one": (one, True, False, "AWAITING_FACILITY_CONFIRMATION", False),
        "two": (two, True, True, "SCREENING_COMPLETED", True),
        "cancelled": (
            cancelled,
            True,
            False,
            "AWAITING_FACILITY_CONFIRMATION",
            False,
        ),
    }
    for stage_name, (
        response,
        incident_confirmed,
        facility_confirmed,
        state,
        rule_executed,
    ) in stages.items():
        gate = _gate(response)
        conflict = _conflict(response)
        both = incident_confirmed and facility_confirmed
        _add_check(
            checks,
            f"{stage_name}_request_id",
            REQUEST_IDS[stage_name],
            response.get("requestId"),
        )
        _add_check(
            checks,
            f"{stage_name}_incident_correlated",
            True,
            response.get("incidentId") == incident_id,
        )
        _add_check(checks, f"{stage_name}_state", state, response.get("state"))
        _add_check(
            checks,
            f"{stage_name}_incident_confirmed",
            incident_confirmed,
            gate.get("incidentConfirmed"),
        )
        _add_check(
            checks,
            f"{stage_name}_facility_confirmed",
            facility_confirmed,
            gate.get("facilityConfirmed"),
        )
        _add_check(
            checks,
            f"{stage_name}_all_required_confirmed",
            both,
            gate.get("allRequiredConfirmed"),
        )
        _add_check(
            checks,
            f"{stage_name}_rule_execution_allowed",
            both,
            gate.get("ruleExecutionAllowed"),
        )
        _add_check(
            checks,
            f"{stage_name}_conflict_executed",
            rule_executed,
            conflict.get("executed"),
        )
        _add_check(
            checks,
            f"{stage_name}_risk_display_allowed",
            rule_executed,
            response.get("riskDisplayAllowed"),
        )

    two_result = _conflict(two).get("result")
    two_result_payload = two_result if isinstance(two_result, Mapping) else {}
    _add_check(
        checks,
        "two_rule_id",
        "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
        two_result_payload.get("ruleId"),
    )
    _add_check(
        checks,
        "two_rule_incident_cas",
        "7681-52-9",
        two_result_payload.get("incidentCas"),
    )
    _add_check(
        checks,
        "two_rule_facility_cas",
        "7647-01-0",
        two_result_payload.get("facilityCas"),
    )
    _add_check(
        checks,
        "incident_confirmation_request_id",
        REQUEST_IDS["confirm_incident"],
        incident_confirmation.get("requestId"),
    )
    _add_check(
        checks,
        "incident_confirmation_role",
        "INCIDENT",
        incident_confirmation.get("role"),
    )
    _add_check(
        checks,
        "incident_confirmation_cas",
        "7681-52-9",
        incident_confirmation.get("casNumber"),
    )
    _add_check(
        checks,
        "incident_confirmation_count",
        1,
        incident_confirmation.get("confirmedCount"),
    )
    _add_check(
        checks,
        "incident_confirmation_pair_complete",
        False,
        incident_confirmation.get("allRequiredConfirmed"),
    )
    _add_check(
        checks,
        "incident_confirmation_classification",
        "PUBLIC_SYNTHETIC",
        incident_confirmation.get("dataClassification"),
    )
    _add_check(
        checks,
        "incident_confirmation_type",
        "SYNTHETIC_DEMO_CONFIRMATION",
        incident_confirmation.get("confirmationType"),
    )
    _add_check(
        checks,
        "facility_confirmation_request_id",
        REQUEST_IDS["confirm_facility"],
        facility_confirmation.get("requestId"),
    )
    _add_check(
        checks,
        "facility_confirmation_role",
        "FACILITY",
        facility_confirmation.get("role"),
    )
    _add_check(
        checks,
        "facility_confirmation_cas",
        "7647-01-0",
        facility_confirmation.get("casNumber"),
    )
    _add_check(
        checks,
        "facility_confirmation_count",
        2,
        facility_confirmation.get("confirmedCount"),
    )
    _add_check(
        checks,
        "facility_confirmation_pair_complete",
        True,
        facility_confirmation.get("allRequiredConfirmed"),
    )
    _add_check(
        checks,
        "facility_confirmation_classification",
        "PUBLIC_SYNTHETIC",
        facility_confirmation.get("dataClassification"),
    )
    _add_check(
        checks,
        "facility_confirmation_type",
        "SYNTHETIC_DEMO_CONFIRMATION",
        facility_confirmation.get("confirmationType"),
    )
    _add_check(
        checks,
        "facility_confirmation_reanalysis_required",
        True,
        facility_confirmation.get("reanalyzeRequired"),
    )
    _add_check(
        checks,
        "cancel_request_id",
        REQUEST_IDS["cancel_facility"],
        cancelled_confirmation.get("requestId"),
    )
    _add_check(
        checks,
        "cancel_status",
        "CANCELLED",
        cancelled_confirmation.get("status"),
    )
    _add_check(
        checks,
        "cancel_role",
        "FACILITY",
        cancelled_confirmation.get("role"),
    )
    _add_check(
        checks,
        "cancel_confirmation_id_matched",
        True,
        cancelled_confirmation.get("confirmationId") == facility_confirmation_id,
    )
    _add_check(
        checks,
        "cancel_reanalysis_required",
        True,
        cancelled_confirmation.get("reanalyzeRequired"),
    )

    passed_count = sum(check["passed"] is True for check in checks)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "COMPLETED" if passed_count == len(checks) else "FAILED",
        "fact_status": FACT_STATUS,
        "claim_scope": CLAIM_SCOPE,
        "field_validated": False,
        "cloud_run_validated": False,
        "speech_input_executed": False,
        "analysis_backend_http_chain_executed": True,
        "full_voice_to_handoff_chain_executed": False,
        "data_classification": "PUBLIC_SYNTHETIC",
        "database_runtime": database_runtime,
        "database_runtime_verified": False,
        "database_runtime_evidence": "OPERATOR_DECLARED",
        "service_boundary": "BFF_REAL_HTTP_TO_MODEL_API_REAL_HTTP",
        "request_correlation": {
            "workflow_key": "incident_id",
            "request_id_policy": "UNIQUE_PER_HTTP_REQUEST_PROPAGATED_WITHIN_CALL",
            "raw_incident_id_stored": False,
        },
        "provenance": {
            "backend_git_commit": backend_git_commit,
            "model_git_commit": model_git_commit,
            "runtime_manifest_sha256": runtime_manifest_sha256,
            "evaluator_source_sha256": sha256_file(Path(__file__)),
            "scenario_id": scenario_id,
            "station_id": station_id,
        },
        "check_count": len(checks),
        "passed_check_count": passed_count,
        "failed_check_count": len(checks) - passed_count,
        "checks": checks,
        "decision": (
            "CONDITIONALLY_ADOPT_FOR_LOCAL_CROSS_SERVICE_REGRESSION"
            if passed_count == len(checks)
            else "REJECT_CROSS_SERVICE_FLOW"
        ),
        "claims_allowed": [
            "공개 합성 지령이 실제 Backend HTTP와 실제 Model API HTTP를 거쳐 분석됨",
            "하나의 합성 incident에서 0→1→2→취소 확인 상태 전이가 관측됨",
            "각 분석 요청의 request ID가 Backend 응답까지 보존됨",
            "두 CAS 확인 전과 확인 취소 뒤 Rule·위험 표시가 차단됨",
        ],
        "claims_not_allowed": [
            "신고 음성 또는 현장 무전부터 인계까지 전체 경로를 실행했다는 주장",
            "실제 대원이 두 물질을 확인했다는 주장",
            "현장 정확도·현장 안전성·상용 운영 성능",
            "H2 결과를 Cloud SQL 고가용성 검증으로 표현하는 주장",
        ],
        "safety_notice": (
            "공개 합성 사고의 개발용 회귀 결과이며 현장 명령이나 실제 안전성 증명이 아닙니다."
        ),
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


__all__ = [
    "CLAIM_SCOPE",
    "FACT_STATUS",
    "HttpCrossServiceFlowClient",
    "REPORT_SCHEMA_VERSION",
    "evaluate_cross_service_confirmation_flow",
]
