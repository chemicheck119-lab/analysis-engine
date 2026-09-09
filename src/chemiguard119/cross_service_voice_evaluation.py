"""공개 합성 음성의 Speech→Backend→Model→record HTTP 경계를 평가한다.

명료하게 띄어 읽도록 선택한 TTS clip 한 건의 연결성 회귀다. 전사 정확도, 실제 사용자
검토, 실제 CAS 확인, 현장 무전 또는 운영 인계 성능을 평가하지 않는다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from chemiguard119.cross_service_confirmation_evaluation import (
    CrossServiceFlowClient,
)
from chemiguard119.utils import sha256_file, write_json


REPORT_SCHEMA_VERSION = "chemicheck119-cross-service-voice-to-record-v2"
MANIFEST_SCHEMA_VERSION = "chemicheck119-synthetic-voice-e2e-manifest-v1"
FACT_STATUS = "부분 구현 또는 개발용 데모"
CLAIM_SCOPE = "LOCAL_SYNTHETIC_VOICE_TO_RECORD_REGRESSION_ONLY"
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MODEL_REPOSITORY_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
)
MAX_AUDIO_BYTES = 16 * 1024 * 1024
REQUEST_IDS = {
    "transcribe": "REQ-VOICE-E2E-TRANSCRIBE-001",
    "zero": "REQ-VOICE-E2E-ZERO-001",
    "confirm_incident": "REQ-VOICE-E2E-CONFIRM-INCIDENT-001",
    "one": "REQ-VOICE-E2E-ONE-001",
    "confirm_facility": "REQ-VOICE-E2E-CONFIRM-FACILITY-001",
    "two": "REQ-VOICE-E2E-TWO-001",
    "record": "REQ-VOICE-E2E-RECORD-001",
    "record_retry": "REQ-VOICE-E2E-RECORD-RETRY-001",
}


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: JSON 최상위 값은 객체여야 합니다.")
    return payload


def _object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _add_check(
    checks: list[dict[str, Any]], name: str, expected: Any, actual: Any
) -> None:
    if isinstance(expected, bool):
        passed = isinstance(actual, bool) and actual is expected
    else:
        passed = actual == expected
    checks.append(
        {
            "name": name,
            "expected": expected,
            "actual": actual,
            "passed": passed,
        }
    )


def _validate_manifest(manifest: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    expected = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "fact_status": FACT_STATUS,
        "purpose": "LOCAL_CROSS_SERVICE_CONNECTIVITY_REGRESSION_ONLY",
        "source_type": "SYNTHETIC_TTS",
        "data_classification": "PUBLIC_SYNTHETIC",
        "contains_personal_information": False,
    }
    for field, value in expected.items():
        if manifest.get(field) != value:
            errors.append(f"MANIFEST_FIELD_MISMATCH:{field}")
    audio = _object(manifest.get("audio"))
    if SHA256_PATTERN.fullmatch(str(audio.get("expected_sha256") or "")) is None:
        errors.append("MANIFEST_AUDIO_SHA256_INVALID")
    expected_forms = audio.get("expected_surface_forms")
    if (
        not isinstance(expected_forms, list)
        or len(expected_forms) != 2
        or any(not isinstance(item, str) or not item.strip() for item in expected_forms)
    ):
        errors.append("MANIFEST_EXPECTED_SURFACE_FORMS_INVALID")
    disclosure = _object(manifest.get("selection_disclosure"))
    required_disclosure = {
        "selected_for_connectivity_not_accuracy": True,
        "prior_unspaced_trial_failed_incident_term": True,
        "performance_claim_allowed": False,
    }
    for field, value in required_disclosure.items():
        if disclosure.get(field) != value:
            errors.append(f"MANIFEST_DISCLOSURE_MISMATCH:{field}")
    return errors


def _analysis_request(envelope: Mapping[str, Any], transcript: str) -> dict[str, Any]:
    location = _object(envelope.get("location"))
    return {
        "incidentId": envelope.get("incidentId"),
        "text": transcript,
        "inputType": "VOICE_TRANSCRIPT",
        "occurredAt": envelope.get("occurredAt"),
        "location": {
            "facilityName": envelope.get("facilityName"),
            "address": envelope.get("addressText"),
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "coordinateSource": "DISPATCH_SYSTEM",
            "resolvedAt": envelope.get("receivedAt"),
        },
        "operationsContext": {
            "dispatchStationName": envelope.get("stationDisplayName"),
            "journeyState": "EN_ROUTE",
        },
        "evidenceTopK": 5,
    }


def _candidate(
    response: Mapping[str, Any], role: str
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    rows = response.get("substanceCandidates")
    candidates = rows if isinstance(rows, list) else []
    surface = next(
        (
            item
            for item in candidates
            if isinstance(item, Mapping) and item.get("role") == role
        ),
        {},
    )
    ranked = surface.get("candidates") if isinstance(surface, Mapping) else []
    ranked_rows = ranked if isinstance(ranked, list) else []
    first = (
        ranked_rows[0] if ranked_rows and isinstance(ranked_rows[0], Mapping) else {}
    )
    return _object(surface), _object(first)


def _record_request(
    envelope: Mapping[str, Any],
    transcript: str,
    analysis_id: str,
    confirmation_ids: list[str],
) -> dict[str, Any]:
    return {
        "conversationStartedAt": envelope.get("occurredAt"),
        "messages": [
            {
                "messageId": "MSG-VOICE-E2E-USER-001",
                "sequence": 1,
                "role": "USER",
                "text": transcript,
                "createdAt": envelope.get("occurredAt"),
                "analysisId": None,
            },
            {
                "messageId": "MSG-VOICE-E2E-ASSISTANT-001",
                "sequence": 2,
                "role": "ASSISTANT",
                "text": "공개 합성 음성 회귀의 확인 완료 결과입니다.",
                "createdAt": envelope.get("receivedAt"),
                "analysisId": analysis_id,
            },
        ],
        "analysisIds": [analysis_id],
        "confirmationIds": confirmation_ids,
        "outcomeReport": {
            "facilityName": envelope.get("facilityName"),
            "facilityAddress": envelope.get("addressText"),
            "performedActions": ["ZONE_CONTROL"],
            "briefApplicationStatus": "REVIEWED_NOT_APPLIED",
            "additionalFactors": [],
            "finalResponseOutcome": "MONITORING_CONTINUES",
        },
    }


def evaluate_cross_service_voice_flow(
    client: CrossServiceFlowClient,
    *,
    manifest_path: Path,
    audio_path: Path,
    station_id: str,
    scenario_id: str,
    backend_git_commit: str,
    model_git_commit: str,
    speech_git_commit: str,
    speech_model_repository: str,
    speech_model_revision: str,
    speech_model_bin_sha256: str,
    runtime_manifest_sha256: str,
    runtime_manifest_actual_sha256: str,
    database_runtime: str,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """합성 WAV 한 건의 실제 3-service HTTP와 record 저장을 평가한다."""

    commits = {
        "backend_git_commit": backend_git_commit,
        "model_git_commit": model_git_commit,
        "speech_git_commit": speech_git_commit,
    }
    for field, value in commits.items():
        if GIT_COMMIT_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{field}은 40자리 소문자 SHA여야 합니다.")
    if MODEL_REPOSITORY_PATTERN.fullmatch(speech_model_repository) is None:
        raise ValueError("speech_model_repository 형식이 올바르지 않습니다.")
    if GIT_COMMIT_PATTERN.fullmatch(speech_model_revision) is None:
        raise ValueError("speech_model_revision은 40자리 소문자 SHA여야 합니다.")
    for label, value in {
        "Speech model.bin": speech_model_bin_sha256,
        "runtime manifest": runtime_manifest_sha256,
        "계산된 runtime manifest": runtime_manifest_actual_sha256,
    }.items():
        if SHA256_PATTERN.fullmatch(value) is None:
            raise ValueError(f"{label} SHA-256 형식이 올바르지 않습니다.")
    if database_runtime not in {
        "H2_POSTGRESQL_COMPATIBILITY_MODE",
        "POSTGRESQL_TESTCONTAINER",
        "CLOUD_SQL_POSTGRESQL",
    }:
        raise ValueError("지원하지 않는 database runtime입니다.")

    manifest_path = Path(manifest_path)
    audio_path = Path(audio_path)
    manifest = _load_object(manifest_path)
    manifest_errors = _validate_manifest(manifest)
    if manifest_errors:
        raise ValueError("합성 음성 manifest가 안전 계약을 충족하지 않습니다.")
    audio = audio_path.read_bytes()
    if (
        len(audio) < 12
        or len(audio) > MAX_AUDIO_BYTES
        or audio[:4] != b"RIFF"
        or audio[8:12] != b"WAVE"
    ):
        raise ValueError("합성 음성은 16MiB 이하 RIFF/WAVE여야 합니다.")
    audio_digest = sha256_file(audio_path)
    audio_manifest = _object(manifest.get("audio"))
    if audio_digest != audio_manifest.get("expected_sha256"):
        raise ValueError("합성 음성 SHA-256이 manifest와 일치하지 않습니다.")
    expected_forms = list(audio_manifest.get("expected_surface_forms") or [])

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
        raise RuntimeError("공개 합성·비개인정보 replay가 아니므로 실행을 중단합니다.")
    incident_id = str(envelope.get("incidentId") or "")

    speech = client.transcribe(incident_id, audio, REQUEST_IDS["transcribe"])
    transcript_payload = _object(speech.get("transcript"))
    transcript = str(transcript_payload.get("text") or "").strip()
    if speech.get("status") != "TRANSCRIBED" or not transcript:
        raise RuntimeError("Speech API가 기권했으므로 후단 분석과 저장을 중단합니다.")

    analysis_payload = _analysis_request(envelope, transcript)
    zero = client.analyze(analysis_payload, REQUEST_IDS["zero"])
    incident_confirmation = client.confirm(
        incident_id, "INCIDENT", REQUEST_IDS["confirm_incident"]
    )
    one = client.analyze(analysis_payload, REQUEST_IDS["one"])
    facility_confirmation = client.confirm(
        incident_id, "FACILITY", REQUEST_IDS["confirm_facility"]
    )
    two = client.analyze(analysis_payload, REQUEST_IDS["two"])
    analysis_id = str(two.get("analysisId") or "")
    confirmation_ids = [
        str(incident_confirmation.get("confirmationId") or ""),
        str(facility_confirmation.get("confirmationId") or ""),
    ]
    if not analysis_id or any(not item for item in confirmation_ids):
        raise RuntimeError(
            "권위 analysis 또는 confirmation ID가 없어 저장을 중단합니다."
        )
    record_payload = _record_request(
        envelope, transcript, analysis_id, confirmation_ids
    )
    record = client.save_record(incident_id, record_payload, REQUEST_IDS["record"])
    record_retry = client.save_record(
        incident_id, record_payload, REQUEST_IDS["record_retry"]
    )

    checks: list[dict[str, Any]] = []
    model_runtime = _object(model_metadata.get("runtime"))
    integrity = _object(model_runtime.get("integrity"))
    backend_release = _object(backend_info.get("release"))
    _add_check(checks, "model_ready", "READY", model_ready.get("status"))
    _add_check(checks, "model_runtime_integrity", "VERIFIED", integrity.get("status"))
    _add_check(
        checks, "model_git_commit", model_git_commit, integrity.get("git_commit")
    )
    _add_check(
        checks,
        "model_runtime_manifest_verified",
        True,
        integrity.get("manifest_sha256_verified"),
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
        backend_release.get("gitCommit"),
    )
    _add_check(
        checks, "audio_sha256", audio_manifest.get("expected_sha256"), audio_digest
    )
    _add_check(
        checks,
        "request_ids_unique",
        len(REQUEST_IDS),
        len(set(REQUEST_IDS.values())),
    )

    speech_input = _object(speech.get("input"))
    speech_runtime = _object(speech.get("runtime"))
    speech_safety = _object(speech.get("safetyBoundary"))
    _add_check(
        checks, "speech_request_id", REQUEST_IDS["transcribe"], speech.get("requestId")
    )
    _add_check(
        checks,
        "speech_incident_correlated",
        True,
        speech.get("incidentId") == incident_id,
    )
    _add_check(checks, "speech_status", "TRANSCRIBED", speech.get("status"))
    _add_check(checks, "speech_abstained", False, speech.get("abstained"))
    _add_check(
        checks,
        "speech_requires_responder_review",
        True,
        speech.get("requiresResponderReview"),
    )
    _add_check(checks, "speech_transcript_nonempty", True, bool(transcript))
    for index, surface in enumerate(expected_forms, start=1):
        _add_check(
            checks,
            f"speech_expected_surface_{index}_present",
            True,
            surface in transcript,
        )
    _add_check(
        checks, "speech_audio_retained", False, speech_input.get("audioRetained")
    )
    _add_check(
        checks, "speech_hotwords_used", False, speech_runtime.get("hotwordsUsed")
    )
    _add_check(
        checks,
        "speech_service_git_commit",
        speech_git_commit,
        speech_runtime.get("serviceGitCommit"),
    )
    _add_check(
        checks,
        "speech_model_repository",
        speech_model_repository,
        speech_runtime.get("modelRepository"),
    )
    _add_check(
        checks,
        "speech_model_revision",
        speech_model_revision,
        speech_runtime.get("modelRevision"),
    )
    _add_check(
        checks,
        "speech_model_bin_sha256",
        speech_model_bin_sha256,
        speech_runtime.get("modelBinSha256"),
    )
    _add_check(
        checks,
        "speech_model_artifact_verified",
        True,
        speech_runtime.get("modelArtifactVerified"),
    )
    _add_check(
        checks, "speech_actual_device", "cpu", speech_runtime.get("actualDevice")
    )
    _add_check(
        checks,
        "speech_actual_compute_type",
        "int8",
        speech_runtime.get("actualComputeType"),
    )
    for field, expected in {
        "uncertaintyPreserved": True,
        "qualitySignalsAreCalibratedProbabilities": False,
        "chemicalIdentificationPerformed": False,
        "casConfirmationPerformed": False,
        "riskAssessmentPerformed": False,
        "decisionSupportOnly": True,
    }.items():
        _add_check(checks, f"speech_safety_{field}", expected, speech_safety.get(field))

    zero_gate = _object(zero.get("confirmationGate"))
    zero_conflict = _object(zero.get("conflictReview"))
    _add_check(checks, "zero_request_id", REQUEST_IDS["zero"], zero.get("requestId"))
    _add_check(
        checks, "zero_incident_correlated", True, zero.get("incidentId") == incident_id
    )
    _add_check(
        checks, "zero_state", "AWAITING_SUBSTANCE_CONFIRMATION", zero.get("state")
    )
    _add_check(
        checks,
        "zero_rule_execution_allowed",
        False,
        zero_gate.get("ruleExecutionAllowed"),
    )
    _add_check(checks, "zero_conflict_executed", False, zero_conflict.get("executed"))
    _add_check(
        checks, "zero_risk_display_allowed", False, zero.get("riskDisplayAllowed")
    )
    incident_surface, incident_candidate = _candidate(zero, "INCIDENT")
    facility_surface, facility_candidate = _candidate(zero, "FACILITY")
    _add_check(
        checks,
        "incident_surface",
        expected_forms[0],
        incident_surface.get("surfaceText"),
    )
    _add_check(
        checks,
        "incident_resolver_status",
        "EXACT_ALIAS_CANDIDATE",
        incident_surface.get("resolverStatus"),
    )
    _add_check(
        checks,
        "incident_candidate_cas",
        "7681-52-9",
        incident_candidate.get("casNumber"),
    )
    _add_check(
        checks,
        "incident_candidate_rule_eligible",
        False,
        incident_candidate.get("ruleEligible"),
    )
    _add_check(
        checks,
        "facility_surface",
        expected_forms[1],
        facility_surface.get("surfaceText"),
    )
    _add_check(
        checks,
        "facility_resolver_status",
        "EXACT_ALIAS_CANDIDATE",
        facility_surface.get("resolverStatus"),
    )
    _add_check(
        checks,
        "facility_candidate_cas",
        "7647-01-0",
        facility_candidate.get("casNumber"),
    )
    _add_check(
        checks,
        "facility_candidate_rule_eligible",
        False,
        facility_candidate.get("ruleEligible"),
    )

    one_gate = _object(one.get("confirmationGate"))
    one_conflict = _object(one.get("conflictReview"))
    _add_check(checks, "one_request_id", REQUEST_IDS["one"], one.get("requestId"))
    _add_check(
        checks, "one_incident_correlated", True, one.get("incidentId") == incident_id
    )
    _add_check(checks, "one_state", "AWAITING_FACILITY_CONFIRMATION", one.get("state"))
    _add_check(
        checks,
        "one_rule_execution_allowed",
        False,
        one_gate.get("ruleExecutionAllowed"),
    )
    _add_check(checks, "one_conflict_executed", False, one_conflict.get("executed"))
    _add_check(checks, "one_risk_display_allowed", False, one.get("riskDisplayAllowed"))

    two_gate = _object(two.get("confirmationGate"))
    two_conflict = _object(two.get("conflictReview"))
    two_result = _object(two_conflict.get("result"))
    _add_check(checks, "two_request_id", REQUEST_IDS["two"], two.get("requestId"))
    _add_check(
        checks, "two_incident_correlated", True, two.get("incidentId") == incident_id
    )
    _add_check(checks, "two_state", "SCREENING_COMPLETED", two.get("state"))
    _add_check(
        checks, "two_all_required_confirmed", True, two_gate.get("allRequiredConfirmed")
    )
    _add_check(
        checks, "two_rule_execution_allowed", True, two_gate.get("ruleExecutionAllowed")
    )
    _add_check(checks, "two_conflict_executed", True, two_conflict.get("executed"))
    _add_check(checks, "two_risk_display_allowed", True, two.get("riskDisplayAllowed"))
    _add_check(
        checks,
        "two_rule_id",
        "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
        two_result.get("ruleId"),
    )
    _add_check(
        checks, "two_rule_incident_cas", "7681-52-9", two_result.get("incidentCas")
    )
    _add_check(
        checks, "two_rule_facility_cas", "7647-01-0", two_result.get("facilityCas")
    )
    _add_check(
        checks,
        "incident_confirmation_type",
        "SYNTHETIC_DEMO_CONFIRMATION",
        incident_confirmation.get("confirmationType"),
    )
    _add_check(
        checks,
        "facility_confirmation_type",
        "SYNTHETIC_DEMO_CONFIRMATION",
        facility_confirmation.get("confirmationType"),
    )

    record_id = str(record.get("recordId") or "")
    _add_check(
        checks, "record_request_id", REQUEST_IDS["record"], record.get("requestId")
    )
    _add_check(
        checks,
        "record_incident_correlated",
        True,
        record.get("incidentId") == incident_id,
    )
    _add_check(checks, "record_id_present", True, bool(record_id))
    _add_check(checks, "record_reset_allowed", True, record.get("resetAllowed"))
    _add_check(
        checks,
        "record_retry_request_id",
        REQUEST_IDS["record_retry"],
        record_retry.get("requestId"),
    )
    _add_check(
        checks,
        "record_exact_retry_same_id",
        True,
        bool(record_id) and record_retry.get("recordId") == record_id,
    )

    passed_count = sum(check.get("passed") is True for check in checks)
    complete = passed_count == len(checks)
    passed_by_name = {
        str(check["name"]): check.get("passed") is True for check in checks
    }

    def all_passed(*names: str) -> bool:
        return all(passed_by_name.get(name, False) for name in names)

    claims_allowed = [
        "잠긴 공개 합성 WAV 1건이 실제 Speech API·Backend·Model API HTTP를 통과함"
    ]
    if all_passed(
        "speech_expected_surface_1_present",
        "speech_expected_surface_2_present",
        "incident_surface",
        "facility_surface",
        "incident_candidate_cas",
        "facility_candidate_cas",
    ):
        claims_allowed.append(
            "명료하게 띄어 읽은 선택 clip에서 두 물질 표면형과 후보 CAS가 보존됨"
        )
    if all_passed(
        "zero_rule_execution_allowed",
        "zero_conflict_executed",
        "zero_risk_display_allowed",
        "one_rule_execution_allowed",
        "one_conflict_executed",
        "one_risk_display_allowed",
    ):
        claims_allowed.append("음성 후보만 있는 상태에서 Rule·위험 표시가 차단됨")
    if all_passed(
        "incident_confirmation_type",
        "facility_confirmation_type",
        "two_all_required_confirmed",
        "two_rule_execution_allowed",
        "two_conflict_executed",
        "two_rule_id",
        "two_rule_incident_cas",
        "two_rule_facility_cas",
        "record_id_present",
    ):
        claims_allowed.append(
            "합성 2-CAS 확인 뒤 제한된 CAMEO 결과를 권위 snapshot과 함께 record로 저장함"
        )
    if all_passed("record_exact_retry_same_id"):
        claims_allowed.append("동일 record payload 재요청이 같은 record ID를 반환함")
    speech_provenance_checks = (
        "speech_service_git_commit",
        "speech_model_repository",
        "speech_model_revision",
        "speech_model_bin_sha256",
        "speech_model_artifact_verified",
    )
    if all_passed(*speech_provenance_checks):
        claims_allowed.append(
            "Speech API가 보고한 service commit과 pinned model artifact identity가 "
            "기대값과 일치함"
        )

    claims_not_allowed = [
        "합성 음성 1건을 신고음성·현장 무전 정확도로 표현",
        "사람이 전사문과 두 CAS를 실제 확인했다고 표현",
        "record 저장을 실제 현장 인계나 대응 조치 수행으로 표현",
        "H2 실행을 Cloud SQL·상용 운영 검증으로 표현",
    ]
    if not all_passed(*speech_provenance_checks):
        claims_not_allowed.append(
            "Speech model artifact와 commit이 API에서 검증됐다고 표현"
        )
    if not all_passed(
        "speech_expected_surface_1_present",
        "speech_expected_surface_2_present",
        "incident_surface",
        "facility_surface",
        "incident_candidate_cas",
        "facility_candidate_cas",
    ):
        claims_not_allowed.append(
            "실패한 물질 표면형 또는 후보 CAS 보존을 성공한 것으로 표현"
        )

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": "COMPLETED" if complete else "FAILED",
        "fact_status": FACT_STATUS,
        "claim_scope": CLAIM_SCOPE,
        "field_validated": False,
        "cloud_run_validated": False,
        "database_runtime": database_runtime,
        "database_runtime_verified": False,
        "database_runtime_evidence": "OPERATOR_DECLARED",
        "speech_input_executed": True,
        "speech_backend_http_chain_executed": True,
        "analysis_backend_http_chain_executed": True,
        "voice_to_record_http_chain_executed": True,
        "full_voice_to_operational_handoff_validated": False,
        "human_transcript_review_performed": False,
        "human_cas_confirmation_performed": False,
        "automatic_transcript_forwarding_for_test_harness": True,
        "operational_actions_performed": False,
        "data_classification": "PUBLIC_SYNTHETIC",
        "input_artifacts": {
            "audio_sha256": audio_digest,
            "audio_size_bytes": len(audio),
            "manifest_sha256": sha256_file(manifest_path),
            "raw_audio_stored_in_report": False,
            "raw_transcript_stored_in_report": False,
            "selection_disclosure": manifest.get("selection_disclosure"),
        },
        "runtime": {
            "speech_model": speech_runtime.get("model"),
            "speech_device": speech_runtime.get("actualDevice"),
            "speech_compute_type": speech_runtime.get("actualComputeType"),
            "speech_hotwords_used": speech_runtime.get("hotwordsUsed"),
            "speech_service_git_commit": speech_runtime.get("serviceGitCommit"),
            "speech_model_repository": speech_runtime.get("modelRepository"),
            "speech_model_revision": speech_runtime.get("modelRevision"),
            "speech_model_bin_sha256": speech_runtime.get("modelBinSha256"),
            "speech_git_commit_verified_by_api": all_passed(
                "speech_service_git_commit"
            ),
            "speech_model_artifact_verified": all_passed(
                "speech_model_repository",
                "speech_model_revision",
                "speech_model_bin_sha256",
                "speech_model_artifact_verified",
            ),
            "model_runtime_integrity": integrity.get("status"),
        },
        "request_correlation": {
            "workflow_key": "incident_id",
            "request_id_policy": "UNIQUE_PER_HTTP_REQUEST_PROPAGATED_WITHIN_CALL",
            "raw_incident_id_stored": False,
            "raw_analysis_id_stored": False,
            "raw_confirmation_id_stored": False,
            "raw_record_id_stored": False,
        },
        "provenance": {
            **commits,
            "speech_model_repository": speech_model_repository,
            "speech_model_revision": speech_model_revision,
            "speech_model_bin_sha256": speech_model_bin_sha256,
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
            "CONDITIONALLY_ADOPT_FOR_LOCAL_SYNTHETIC_CONNECTIVITY_REGRESSION"
            if complete
            else "REJECT_SYNTHETIC_VOICE_TO_RECORD_FLOW"
        ),
        "claims_allowed": claims_allowed,
        "claims_not_allowed": claims_not_allowed,
        "safety_notice": (
            "공개 합성 음성의 로컬 연결성 회귀이며 현장 명령·정확도·안전성 증명이 아닙니다."
        ),
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


__all__ = [
    "CLAIM_SCOPE",
    "FACT_STATUS",
    "MANIFEST_SCHEMA_VERSION",
    "REPORT_SCHEMA_VERSION",
    "evaluate_cross_service_voice_flow",
]
