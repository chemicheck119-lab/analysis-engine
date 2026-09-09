from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from chemiguard119.cross_service_voice_evaluation import (
    MANIFEST_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    evaluate_cross_service_voice_flow,
)
from chemiguard119.utils import sha256_file


BACKEND_COMMIT = "1" * 40
MODEL_COMMIT = "2" * 40
SPEECH_COMMIT = "3" * 40
SPEECH_MODEL_REPOSITORY = "Systran/faster-whisper-small"
SPEECH_MODEL_REVISION = "5" * 40
SPEECH_MODEL_SHA256 = "6" * 64
RUNTIME_SHA256 = "4" * 64
TRANSCRIPT = "차아염소산 나트륨 저장 탱크 누출 의심, 인접 저장고에는 염산 표기"


class FakeVoiceFlowClient:
    def __init__(
        self,
        *,
        unsafe_before_confirmation: bool = False,
        data_classification: str = "PUBLIC_SYNTHETIC",
        transcript: str = TRANSCRIPT,
        incident_surface: str = "차아염소산 나트륨",
        incident_cas: str = "7681-52-9",
        confirmation_type: str = "SYNTHETIC_DEMO_CONFIRMATION",
        speech_runtime_overrides: Mapping[str, Any] | None = None,
        record_incident_id: str = "INC-SYNTHETIC-001",
    ) -> None:
        self.unsafe_before_confirmation = unsafe_before_confirmation
        self.data_classification = data_classification
        self.transcript = transcript
        self.incident_surface = incident_surface
        self.incident_cas = incident_cas
        self.confirmation_type = confirmation_type
        self.speech_runtime_overrides = dict(speech_runtime_overrides or {})
        self.record_incident_id = record_incident_id
        self.confirmed: set[str] = set()
        self.transcribe_call_count = 0
        self.record_id = "REC-SYNTHETIC-001"

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
            "occurredAt": "2026-09-08T00:00:00Z",
            "receivedAt": "2026-09-08T00:00:01Z",
            "facilityName": "공개 합성 시설",
            "addressText": "관할 공개 합성 사고지점",
            "stationDisplayName": "합성 소방서",
            "sourceType": "SYNTHETIC_DISPATCH_REPLAY",
            "dataClassification": self.data_classification,
            "containsPersonalInformation": False,
            "location": {"latitude": 37.5, "longitude": 127.0},
        }

    def transcribe(
        self, incident_id: str, audio: bytes, request_id: str
    ) -> Mapping[str, Any]:
        self.transcribe_call_count += 1
        assert incident_id == "INC-SYNTHETIC-001"
        assert audio.startswith(b"RIFF")
        return {
            "requestId": request_id,
            "incidentId": incident_id,
            "status": "TRANSCRIBED",
            "abstained": False,
            "requiresResponderReview": True,
            "transcript": {"text": self.transcript},
            "input": {"audioRetained": False},
            "runtime": {
                "model": "small",
                "actualDevice": "cpu",
                "actualComputeType": "int8",
                "hotwordsUsed": False,
                "serviceGitCommit": SPEECH_COMMIT,
                "modelRepository": SPEECH_MODEL_REPOSITORY,
                "modelRevision": SPEECH_MODEL_REVISION,
                "modelBinSha256": SPEECH_MODEL_SHA256,
                "modelArtifactVerified": True,
                **self.speech_runtime_overrides,
            },
            "safetyBoundary": {
                "uncertaintyPreserved": True,
                "qualitySignalsAreCalibratedProbabilities": False,
                "chemicalIdentificationPerformed": False,
                "casConfirmationPerformed": False,
                "riskAssessmentPerformed": False,
                "decisionSupportOnly": True,
            },
        }

    def analyze(self, payload: Mapping[str, Any], request_id: str) -> Mapping[str, Any]:
        assert payload["incidentId"] == "INC-SYNTHETIC-001"
        assert payload["text"] == self.transcript
        assert payload["inputType"] == "VOICE_TRANSCRIPT"
        both = self.confirmed == {"INCIDENT", "FACILITY"}
        conflict_executed = both or self.unsafe_before_confirmation
        conflict: dict[str, Any] = {"executed": conflict_executed}
        if both:
            conflict["result"] = {
                "ruleId": "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
                "incidentCas": "7681-52-9",
                "facilityCas": "7647-01-0",
            }
        return {
            "requestId": request_id,
            "incidentId": "INC-SYNTHETIC-001",
            "analysisId": "ANL-SYNTHETIC-001",
            "state": (
                "SCREENING_COMPLETED"
                if both
                else (
                    "AWAITING_FACILITY_CONFIRMATION"
                    if "INCIDENT" in self.confirmed
                    else "AWAITING_SUBSTANCE_CONFIRMATION"
                )
            ),
            "confirmationGate": {
                "allRequiredConfirmed": both,
                "ruleExecutionAllowed": both,
            },
            "conflictReview": conflict,
            "riskDisplayAllowed": conflict_executed,
            "substanceCandidates": [
                {
                    "surfaceText": self.incident_surface,
                    "role": "INCIDENT",
                    "resolverStatus": "EXACT_ALIAS_CANDIDATE",
                    "candidates": [
                        {"casNumber": self.incident_cas, "ruleEligible": False}
                    ],
                },
                {
                    "surfaceText": "염산",
                    "role": "FACILITY",
                    "resolverStatus": "EXACT_ALIAS_CANDIDATE",
                    "candidates": [{"casNumber": "7647-01-0", "ruleEligible": False}],
                },
            ],
        }

    def confirm(
        self, incident_id: str, role: str, request_id: str
    ) -> Mapping[str, Any]:
        assert incident_id == "INC-SYNTHETIC-001"
        self.confirmed.add(role)
        return {
            "requestId": request_id,
            "confirmationId": f"CNF-{role}",
            "confirmationType": self.confirmation_type,
        }

    def cancel(
        self,
        incident_id: str,
        role: str,
        confirmation_id: str,
        request_id: str,
    ) -> Mapping[str, Any]:
        raise AssertionError("voice-to-record flow는 취소를 호출하지 않습니다.")

    def save_record(
        self,
        incident_id: str,
        payload: Mapping[str, Any],
        request_id: str,
    ) -> Mapping[str, Any]:
        assert incident_id == "INC-SYNTHETIC-001"
        assert payload["analysisIds"] == ["ANL-SYNTHETIC-001"]
        assert payload["confirmationIds"] == ["CNF-INCIDENT", "CNF-FACILITY"]
        return {
            "requestId": request_id,
            "incidentId": self.record_incident_id,
            "recordId": self.record_id,
            "resetAllowed": True,
        }


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    audio = tmp_path / "synthetic.wav"
    audio.write_bytes(b"RIFF" + b"\0\0\0\0" + b"WAVE" + b"synthetic")
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "fact_status": "부분 구현 또는 개발용 데모",
        "purpose": "LOCAL_CROSS_SERVICE_CONNECTIVITY_REGRESSION_ONLY",
        "source_type": "SYNTHETIC_TTS",
        "data_classification": "PUBLIC_SYNTHETIC",
        "contains_personal_information": False,
        "audio": {
            "expected_sha256": sha256_file(audio),
            "expected_surface_forms": ["차아염소산 나트륨", "염산"],
        },
        "selection_disclosure": {
            "selected_for_connectivity_not_accuracy": True,
            "prior_unspaced_trial_failed_incident_term": True,
            "performance_claim_allowed": False,
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest_path, audio


def _evaluate(
    client: FakeVoiceFlowClient,
    manifest_path: Path,
    audio_path: Path,
    report_path: Path | None = None,
) -> dict[str, Any]:
    return evaluate_cross_service_voice_flow(
        client,
        manifest_path=manifest_path,
        audio_path=audio_path,
        station_id="nfa-0985",
        scenario_id="CONTEST-LIVE-CHEMICAL-001",
        backend_git_commit=BACKEND_COMMIT,
        model_git_commit=MODEL_COMMIT,
        speech_git_commit=SPEECH_COMMIT,
        speech_model_repository=SPEECH_MODEL_REPOSITORY,
        speech_model_revision=SPEECH_MODEL_REVISION,
        speech_model_bin_sha256=SPEECH_MODEL_SHA256,
        runtime_manifest_sha256=RUNTIME_SHA256,
        runtime_manifest_actual_sha256=RUNTIME_SHA256,
        database_runtime="H2_POSTGRESQL_COMPATIBILITY_MODE",
        report_path=report_path,
    )


def test_voice_flow_accepts_synthetic_audio_to_record_transition(
    tmp_path: Path,
) -> None:
    manifest, audio = _fixture(tmp_path)
    report_path = tmp_path / "report.json"

    report = _evaluate(FakeVoiceFlowClient(), manifest, audio, report_path)

    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["status"] == "COMPLETED"
    assert report["failed_check_count"] == 0
    assert report["speech_input_executed"] is True
    assert report["voice_to_record_http_chain_executed"] is True
    assert report["full_voice_to_operational_handoff_validated"] is False
    assert report["human_transcript_review_performed"] is False
    assert report["human_cas_confirmation_performed"] is False
    assert "INC-SYNTHETIC-001" not in json.dumps(report, ensure_ascii=False)
    assert TRANSCRIPT not in json.dumps(report, ensure_ascii=False)
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert report["runtime"]["speech_git_commit_verified_by_api"] is True
    assert report["runtime"]["speech_model_artifact_verified"] is True
    assert any(
        "pinned model artifact identity" in claim for claim in report["claims_allowed"]
    )


def test_voice_flow_rejects_rule_execution_before_confirmation(tmp_path: Path) -> None:
    manifest, audio = _fixture(tmp_path)

    report = _evaluate(
        FakeVoiceFlowClient(unsafe_before_confirmation=True), manifest, audio
    )

    assert report["status"] == "FAILED"
    failed = {row["name"] for row in report["checks"] if row.get("passed") is False}
    assert "zero_conflict_executed" in failed
    assert "zero_risk_display_allowed" in failed
    assert "one_conflict_executed" in failed
    assert "one_risk_display_allowed" in failed


def test_failed_asr_surface_is_not_reported_as_successful_claim(tmp_path: Path) -> None:
    manifest, audio = _fixture(tmp_path)
    client = FakeVoiceFlowClient(
        transcript=(
            "차아 염소산 나트륨 저장 탱크에서 노출이 의심됩니다. "
            "인접 저장고에는 염산 표기가 있습니다."
        ),
        incident_surface="나트륨",
        incident_cas="7440-23-5",
    )

    report = _evaluate(client, manifest, audio)

    assert report["status"] == "FAILED"
    assert not any(
        "두 물질 표면형과 후보 CAS가 보존됨" in claim
        for claim in report["claims_allowed"]
    )
    assert (
        "실패한 물질 표면형 또는 후보 CAS 보존을 성공한 것으로 표현"
        in report["claims_not_allowed"]
    )
    assert (
        "음성 후보만 있는 상태에서 Rule·위험 표시가 차단됨" in report["claims_allowed"]
    )


def test_wrong_record_incident_is_not_reported_as_authoritative_storage(
    tmp_path: Path,
) -> None:
    manifest, audio = _fixture(tmp_path)

    report = _evaluate(
        FakeVoiceFlowClient(record_incident_id="INC-WRONG"), manifest, audio
    )

    assert report["status"] == "FAILED"
    failed = {row["name"] for row in report["checks"] if row.get("passed") is False}
    assert "record_incident_correlated" in failed
    assert not any(
        "권위 snapshot과 함께 record로 저장함" in claim
        for claim in report["claims_allowed"]
    )
    assert (
        "사고 상관관계가 검증되지 않은 record를 권위 snapshot 저장으로 표현"
        in report["claims_not_allowed"]
    )


def test_wrong_confirmation_type_cannot_allow_cameo_success_claim(
    tmp_path: Path,
) -> None:
    manifest, audio = _fixture(tmp_path)

    report = _evaluate(
        FakeVoiceFlowClient(confirmation_type="UNVERIFIED_CONFIRMATION"),
        manifest,
        audio,
    )

    assert report["status"] == "FAILED"
    failed = {row["name"] for row in report["checks"] if row.get("passed") is False}
    assert "incident_confirmation_type" in failed
    assert "facility_confirmation_type" in failed
    assert not any(
        "합성 2-CAS 확인 뒤 제한된 CAMEO 결과" in claim
        for claim in report["claims_allowed"]
    )


def test_unverified_speech_model_provenance_fails_the_gate(tmp_path: Path) -> None:
    manifest, audio = _fixture(tmp_path)
    client = FakeVoiceFlowClient(
        speech_runtime_overrides={
            "modelBinSha256": None,
            "modelArtifactVerified": False,
        }
    )

    report = _evaluate(client, manifest, audio)

    assert report["status"] == "FAILED"
    failed = {row["name"] for row in report["checks"] if row.get("passed") is False}
    assert "speech_model_bin_sha256" in failed
    assert "speech_model_artifact_verified" in failed
    assert report["runtime"]["speech_model_artifact_verified"] is False
    assert not any(
        "pinned model artifact identity" in claim for claim in report["claims_allowed"]
    )
    assert (
        "Speech model artifact와 commit이 API에서 검증됐다고 표현"
        in report["claims_not_allowed"]
    )


def test_integer_artifact_verification_cannot_pass_boolean_gate(tmp_path: Path) -> None:
    manifest, audio = _fixture(tmp_path)
    client = FakeVoiceFlowClient(speech_runtime_overrides={"modelArtifactVerified": 1})

    report = _evaluate(client, manifest, audio)

    assert report["status"] == "FAILED"
    check = next(
        row
        for row in report["checks"]
        if row["name"] == "speech_model_artifact_verified"
    )
    assert check["actual"] == 1
    assert check["passed"] is False
    assert report["runtime"]["speech_model_artifact_verified"] is False


@pytest.mark.parametrize(
    ("field", "numeric_value"),
    [
        ("contains_personal_information", 0),
        ("selected_for_connectivity_not_accuracy", 1),
        ("performance_claim_allowed", 0),
    ],
)
def test_manifest_numeric_boolean_cannot_pass_safety_contract(
    tmp_path: Path,
    field: str,
    numeric_value: int,
) -> None:
    manifest_path, audio = _fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if field == "contains_personal_information":
        manifest[field] = numeric_value
    else:
        manifest["selection_disclosure"][field] = numeric_value
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    client = FakeVoiceFlowClient()

    with pytest.raises(ValueError, match="manifest"):
        _evaluate(client, manifest_path, audio)

    assert client.transcribe_call_count == 0


def test_voice_flow_rejects_tampered_audio_before_http(tmp_path: Path) -> None:
    manifest, audio = _fixture(tmp_path)
    audio.write_bytes(audio.read_bytes() + b"tampered")
    client = FakeVoiceFlowClient()

    with pytest.raises(ValueError, match="SHA-256"):
        _evaluate(client, manifest, audio)

    assert client.transcribe_call_count == 0


def test_voice_flow_does_not_forward_non_synthetic_replay(tmp_path: Path) -> None:
    manifest, audio = _fixture(tmp_path)
    client = FakeVoiceFlowClient(data_classification="RESTRICTED_INCIDENT")

    with pytest.raises(RuntimeError, match="실행을 중단"):
        _evaluate(client, manifest, audio)

    assert client.transcribe_call_count == 0


def test_voice_flow_report_is_deterministic(tmp_path: Path) -> None:
    manifest, audio = _fixture(tmp_path)

    first = _evaluate(FakeVoiceFlowClient(), manifest, audio)
    second = _evaluate(FakeVoiceFlowClient(), manifest, audio)

    assert first == second
