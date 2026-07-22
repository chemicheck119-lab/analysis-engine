from __future__ import annotations

import sqlite3
import json
from hashlib import sha256
from pathlib import Path

import joblib
import pytest

from chemiguard119.release import (
    REQUIRED_CONFIG_FILES,
    RuntimeIntegrityError,
    create_runtime_manifest,
    verify_runtime_release,
)
from chemiguard119.resolver import MODEL_SCHEMA_VERSION as RESOLVER_SCHEMA_VERSION
from chemiguard119.retrieval import MODEL_SCHEMA_VERSION as RETRIEVER_SCHEMA_VERSION


def _runtime_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
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
        (config_dir / name).write_text("header\nvalue\n", encoding="utf-8")
    return db_path, resolver_path, retriever_path, config_dir


def test_release_manifest_verifies_every_runtime_file(
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
    assert verified["manifest_sha256_verified"] is True
    assert verified["git_commit"] == "a" * 40
    assert verified["manifest_contract"]["status"] == "MATCHED"
    assert set(verified["artifacts"]) == {"database", "resolver", "retriever"}
    assert all(item["sha256_verified"] for item in verified["artifacts"].values())


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


def test_tampered_joblib_is_blocked_by_hash_before_runtime_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit="c" * 40,
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
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit="b" * 40,
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "b" * 40)
    manifest_path = Path(created["manifest_path"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["artifacts"]["resolver"]["model_schema_version"] = "wrong-schema"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    retrusted_hash = sha256(manifest_path.read_bytes()).hexdigest()

    with pytest.raises(RuntimeIntegrityError, match="코드 계약 불일치"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=retrusted_hash,
        )


def test_external_git_commit_trust_anchor_must_match_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit="d" * 40,
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


def test_production_requires_external_git_commit_trust_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit="f" * 40,
    )
    monkeypatch.delenv("CHEMIGUARD119_GIT_COMMIT", raising=False)

    with pytest.raises(RuntimeIntegrityError, match="git_commit_trust_anchor"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=created["manifest_sha256"],
        )


def test_production_rejects_malformed_external_git_commit_trust_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path, resolver_path, retriever_path, config_dir = _runtime_fixture(tmp_path)
    created = create_runtime_manifest(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=config_dir,
        git_commit="f" * 40,
    )
    monkeypatch.setenv("CHEMIGUARD119_GIT_COMMIT", "not-a-commit")

    with pytest.raises(RuntimeIntegrityError, match="git_commit_trust_anchor"):
        verify_runtime_release(
            db_path=db_path,
            resolver_model_path=resolver_path,
            retriever_model_path=retriever_path,
            config_dir=config_dir,
            environment="production",
            expected_manifest_sha256=created["manifest_sha256"],
        )
