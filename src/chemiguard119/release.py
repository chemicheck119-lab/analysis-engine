"""모델 artifact의 버전·해시를 고정하는 배포 manifest."""

from __future__ import annotations

import json
import hashlib
import hmac
import math
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


RUNTIME_MANIFEST_SCHEMA_VERSION = "chemicheck119-runtime-release-v4"
RUNTIME_MANIFEST_FILE = "runtime_manifest.json"
SERVICE_ID = "chemicheck119-model-api"
DATA_SOURCE_REGISTRY_FILE = "data_source_registry.json"
RELEASE_QUALITY_POLICY_FILE = "release_quality_policy.json"
RELEASE_ATTESTATION_SCHEMA_FILE = "release_attestation.schema.json"
RELEASE_QUALITY_POLICY_SCHEMA_VERSION = "chemicheck119-release-quality-policy-v1"
RELEASE_QUALIFICATION_SCHEMA_VERSION = "chemicheck119-release-qualification-v1"
RELEASE_REPORT_BINDING_SCHEMA_VERSION = "chemicheck119-evaluation-report-binding-v1"
RELEASE_ATTESTATION_SCHEMA_VERSION = "chemicheck119-release-attestation-v1"
RELEASE_ATTESTATION_KEY_ENV_VAR = "CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY"
SUPPORTED_DEPLOYMENT_ENVIRONMENTS = frozenset(
    {"development", "test", "staging", "production"}
)
TRUSTED_DEPLOYMENT_ENVIRONMENTS = frozenset({"staging", "production"})
PILOT_RELEASE_POLICY_ID = "PILOT_RELEASE_V1"
PILOT_QUALIFICATION_MINIMUM_CASES = {
    "resolver": 1200,
    "resolver_hint_safety": 300,
    "retriever_sections": 400,
    "parser_locked": 400,
    "e2e_scenarios": 200,
}
PILOT_REQUIRED_DATA_SOURCE_IDS = frozenset(
    {
        "KOSHA_MSDS_OPEN_API",
        "NOAA_CAMEO_CHEMICALS",
        "NFA_ULSAN_CHEMICAL_INFORMATION",
        "ICIS_2024_HANDLING",
        "FACILITY_CANDIDATE_DERIVED",
    }
)
UNSAFE_CAS_CONFIDENCE_LEVEL = 0.95
UNSAFE_CAS_MAX_ONE_SIDED_UPPER_RATE = 0.01
REQUIRED_CONFIG_FILES = (
    "cameo_crosswalk.csv",
    "conflict_policy.json",
    "pair_rules.csv",
    "reference_assurance_registry.json",
    "substance_overrides.csv",
    DATA_SOURCE_REGISTRY_FILE,
    RELEASE_QUALITY_POLICY_FILE,
    RELEASE_ATTESTATION_SCHEMA_FILE,
)


class RuntimeIntegrityError(RuntimeError):
    """배포 artifact 신뢰성 또는 구조 검증 실패."""


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeIntegrityError(
            f"{label} JSON을 읽을 수 없습니다: {path}"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeIntegrityError(f"{label}에는 JSON 객체가 필요합니다: {path}")
    return payload


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _parse_utc(value: object, field: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeIntegrityError(f"{field}는 ISO-8601 시각이어야 합니다.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeIntegrityError(f"{field}에는 UTC offset이 필요합니다.")
    return parsed.astimezone(timezone.utc)


def _quality_policy(policy_path: Path) -> dict[str, Any]:
    policy = _read_json_object(policy_path, "release quality policy")
    if policy.get("schema_version") != RELEASE_QUALITY_POLICY_SCHEMA_VERSION:
        raise RuntimeIntegrityError("지원하지 않는 release quality policy 버전입니다.")
    if policy.get("policy_id") != PILOT_RELEASE_POLICY_ID:
        raise RuntimeIntegrityError("지원하지 않는 release quality policy id입니다.")
    if policy.get("profile") != "PILOT_REVIEWED":
        raise RuntimeIntegrityError(
            "release quality policy profile은 PILOT_REVIEWED여야 합니다."
        )

    required_sources = {
        str(value).strip() for value in policy.get("required_data_source_ids") or []
    }
    if required_sources != PILOT_REQUIRED_DATA_SOURCE_IDS:
        raise RuntimeIntegrityError(
            "release quality policy의 필수 data source 집합이 코드 안전 하한과 다릅니다."
        )

    evaluations = policy.get("required_evaluations")
    if not isinstance(evaluations, dict) or set(evaluations) != set(
        PILOT_QUALIFICATION_MINIMUM_CASES
    ):
        raise RuntimeIntegrityError(
            "release quality policy의 필수 평가 목록이 코드 안전 하한과 다릅니다."
        )
    for name, hard_minimum in PILOT_QUALIFICATION_MINIMUM_CASES.items():
        specification = evaluations.get(name)
        if not isinstance(specification, dict):
            raise RuntimeIntegrityError(f"{name}: 평가 정책은 객체여야 합니다.")
        minimum = specification.get("minimum_case_count")
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or minimum < hard_minimum
        ):
            raise RuntimeIntegrityError(
                f"{name}: minimum_case_count는 {hard_minimum} 이상이어야 합니다."
            )
        if not str(specification.get("metrics_version") or "").strip():
            raise RuntimeIntegrityError(f"{name}: metrics_version이 필요합니다.")
        thresholds = specification.get("thresholds")
        if not isinstance(thresholds, dict) or not thresholds:
            raise RuntimeIntegrityError(f"{name}: 성능 임계값이 필요합니다.")
        for metric, threshold in thresholds.items():
            if not str(metric).strip() or not isinstance(threshold, dict):
                raise RuntimeIntegrityError(f"{name}: 잘못된 성능 임계값입니다.")
            if threshold.get("operator") not in {"eq", "gte", "lte"}:
                raise RuntimeIntegrityError(
                    f"{name}.{metric}: operator는 eq/gte/lte 중 하나여야 합니다."
                )
            value = threshold.get("value")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise RuntimeIntegrityError(
                    f"{name}.{metric}: 임계값은 숫자여야 합니다."
                )

    unsafe_policy = policy.get("unsafe_cas_auto_confirmation")
    if not isinstance(unsafe_policy, dict):
        raise RuntimeIntegrityError("unsafe CAS 자동확정 정책이 필요합니다.")
    failure_metrics = {
        str(value).strip() for value in unsafe_policy.get("failure_metrics") or []
    }
    required_failure_metrics = {
        "unsafe_auto_hint_count",
        "wrong_cas_auto_hint_count",
        "resolver_rule_eligibility_violation_count",
    }
    confidence = unsafe_policy.get("confidence_level")
    upper_rate = unsafe_policy.get("maximum_one_sided_upper_rate")
    if (
        unsafe_policy.get("evaluation") != "resolver_hint_safety"
        or failure_metrics != required_failure_metrics
        or unsafe_policy.get("maximum_failures") != 0
        or not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or float(confidence) < UNSAFE_CAS_CONFIDENCE_LEVEL
        or not isinstance(upper_rate, (int, float))
        or isinstance(upper_rate, bool)
        or float(upper_rate) > UNSAFE_CAS_MAX_ONE_SIDED_UPPER_RATE
    ):
        raise RuntimeIntegrityError(
            "unsafe CAS 자동확정 정책이 코드의 0건 실패·95% CI 안전 하한보다 약합니다."
        )
    return policy


def _attestation_schema(schema_path: Path) -> dict[str, Any]:
    schema = _read_json_object(schema_path, "release attestation schema")
    if schema.get("x-schema-version") != RELEASE_ATTESTATION_SCHEMA_VERSION:
        raise RuntimeIntegrityError("지원하지 않는 release attestation schema입니다.")
    required = set(schema.get("required") or [])
    expected = {
        "schema_version",
        "attestation_id",
        "approval_status",
        "profile",
        "git_commit",
        "quality_policy_sha256",
        "data_registry_sha256",
        "evidence_digest",
        "issued_at_utc",
        "expires_at_utc",
        "reviewer",
        "field_validation",
        "signature",
    }
    if not expected.issubset(required):
        raise RuntimeIntegrityError(
            "release attestation schema의 required 필드가 부족합니다."
        )
    return schema


def _metric_value(payload: Mapping[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _threshold_passed(actual: Any, operator: str, expected: float) -> bool:
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return False
    if not math.isfinite(float(actual)):
        return False
    if operator == "eq":
        return float(actual) == float(expected)
    if operator == "gte":
        return float(actual) >= float(expected)
    return float(actual) <= float(expected)


def _zero_failure_upper_rate(case_count: int, confidence_level: float) -> float:
    """0건 실패일 때의 exact one-sided Clopper-Pearson 상한."""

    if case_count <= 0:
        return 1.0
    return 1.0 - (1.0 - confidence_level) ** (1.0 / case_count)


def bind_evaluation_report(
    report: Mapping[str, Any],
    *,
    report_path: Path,
    dataset_path: Path,
    evaluation_contract: Mapping[str, Any],
    profile: str,
    git_commit: str,
) -> dict[str, Any]:
    """같은 실행에서 생성한 evaluator 보고서를 dataset·profile·commit에 결합한다."""

    if re.fullmatch(r"[0-9a-fA-F]{40}", git_commit) is None:
        raise RuntimeIntegrityError(
            "평가 보고서 binding에는 40자리 Git commit이 필요합니다."
        )
    contract = dict(evaluation_contract)
    dataset_hash = sha256_file(dataset_path)
    if contract.get("dataset_sha256") != dataset_hash:
        raise RuntimeIntegrityError(
            "평가 계약의 dataset SHA-256이 실제 파일과 다릅니다."
        )
    if contract.get("profile") != profile:
        raise RuntimeIntegrityError(
            "평가 계약 profile이 release binding profile과 다릅니다."
        )
    bound = dict(report)
    bound["evaluation_contract"] = contract
    bound["release_binding"] = {
        "schema_version": RELEASE_REPORT_BINDING_SCHEMA_VERSION,
        "profile": profile,
        "claim_scope": contract.get("claim_scope"),
        "git_commit": git_commit,
        "dataset_sha256": dataset_hash,
    }
    write_json(report_path, bound)
    return bound


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


def _data_governance_summary(
    registry_path: Path,
    *,
    required_source_ids: frozenset[str] = PILOT_REQUIRED_DATA_SOURCE_IDS,
) -> dict[str, Any]:
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeIntegrityError(
            "data source registry를 읽을 수 없습니다."
        ) from error
    if payload.get("schema_version") != "chemicheck119-data-source-registry-v1":
        raise RuntimeIntegrityError("지원하지 않는 data source registry 버전입니다.")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise RuntimeIntegrityError("data source registry의 sources가 비어 있습니다.")
    allowed_statuses = {"APPROVED", "REVIEW_REQUIRED", "PROHIBITED"}
    source_ids: set[str] = set()
    status_counts: dict[str, int] = {}
    blockers: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise RuntimeIntegrityError("data source registry 항목은 객체여야 합니다.")
        source_id = str(source.get("source_id") or "").strip()
        status = str(source.get("redistribution_status") or "").strip().upper()
        if not source_id or source_id in source_ids:
            raise RuntimeIntegrityError(
                f"비어 있거나 중복된 data source id입니다: {source_id!r}"
            )
        if status not in allowed_statuses:
            raise RuntimeIntegrityError(
                f"{source_id}: 지원하지 않는 redistribution_status={status!r}"
            )
        for field in ("provider", "source_url", "terms_url", "review_basis"):
            if not str(source.get(field) or "").strip():
                raise RuntimeIntegrityError(f"{source_id}: {field}가 비어 있습니다.")
        runtime_artifacts = source.get("runtime_artifacts")
        if not isinstance(runtime_artifacts, list) or not all(
            str(value).strip() for value in runtime_artifacts
        ):
            raise RuntimeIntegrityError(
                f"{source_id}: runtime_artifacts 비어 있거나 형식이 잘못됐습니다."
            )
        if status == "APPROVED" and not str(source.get("reviewer") or "").strip():
            raise RuntimeIntegrityError(
                f"{source_id}: APPROVED에는 reviewer가 필요합니다."
            )
        source_ids.add(source_id)
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "APPROVED":
            blockers.append(
                {
                    "source_id": source_id,
                    "redistribution_status": status,
                    "reason": str(source.get("review_basis") or ""),
                }
            )
    missing_source_ids = sorted(required_source_ids - source_ids)
    if missing_source_ids:
        blockers.extend(
            {
                "source_id": source_id,
                "redistribution_status": "MISSING_FROM_REGISTRY",
                "reason": "runtime 필수 source가 registry에 없습니다.",
            }
            for source_id in missing_source_ids
        )
    return {
        "schema_version": payload["schema_version"],
        "registry_sha256": sha256_file(registry_path),
        "source_count": len(sources),
        "required_source_ids": sorted(required_source_ids),
        "registered_source_ids": sorted(source_ids),
        "missing_required_source_ids": missing_source_ids,
        "redistribution_status_counts": dict(sorted(status_counts.items())),
        "public_container_redistribution_ready": not blockers,
        "publish_blockers": blockers,
    }


def _evaluation_entry(
    name: str,
    specification: Mapping[str, Any],
    paths: Mapping[str, Any],
    *,
    profile: str,
    git_commit: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from chemiguard119.evaluation_contract import audit_evaluation_dataset

    report_path = Path(str(paths.get("report_path") or ""))
    dataset_path = Path(str(paths.get("dataset_path") or ""))
    blockers: list[dict[str, Any]] = []
    if not report_path.is_file() or not dataset_path.is_file():
        return (
            {
                "report_path": str(report_path),
                "dataset_path": str(dataset_path),
            },
            [
                {
                    "code": "MISSING_EVALUATION_EVIDENCE",
                    "evaluation": name,
                    "report_path": str(report_path),
                    "dataset_path": str(dataset_path),
                }
            ],
        )

    report = _read_json_object(report_path, f"{name} evaluator report")
    contract = audit_evaluation_dataset(dataset_path, profile)
    binding = report.get("release_binding")
    expected_binding = {
        "schema_version": RELEASE_REPORT_BINDING_SCHEMA_VERSION,
        "profile": profile,
        "claim_scope": contract.get("claim_scope"),
        "git_commit": git_commit,
        "dataset_sha256": contract.get("dataset_sha256"),
    }
    if binding != expected_binding:
        blockers.append(
            {
                "code": "EVALUATION_REPORT_BINDING_MISMATCH",
                "evaluation": name,
            }
        )
    report_contract = report.get("evaluation_contract")
    normalized_contract = {
        key: value for key, value in contract.items() if key != "dataset"
    }
    normalized_report_contract = (
        {key: value for key, value in report_contract.items() if key != "dataset"}
        if isinstance(report_contract, Mapping)
        else None
    )
    # dataset 파일의 절대/상대 경로는 검수자 환경과 CI 환경에서 달라질 수 있다.
    # 파일 내용은 dataset_sha256으로 고정하므로 경로 문자열만 비교·서명에서 제외한다.
    if normalized_report_contract != normalized_contract:
        blockers.append(
            {
                "code": "EVALUATION_CONTRACT_MISMATCH",
                "evaluation": name,
            }
        )

    case_count = report.get("case_count")
    if (
        not isinstance(case_count, int)
        or isinstance(case_count, bool)
        or case_count != contract.get("case_count")
    ):
        blockers.append(
            {
                "code": "EVALUATION_CASE_COUNT_MISMATCH",
                "evaluation": name,
                "report": case_count,
                "dataset": contract.get("case_count"),
            }
        )
        case_count = int(contract.get("case_count") or 0)

    expected_metrics_version = str(specification.get("metrics_version") or "")
    actual_metrics_version = str(report.get("metrics_version") or "")
    if actual_metrics_version != expected_metrics_version:
        blockers.append(
            {
                "code": "METRICS_VERSION_MISMATCH",
                "evaluation": name,
                "expected": expected_metrics_version,
                "actual": actual_metrics_version,
            }
        )

    metric_paths = set(specification.get("thresholds") or {})
    if name == "resolver_hint_safety":
        metric_paths.update(
            {
                "unsafe_auto_hint_count",
                "wrong_cas_auto_hint_count",
                "resolver_rule_eligibility_violation_count",
            }
        )
    metrics = {path: _metric_value(report, path) for path in sorted(metric_paths)}
    normalized_report_evidence = {
        "metrics_version": actual_metrics_version,
        "case_count": case_count,
        "evaluation_contract": normalized_contract,
        "release_binding": binding,
        "metrics": metrics,
    }
    entry = {
        "metrics_version": actual_metrics_version,
        # Evaluator 보고서의 생성 시각·latency처럼 재실행마다 달라지는 운영 측정값은
        # 서명 대상을 불안정하게 만든다. 릴리스 판정에 실제 사용한 계약·binding·
        # 품질 지표만 정규화해 digest하되, 원본 dataset은 파일 전체를 hash한다.
        "report_digest_kind": "NORMALIZED_RELEASE_EVIDENCE_V1",
        "report_sha256": _payload_sha256(normalized_report_evidence),
        "dataset_sha256": sha256_file(dataset_path),
        "case_count": case_count,
        "eligible_case_count": contract.get("eligible_case_count"),
        "profile": contract.get("profile"),
        "claim_scope": contract.get("claim_scope"),
        "contract_passed": contract.get("passed"),
        "expert_reviewed": contract.get("expert_reviewed"),
        "split_counts": contract.get("split_counts"),
        "missing_provenance_count": contract.get("missing_provenance_count"),
        "duplicate_case_ids": contract.get("duplicate_case_ids"),
        "split_leakage_groups": contract.get("split_leakage_groups"),
        "metrics": metrics,
    }
    return entry, blockers


def _build_release_evidence(
    *,
    quality_policy: Mapping[str, Any],
    quality_policy_sha256: str,
    data_governance: Mapping[str, Any],
    evaluation_evidence: Mapping[str, Mapping[str, Any]] | None,
    git_commit: str,
) -> dict[str, Any]:
    profile = str(quality_policy["profile"])
    provided = evaluation_evidence or {}
    evaluations: dict[str, Any] = {}
    source_blockers: list[dict[str, Any]] = []
    for name, specification in quality_policy["required_evaluations"].items():
        paths = provided.get(name)
        if not isinstance(paths, Mapping):
            evaluations[name] = {}
            source_blockers.append(
                {
                    "code": "MISSING_EVALUATION_EVIDENCE",
                    "evaluation": name,
                }
            )
            continue
        entry, blockers = _evaluation_entry(
            name,
            specification,
            paths,
            profile=profile,
            git_commit=git_commit,
        )
        evaluations[name] = entry
        source_blockers.extend(blockers)

    return {
        "schema_version": RELEASE_QUALIFICATION_SCHEMA_VERSION,
        "policy_id": quality_policy["policy_id"],
        "quality_policy_sha256": quality_policy_sha256,
        "profile": profile,
        "git_commit": git_commit,
        "data_governance": {
            "registry_sha256": data_governance.get("registry_sha256"),
            "required_source_ids": data_governance.get("required_source_ids"),
            "registered_source_ids": data_governance.get("registered_source_ids"),
            "missing_required_source_ids": data_governance.get(
                "missing_required_source_ids"
            ),
            "public_container_redistribution_ready": data_governance.get(
                "public_container_redistribution_ready"
            ),
        },
        "evaluations": evaluations,
        "source_blockers": source_blockers,
    }


def _evaluate_embedded_evidence(
    evidence: Mapping[str, Any],
    *,
    quality_policy: Mapping[str, Any],
    quality_policy_sha256: str,
    data_governance: Mapping[str, Any],
    git_commit: str,
) -> dict[str, Any]:
    blockers: list[dict[str, Any]] = []
    expected_header = {
        "schema_version": RELEASE_QUALIFICATION_SCHEMA_VERSION,
        "policy_id": quality_policy["policy_id"],
        "quality_policy_sha256": quality_policy_sha256,
        "profile": quality_policy["profile"],
        "git_commit": git_commit,
    }
    for field, expected in expected_header.items():
        if evidence.get(field) != expected:
            blockers.append(
                {
                    "code": "EVIDENCE_HEADER_MISMATCH",
                    "field": field,
                    "expected": expected,
                    "actual": evidence.get(field),
                }
            )

    governance_evidence = evidence.get("data_governance")
    expected_governance = {
        "registry_sha256": data_governance.get("registry_sha256"),
        "required_source_ids": data_governance.get("required_source_ids"),
        "registered_source_ids": data_governance.get("registered_source_ids"),
        "missing_required_source_ids": data_governance.get(
            "missing_required_source_ids"
        ),
        "public_container_redistribution_ready": data_governance.get(
            "public_container_redistribution_ready"
        ),
    }
    if governance_evidence != expected_governance:
        blockers.append({"code": "DATA_GOVERNANCE_EVIDENCE_MISMATCH"})
    if data_governance.get("public_container_redistribution_ready") is not True:
        blockers.append({"code": "DATA_REDISTRIBUTION_NOT_APPROVED"})

    source_blockers = evidence.get("source_blockers")
    if not isinstance(source_blockers, list) or source_blockers:
        blockers.append(
            {
                "code": "SOURCE_EVIDENCE_BLOCKED",
                "details": source_blockers,
            }
        )

    evaluations = evidence.get("evaluations")
    if not isinstance(evaluations, Mapping):
        evaluations = {}
        blockers.append({"code": "EVALUATIONS_NOT_OBJECT"})
    threshold_results: dict[str, Any] = {}
    for name, specification in quality_policy["required_evaluations"].items():
        entry = evaluations.get(name)
        if not isinstance(entry, Mapping):
            blockers.append({"code": "MISSING_EVALUATION_EVIDENCE", "evaluation": name})
            continue
        minimum = int(specification["minimum_case_count"])
        case_count = entry.get("case_count")
        if (
            not isinstance(case_count, int)
            or isinstance(case_count, bool)
            or case_count < minimum
        ):
            blockers.append(
                {
                    "code": "MINIMUM_CASE_COUNT_NOT_MET",
                    "evaluation": name,
                    "required": minimum,
                    "actual": case_count,
                }
            )
        if entry.get("eligible_case_count") != case_count:
            blockers.append({"code": "INELIGIBLE_EVALUATION_ROWS", "evaluation": name})
        if (
            entry.get("profile") != quality_policy["profile"]
            or entry.get("claim_scope") != quality_policy["profile"]
            or entry.get("contract_passed") is not True
        ):
            blockers.append(
                {"code": "EVALUATION_PROFILE_NOT_REVIEWED", "evaluation": name}
            )
        if entry.get("split_counts") != {"locked_test": case_count}:
            blockers.append(
                {"code": "EVALUATION_NOT_LOCKED_TEST_ONLY", "evaluation": name}
            )
        if (
            entry.get("missing_provenance_count") != 0
            or entry.get("duplicate_case_ids") != []
            or entry.get("split_leakage_groups") != []
        ):
            blockers.append(
                {"code": "EVALUATION_PROVENANCE_INVALID", "evaluation": name}
            )
        if entry.get("metrics_version") != specification["metrics_version"]:
            blockers.append({"code": "METRICS_VERSION_MISMATCH", "evaluation": name})
        if entry.get("report_digest_kind") != "NORMALIZED_RELEASE_EVIDENCE_V1":
            blockers.append(
                {
                    "code": "REPORT_DIGEST_KIND_MISMATCH",
                    "evaluation": name,
                }
            )
        for hash_field in ("report_sha256", "dataset_sha256"):
            if (
                re.fullmatch(r"[0-9a-f]{64}", str(entry.get(hash_field) or "").lower())
                is None
            ):
                blockers.append(
                    {
                        "code": "EVALUATION_HASH_INVALID",
                        "evaluation": name,
                        "field": hash_field,
                    }
                )

        metrics = entry.get("metrics")
        if not isinstance(metrics, Mapping):
            metrics = {}
        if name == "retriever_sections":
            answerable_count = metrics.get("answerable_case_count")
            unanswerable_count = metrics.get("unanswerable_case_count")
            if (
                isinstance(answerable_count, int)
                and not isinstance(answerable_count, bool)
                and isinstance(unanswerable_count, int)
                and not isinstance(unanswerable_count, bool)
                and isinstance(case_count, int)
                and answerable_count + unanswerable_count != case_count
            ):
                blockers.append(
                    {
                        "code": "EVALUATION_CASE_PARTITION_MISMATCH",
                        "evaluation": name,
                        "case_count": case_count,
                        "answerable_case_count": answerable_count,
                        "unanswerable_case_count": unanswerable_count,
                    }
                )
        evaluation_thresholds: dict[str, Any] = {}
        for metric, threshold in specification["thresholds"].items():
            actual = metrics.get(metric)
            passed = _threshold_passed(
                actual,
                str(threshold["operator"]),
                float(threshold["value"]),
            )
            evaluation_thresholds[metric] = {
                "actual": actual,
                "operator": threshold["operator"],
                "required": threshold["value"],
                "passed": passed,
            }
            if not passed:
                blockers.append(
                    {
                        "code": "QUALITY_THRESHOLD_NOT_MET",
                        "evaluation": name,
                        "metric": metric,
                    }
                )
        threshold_results[name] = evaluation_thresholds

    unsafe_policy = quality_policy["unsafe_cas_auto_confirmation"]
    unsafe_entry = evaluations.get(str(unsafe_policy["evaluation"]))
    unsafe_metrics = (
        unsafe_entry.get("metrics")
        if isinstance(unsafe_entry, Mapping)
        and isinstance(unsafe_entry.get("metrics"), Mapping)
        else {}
    )
    failure_values = [
        unsafe_metrics.get(str(metric)) for metric in unsafe_policy["failure_metrics"]
    ]
    valid_failure_values = all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in failure_values
    )
    total_failures = sum(failure_values) if valid_failure_values else None
    unsafe_case_count = (
        unsafe_entry.get("case_count") if isinstance(unsafe_entry, Mapping) else None
    )
    confidence = float(unsafe_policy["confidence_level"])
    upper_rate = (
        _zero_failure_upper_rate(int(unsafe_case_count), confidence)
        if total_failures == 0
        and isinstance(unsafe_case_count, int)
        and not isinstance(unsafe_case_count, bool)
        else 1.0
    )
    unsafe_control_passed = bool(
        total_failures == int(unsafe_policy["maximum_failures"])
        and upper_rate <= float(unsafe_policy["maximum_one_sided_upper_rate"])
    )
    if not unsafe_control_passed:
        blockers.append({"code": "UNSAFE_CAS_AUTO_CONFIRMATION_GATE_FAILED"})

    return {
        "passed": not blockers,
        "blockers": blockers,
        "threshold_results": threshold_results,
        "unsafe_cas_auto_confirmation": {
            "failure_metrics": list(unsafe_policy["failure_metrics"]),
            "total_failures": total_failures,
            "case_count": unsafe_case_count,
            "confidence_level": confidence,
            "one_sided_upper_rate": upper_rate,
            "maximum_one_sided_upper_rate": unsafe_policy[
                "maximum_one_sided_upper_rate"
            ],
            "passed": unsafe_control_passed,
        },
    }


def release_attestation_signature(
    attestation: Mapping[str, Any],
    key: str,
) -> str:
    """오프라인 검수자가 signature 필드를 제외한 증명을 HMAC-SHA256으로 서명한다."""

    encoded_key = key.encode("utf-8")
    if len(encoded_key) < 32:
        raise RuntimeIntegrityError(
            "release attestation HMAC key는 32바이트 이상이어야 합니다."
        )
    unsigned = dict(attestation)
    signature = unsigned.get("signature")
    if isinstance(signature, Mapping):
        unsigned["signature"] = {
            name: value for name, value in signature.items() if name != "value"
        }
    else:
        unsigned.pop("signature", None)
    return hmac.new(
        encoded_key, _canonical_json_bytes(unsigned), hashlib.sha256
    ).hexdigest()


def _verify_release_attestation(
    attestation: Mapping[str, Any] | None,
    *,
    schema: Mapping[str, Any],
    evidence_digest: str,
    git_commit: str,
    quality_policy_sha256: str,
    data_registry_sha256: str,
    key: str | None,
    trust_preverified_signature: bool = False,
) -> dict[str, Any]:
    del (
        schema
    )  # schema 파일 자체의 버전·required 계약은 _attestation_schema가 검증한다.
    blockers: list[dict[str, Any]] = []
    if not isinstance(attestation, Mapping):
        return {
            "verified": False,
            "blockers": [{"code": "RELEASE_ATTESTATION_MISSING"}],
        }
    expected_fields = {
        "schema_version": RELEASE_ATTESTATION_SCHEMA_VERSION,
        "approval_status": "APPROVED",
        "profile": "PILOT_REVIEWED",
        "git_commit": git_commit,
        "quality_policy_sha256": quality_policy_sha256,
        "data_registry_sha256": data_registry_sha256,
        "evidence_digest": evidence_digest,
    }
    for field, expected in expected_fields.items():
        if attestation.get(field) != expected:
            blockers.append(
                {
                    "code": "RELEASE_ATTESTATION_FIELD_MISMATCH",
                    "field": field,
                }
            )
    if not str(attestation.get("attestation_id") or "").strip():
        blockers.append({"code": "RELEASE_ATTESTATION_ID_MISSING"})

    reviewer = attestation.get("reviewer")
    if not isinstance(reviewer, Mapping) or any(
        not str(reviewer.get(field) or "").strip()
        for field in ("reviewer_id", "organization", "independence_statement")
    ):
        blockers.append({"code": "RELEASE_ATTESTATION_REVIEWER_INVALID"})
    field_validation = attestation.get("field_validation")
    if (
        not isinstance(field_validation, Mapping)
        or field_validation.get("status") != "COMPLETED"
        or any(
            not str(field_validation.get(field) or "").strip()
            for field in ("protocol_id", "evidence_reference", "completed_at_utc")
        )
    ):
        blockers.append({"code": "FIELD_VALIDATION_ATTESTATION_INVALID"})
    else:
        try:
            _parse_utc(
                field_validation.get("completed_at_utc"),
                "field_validation.completed_at_utc",
            )
        except RuntimeIntegrityError:
            blockers.append({"code": "FIELD_VALIDATION_TIMESTAMP_INVALID"})

    try:
        issued = _parse_utc(attestation.get("issued_at_utc"), "issued_at_utc")
        expires = _parse_utc(attestation.get("expires_at_utc"), "expires_at_utc")
        now = datetime.now(timezone.utc)
        if expires <= issued or expires <= now:
            blockers.append({"code": "RELEASE_ATTESTATION_EXPIRED"})
        if issued > now:
            blockers.append({"code": "RELEASE_ATTESTATION_ISSUED_IN_FUTURE"})
    except RuntimeIntegrityError:
        blockers.append({"code": "RELEASE_ATTESTATION_TIMESTAMP_INVALID"})

    signature = attestation.get("signature")
    if (
        not isinstance(signature, Mapping)
        or signature.get("algorithm") != "HMAC-SHA256"
        or not str(signature.get("key_id") or "").strip()
        or re.fullmatch(r"[0-9a-f]{64}", str(signature.get("value") or "").lower())
        is None
    ):
        blockers.append({"code": "RELEASE_ATTESTATION_SIGNATURE_INVALID"})
    if not key and not trust_preverified_signature:
        blockers.append({"code": "RELEASE_ATTESTATION_TRUST_KEY_MISSING"})
    elif key and isinstance(signature, Mapping):
        try:
            expected_signature = release_attestation_signature(attestation, key)
        except RuntimeIntegrityError:
            blockers.append({"code": "RELEASE_ATTESTATION_TRUST_KEY_INVALID"})
        else:
            if not hmac.compare_digest(
                expected_signature,
                str(signature.get("value") or "").lower(),
            ):
                blockers.append({"code": "RELEASE_ATTESTATION_SIGNATURE_MISMATCH"})

    return {
        "verified": not blockers,
        "attestation_id": attestation.get("attestation_id"),
        "reviewer_id": (
            reviewer.get("reviewer_id") if isinstance(reviewer, Mapping) else None
        ),
        "field_validation_status": (
            field_validation.get("status")
            if isinstance(field_validation, Mapping)
            else None
        ),
        "blockers": blockers,
    }


def _release_qualification(
    *,
    evidence: Mapping[str, Any],
    quality_policy: Mapping[str, Any],
    quality_policy_sha256: str,
    data_governance: Mapping[str, Any],
    git_commit: str,
    attestation: Mapping[str, Any] | None,
    attestation_schema: Mapping[str, Any],
    attestation_key: str | None,
    trust_preverified_attestation: bool = False,
) -> dict[str, Any]:
    evidence_evaluation = _evaluate_embedded_evidence(
        evidence,
        quality_policy=quality_policy,
        quality_policy_sha256=quality_policy_sha256,
        data_governance=data_governance,
        git_commit=git_commit,
    )
    evidence_digest = _payload_sha256(evidence)
    attestation_result = _verify_release_attestation(
        attestation,
        schema=attestation_schema,
        evidence_digest=evidence_digest,
        git_commit=git_commit,
        quality_policy_sha256=quality_policy_sha256,
        data_registry_sha256=str(data_governance.get("registry_sha256") or ""),
        key=attestation_key,
        trust_preverified_signature=trust_preverified_attestation,
    )
    passed = bool(evidence_evaluation["passed"] and attestation_result["verified"])
    return {
        "schema_version": RELEASE_QUALIFICATION_SCHEMA_VERSION,
        "passed": passed,
        "claim_scope": "PILOT_REVIEWED" if passed else "ARTIFACT_INTEGRITY_ONLY",
        "field_validated": attestation_result["verified"],
        "evidence": dict(evidence),
        "evidence_digest": evidence_digest,
        "quality_gate": evidence_evaluation,
        "attestation": dict(attestation) if isinstance(attestation, Mapping) else None,
        "attestation_verification": attestation_result,
    }


def create_runtime_manifest(
    *,
    db_path: Path,
    resolver_model_path: Path,
    retriever_model_path: Path,
    config_dir: Path,
    output_path: Path | None = None,
    git_commit: str | None = None,
    evaluation_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    release_attestation_path: Path | None = None,
    attestation_hmac_key: str | None = None,
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
    quality_policy_path = config_dir / RELEASE_QUALITY_POLICY_FILE
    attestation_schema_path = config_dir / RELEASE_ATTESTATION_SCHEMA_FILE
    quality_policy = _quality_policy(quality_policy_path)
    attestation_schema = _attestation_schema(attestation_schema_path)
    data_governance = _data_governance_summary(
        config_dir / DATA_SOURCE_REGISTRY_FILE,
        required_source_ids=frozenset(quality_policy["required_data_source_ids"]),
    )
    release_commit = (
        git_commit or os.getenv("CHEMIGUARD119_GIT_COMMIT") or "UNKNOWN"
    ).strip()
    evidence = _build_release_evidence(
        quality_policy=quality_policy,
        quality_policy_sha256=sha256_file(quality_policy_path),
        data_governance=data_governance,
        evaluation_evidence=evaluation_evidence,
        git_commit=release_commit,
    )
    attestation = (
        _read_json_object(release_attestation_path, "release attestation")
        if release_attestation_path is not None
        else None
    )
    qualification = _release_qualification(
        evidence=evidence,
        quality_policy=quality_policy,
        quality_policy_sha256=sha256_file(quality_policy_path),
        data_governance=data_governance,
        git_commit=release_commit,
        attestation=attestation,
        attestation_schema=attestation_schema,
        attestation_key=(
            attestation_hmac_key
            if attestation_hmac_key is not None
            else os.getenv(RELEASE_ATTESTATION_KEY_ENV_VAR)
        ),
    )
    manifest = {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "service": SERVICE_ID,
        "package_version": __version__,
        "git_commit": release_commit,
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
        "data_governance": data_governance,
        "evaluation_qualification": qualification,
        "security": {
            "joblib_is_pickle_based": True,
            "verify_manifest_and_file_hashes_before_joblib_load": True,
            "production_requires_manifest_sha256_trust_anchor": True,
            "production_requires_git_commit_trust_anchor": True,
            "production_requires_redistribution_approval": True,
            "production_requires_pilot_reviewed_evaluation": True,
            "production_requires_signed_release_attestation": True,
            "unsafe_cas_auto_confirmation_max_failures": 0,
            "unsafe_cas_auto_confirmation_confidence_level": (
                UNSAFE_CAS_CONFIDENCE_LEVEL
            ),
            "unsafe_cas_auto_confirmation_max_upper_rate": (
                UNSAFE_CAS_MAX_ONE_SIDED_UPPER_RATE
            ),
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
    trusted_deployment: bool,
    quality_policy: Mapping[str, Any],
    quality_policy_sha256: str,
    data_governance: Mapping[str, Any],
    attestation_schema: Mapping[str, Any],
    attestation_key: str | None,
    manifest_hash_trusted: bool,
) -> dict[str, Any]:
    """파일을 역직렬화하기 전에 코드와 manifest의 정적 계약을 비교한다."""

    from chemiguard119.resolver import SUPPORTED_MODEL_SCHEMA_VERSIONS
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
            "model_schema_version": SUPPORTED_MODEL_SCHEMA_VERSIONS,
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
            if (isinstance(expected, (set, frozenset)) and actual not in expected) or (
                not isinstance(expected, (set, frozenset)) and actual != expected
            ):
                mismatches[f"artifacts.{model_name}.{field}"] = {
                    "expected": sorted(expected)
                    if isinstance(expected, (set, frozenset))
                    else expected,
                    "actual": actual,
                }

    git_commit = str(payload.get("git_commit") or "")
    if trusted_deployment and re.fullmatch(r"[0-9a-fA-F]{40}", git_commit) is None:
        mismatches["git_commit"] = {
            "expected": "40자리 Git commit SHA",
            "actual": git_commit,
        }
    expected_git_commit = (os.getenv("CHEMIGUARD119_GIT_COMMIT") or "").strip()
    if (
        trusted_deployment
        and re.fullmatch(r"[0-9a-fA-F]{40}", expected_git_commit) is None
    ):
        mismatches["git_commit_trust_anchor"] = {
            "expected": "CHEMIGUARD119_GIT_COMMIT 40자리 Git commit SHA",
            "actual": expected_git_commit,
        }
    elif expected_git_commit and git_commit != expected_git_commit:
        mismatches["git_commit_trust_anchor"] = {
            "expected": expected_git_commit,
            "actual": git_commit,
        }
    manifest_data_governance = payload.get("data_governance") or {}
    if manifest_data_governance != data_governance:
        mismatches["data_governance"] = {
            "expected": data_governance,
            "actual": manifest_data_governance,
        }
    if (
        trusted_deployment
        and manifest_data_governance.get("public_container_redistribution_ready")
        is not True
    ):
        mismatches["data_governance.public_container_redistribution_ready"] = {
            "expected": True,
            "actual": manifest_data_governance.get(
                "public_container_redistribution_ready"
            ),
        }
    qualification = payload.get("evaluation_qualification")
    if not isinstance(qualification, Mapping):
        qualification = {}
    evidence = qualification.get("evidence")
    if not isinstance(evidence, Mapping):
        evidence = {}
    attestation = qualification.get("attestation")
    if not isinstance(attestation, Mapping):
        attestation = None
    stored_attestation_verification = qualification.get("attestation_verification")
    trust_preverified_attestation = bool(
        trusted_deployment
        and manifest_hash_trusted
        and isinstance(stored_attestation_verification, Mapping)
        and stored_attestation_verification.get("verified") is True
        and not stored_attestation_verification.get("blockers")
    )
    recalculated = _release_qualification(
        evidence=evidence,
        quality_policy=quality_policy,
        quality_policy_sha256=quality_policy_sha256,
        data_governance=data_governance,
        git_commit=git_commit,
        attestation=attestation,
        attestation_schema=attestation_schema,
        attestation_key=attestation_key,
        trust_preverified_attestation=trust_preverified_attestation,
    )
    qualification_fields = (
        "schema_version",
        "passed",
        "claim_scope",
        "field_validated",
        "evidence_digest",
        "quality_gate",
        "attestation_verification",
    )
    qualification_mismatches = {
        field: {
            "expected": recalculated.get(field),
            "actual": qualification.get(field),
        }
        for field in qualification_fields
        if qualification.get(field) != recalculated.get(field)
    }
    if qualification_mismatches:
        mismatches["evaluation_qualification_consistency"] = {
            "expected": "manifest qualification을 실제 policy·evidence·서명에서 재계산한 값",
            "actual": qualification_mismatches,
        }
    if trusted_deployment and recalculated["passed"] is not True:
        mismatches["evaluation_qualification"] = {
            "expected": {
                "passed": True,
                "claim_scope": "PILOT_REVIEWED",
                "field_validated": True,
                "minimum_case_counts": PILOT_QUALIFICATION_MINIMUM_CASES,
                "signed_release_attestation": True,
                "unsafe_cas_auto_confirmation_failures": 0,
                "unsafe_cas_auto_confirmation_max_one_sided_upper_rate": (
                    UNSAFE_CAS_MAX_ONE_SIDED_UPPER_RATE
                ),
            },
            "actual": {
                "passed": recalculated.get("passed"),
                "claim_scope": recalculated.get("claim_scope"),
                "field_validated": recalculated.get("field_validated"),
                "quality_blockers": recalculated.get("quality_gate", {}).get(
                    "blockers"
                ),
                "attestation_blockers": recalculated.get(
                    "attestation_verification", {}
                ).get("blockers"),
            },
        }
    if mismatches:
        raise RuntimeIntegrityError(f"runtime manifest 코드 계약 불일치: {mismatches}")
    return {
        "status": "MATCHED",
        "service": SERVICE_ID,
        "package_version": __version__,
        "git_commit": git_commit,
        "resolver_schema_version": (payload.get("artifacts") or {})
        .get("resolver", {})
        .get("model_schema_version"),
        "retriever_schema_version": RETRIEVER_SCHEMA_VERSION,
        "public_container_redistribution_ready": manifest_data_governance.get(
            "public_container_redistribution_ready"
        ),
        "evaluation_claim_scope": recalculated.get("claim_scope"),
        "release_attestation_verified": recalculated.get(
            "attestation_verification", {}
        ).get("verified"),
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
    if deployment_environment not in SUPPORTED_DEPLOYMENT_ENVIRONMENTS:
        allowed = ", ".join(sorted(SUPPORTED_DEPLOYMENT_ENVIRONMENTS))
        raise RuntimeIntegrityError(
            "지원하지 않는 배포 환경입니다: "
            f"{deployment_environment!r} (허용: {allowed})"
        )
    trusted_deployment = deployment_environment in TRUSTED_DEPLOYMENT_ENVIRONMENTS
    manifest = manifest_path or db_path.parent / RUNTIME_MANIFEST_FILE
    config_paths = [config_dir / name for name in REQUIRED_CONFIG_FILES]
    required_paths = [db_path, resolver_model_path, retriever_model_path, *config_paths]
    require_materialized_files(required_paths)

    if not manifest.is_file():
        if trusted_deployment:
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
    if trusted_deployment and len(expected_manifest_hash) != 64:
        raise RuntimeIntegrityError(
            "staging·production 환경에는 "
            "CHEMIGUARD119_RUNTIME_MANIFEST_SHA256가 필요합니다."
        )
    if expected_manifest_hash and actual_manifest_hash != expected_manifest_hash:
        raise RuntimeIntegrityError("runtime manifest SHA-256 불일치")

    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeIntegrityError("runtime manifest를 읽을 수 없습니다.") from error
    if payload.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise RuntimeIntegrityError("지원하지 않는 runtime manifest 버전입니다.")
    quality_policy_path = config_dir / RELEASE_QUALITY_POLICY_FILE
    quality_policy = _quality_policy(quality_policy_path)
    attestation_schema = _attestation_schema(
        config_dir / RELEASE_ATTESTATION_SCHEMA_FILE
    )
    data_governance = _data_governance_summary(
        config_dir / DATA_SOURCE_REGISTRY_FILE,
        required_source_ids=frozenset(quality_policy["required_data_source_ids"]),
    )
    contract = _verify_manifest_contract(
        payload,
        trusted_deployment=trusted_deployment,
        quality_policy=quality_policy,
        quality_policy_sha256=sha256_file(quality_policy_path),
        data_governance=data_governance,
        attestation_schema=attestation_schema,
        attestation_key=os.getenv(RELEASE_ATTESTATION_KEY_ENV_VAR),
        manifest_hash_trusted=bool(expected_manifest_hash),
    )

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
    "DATA_SOURCE_REGISTRY_FILE",
    "RELEASE_QUALITY_POLICY_FILE",
    "RELEASE_ATTESTATION_SCHEMA_FILE",
    "RELEASE_ATTESTATION_KEY_ENV_VAR",
    "SUPPORTED_DEPLOYMENT_ENVIRONMENTS",
    "TRUSTED_DEPLOYMENT_ENVIRONMENTS",
    "PILOT_QUALIFICATION_MINIMUM_CASES",
    "RuntimeIntegrityError",
    "bind_evaluation_report",
    "create_runtime_manifest",
    "release_attestation_signature",
    "verify_runtime_release",
]
