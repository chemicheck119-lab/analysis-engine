"""모델 artifact의 버전·해시를 고정하는 배포 manifest."""

from __future__ import annotations

import json
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import joblib
import numpy as np
import sklearn

from chemiguard119 import __version__
from chemiguard119.database import connect_readonly
from chemiguard119.utils import require_materialized_files, sha256_file, write_json


RUNTIME_MANIFEST_SCHEMA_VERSION = "chemicheck119-runtime-release-v2"
RUNTIME_MANIFEST_FILE = "runtime_manifest.json"
SERVICE_ID = "chemicheck119-model-api"
REQUIRED_CONFIG_FILES = (
    "cameo_crosswalk.csv",
    "conflict_policy.json",
    "pair_rules.csv",
    "substance_overrides.csv",
)


class RuntimeIntegrityError(RuntimeError):
    """배포 artifact 신뢰성 또는 구조 검증 실패."""


def _file_entry(path: Path, **metadata: Any) -> dict[str, Any]:
    return {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        **metadata,
    }


def _database_summary(db_path: Path) -> dict[str, Any]:
    required_tables = {
        "substance",
        "alias",
        "evidence",
        "cameo_chemical",
        "cameo_mapping",
        "compatibility",
        "facility_candidate",
    }
    with connect_readonly(db_path) as connection:
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        missing = sorted(required_tables - tables)
        if integrity != "ok" or missing:
            raise RuntimeIntegrityError(
                f"SQLite 구조 검증 실패: quick_check={integrity}, missing_tables={missing}"
            )
        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in sorted(required_tables)
        }
    return {"quick_check": integrity, "required_table_counts": counts}


def create_runtime_manifest(
    *,
    db_path: Path,
    resolver_model_path: Path,
    retriever_model_path: Path,
    config_dir: Path,
    output_path: Path | None = None,
    git_commit: str | None = None,
) -> dict[str, Any]:
    """신뢰된 빌드 단계에서 실행할 배포 manifest를 만든다."""

    config_paths = [config_dir / name for name in REQUIRED_CONFIG_FILES]
    required_paths = [db_path, resolver_model_path, retriever_model_path, *config_paths]
    require_materialized_files(required_paths)

    # 이 함수는 신뢰된 학습/릴리스 단계에서만 실행한다. 운영 서버에서는 아래
    # joblib 파일을 load하기 전에 verify_runtime_release가 먼저 해시를 확인한다.
    resolver_artifact = joblib.load(resolver_model_path)
    retriever_artifact = joblib.load(retriever_model_path)
    database = _database_summary(db_path)
    manifest = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "service": SERVICE_ID,
        "package_version": __version__,
        "git_commit": git_commit or os.getenv("CHEMIGUARD119_GIT_COMMIT") or "UNKNOWN",
        "runtime_versions": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scikit_learn": sklearn.__version__,
            "joblib": joblib.__version__,
        },
        "artifacts": {
            "database": _file_entry(db_path, **database),
            "resolver": _file_entry(
                resolver_model_path,
                model_schema_version=resolver_artifact.get("schema_version"),
                task=resolver_artifact.get("task"),
            ),
            "retriever": _file_entry(
                retriever_model_path,
                model_schema_version=retriever_artifact.get("schema_version"),
                task=retriever_artifact.get("task"),
            ),
        },
        "config_files": {path.name: _file_entry(path) for path in config_paths},
        "security": {
            "joblib_is_pickle_based": True,
            "verify_manifest_and_file_hashes_before_joblib_load": True,
            "production_requires_manifest_sha256_trust_anchor": True,
            "production_requires_git_commit_trust_anchor": True,
        },
    }
    destination = output_path or db_path.parent / RUNTIME_MANIFEST_FILE
    write_json(destination, manifest)
    return {
        **manifest,
        "manifest_path": str(destination),
        "manifest_sha256": sha256_file(destination),
    }


def _verify_entry(
    label: str,
    path: Path,
    entry: Mapping[str, Any],
) -> dict[str, Any]:
    if entry.get("filename") != path.name:
        raise RuntimeIntegrityError(f"{label} filename 불일치")
    actual_size = path.stat().st_size
    if int(entry.get("bytes", -1)) != actual_size:
        raise RuntimeIntegrityError(f"{label} 파일 크기 불일치")
    expected_hash = str(entry.get("sha256") or "").lower()
    if len(expected_hash) != 64 or sha256_file(path).lower() != expected_hash:
        raise RuntimeIntegrityError(f"{label} SHA-256 불일치")
    return {"filename": path.name, "bytes": actual_size, "sha256_verified": True}


def _verify_runtime_versions(recorded: Mapping[str, Any]) -> dict[str, Any]:
    actual = {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scikit_learn": sklearn.__version__,
        "joblib": joblib.__version__,
    }
    mismatches: dict[str, dict[str, str]] = {}
    for name in ("numpy", "scikit_learn", "joblib"):
        expected = str(recorded.get(name) or "")
        if expected != actual[name]:
            mismatches[name] = {"expected": expected, "actual": actual[name]}
    expected_python = str(recorded.get("python") or "")
    if expected_python.split(".")[:2] != actual["python"].split(".")[:2]:
        mismatches["python"] = {"expected": expected_python, "actual": actual["python"]}
    if mismatches:
        raise RuntimeIntegrityError(f"학습·서빙 런타임 버전 불일치: {mismatches}")
    return {"status": "MATCHED", **actual}


def _verify_manifest_contract(
    payload: Mapping[str, Any],
    *,
    production: bool,
) -> dict[str, Any]:
    """파일을 역직렬화하기 전에 코드와 manifest의 정적 계약을 비교한다."""

    from chemiguard119.resolver import MODEL_SCHEMA_VERSION as RESOLVER_SCHEMA_VERSION
    from chemiguard119.retrieval import MODEL_SCHEMA_VERSION as RETRIEVER_SCHEMA_VERSION

    expected_top_level = {
        "service": SERVICE_ID,
        "package_version": __version__,
    }
    mismatches: dict[str, dict[str, Any]] = {}
    for field, expected in expected_top_level.items():
        actual = payload.get(field)
        if actual != expected:
            mismatches[field] = {"expected": expected, "actual": actual}

    artifact_entries = payload.get("artifacts") or {}
    expected_models = {
        "resolver": {
            "model_schema_version": RESOLVER_SCHEMA_VERSION,
            "task": "substance_candidate_retrieval",
        },
        "retriever": {
            "model_schema_version": RETRIEVER_SCHEMA_VERSION,
            "task": "official_evidence_retrieval",
        },
    }
    for model_name, fields in expected_models.items():
        entry = artifact_entries.get(model_name) or {}
        for field, expected in fields.items():
            actual = entry.get(field)
            if actual != expected:
                mismatches[f"artifacts.{model_name}.{field}"] = {
                    "expected": expected,
                    "actual": actual,
                }

    git_commit = str(payload.get("git_commit") or "")
    if production and re.fullmatch(r"[0-9a-fA-F]{40}", git_commit) is None:
        mismatches["git_commit"] = {
            "expected": "40자리 Git commit SHA",
            "actual": git_commit,
        }
    expected_git_commit = (os.getenv("CHEMIGUARD119_GIT_COMMIT") or "").strip()
    if production and re.fullmatch(r"[0-9a-fA-F]{40}", expected_git_commit) is None:
        mismatches["git_commit_trust_anchor"] = {
            "expected": "CHEMIGUARD119_GIT_COMMIT 40자리 Git commit SHA",
            "actual": expected_git_commit,
        }
    elif expected_git_commit and git_commit != expected_git_commit:
        mismatches["git_commit_trust_anchor"] = {
            "expected": expected_git_commit,
            "actual": git_commit,
        }
    if mismatches:
        raise RuntimeIntegrityError(f"runtime manifest 코드 계약 불일치: {mismatches}")
    return {
        "status": "MATCHED",
        "service": SERVICE_ID,
        "package_version": __version__,
        "git_commit": git_commit,
        "resolver_schema_version": RESOLVER_SCHEMA_VERSION,
        "retriever_schema_version": RETRIEVER_SCHEMA_VERSION,
    }


def verify_runtime_release(
    *,
    db_path: Path,
    resolver_model_path: Path,
    retriever_model_path: Path,
    config_dir: Path,
    environment: str | None = None,
    manifest_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """운영에서 joblib.load 전에 manifest와 모든 파일 해시를 검증한다."""

    deployment_environment = (
        (environment or os.getenv("CHEMIGUARD119_ENVIRONMENT") or "development")
        .strip()
        .lower()
    )
    production = deployment_environment == "production"
    manifest = manifest_path or db_path.parent / RUNTIME_MANIFEST_FILE
    config_paths = [config_dir / name for name in REQUIRED_CONFIG_FILES]
    required_paths = [db_path, resolver_model_path, retriever_model_path, *config_paths]
    require_materialized_files(required_paths)

    if not manifest.is_file():
        if production:
            raise RuntimeIntegrityError(f"운영 배포 manifest가 없습니다: {manifest}")
        return {
            "status": "UNVERIFIED_DEVELOPMENT",
            "environment": deployment_environment,
            "manifest_path": str(manifest),
        }

    expected_manifest_hash = (
        (
            expected_manifest_sha256
            or os.getenv("CHEMIGUARD119_RUNTIME_MANIFEST_SHA256")
            or ""
        )
        .strip()
        .lower()
    )
    actual_manifest_hash = sha256_file(manifest).lower()
    if production and len(expected_manifest_hash) != 64:
        raise RuntimeIntegrityError(
            "운영 환경에는 CHEMIGUARD119_RUNTIME_MANIFEST_SHA256가 필요합니다."
        )
    if expected_manifest_hash and actual_manifest_hash != expected_manifest_hash:
        raise RuntimeIntegrityError("runtime manifest SHA-256 불일치")

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeIntegrityError("runtime manifest를 읽을 수 없습니다.") from error
    if payload.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise RuntimeIntegrityError("지원하지 않는 runtime manifest 버전입니다.")
    contract = _verify_manifest_contract(payload, production=production)

    artifact_entries = payload.get("artifacts") or {}
    verified_artifacts = {
        "database": _verify_entry(
            "database", db_path, artifact_entries.get("database") or {}
        ),
        "resolver": _verify_entry(
            "resolver", resolver_model_path, artifact_entries.get("resolver") or {}
        ),
        "retriever": _verify_entry(
            "retriever", retriever_model_path, artifact_entries.get("retriever") or {}
        ),
    }
    config_entries = payload.get("config_files") or {}
    verified_configs = {
        path.name: _verify_entry(
            f"config:{path.name}", path, config_entries.get(path.name) or {}
        )
        for path in config_paths
    }
    database = _database_summary(db_path)
    runtime_compatibility = _verify_runtime_versions(
        payload.get("runtime_versions") or {}
    )
    return {
        "status": "VERIFIED",
        "environment": deployment_environment,
        "manifest_path": str(manifest),
        "manifest_sha256_verified": bool(expected_manifest_hash),
        "manifest_sha256": actual_manifest_hash,
        "git_commit": payload.get("git_commit"),
        "runtime_versions": payload.get("runtime_versions"),
        "runtime_compatibility": runtime_compatibility,
        "manifest_contract": contract,
        "artifacts": verified_artifacts,
        "config_files": verified_configs,
        "database": database,
    }


__all__ = [
    "RUNTIME_MANIFEST_FILE",
    "RUNTIME_MANIFEST_SCHEMA_VERSION",
    "RuntimeIntegrityError",
    "create_runtime_manifest",
    "verify_runtime_release",
]
