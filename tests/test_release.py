from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import joblib
import pytest

from chemiguard119.evaluation_contract import audit_evaluation_dataset
from chemiguard119.paths import CONFIG_DIR
from chemiguard119.release import (
    DATA_SOURCE_REGISTRY_FILE,
    PILOT_QUALIFICATION_MINIMUM_CASES,
    RELEASE_ATTESTATION_KEY_ENV_VAR,
    REQUIRED_CONFIG_FILES,
    RuntimeIntegrityError,
    bind_evaluation_report,
    create_runtime_manifest,
    release_attestation_signature,
    verify_runtime_release,
)
from chemiguard119.resolver import (
    INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION,
    MODEL_SCHEMA_VERSION as RESOLVER_SCHEMA_VERSION,
)
from chemiguard119.retrieval import MODEL_SCHEMA_VERSION as RETRIEVER_SCHEMA_VERSION


ATTESTATION_KEY = "test-only-release-attestation-key-" + "x" * 32


def _runtime_fixture(
    tmp_path: Path,
    *,
    redistribution_approved: bool = True,
    resolver_schema_version: str = RESOLVER_SCHEMA_VERSION,
) -> tuple[Path, Path, Path, Path]:
    artifact_dir = tmp_path / "artifacts"
    config_dir = tmp_path / "config"
    artifact_dir.mkdir()
    config_dir.mkdir()
    db_path = artifact_dir / "chemiguard119.sqlite"
    resolver_path = artifact_dir / "resolver.joblib"
    retriever_path = artifact_dir / "retriever.joblib"

    with sqlite3.connect(db_path) as connection:
        for table in (
            "substance",
            "alias",
            "evidence",
            "cameo_chemical",
            "cameo_mapping",
            "compatibility",
            "facility_candidate",
        ):
            connection.execute(f"CREATE TABLE {table} (id TEXT)")
    joblib.dump(
        {
            "schema_version": resolver_schema_version,
            "task": "substance_candidate_retrieval",
        },
        resolver_path,
    )
    joblib.dump(
        {
            "schema_version": RETRIEVER_SCHEMA_VERSION,
            "task": "official_evidence_retrieval",
        },
        retriever_path,
    )
    for name in REQUIRED_CONFIG_FILES:
        shutil.copy2(CONFIG_DIR / name, config_dir / name)

    registry_path = config_dir / DATA_SOURCE_REGISTRY_FILE
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for source in registry["sources"]:
        source["redistribution_status"] = (
            "APPROVED" if redistribution_approved else "REVIEW_REQUIRED"
        )
        source["reviewer"] = "independent-data-reviewer"
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return db_path, resolver_path, retriever_path, config_dir


def _write_reviewed_dataset(path: Path, name: str, count: int) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for index in range(count):
            handle.write(
                json.dumps(
                    {
                        "case_id": f"{name}-{index:05d}",
                        "review_status": "DOUBLE_REVIEWED_NON_EXPERT",
                        "source_type": "CURATED_OFFICIAL_DOCUMENT_QUERY",
                        "source_reference": f"https://example.test/{name}/{index}",
                        "labeler_id": f"labeler-{index % 7}",
                        "reviewer_id": f"reviewer-{index % 5}",
                        "split": "locked_test",
                        "duplicate_group": f"{name}-group-{index:05d}",
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _report_payload(
    name: str,
    case_count: int,
    *,
    unsafe_failures: int,
    retriever_high_relevance_recall: float = 0.99,
    retriever_graded_gain_recall: float = 0.97,
    retriever_answerable_case_count: int = 300,
    retriever_unanswerable_case_count: int = 100,
    retriever_high_relevance_fact_coverage: float = 0.99,
    retriever_fact_complete_lower: float = 0.96,
) -> dict:
    reports = {
        "resolver": {
            "metrics_version": "resolver-evaluation-v2",
            "case_count": case_count,
            "candidate_top3_recall": 0.99,
            "unique_resolution_accuracy": 0.95,
        },
        "resolver_hint_safety": {
            "metrics_version": "resolver-hint-safety-v1",
            "case_count": case_count,
            "safety_pass_rate": 1.0 if unsafe_failures == 0 else 0.99,
            "unsafe_auto_hint_count": unsafe_failures,
            "wrong_cas_auto_hint_count": 0,
            "resolver_rule_eligibility_violation_count": 0,
            "ambiguous_preservation_rate": 1.0,
        },
        "retriever_sections": {
            "metrics_version": "retriever-section-qrel-v3",
            "case_count": case_count,
            "answerable_case_count": retriever_answerable_case_count,
            "unanswerable_case_count": retriever_unanswerable_case_count,
            "metrics": {
                "graded_gain_recall_at_k": retriever_graded_gain_recall,
                "high_relevance_fact_complete_case_rate_at_k": 0.99,
                "high_relevance_fact_coverage_at_k": (
                    retriever_high_relevance_fact_coverage
                ),
                "high_relevance_recall_at_k": retriever_high_relevance_recall,
                "mrr_at_k": 0.95,
                "recall_at_k": 0.95,
                "required_fact_coverage_at_k": 0.95,
                "unanswerable_abstention_rate": 0.98,
                "valid_source_url_coverage_at_k": 1.0,
                "wrong_cas_rate_at_k": 0.0,
            },
            "uncertainty": {
                "high_relevance_fact_complete_case_rate_at_k": {
                    "lower": retriever_fact_complete_lower,
                }
            },
        },
        "parser_locked": {
            "metrics_version": "incident-parser-evaluation-v1",
            "case_count": case_count,
            "metrics": {
                "field_micro_f1": 0.94,
                "incident_type_f1": 0.93,
                "substance_recall": 0.99,
            },
        },
        "e2e_scenarios": {
            "metrics_version": "incident-e2e-evaluation-v1",
            "case_count": case_count,
            "metrics": {
                "output_contract_pass_rate": 1.0,
                "scenario_pass_rate": 0.97,
                "unsafe_conflict_execution_count": 0,
            },
        },
    }
    return reports[name]


def _evaluation_evidence(
    tmp_path: Path,
    *,
    git_commit: str,
    unsafe_failures: int = 0,
    safety_case_count: int | None = None,
    retriever_high_relevance_recall: float = 0.99,
    retriever_graded_gain_recall: float = 0.97,
    retriever_answerable_case_count: int = 300,
    retriever_unanswerable_case_count: int = 100,
    retriever_high_relevance_fact_coverage: float = 0.99,
    retriever_fact_complete_lower: float = 0.96,
) -> dict[str, dict[str, Path]]:
    evidence_dir = tmp_path / "release-evidence"
    evidence_dir.mkdir()
    result: dict[str, dict[str, Path]] = {}
    for name, minimum in PILOT_QUALIFICATION_MINIMUM_CASES.items():
        case_count = (
            safety_case_count
            if name == "resolver_hint_safety" and safety_case_count is not None
            else minimum
        )
        dataset_path = evidence_dir / f"{name}.jsonl"
        report_path = evidence_dir / f"{name}.report.json"
        _write_reviewed_dataset(dataset_path, name, case_count)
        contract = audit_evaluation_dataset(dataset_path, "PILOT_REVIEWED")
        bind_evaluation_report(
            _report_payload(
                name,
                case_count,
                unsafe_failures=unsafe_failures
                if name == "resolver_hint_safety"
                else 0,
                retriever_high_relevance_recall=retriever_high_relevance_recall,
                retriever_graded_gain_recall=retriever_graded_gain_recall,
                retriever_answerable_case_count=retriever_answerable_case_count,
                retriever_unanswerable_case_count=retriever_unanswerable_case_count,
                retriever_high_relevance_fact_coverage=(
                    retriever_high_relevance_fact_coverage
                ),
                retriever_fact_complete_lower=retriever_fact_complete_lower,
            ),
            report_path=report_path,
            dataset_path=dataset_path,
            evaluation_contract=contract,
            profile="PILOT_REVIEWED",
            git_commit=git_commit,
        )
        result[name] = {
            "report_path": report_path,
            "dataset_path": dataset_path,
        }
    return result


def _create_attestation(
    path: Path,
    *,
    unsigned_manifest: dict,
    git_commit: str,
    signature_value: str | None = None,
) -> None:
    qualification = unsigned_manifest["evaluation_qualification"]
    attestation = {
        "schema_version": "chemicheck119-release-attestation-v1",
        "attestation_id": "ATT-TEST-001",
        "approval_status": "APPROVED",
        "profile": "PILOT_REVIEWED",
        "git_commit": git_commit,
        "quality_policy_sha256": qualification["evidence"]["quality_policy_sha256"],
        "data_registry_sha256": unsigned_manifest["data_governance"]["registry_sha256"],
        "evidence_digest": qualification["evidence_digest"],
        "issued_at_utc": "2026-01-01T00:00:00+00:00",
        "expires_at_utc": "2030-01-01T00:00:00+00:00",
        "reviewer": {
            "reviewer_id": "independent-reviewer",
            "organization": "test-review-board",
            "independence_statement": "모델 작성자와 다른 검수자입니다.",
        },
        "field_validation": {
            "status": "COMPLETED",
            "protocol_id": "FIELD-PILOT-V1",
            "evidence_reference": "TEST-EVIDENCE-ARCHIVE-001",
            "completed_at_utc": "2026-01-01T00:00:00+00:00",
        },
        "signature": {
            "algorithm": "HMAC-SHA256",
            "key_id": "test-release-review-key",
            "value": "",
        },
    }
    attestation["signature"]["value"] = (
        signature_value
        if signature_value is not None
        else release_attestation_signature(attestation, ATTESTATION_KEY)
    )
    path.write_text(
        json.dumps(attestation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _qualified_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    git_commit: str = "a" * 40,
    redistribution_approved: bool = True,
    unsafe_failures: int = 0,
    safety_case_count: int | None = None,
    retriever_high_relevance_recall: float = 0.99,
    retriever_graded_gain_recall: float = 0.97,
    retriever_answerable_case_count: int = 300,
    retriever_unanswerable_case_count: int = 100,
    retriever_high_relevance_fact_coverage: float = 0.99,
    retriever_fact_complete_lower: float = 0.96,
    valid_signature: bool = True,
    resolver_schema_version: str = RESOLVER_SCHEMA_VERSION,
) -> tuple[dict, Path, Path, Path, Path]:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(
        tmp_path,
        redistribution_approved=redistribution_approved,
        resolver_schema_version=resolver_schema_version,
    )
    evidence = _evaluation_evidence(
        tmp_path,
        git_commit=git_commit,
        unsafe_failures=unsafe_failures,
        safety_case_count=safety_case_count,
        retriever_high_relevance_recall=retriever_high_relevance_recall,
        retriever_graded_gain_recall=retriever_graded_gain_recall,
        retriever_answerable_case_count=retriever_answerable_case_count,
        retriever_unanswerable_case_count=retriever_unanswerable_case_count,
        retriever_high_relevance_fact_coverage=retriever_high_relevance_fact_coverage,
        retriever_fact_complete_lower=retriever_fact_complete_lower,
    )
    unsigned = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit=git_commit,
        evaluation_evidence=evidence,
    )
    attestation_path = tmp_path / "release-attestation.json"
    _create_attestation(
        attestation_path,
        unsigned_manifest=unsigned,
        git_commit=git_commit,
        signature_value="0" * 64 if not valid_signature else None,
    )
    monkeypatch.setenv(RELEASE_ATTESTATION_KEY_ENV_VAR, ATTESTATION_KEY)
    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit=git_commit,
        evaluation_evidence=evidence,
        release_attestation_path=attestation_path,
    )
    return created, db_path, resolver_path, retriever_path, config_dir


def test_release_manifest_verifies_bound_reviewed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "a" * 40)

    verified = verify_runtime_release(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        environment="production",
        expected_manifest_sha256=created["manifest_sha256"],
    )

    assert verified["status"] == "VERIFIED"
    assert verified["manifest_contract"]["release_attestation_verified"] is True
    assert set(verified["artifacts"]) == {"database", "resolver", "retriever"}
    assert all(item["sha256_verified"] for item in verified["artifacts"].values())
    unsafe = created["evaluation_qualification"]["quality_gate"][
        "unsafe_cas_auto_confirmation"
    ]
    assert unsafe["total_failures"] == 0
    assert unsafe["one_sided_upper_rate"] <= 0.01


def test_release_manifest_accepts_incident_adapted_resolver_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path,
        monkeypatch,
        resolver_schema_version=INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION,
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "a" * 40)

    verified = verify_runtime_release(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        environment="production",
        expected_manifest_sha256=created["manifest_sha256"],
    )

    assert verified["manifest_contract"]["resolver_schema_version"] == (
        INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION
    )


def test_production_runtime_does_not_receive_attestation_signing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path, monkeypatch
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "a" * 40)
    monkeypatch.delenv(RELEASE_ATTESTATION_KEY_ENV_VAR)

    verified = verify_runtime_release(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        environment="production",
        expected_manifest_sha256=created["manifest_sha256"],
    )

    assert verified["status"] == "VERIFIED"
    assert verified["manifest_sha256_verified"] is True
    assert verified["manifest_contract"]["release_attestation_verified"] is True


def test_preissued_attestation_survives_nongating_report_runtime_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI 재실행의 timestamp·latency 차이가 사전 검수 서명을 깨지 않아야 한다."""

    git_commit = "9" * 40
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    evidence = _evaluation_evidence(tmp_path, git_commit=git_commit)
    unsigned = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit=git_commit,
        evaluation_evidence=evidence,
    )
    attestation_path = tmp_path / "release-attestation.json"
    _create_attestation(
        attestation_path,
        unsigned_manifest=unsigned,
        git_commit=git_commit,
    )

    relocated_evidence: dict[str, dict[str, Path]] = {}
    relocated_dir = tmp_path / "runner-release-evidence"
    relocated_dir.mkdir()
    for name, paths in evidence.items():
        dataset_path = relocated_dir / paths["dataset_path"].name
        report_path = relocated_dir / paths["report_path"].name
        shutil.copy2(paths["dataset_path"], dataset_path)
        shutil.copy2(paths["report_path"], report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["generated_at_utc"] = "2029-01-01T00:00:00+00:00"
        report["latency_ms"] = {"mean": 123.45, "p95": 456.78}
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        relocated_evidence[name] = {
            "report_path": report_path,
            "dataset_path": dataset_path,
        }

    monkeypatch.setenv(RELEASE_ATTESTATION_KEY_ENV_VAR, ATTESTATION_KEY)
    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit=git_commit,
        evaluation_evidence=relocated_evidence,
        release_attestation_path=attestation_path,
    )

    qualification = created["evaluation_qualification"]
    assert qualification["passed"] is True
    assert (
        qualification["evidence_digest"]
        == unsigned["evaluation_qualification"]["evidence_digest"]
    )
    assert all(
        item["report_digest_kind"] == "NORMALIZED_RELEASE_EVIDENCE_V1"
        for item in qualification["evidence"]["evaluations"].values()
    )


def test_production_rejects_manifest_without_external_trust_anchor(
    tmp_path: Path,
) -> None:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
    )

    with pytest.raises(RuntimeIntegrityError, match="RUNTIME_MANIFEST_SHA256"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
        )


def test_staging_rejects_manifest_without_external_trust_anchor(
    tmp_path: Path,
) -> None:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
    )

    with pytest.raises(RuntimeIntegrityError, match="RUNTIME_MANIFEST_SHA256"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="staging",
        )


def test_runtime_verifier_rejects_unknown_environment_before_file_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeIntegrityError, match="지원하지 않는 배포 환경"):
        verify_runtime_release(
            db_path=tmp_path / "missing.sqlite",
            resolver_model_path=tmp_path / "missing-resolver.joblib",
            retriever_model_path=tmp_path / "missing-retriever.joblib",
            config_dir=tmp_path / "missing-config",
            environment="prod",
        )


def test_tampered_joblib_is_blocked_before_runtime_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path, monkeypatch, git_commit="c" * 40
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "c" * 40)
    resolver_path.write_bytes(resolver_path.read_bytes() + b"tampered")

    with pytest.raises(RuntimeIntegrityError, match="resolver 파일 크기 불일치"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=created["manifest_sha256"],
        )


def test_retrusted_manifest_with_wrong_model_contract_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path, monkeypatch, git_commit="b" * 40
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "b" * 40)
    manifest_path = Path(created["manifest_path"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"]["resolver"]["model_schema_version"] = "wrong-schema"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeIntegrityError, match="코드 계약 불일치"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=sha256(manifest_path.read_bytes()).hexdigest(),
        )


def test_external_git_commit_trust_anchor_must_match_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path, monkeypatch, git_commit="d" * 40
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "e" * 40)

    with pytest.raises(RuntimeIntegrityError, match="git_commit_trust_anchor"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=created["manifest_sha256"],
        )


def test_inline_self_attested_qualification_cannot_be_promoted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit="a" * 40,
    )
    manifest_path = Path(created["manifest_path"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["evaluation_qualification"] = {
        "passed": True,
        "claim_scope": "PILOT_REVIEWED",
        "field_validated": True,
        "datasets": {
            name: {"case_count": count}
            for name, count in PILOT_QUALIFICATION_MINIMUM_CASES.items()
        },
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "a" * 40)
    monkeypatch.setenv(RELEASE_ATTESTATION_KEY_ENV_VAR, ATTESTATION_KEY)

    with pytest.raises(RuntimeIntegrityError, match="evaluation_qualification"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=sha256(manifest_path.read_bytes()).hexdigest(),
        )


def test_production_rejects_invalid_release_attestation_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path, monkeypatch, valid_signature=False
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "a" * 40)
    assert created["evaluation_qualification"]["passed"] is False

    with pytest.raises(RuntimeIntegrityError, match="evaluation_qualification"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=created["manifest_sha256"],
        )


def test_production_rejects_unsafe_cas_auto_confirmation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path, monkeypatch, unsafe_failures=1
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "a" * 40)
    gate = created["evaluation_qualification"]["quality_gate"]
    assert gate["unsafe_cas_auto_confirmation"]["total_failures"] == 1
    assert gate["unsafe_cas_auto_confirmation"]["passed"] is False

    with pytest.raises(RuntimeIntegrityError, match="evaluation_qualification"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=created["manifest_sha256"],
        )


def test_release_rejects_low_high_relevance_retrieval_recall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path,
        monkeypatch,
        retriever_high_relevance_recall=0.97,
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "a" * 40)
    qualification = created["evaluation_qualification"]

    assert qualification["passed"] is False
    assert {
        "code": "QUALITY_THRESHOLD_NOT_MET",
        "evaluation": "retriever_sections",
        "metric": "metrics.high_relevance_recall_at_k",
    } in qualification["quality_gate"]["blockers"]

    with pytest.raises(RuntimeIntegrityError, match="evaluation_qualification"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=created["manifest_sha256"],
        )


@pytest.mark.parametrize(
    ("overrides", "blocked_metric"),
    [
        (
            {"retriever_graded_gain_recall": 0.94},
            "metrics.graded_gain_recall_at_k",
        ),
        (
            {"retriever_high_relevance_fact_coverage": 0.97},
            "metrics.high_relevance_fact_coverage_at_k",
        ),
        (
            {"retriever_fact_complete_lower": 0.94},
            "uncertainty.high_relevance_fact_complete_case_rate_at_k.lower",
        ),
    ],
)
def test_release_rejects_weak_retrieval_evidence_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, float],
    blocked_metric: str,
) -> None:
    created, *_ = _qualified_manifest(tmp_path, monkeypatch, **overrides)

    assert created["evaluation_qualification"]["passed"] is False
    assert {
        "code": "QUALITY_THRESHOLD_NOT_MET",
        "evaluation": "retriever_sections",
        "metric": blocked_metric,
    } in created["evaluation_qualification"]["quality_gate"]["blockers"]


def test_release_rejects_one_answerable_plus_many_unanswerable_cases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, *_ = _qualified_manifest(
        tmp_path,
        monkeypatch,
        retriever_answerable_case_count=1,
        retriever_unanswerable_case_count=399,
    )

    assert created["evaluation_qualification"]["passed"] is False
    assert {
        "code": "QUALITY_THRESHOLD_NOT_MET",
        "evaluation": "retriever_sections",
        "metric": "answerable_case_count",
    } in created["evaluation_qualification"]["quality_gate"]["blockers"]


def test_release_rejects_inconsistent_answerability_partition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, *_ = _qualified_manifest(
        tmp_path,
        monkeypatch,
        retriever_answerable_case_count=300,
        retriever_unanswerable_case_count=101,
    )

    assert created["evaluation_qualification"]["passed"] is False
    assert {
        "code": "EVALUATION_CASE_PARTITION_MISMATCH",
        "evaluation": "retriever_sections",
        "case_count": 400,
        "answerable_case_count": 300,
        "unanswerable_case_count": 101,
    } in created["evaluation_qualification"]["quality_gate"]["blockers"]


def test_zero_failures_with_too_few_cases_fails_ci_upper_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, db_path, resolver_path, retriever_path, config_dir = _qualified_manifest(
        tmp_path, monkeypatch, safety_case_count=298
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "a" * 40)
    unsafe = created["evaluation_qualification"]["quality_gate"][
        "unsafe_cas_auto_confirmation"
    ]
    assert unsafe["total_failures"] == 0
    assert unsafe["one_sided_upper_rate"] > 0.01
    assert unsafe["passed"] is False

    with pytest.raises(RuntimeIntegrityError, match="evaluation_qualification"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=created["manifest_sha256"],
        )


def test_production_rejects_missing_required_data_source(
    tmp_path: Path,
) -> None:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    registry_path = config_dir / DATA_SOURCE_REGISTRY_FILE
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["sources"] = registry["sources"][:-1]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit="a" * 40,
    )

    assert created["data_governance"]["public_container_redistribution_ready"] is False
    assert created["data_governance"]["missing_required_source_ids"]


def test_bundle_build_uses_non_persistent_attestation_secret_mount() -> None:
    dockerfile = (CONFIG_DIR.parent / "Dockerfile.bundle").read_text(encoding="utf-8")

    assert "--mount=type=secret,id=release_attestation_key,required=true" in dockerfile
    assert (
        'CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY="$(cat '
        '/run/secrets/release_attestation_key)"'
    ) in dockerfile
    assert "ARG CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY" not in dockerfile
    assert "ENV CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY" not in dockerfile


def test_compose_is_local_development_and_does_not_receive_signing_key() -> None:
    compose = (CONFIG_DIR.parent / "compose.yaml").read_text(encoding="utf-8")

    assert "CHEMIGUARD119_ENVIRONMENT: development" in compose
    assert "CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY:" not in compose


def test_release_workflow_keeps_signing_key_out_of_runtime_and_records_digest() -> None:
    workflow = (
        CONFIG_DIR.parent / ".github" / "workflows" / "release-model.yml"
    ).read_text(encoding="utf-8")

    assert '--env "CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY=' not in workflow
    assert r"(?:[0-9a-fA-F]{64}|[A-Za-z0-9_-]{43})" in workflow
    assert "digest_reference=" in workflow
    assert "배포 고정 digest" in workflow
    assert "google-github-actions/auth@v3" in workflow
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in workflow
    assert "Artifact Registry digest" in workflow
    assert "service_account_key" not in workflow.lower()


def test_release_workflow_adopts_incident_adapted_resolver_before_manifest() -> None:
    workflow = (
        CONFIG_DIR.parent / ".github" / "workflows" / "release-model.yml"
    ).read_text(encoding="utf-8")

    assert '"07_울산소방_화학사고별_유해물질판단.csv"' in workflow
    assert "--incident-adaptation-csv" in workflow
    assert workflow.index("--incident-adaptation-csv") < workflow.index(
        "Runtime manifest SHA-256 추출"
    )


def test_cloud_run_workflow_uses_oidc_and_staging_environment() -> None:
    workflow_path = CONFIG_DIR.parent / ".github" / "workflows" / "deploy-cloud-run.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "id-token: write" in workflow
    assert "environment: staging" in workflow
    assert "google-github-actions/auth@v3" in workflow
    assert "google-github-actions/setup-gcloud@v3" in workflow
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in workflow
    assert "CHEMIGUARD119_API_KEY: ${{ secrets.CHEMIGUARD119_API_KEY }}" in workflow
    assert "service_account_key" not in workflow.lower()
    assert "cancel-in-progress: false" in workflow


def test_cloud_run_script_smokes_before_traffic_and_rolls_back() -> None:
    script_path = (
        CONFIG_DIR.parent / "scripts" / "deployment" / "deploy_cloud_run_blue_green.sh"
    )
    script = script_path.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(script_path)], check=True)
    no_traffic = script.index("--no-traffic")
    candidate_smoke = script.index('smoke "$candidate_url"')
    promote = script.index('--to-revisions "$candidate_revision=100"')
    post_promote_smoke = script.index('smoke "$service_url"')

    assert no_traffic < candidate_smoke < promote < post_promote_smoke
    assert '--to-revisions "$previous_revision=100"' in script
    assert 'minimum_instances="${GCP_MIN_INSTANCES:-0}"' in script
    assert "IMAGE_DIGEST" in script and "@sha256:" in script
    assert "gcloud run revisions describe" in script
    assert 'test "$deployed_image" = "$IMAGE_DIGEST"' in script or (
        'if [ "$deployed_image" != "$IMAGE_DIGEST" ]' in script
    )
    assert "GCP_MODEL_API_KEY_SECRET_VERSION" in script
    assert "CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY" not in script
    assert "CHEMIGUARD119_RAG_MODE=extractive" in script
    assert "--readiness-probe" not in script
    assert '--startup-probe="httpGet.path=/health/ready' in script
    assert "--no-allow-unauthenticated" in script
    assert "--invoker-iam-check" in script
    assert 'local identity_token="$CHEMICHECK119_MODEL_API_ID_TOKEN"' in script
    assert 'test "$service_url" = "$GCP_CLOUD_RUN_URL"' in script
    assert 'smoke "$candidate_url"' in script
    assert 'smoke "$service_url"' in script
    assert "gcloud auth print-identity-token" not in script


def test_competition_preview_is_explicitly_non_production_and_keyless() -> None:
    project_root = CONFIG_DIR.parent
    dockerfile = (project_root / "Dockerfile.preview").read_text(encoding="utf-8")
    cloudbuild = (project_root / "cloudbuild.preview.yaml").read_text(encoding="utf-8")
    script_path = project_root / "scripts" / "deployment" / "build_cloud_run_preview.sh"
    script = script_path.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(script_path)], check=True)
    assert "CHEMIGUARD119_ENVIRONMENT=development" in dockerfile
    assert "CHEMIGUARD119_API_KEY" not in dockerfile
    assert "RELEASE_ATTESTATION_HMAC_KEY" not in dockerfile
    assert "Dockerfile.preview" in cloudbuild
    assert "MODEL_GIT_COMMIT=${_MODEL_GIT_COMMIT}" in cloudbuild
    assert "test -z" in script and "status --porcelain" in script
    assert "runtime_manifest.json" in script
    assert "gcloud builds submit" in script
    assert "digest_reference" in script
    assert "validate_manifest_runtime_versions.py" in script


def test_preview_runtime_version_preflight_rejects_dependency_drift(
    tmp_path: Path,
) -> None:
    project_root = CONFIG_DIR.parent
    validator = (
        project_root
        / "scripts"
        / "deployment"
        / "validate_manifest_runtime_versions.py"
    )
    manifest = tmp_path / "runtime_manifest.json"
    requirements = tmp_path / "requirements-production.txt"
    dockerfile = tmp_path / "Dockerfile.preview"
    requirements.write_text(
        "numpy==2.4.6\nscikit-learn==1.9.0\njoblib==1.5.3\n",
        encoding="utf-8",
    )
    dockerfile.write_text("FROM python:3.11.15-slim\n", encoding="utf-8")

    manifest.write_text(
        json.dumps(
            {
                "git_commit": "a" * 40,
                "runtime_versions": {
                    "python": "3.11.15",
                    "numpy": "2.4.6",
                    "scikit_learn": "1.9.0",
                    "joblib": "1.5.3",
                },
            }
        ),
        encoding="utf-8",
    )
    valid = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--manifest",
            str(manifest),
            "--requirements",
            str(requirements),
            "--dockerfile",
            str(dockerfile),
            "--expected-git-commit",
            "a" * 40,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert valid.returncode == 0
    assert json.loads(valid.stdout)["status"] == "VERIFIED"

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["runtime_versions"]["joblib"] = "1.6.0"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    invalid = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--manifest",
            str(manifest),
            "--requirements",
            str(requirements),
            "--dockerfile",
            str(dockerfile),
            "--expected-git-commit",
            "a" * 40,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert invalid.returncode == 1
    result = json.loads(invalid.stdout)
    assert result["status"] == "MISMATCH"
    assert result["mismatches"]["joblib"] == {
        "manifest": "1.6.0",
        "serving": "1.5.3",
    }

    payload["runtime_versions"]["joblib"] = "1.5.3"
    payload["git_commit"] = "b" * 40
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    wrong_commit = subprocess.run(
        [
            sys.executable,
            str(validator),
            "--manifest",
            str(manifest),
            "--requirements",
            str(requirements),
            "--dockerfile",
            str(dockerfile),
            "--expected-git-commit",
            "a" * 40,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert wrong_commit.returncode == 1
    assert json.loads(wrong_commit.stdout)["mismatches"]["git_commit"] == {
        "manifest": "b" * 40,
        "serving": "a" * 40,
    }


def test_preview_build_requires_model_commit_to_match_clean_head() -> None:
    project_root = CONFIG_DIR.parent
    script = (
        project_root / "scripts" / "deployment" / "build_cloud_run_preview.sh"
    ).read_text(encoding="utf-8")

    assert 'current_git_commit="$(git -C "$project_root" rev-parse HEAD)"' in script
    assert 'test "$MODEL_GIT_COMMIT" = "$current_git_commit"' in script
    assert '--expected-git-commit "$MODEL_GIT_COMMIT"' in script


def test_competition_preview_deploy_is_separate_and_fail_closed() -> None:
    project_root = CONFIG_DIR.parent
    workflow_path = (
        project_root / ".github" / "workflows" / "deploy-preview-cloud-run.yml"
    )
    script_path = (
        project_root
        / "scripts"
        / "deployment"
        / "deploy_cloud_run_preview_blue_green.sh"
    )
    workflow = workflow_path.read_text(encoding="utf-8")
    script = script_path.read_text(encoding="utf-8")

    subprocess.run(["bash", "-n", str(script_path)], check=True)
    assert "id-token: write" in workflow
    assert "environment: staging" in workflow
    assert "GCP_PREVIEW_CLOUD_RUN_SERVICE" in workflow
    assert "GCP_PREVIEW_CLOUD_RUN_URL" in workflow
    assert "google-github-actions/auth@v3" in workflow
    assert "model-api-preview@sha256:" in workflow
    assert "service_account_key" not in workflow.lower()
    assert "deploy_cloud_run_preview_blue_green.sh" in workflow
    assert "CHEMIGUARD119_ENVIRONMENT=development" in script
    assert "CHEMIGUARD119_RELEASE_TIER=competition-preview" in script
    assert "model-api-preview@sha256:" in script
    assert "--no-allow-unauthenticated" in script
    assert "--invoker-iam-check" in script
    assert "token_format: id_token" in workflow
    assert "id_token_audience:" in workflow
    assert "steps.auth.outputs.id_token" in workflow
    assert 'local identity_token="$CHEMICHECK119_MODEL_API_ID_TOKEN"' in script
    assert 'test "$service_url" = "$GCP_CLOUD_RUN_URL"' in script
    assert 'smoke "$candidate_url"' in script
    assert "gcloud auth print-identity-token" not in script
    assert '--to-revisions "$candidate_revision=100"' in script
    assert '--to-revisions "$previous_revision=100"' in script
    assert 'payload.get("expert_reviewed") is not False' in script
    assert 'payload.get("release_tier") != "competition-preview"' in script
    assert 'payload.get("decision_support_only") is not True' in script
    assert "NATIONWIDE_KOREA_HISTORICAL_CANDIDATES" in script
    assert 'candidate_tag="p${RELEASE_GIT_COMMIT:0:7}${run_attempt}"' in script
    assert "CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY" not in script
    assert "--readiness-probe" not in script
    assert "--startup-probe=httpGet.path=/health/ready" in script


def test_cloud_run_service_tier_validation_rejects_mixed_boundaries() -> None:
    project_root = CONFIG_DIR.parent
    validator = (
        project_root / "scripts" / "deployment" / "validate_cloud_run_service_tier.sh"
    )
    preview_image = (
        "asia-northeast3-docker.pkg.dev/chemi-check/chemicheck119/"
        f"model-api-preview@sha256:{'a' * 64}"
    )
    staging_image = (
        "asia-northeast3-docker.pkg.dev/chemi-check/chemicheck119/"
        f"model-api@sha256:{'b' * 64}"
    )

    subprocess.run(["bash", "-n", str(validator)], check=True)
    subprocess.run(
        [
            str(validator),
            "competition-preview",
            "chemicheck119-model-api-preview",
            "development",
            preview_image,
        ],
        check=True,
    )
    subprocess.run(
        [
            str(validator),
            "reviewed-staging",
            "chemicheck119-model-api-staging",
            "staging",
            staging_image,
        ],
        check=True,
    )

    invalid_cases = (
        (
            "competition-preview",
            "chemicheck119-model-api-staging",
            "development",
            preview_image,
        ),
        (
            "competition-preview",
            "chemicheck119-model-api-preview",
            "staging",
            preview_image,
        ),
        (
            "reviewed-staging",
            "chemicheck119-model-api-staging",
            "staging",
            preview_image,
        ),
    )
    for invalid_case in invalid_cases:
        assert (
            subprocess.run([str(validator), *invalid_case], check=False).returncode != 0
        )


def test_reviewed_staging_deploy_checks_runtime_tier() -> None:
    project_root = CONFIG_DIR.parent
    script = (
        project_root / "scripts" / "deployment" / "deploy_cloud_run_blue_green.sh"
    ).read_text(encoding="utf-8")

    assert "CHEMIGUARD119_RELEASE_TIER=reviewed-staging" in script
    assert 'payload.get("deployment_environment") != "staging"' in script
    assert 'payload.get("release_tier") != "reviewed-staging"' in script
