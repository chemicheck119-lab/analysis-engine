from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from chemiguard119.cross_service_confirmation_evaluation import (
    REPORT_SCHEMA_VERSION,
    HttpCrossServiceFlowClient,
    evaluate_cross_service_confirmation_flow,
)


BACKEND_COMMIT = "1" * 40
MODEL_COMMIT = "2" * 40
MANIFEST_SHA256 = "3" * 64


class FakeFlowClient:
    def __init__(
        self,
        *,
        unsafe_before_confirmation: bool = False,
        data_classification: str = "PUBLIC_SYNTHETIC",
    ) -> None:
        self.confirmed: set[str] = set()
        self.unsafe_before_confirmation = unsafe_before_confirmation
        self.data_classification = data_classification
        self.analyze_call_count = 0

    def model_ready(self) -> Mapping[str, Any]:
        return {"status": "READY"}

    def model_metadata(self) -> Mapping[str, Any]:
        return {
            "runtime": {
                "integrity": {
                    "status": "VERIFIED",
                    "git_commit": MODEL_COMMIT,
                    "manifest_sha256_verified": True,
                }
            }
        }

    def backend_info(self) -> Mapping[str, Any]:
        return {"release": {"gitCommit": BACKEND_COMMIT}}

    def open_pilot_session(self, station_id: str) -> None:
        assert station_id == "nfa-0985"

    def replay(self, scenario_id: str) -> Mapping[str, Any]:
        assert scenario_id == "CONTEST-LIVE-CHEMICAL-001"
        return {
            "incidentId": "INC-SYNTHETIC-001",
            "reportText": "공개 합성 신고문",
            "occurredAt": "2026-09-08T00:00:00Z",
            "receivedAt": "2026-09-08T00:00:01Z",
            "facilityName": "공개 합성 시설",
            "addressText": "관할 공개 합성 사고지점",
            "stationDisplayName": "서울 강남소방서",
            "sourceType": "SYNTHETIC_DISPATCH_REPLAY",
            "dataClassification": self.data_classification,
            "containsPersonalInformation": False,
            "location": {"latitude": 37.5, "longitude": 127.0},
        }

    def analyze(self, payload: Mapping[str, Any], request_id: str) -> Mapping[str, Any]:
        self.analyze_call_count += 1
        assert payload["incidentId"] == "INC-SYNTHETIC-001"
        incident_confirmed = "INCIDENT" in self.confirmed
        facility_confirmed = "FACILITY" in self.confirmed
        both = incident_confirmed and facility_confirmed
        unsafe = self.unsafe_before_confirmation and not both
        if both:
            state = "SCREENING_COMPLETED"
        elif incident_confirmed:
            state = "AWAITING_FACILITY_CONFIRMATION"
        else:
            state = "AWAITING_SUBSTANCE_CONFIRMATION"
        conflict: dict[str, Any] = {"executed": both or unsafe}
        if both:
            conflict["result"] = {
                "ruleId": "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
                "incidentCas": "7681-52-9",
                "facilityCas": "7647-01-0",
            }
        return {
            "requestId": request_id,
            "incidentId": "INC-SYNTHETIC-001",
            "state": state,
            "confirmationGate": {
                "incidentConfirmed": incident_confirmed,
                "facilityConfirmed": facility_confirmed,
                "allRequiredConfirmed": both,
                "ruleExecutionAllowed": both,
            },
            "conflictReview": conflict,
            "riskDisplayAllowed": both or unsafe,
        }

    def confirm(
        self, incident_id: str, role: str, request_id: str
    ) -> Mapping[str, Any]:
        assert incident_id == "INC-SYNTHETIC-001"
        self.confirmed.add(role)
        return {
            "requestId": request_id,
            "confirmationId": f"CNF-{role}",
            "role": role,
            "casNumber": "7681-52-9" if role == "INCIDENT" else "7647-01-0",
            "dataClassification": "PUBLIC_SYNTHETIC",
            "confirmationType": "SYNTHETIC_DEMO_CONFIRMATION",
            "confirmedCount": len(self.confirmed),
            "allRequiredConfirmed": len(self.confirmed) == 2,
            "reanalyzeRequired": True,
        }

    def cancel(
        self,
        incident_id: str,
        role: str,
        confirmation_id: str,
        request_id: str,
    ) -> Mapping[str, Any]:
        assert incident_id == "INC-SYNTHETIC-001"
        assert confirmation_id == "CNF-FACILITY"
        self.confirmed.remove(role)
        return {
            "requestId": request_id,
            "role": role,
            "confirmationId": confirmation_id,
            "status": "CANCELLED",
            "reanalyzeRequired": True,
        }


def _evaluate(client: FakeFlowClient, report_path: Path | None = None) -> dict:
    return evaluate_cross_service_confirmation_flow(
        client,
        station_id="nfa-0985",
        scenario_id="CONTEST-LIVE-CHEMICAL-001",
        backend_git_commit=BACKEND_COMMIT,
        model_git_commit=MODEL_COMMIT,
        runtime_manifest_sha256=MANIFEST_SHA256,
        runtime_manifest_actual_sha256=MANIFEST_SHA256,
        database_runtime="H2_POSTGRESQL_COMPATIBILITY_MODE",
        report_path=report_path,
    )


def test_cross_service_flow_accepts_zero_one_two_cancelled_transition(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "report.json"

    report = _evaluate(FakeFlowClient(), report_path)

    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["status"] == "COMPLETED"
    assert report["failed_check_count"] == 0
    assert report["analysis_backend_http_chain_executed"] is True
    assert report["full_voice_to_handoff_chain_executed"] is False
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["zero_conflict_executed"]["actual"] is False
    assert checks["two_conflict_executed"]["actual"] is True
    assert checks["cancelled_conflict_executed"]["actual"] is False
    assert checks["cancelled_risk_display_allowed"]["actual"] is False
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_cross_service_flow_rejects_rule_or_risk_before_two_confirmations() -> None:
    report = _evaluate(FakeFlowClient(unsafe_before_confirmation=True))

    assert report["status"] == "FAILED"
    assert report["decision"] == "REJECT_CROSS_SERVICE_FLOW"
    failed_names = {
        check["name"] for check in report["checks"] if check["passed"] is False
    }
    assert "zero_conflict_executed" in failed_names
    assert "zero_risk_display_allowed" in failed_names


def test_http_client_requires_explicit_opt_in_for_non_loopback_targets() -> None:
    with pytest.raises(ValueError, match="loopback"):
        HttpCrossServiceFlowClient(
            bff_base_url="https://bff.example",
            model_base_url="http://127.0.0.1:18000",
            origin="https://client.example",
        )


def test_cross_service_flow_does_not_forward_non_synthetic_replay() -> None:
    client = FakeFlowClient(data_classification="RESTRICTED_INCIDENT")

    with pytest.raises(RuntimeError, match="후단 전송을 중단"):
        _evaluate(client)

    assert client.analyze_call_count == 0


def test_report_is_deterministic_for_the_same_evidence() -> None:
    first = _evaluate(FakeFlowClient())
    second = _evaluate(FakeFlowClient())

    assert first == second
