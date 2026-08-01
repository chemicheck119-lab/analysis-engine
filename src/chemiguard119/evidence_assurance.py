"""공식 출처의 범위와 한계를 주장 단위로 검증하는 보조 감사 계층.

이 모듈은 사람 화학 전문가의 승인이나 현장 검증을 대신하지 않는다. CAMEO
스크리닝 결과에 대해 어떤 공식 출처가 같은 위험 주장을 뒷받침하는지, 독립 기관
수와 미검증 조건이 무엇인지 재현 가능한 형태로 기록한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from chemiguard119.utils import normalize_cas, sha256_file, valid_cas_checksum


REFERENCE_ASSURANCE_SCHEMA_VERSION = "chemicheck119-reference-assurance-v1"
REFERENCE_REGISTRY_SCHEMA_VERSION = "chemicheck119-reference-assurance-registry-v1"
REFERENCE_POLICY_ID = "OFFICIAL_REFERENCE_TRIANGULATION_V1"

TRIANGULATED_STATUS = "REFERENCE_TRIANGULATED"
PRIMARY_ONLY_STATUS = "PRIMARY_AUTHORITY_ONLY"


class ReferenceAssuranceError(ValueError):
    """근거 registry 또는 결과가 fail-closed 조건을 만족하지 못한 경우."""


def _pair(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((normalize_cas(left), normalize_cas(right))))  # type: ignore[return-value]


def _load_registry(config_dir: Path) -> tuple[dict[str, Any], Path]:
    path = config_dir / "reference_assurance_registry.json"
    if not path.is_file():
        raise ReferenceAssuranceError("공식근거 보증 registry를 찾을 수 없습니다.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReferenceAssuranceError(
            "공식근거 보증 registry를 읽을 수 없습니다."
        ) from error
    if not isinstance(payload, dict):
        raise ReferenceAssuranceError(
            "공식근거 보증 registry 최상위는 객체여야 합니다."
        )
    return payload, path


def _authority_index(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    authorities = registry.get("authorities")
    if not isinstance(authorities, list) or not authorities:
        raise ReferenceAssuranceError("공식근거 기관 registry가 비어 있습니다.")
    result: dict[str, dict[str, Any]] = {}
    for item in authorities:
        if not isinstance(item, dict):
            raise ReferenceAssuranceError("공식근거 기관 항목은 객체여야 합니다.")
        authority_id = str(item.get("authority_id") or "")
        if not authority_id or authority_id in result:
            raise ReferenceAssuranceError("공식근거 기관 ID가 없거나 중복됩니다.")
        hosts = item.get("allowed_hosts")
        if not isinstance(hosts, list) or not hosts:
            raise ReferenceAssuranceError("공식근거 기관의 허용 host가 비어 있습니다.")
        if not item.get("independence_group"):
            raise ReferenceAssuranceError("공식근거 기관의 독립성 그룹이 없습니다.")
        result[authority_id] = item
    return result


def _validate_registry(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if registry.get("schema_version") != REFERENCE_REGISTRY_SCHEMA_VERSION:
        raise ReferenceAssuranceError("지원하지 않는 공식근거 registry 버전입니다.")
    if registry.get("policy_id") != REFERENCE_POLICY_ID:
        raise ReferenceAssuranceError("지원하지 않는 공식근거 보증 정책입니다.")
    if registry.get("expert_reviewed") is not False:
        raise ReferenceAssuranceError(
            "registry는 전문가 검토 완료를 주장할 수 없습니다."
        )
    if registry.get("human_expert_substitute") is not False:
        raise ReferenceAssuranceError(
            "registry는 사람 전문가 대체를 주장할 수 없습니다."
        )
    minimum = registry.get("minimum_independent_authorities")
    if not isinstance(minimum, int) or minimum < 2:
        raise ReferenceAssuranceError("독립 공식기관 최소 수가 올바르지 않습니다.")
    required_roles = registry.get("required_source_roles")
    if not isinstance(required_roles, list) or not required_roles:
        raise ReferenceAssuranceError("필수 공식근거 역할이 비어 있습니다.")

    authorities = _authority_index(registry)
    claims = registry.get("pair_claims")
    if not isinstance(claims, list):
        raise ReferenceAssuranceError("물질쌍 주장 registry는 배열이어야 합니다.")
    seen_claim_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    for claim in claims:
        if not isinstance(claim, dict):
            raise ReferenceAssuranceError("물질쌍 주장 항목은 객체여야 합니다.")
        claim_id = str(claim.get("claim_id") or "")
        if not claim_id or claim_id in seen_claim_ids:
            raise ReferenceAssuranceError("물질쌍 claim_id가 없거나 중복됩니다.")
        seen_claim_ids.add(claim_id)

        cas_pair = claim.get("cas_pair")
        if not isinstance(cas_pair, list) or len(cas_pair) != 2:
            raise ReferenceAssuranceError("물질쌍 CAS는 두 건이어야 합니다.")
        normalized_pair = _pair(str(cas_pair[0]), str(cas_pair[1]))
        if list(normalized_pair) != cas_pair or any(
            not valid_cas_checksum(value) for value in normalized_pair
        ):
            raise ReferenceAssuranceError("물질쌍 CAS 형식·정렬이 올바르지 않습니다.")
        if normalized_pair in seen_pairs:
            raise ReferenceAssuranceError("동일 물질쌍 주장이 중복됩니다.")
        seen_pairs.add(normalized_pair)

        sources = claim.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ReferenceAssuranceError("물질쌍 공식근거가 비어 있습니다.")
        source_ids: set[str] = set()
        for source in sources:
            if not isinstance(source, dict):
                raise ReferenceAssuranceError("공식근거 항목은 객체여야 합니다.")
            source_id = str(source.get("source_id") or "")
            if not source_id or source_id in source_ids:
                raise ReferenceAssuranceError("공식근거 ID가 없거나 중복됩니다.")
            source_ids.add(source_id)
            authority_id = str(source.get("authority_id") or "")
            authority = authorities.get(authority_id)
            if authority is None:
                raise ReferenceAssuranceError("등록되지 않은 공식기관 근거입니다.")
            parsed = urlparse(str(source.get("source_url") or ""))
            allowed_hosts = {str(host).lower() for host in authority["allowed_hosts"]}
            if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
                raise ReferenceAssuranceError(
                    "공식근거 URL이 기관 allowlist와 다릅니다."
                )
            if not source.get("source_role") or not source.get("locator"):
                raise ReferenceAssuranceError(
                    "공식근거 역할 또는 문서 위치가 없습니다."
                )
            expected_content_type = str(
                source.get("expected_content_type_prefix") or ""
            )
            if expected_content_type not in {"text/html", "application/pdf"}:
                raise ReferenceAssuranceError(
                    "공식근거의 예상 문서 형식이 없거나 지원되지 않습니다."
                )
            if expected_content_type == "text/html" and not source.get("content_probe"):
                raise ReferenceAssuranceError(
                    "HTML 공식근거의 문서 위치 probe가 없습니다."
                )
            if not str(source.get("relation") or "").startswith("SUPPORTS"):
                raise ReferenceAssuranceError(
                    "반대 또는 불명확한 근거를 지지 근거로 쓸 수 없습니다."
                )
    return authorities


def reference_assurance_configuration_status(config_dir: Path) -> dict[str, Any]:
    """readiness와 배포 감사에 사용할 근거 registry 상태를 반환한다.

    원문·질의·비밀정보는 포함하지 않으며, 실패 시 예외 대신 안전한 상태 객체를
    반환한다. PUBLIC_SOURCE_PILOT 정책은 ``ready``가 거짓이면 실행할 수 없다.
    """

    try:
        registry, registry_path = _load_registry(config_dir)
        authorities = _validate_registry(registry)
    except ReferenceAssuranceError as error:
        return {
            "ready": False,
            "schema_version": None,
            "policy_id": None,
            "registry_sha256": None,
            "authority_count": 0,
            "triangulated_pair_count": 0,
            "expert_reviewed": False,
            "human_expert_substitute": False,
            "error": str(error),
        }

    return {
        "ready": True,
        "schema_version": registry["schema_version"],
        "policy_id": registry["policy_id"],
        "registry_sha256": sha256_file(registry_path),
        "authority_count": len(authorities),
        "triangulated_pair_count": len(registry["pair_claims"]),
        "expert_reviewed": False,
        "human_expert_substitute": False,
        "error": None,
    }


def _claim_for_pair(
    registry: dict[str, Any], incident_cas: str, facility_cas: str
) -> dict[str, Any] | None:
    wanted = _pair(incident_cas, facility_cas)
    for claim in registry.get("pair_claims", []):
        if tuple(claim["cas_pair"]) == wanted:
            return claim
    return None


def _base_checks(status: str) -> list[dict[str, str]]:
    mechanism = "PASSED" if status == TRIANGULATED_STATUS else "LIMITED"
    return [
        {
            "claim": "SUBSTANCE_IDENTITY_AND_FORM",
            "status": "PASSED",
            "basis": "CAMEO 공식 물질 페이지의 CAS와 형태를 정확 대조",
        },
        {
            "claim": "PAIR_REACTIVITY_SCREENING",
            "status": mechanism,
            "basis": (
                "서로 독립된 공식기관 자료가 동일 위험 메커니즘을 지지"
                if mechanism == "PASSED"
                else "CAMEO 반응성 그룹 체계만으로 스크리닝"
            ),
        },
        {
            "claim": "CURRENT_SITE_INVENTORY",
            "status": "NOT_PROVEN",
            "basis": "공개 이력과 문헌은 현재 재고를 증명하지 않음",
        },
        {
            "claim": "ACTUAL_MIXING_AND_FIELD_CONDITIONS",
            "status": "NOT_PROVEN",
            "basis": "농도·온도·누출량·실제 혼합은 현장 확인 대상",
        },
        {
            "claim": "HUMAN_CHEMICAL_EXPERT_REVIEW",
            "status": "NOT_PERFORMED",
            "basis": "공식자료 자동 감사는 사람 전문가 서명 또는 승인이 아님",
        },
    ]


def build_reference_assurance(
    rule_result: dict[str, Any], config_dir: Path
) -> dict[str, Any]:
    """CAMEO 결과를 공식기관 주장 registry와 대조한다.

    registry가 없거나 변조되면 예외를 발생시켜 상위 Rule Engine이 완료 위험등급을
    공개하지 못하게 한다.
    """

    registry, registry_path = _load_registry(config_dir)
    authorities = _validate_registry(registry)
    incident_cas = normalize_cas(str(rule_result.get("incident_cas") or ""))
    facility_cas = normalize_cas(str(rule_result.get("facility_cas") or ""))
    if not all(valid_cas_checksum(value) for value in (incident_cas, facility_cas)):
        raise ReferenceAssuranceError("Rule 결과의 물질쌍 CAS가 올바르지 않습니다.")

    claim = _claim_for_pair(registry, incident_cas, facility_cas)
    if claim is None:
        return {
            "schema_version": REFERENCE_ASSURANCE_SCHEMA_VERSION,
            "policy_id": REFERENCE_POLICY_ID,
            "status": PRIMARY_ONLY_STATUS,
            "claim_id": None,
            "cas_pair": list(_pair(incident_cas, facility_cas)),
            "claim_text_ko": None,
            "reference_count": 1,
            "independent_authority_count": 1,
            "sources": [
                {
                    "source_id": "CAMEO_REACTIVE_GROUP_SCREENING",
                    "authority_id": "NOAA_EPA_CAMEO",
                    "organization": authorities["NOAA_EPA_CAMEO"]["organization"],
                    "independence_group": authorities["NOAA_EPA_CAMEO"][
                        "independence_group"
                    ],
                    "authority_kind": authorities["NOAA_EPA_CAMEO"]["authority_kind"],
                    "source_role": "PRIMARY_REACTIVITY_DATASHEET",
                    "title": "CAMEO reactive group compatibility screening",
                    "source_url": "https://cameochemicals.noaa.gov/reactivity",
                    "locator": "Reactive group compatibility result",
                    "published_or_updated": "CAMEO Chemicals 3.1.0 rev 1",
                    "relation": "SUPPORTS_SCREENING_ONLY",
                }
            ],
            "claim_checks": _base_checks(PRIMARY_ONLY_STATUS),
            "registry_sha256": sha256_file(registry_path),
            "reviewed_at_utc": registry["reviewed_at_utc"],
            "machine_checked": True,
            "expert_reviewed": False,
            "human_expert_substitute": False,
            "decision_support_only": True,
            "limitations": [
                "이 물질쌍은 CAMEO 단일 공식체계 스크리닝이며 독립기관 교차증빙이 없습니다.",
                "낮음은 안전 보장이 아니라 알려진 유해 반응이 없다는 제한된 분류입니다.",
                "현장 재고·농도·실제 혼합·보호구 적합성은 증명하지 않습니다.",
            ],
        }

    expected_gases = set(claim.get("expected_gas_products") or [])
    actual_gases = set(rule_result.get("gas_products") or [])
    if not expected_gases.issubset(actual_gases):
        raise ReferenceAssuranceError(
            "공식근거 claim의 예상 생성물과 CAMEO 결과가 일치하지 않습니다."
        )

    sources: list[dict[str, Any]] = []
    independence_groups: set[str] = set()
    source_roles: set[str] = set()
    for source in claim["sources"]:
        authority = authorities[source["authority_id"]]
        independence_groups.add(str(authority["independence_group"]))
        source_roles.add(str(source["source_role"]))
        public_source = {
            key: value
            for key, value in source.items()
            if key not in {"content_probe", "expected_content_type_prefix"}
        }
        sources.append(
            {
                **public_source,
                "organization": authority["organization"],
                "independence_group": authority["independence_group"],
                "authority_kind": authority["authority_kind"],
            }
        )

    minimum = int(registry["minimum_independent_authorities"])
    required_roles = set(registry["required_source_roles"])
    if len(independence_groups) < minimum or not required_roles.issubset(source_roles):
        raise ReferenceAssuranceError(
            "공식근거 교차증빙 최소 조건을 충족하지 못했습니다."
        )

    return {
        "schema_version": REFERENCE_ASSURANCE_SCHEMA_VERSION,
        "policy_id": REFERENCE_POLICY_ID,
        "status": TRIANGULATED_STATUS,
        "claim_id": claim["claim_id"],
        "claim_type": claim["claim_type"],
        "cas_pair": list(_pair(incident_cas, facility_cas)),
        "claim_text_ko": claim["claim_text_ko"],
        "expected_gas_products": sorted(expected_gases),
        "scope_conditions": claim["scope_conditions"],
        "not_proven_by_claim": claim["not_proven_by_claim"],
        "reference_count": len(sources),
        "independent_authority_count": len(independence_groups),
        "sources": sources,
        "claim_checks": _base_checks(TRIANGULATED_STATUS),
        "registry_sha256": sha256_file(registry_path),
        "reviewed_at_utc": registry["reviewed_at_utc"],
        "machine_checked": True,
        "expert_reviewed": False,
        "human_expert_substitute": False,
        "decision_support_only": True,
        "limitations": [
            "공식기관 교차증빙은 사람 화학 전문가의 현장 승인 또는 법적 인증이 아닙니다.",
            "자료는 위험 메커니즘을 지지하지만 현장 발생확률이나 피해확률을 제공하지 않습니다.",
            "현장 재고·농도·온도·누출량·실제 혼합·보호구 적합성은 별도 확인이 필요합니다.",
        ],
    }


def validate_reference_assurance(
    payload: Any, incident_cas: str, facility_cas: str
) -> list[str]:
    """API에 공개되는 근거 보증 객체의 안전 불변조건을 검사한다."""

    if not isinstance(payload, dict):
        return ["reference_assurance 누락 또는 형식 오류"]
    errors: list[str] = []
    if payload.get("schema_version") != REFERENCE_ASSURANCE_SCHEMA_VERSION:
        errors.append("reference_assurance schema_version 오류")
    if payload.get("policy_id") != REFERENCE_POLICY_ID:
        errors.append("reference_assurance policy_id 오류")
    status = payload.get("status")
    if status not in {TRIANGULATED_STATUS, PRIMARY_ONLY_STATUS}:
        errors.append("reference_assurance status 오류")
    if payload.get("cas_pair") != list(_pair(incident_cas, facility_cas)):
        errors.append("reference_assurance CAS 물질쌍 불일치")
    if payload.get("machine_checked") is not True:
        errors.append("reference_assurance 기계 검증 표시 누락")
    if payload.get("expert_reviewed") is not False:
        errors.append("reference_assurance는 전문가 검토 완료를 주장할 수 없습니다.")
    if payload.get("human_expert_substitute") is not False:
        errors.append("reference_assurance는 사람 전문가 대체를 주장할 수 없습니다.")
    if payload.get("decision_support_only") is not True:
        errors.append("reference_assurance 의사결정 보조 표시 누락")

    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("reference_assurance 공식근거 누락")
        sources = []
    if payload.get("reference_count") != len(sources):
        errors.append("reference_assurance 근거 수 불일치")
    groups = {
        str(source.get("independence_group") or "")
        for source in sources
        if isinstance(source, dict) and source.get("independence_group")
    }
    if payload.get("independent_authority_count") != len(groups):
        errors.append("reference_assurance 독립기관 수 불일치")
    if status == TRIANGULATED_STATUS:
        if len(groups) < 3:
            errors.append("교차증빙 상태에는 독립 공식기관이 3개 이상 필요합니다.")
        if not payload.get("claim_id") or not payload.get("claim_text_ko"):
            errors.append("교차증빙 상태의 주장 ID 또는 문구 누락")
    elif status == PRIMARY_ONLY_STATUS and len(groups) != 1:
        errors.append("단일 공식체계 상태의 독립기관 수가 1이 아닙니다.")

    checks = payload.get("claim_checks")
    if not isinstance(checks, list) or not checks:
        errors.append("reference_assurance 주장별 검증 상태 누락")
    else:
        indexed = {
            str(item.get("claim")): str(item.get("status"))
            for item in checks
            if isinstance(item, dict)
        }
        if indexed.get("CURRENT_SITE_INVENTORY") != "NOT_PROVEN":
            errors.append("현재 재고는 공식근거로 증명됐다고 표시할 수 없습니다.")
        if indexed.get("ACTUAL_MIXING_AND_FIELD_CONDITIONS") != "NOT_PROVEN":
            errors.append(
                "실제 혼합·현장조건은 공식근거로 증명됐다고 표시할 수 없습니다."
            )
        if indexed.get("HUMAN_CHEMICAL_EXPERT_REVIEW") != "NOT_PERFORMED":
            errors.append("사람 화학 전문가 검토는 미수행 상태여야 합니다.")
    return errors


__all__ = [
    "PRIMARY_ONLY_STATUS",
    "REFERENCE_ASSURANCE_SCHEMA_VERSION",
    "REFERENCE_POLICY_ID",
    "ReferenceAssuranceError",
    "TRIANGULATED_STATUS",
    "build_reference_assurance",
    "reference_assurance_configuration_status",
    "validate_reference_assurance",
]
