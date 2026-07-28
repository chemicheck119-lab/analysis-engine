from __future__ import annotations

import json
import shutil
import sqlite3
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
from chemiguard119.resolver import MODEL_SCHEMA_VERSION as RESOLVER_SCHEMA_VERSION
from chemiguard119.retrieval import MODEL_SCHEMA_VERSION as RETRIEVER_SCHEMA_VERSION


ATTESTATION_KEY = "test-only-release-attestation-key-" + "x" * 32


def _runtime_fixture(
    tmp_path: Path,
    *,
    redistribution_approved: bool = True,
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
            "schema_version": RESOLVER_SCHEMA_VERSION,
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


def _report_payload(name: str, case_count: int, *, unsafe_failures: int) -> dict:
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
            "metrics_version": "retriever-section-qrel-v2",
            "case_count": case_count,
            "metrics": {
                "mrr_at_k": 0.95,
                "recall_at_k": 0.95,
                "valid_source_url_coverage_at_k": 1.0,
                "wrong_cas_rate_at_k": 0.0,
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
    valid_signature: bool = True,
) -> tuple[dict, Path, Path, Path, Path]:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(
        tmp_path,
        redistribution_approved=redistribution_approved,
    )
    evidence = _evaluation_evidence(
        tmp_path,
        git_commit=git_commit,
        unsafe_failures=unsafe_failures,
        safety_case_count=safety_case_count,
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
