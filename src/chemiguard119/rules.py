"""LLM과 분리된 버전형 화학물질쌍 Rule Engine."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from chemiguard119.database import connect_readonly
from chemiguard119.paths import CONFIG_DIR
from chemiguard119.utils import normalize_cas, valid_cas_checksum


APPROVED_ONLY_POLICY = "APPROVED_ONLY"
PUBLIC_SOURCE_PILOT_POLICY = "PUBLIC_SOURCE_PILOT_V1"
SUPPORTED_POLICY_MODES = {
    APPROVED_ONLY_POLICY,
    PUBLIC_SOURCE_PILOT_POLICY,
}
PUBLIC_SOURCE_VERIFIED = "PUBLIC_SOURCE_VERIFIED"
PUBLIC_SOURCE_SCOPE = "PUBLIC_SOURCE_CAMEO_SCREENING"
PUBLIC_SOURCE_METHOD = "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET"
PUBLIC_SOURCE_PRODUCT = "NOAA/EPA CAMEO Chemicals"
PUBLIC_SCREENING_BRIEF_TEMPLATE = (
    "NOAA/EPA CAMEO 공개 원자료로 대조한 반응성 그룹 조합 중 "
    "가장 보수적인 등급은 {risk_level_ko}입니다."
)
PUBLIC_SCREENING_REQUIRED_CHECKS = (
    "용기 라벨·현장 MSDS로 두 물질과 물리적 형태를 재확인",
    "저장구역·배수로·환기계통의 실제 연결 여부 확인",
    "농도·온도·압력과 누출액의 실제 혼합 여부 확인",
)
PUBLIC_SCREENING_LIMITATIONS = (
    "이 결과는 전문가 승인 결과가 아니라 공개 원자료 기반 파일럿 스크리닝입니다.",
    "낮음은 '알려진 유해 반응 없음'이며 안전 보장을 뜻하지 않습니다.",
    "이 등급은 사고확률이나 피해확률이 아닌 CAMEO 반응성 그룹의 서수 분류입니다.",
    "시설물질의 현재 수량·농도·저장상태와 실제 혼합 여부를 현장에서 확인해야 합니다.",
    "최종 결정은 현장 지휘관이 수행합니다.",
)

ALLOWED_SEVERITIES = {
    "PROHIBITED",
    "HIGH_RISK",
    "CAUTION",
    "CONDITIONAL",
    "VERIFY_REQUIRED",
    "UNCLASSIFIED",
    "NO_KNOWN_HAZARDOUS_REACTION",
}

CAMEO_CLASS_RESULTS = {
    "0": {
        "severity": "NO_KNOWN_HAZARDOUS_REACTION",
        "risk_level": "LOW",
        "risk_level_ko": "낮음",
    },
    "1": {
        "severity": "CAUTION",
        "risk_level": "MEDIUM",
        "risk_level_ko": "중간",
    },
    "2": {
        "severity": "HIGH_RISK",
        "risk_level": "HIGH",
        "risk_level_ko": "높음",
    },
}

SEVERITY_RISK_LEVELS = {
    "PROHIBITED": ("HIGH", "높음"),
    "HIGH_RISK": ("HIGH", "높음"),
    "CAUTION": ("MEDIUM", "중간"),
    "CONDITIONAL": ("MEDIUM", "중간"),
    "NO_KNOWN_HAZARDOUS_REACTION": ("LOW", "낮음"),
}


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _pair(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((normalize_cas(left), normalize_cas(right))))  # type: ignore[return-value]


def _config_path(config_dir: Path, primary: str, legacy: str) -> Path:
    """운영 파일을 우선하고 기존 프로젝트 fixture에는 하위 호환을 제공한다."""

    primary_path = config_dir / primary
    return primary_path if primary_path.is_file() else config_dir / legacy


def _crosswalk(config_dir: Path = CONFIG_DIR) -> dict[str, dict[str, str]]:
    path = _config_path(config_dir, "cameo_crosswalk.csv", "cameo_crosswalk_demo.csv")
    return {normalize_cas(row["cas_number"]): row for row in _load_csv(path)}


def _direct_rule(
    cas_a: str, cas_b: str, config_dir: Path = CONFIG_DIR
) -> dict[str, str] | None:
    wanted = _pair(cas_a, cas_b)
    path = _config_path(config_dir, "pair_rules.csv", "demo_pair_rules.csv")
    for row in _load_csv(path):
        if _pair(row["cas_a"], row["cas_b"]) == wanted:
            return row
    return None


def _public_source_policy(config_dir: Path) -> dict[str, Any]:
    path = config_dir / "conflict_policy.json"
    with path.open(encoding="utf-8") as handle:
        policy = json.load(handle)
    if not isinstance(policy, dict):
        raise ValueError("conflict_policy.json 최상위 값은 객체여야 합니다.")
    if policy.get("policy_id") != PUBLIC_SOURCE_PILOT_POLICY:
        raise ValueError("지원하지 않는 공개근거 파일럿 policy_id입니다.")
    if policy.get("allow_direct_rules") is not False:
        raise ValueError("공개근거 파일럿은 프로젝트 직접 Rule을 허용하지 않습니다.")
    if policy.get("expert_review_required") is not False:
        raise ValueError("공개근거 파일럿의 전문가 검토 정책 값이 올바르지 않습니다.")
    if policy.get("probability_output_allowed") is not False:
        raise ValueError("공개근거 파일럿은 확률 출력을 허용할 수 없습니다.")
    if policy.get("final_decision_authority") != "현장 지휘관 판단":
        raise ValueError("공개근거 파일럿의 최종 결정권자 문구가 올바르지 않습니다.")
    statuses = policy.get("eligible_crosswalk_statuses")
    if statuses != [PUBLIC_SOURCE_VERIFIED]:
        raise ValueError("공개근거 파일럿의 허용 crosswalk 상태가 올바르지 않습니다.")
    return policy


def _public_mapping_errors(row: dict[str, str], policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if row.get("verification_status") not in policy["eligible_crosswalk_statuses"]:
        errors.append("verification_status")
    if row.get("verification_method") != policy.get("required_verification_method"):
        errors.append("verification_method")
    if row.get("source_product") != policy.get("required_source_product"):
        errors.append("source_product")
    if not row.get("source_version"):
        errors.append("source_version")
    if not row.get("selected_form"):
        errors.append("selected_form")

    cameo_id = str(row.get("cameo_chemical_id") or "")
    expected_url = f"https://cameochemicals.noaa.gov/chemical/{cameo_id}"
    if not cameo_id.isdigit() or row.get("evidence_url") != expected_url:
        errors.append("evidence_url")

    checked_at = str(row.get("checked_at_utc") or "")
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        if checked.tzinfo is None or checked.utcoffset() is None:
            raise ValueError
    except ValueError:
        errors.append("checked_at_utc")
    return errors


def _mapping_provenance(role: str, row: dict[str, str]) -> dict[str, str]:
    fields = (
        "cas_number",
        "cameo_chemical_id",
        "selected_form",
        "verification_status",
        "verification_method",
        "evidence_url",
        "source_product",
        "source_version",
        "checked_at_utc",
    )
    return {"role": role, **{field: str(row.get(field) or "") for field in fields}}


def _cameo_screening(
    db_path: Path,
    left_cameo_id: str,
    right_cameo_id: str,
) -> list[dict[str, Any]]:
    with connect_readonly(db_path) as connection:
        connection.row_factory = sqlite3.Row
        left_groups = [
            str(row[0])
            for row in connection.execute(
                "SELECT reactive_group_id FROM cameo_mapping WHERE cameo_chemical_id = ?",
                (left_cameo_id,),
            )
        ]
        right_groups = [
            str(row[0])
            for row in connection.execute(
                "SELECT reactive_group_id FROM cameo_mapping WHERE cameo_chemical_id = ?",
                (right_cameo_id,),
            )
        ]
        hits: list[dict[str, Any]] = []
        for left_group in left_groups:
            for right_group in right_groups:
                group_a, group_b = sorted((int(left_group), int(right_group)))
                row = connection.execute(
                    """
                    SELECT pair_id, group_a_id, group_b_id, compatibility_label,
                           compatibility_class_id, hazard_codes, hazard_text, gases, source_url
                    FROM compatibility
                    WHERE CAST(group_a_id AS INTEGER) = ? AND CAST(group_b_id AS INTEGER) = ?
                    """,
                    (group_a, group_b),
                ).fetchone()
                if row:
                    hits.append(dict(row))
    return hits


def _screening_evidence_urls(
    screening: list[dict[str, Any]],
    left_map: dict[str, str],
    right_map: dict[str, str],
) -> list[str]:
    values = [
        left_map.get("evidence_url", ""),
        right_map.get("evidence_url", ""),
        *(str(item.get("source_url") or "") for item in screening),
    ]
    return list(dict.fromkeys(value for value in values if value))


def _approved_cameo_result(
    *,
    incident_cas: str,
    facility_cas: str,
    screening: list[dict[str, Any]],
    left_map: dict[str, str],
    right_map: dict[str, str],
    planned_actions: list[str] | None,
) -> dict[str, Any]:
    """전문가 승인 CAS 교차표와 CAMEO 원자료만으로 서수 등급을 만든다.

    이 결과는 통계 모델의 사고확률이 아니다. 한 물질이 여러 반응성 그룹에
    속하면 모든 조합 중 가장 보수적인 CAMEO class를 사용한다.
    """

    class_ids = {str(item.get("compatibility_class_id") or "") for item in screening}
    unsupported = sorted(class_ids - CAMEO_CLASS_RESULTS.keys())
    if unsupported:
        return {
            "status": "VERIFY_REQUIRED",
            "severity": None,
            "reason": "지원하지 않는 CAMEO 호환성 클래스가 포함되어 있습니다.",
            "unsupported_compatibility_class_ids": unsupported,
            "cameo_group_screening": screening,
            "human_confirmation_required": True,
        }

    worst_class_id = max(class_ids, key=int)
    classification = CAMEO_CLASS_RESULTS[worst_class_id]
    hazard_codes = sorted(
        {
            code
            for item in screening
            for code in str(item.get("hazard_codes") or "").split("|")
            if code
        }
    )
    gases = sorted(
        {
            gas
            for item in screening
            for gas in str(item.get("gases") or "").split("|")
            if gas
        }
    )
    evidence_urls = _screening_evidence_urls(screening, left_map, right_map)
    actions = planned_actions or []
    return {
        "status": "COMPLETED",
        "scope": "APPROVED_CAMEO_GROUP_SCREENING",
        "incident_cas": incident_cas,
        "facility_cas": facility_cas,
        "rule_id": "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
        "rule_version": "RUNTIME_MANIFEST_PINNED",
        "severity": classification["severity"],
        "risk_level": classification["risk_level"],
        "risk_level_ko": classification["risk_level_ko"],
        "risk_scale": {
            "type": "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
            "raw_class_id": int(worst_class_id),
            "is_probability": False,
            "probability_percent": None,
        },
        "hazard_codes": hazard_codes,
        "gas_products": gases,
        "brief_text": (
            "전문가 승인 CAS–CAMEO 연결을 통해 조회한 반응성 그룹 조합 중 "
            f"가장 보수적인 등급은 {classification['risk_level_ko']}입니다."
        ),
        "required_checks": list(PUBLIC_SCREENING_REQUIRED_CHECKS),
        "evidence_urls": evidence_urls,
        "cameo_group_screening": screening,
        "planned_actions": [
            {"raw_text": action, "status": "UNVALIDATED_ACTION_INPUT"}
            for action in actions
        ],
        "limitations": [
            "낮음은 '알려진 유해 반응 없음'이며 안전 보장을 뜻하지 않습니다.",
            "이 등급은 사고확률이나 피해확률이 아닌 CAMEO 반응성 그룹의 서수 분류입니다.",
            "시설물질의 현재 수량·농도·저장상태는 별도 현장 확인이 필요합니다.",
            "최종 결정은 현장 지휘관이 수행합니다.",
        ],
        "final_decision": "현장 지휘관 판단",
        "human_confirmation_required": True,
    }


def _public_source_cameo_result(
    *,
    incident_cas: str,
    facility_cas: str,
    screening: list[dict[str, Any]],
    left_map: dict[str, str],
    right_map: dict[str, str],
    planned_actions: list[str] | None,
    ignored_direct_rule: dict[str, str] | None,
) -> dict[str, Any]:
    """공식 공개 출처로 대조한 CAS–CAMEO 연결의 파일럿 스크리닝."""

    mapping_provenance = [
        _mapping_provenance("INCIDENT", left_map),
        _mapping_provenance("FACILITY", right_map),
    ]
    result = _approved_cameo_result(
        incident_cas=incident_cas,
        facility_cas=facility_cas,
        screening=screening,
        left_map=left_map,
        right_map=right_map,
        planned_actions=planned_actions,
    )
    result.update(
        {
            "policy_mode": PUBLIC_SOURCE_PILOT_POLICY,
            "expert_reviewed": False,
            "mapping_provenance": mapping_provenance,
            "evidence_provenance": {
                "basis": "PUBLIC_OFFICIAL_SOURCE",
                "source_product": PUBLIC_SOURCE_PRODUCT,
                "source_versions": sorted(
                    {
                        item["source_version"]
                        for item in mapping_provenance
                        if item["source_version"]
                    }
                ),
                "mapping_evidence_urls": [
                    item["evidence_url"] for item in mapping_provenance
                ],
                "compatibility_evidence_urls": list(
                    dict.fromkeys(
                        str(item.get("source_url") or "")
                        for item in screening
                        if item.get("source_url")
                    )
                ),
            },
        }
    )
    if ignored_direct_rule is not None:
        result["ignored_direct_rule_ids"] = [ignored_direct_rule.get("rule_id", "")]

    if result.get("status") != "COMPLETED":
        return result

    result.update(
        {
            "status": "SCREENING_COMPLETED",
            "scope": PUBLIC_SOURCE_SCOPE,
            "brief_text": PUBLIC_SCREENING_BRIEF_TEMPLATE.format(
                risk_level_ko=result["risk_level_ko"]
            ),
            "limitations": list(PUBLIC_SCREENING_LIMITATIONS),
        }
    )
    return result


def _direct_rule_result(
    *,
    direct: dict[str, str],
    incident_cas: str,
    facility_cas: str,
    planned_actions: list[str] | None,
) -> dict[str, Any]:
    """승인 상태를 확인한 직접 Rule을 crosswalk와 독립적으로 반환한다."""

    severity = direct["severity"]
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(f"허용되지 않은 Rule severity: {severity}")
    risk_mapping = SEVERITY_RISK_LEVELS.get(severity)
    if risk_mapping is None:
        raise ValueError(f"완료 Rule에 등급 매핑이 없는 severity: {severity}")
    risk_level, risk_level_ko = risk_mapping
    actions = planned_actions or []
    approval_status = direct["approval_status"]
    return {
        "status": "COMPLETED_DEMO" if approval_status != "APPROVED" else "COMPLETED",
        "scope": approval_status,
        "incident_cas": incident_cas,
        "facility_cas": facility_cas,
        "rule_id": direct["rule_id"],
        "rule_version": direct["rule_version"],
        "severity": severity,
        "risk_level": risk_level,
        "risk_level_ko": risk_level_ko,
        "risk_scale": {
            "type": "ORDINAL_RULE_CLASSIFICATION",
            "is_probability": False,
            "probability_percent": None,
        },
        "hazard_codes": [item for item in direct["hazard_codes"].split("|") if item],
        "brief_text": direct["brief_text_ko"],
        "required_checks": [
            item for item in direct["required_checks"].split("|") if item
        ],
        "evidence_urls": [item for item in direct["evidence_urls"].split("|") if item],
        "cameo_group_screening": [],
        "planned_actions": [
            {"raw_text": action, "status": "UNVALIDATED_ACTION_INPUT"}
            for action in actions
        ],
        "limitations": [
            "시설물질의 현재 존재·수량·저장위치는 이 데이터로 확인되지 않습니다.",
            "대응행위 Rule이 승인되지 않아 입력된 대응의 허용·금지를 판단하지 않습니다.",
            "공모전 시연 Rule은 실제 현장 명령이 아닙니다."
            if approval_status != "APPROVED"
            else "최종 결정은 현장 지휘관이 수행합니다.",
        ],
        "final_decision": "현장 지휘관 판단",
        "human_confirmation_required": True,
    }


def review_pair(
    incident_cas: str,
    facility_cas: str,
    db_path: Path,
    planned_actions: list[str] | None = None,
    allow_demo_rules: bool = False,
    config_dir: Path = CONFIG_DIR,
    policy_mode: str = APPROVED_ONLY_POLICY,
) -> dict[str, Any]:
    if policy_mode not in SUPPORTED_POLICY_MODES:
        raise ValueError(f"지원하지 않는 conflict policy_mode: {policy_mode}")

    incident_cas = normalize_cas(incident_cas)
    facility_cas = normalize_cas(facility_cas)
    invalid = [
        cas for cas in (incident_cas, facility_cas) if not valid_cas_checksum(cas)
    ]
    if invalid:
        return {
            "status": "INVALID_INPUT",
            "invalid_cas": invalid,
            "severity": None,
            "human_confirmation_required": True,
        }

    direct = _direct_rule(incident_cas, facility_cas, config_dir)
    if direct and policy_mode == APPROVED_ONLY_POLICY:
        if direct["approval_status"] != "APPROVED" and not allow_demo_rules:
            return {
                "status": "VERIFY_REQUIRED",
                "severity": None,
                "reason": "직접 물질쌍 Rule이 APPROVED 상태가 아닙니다.",
                "rule_id": direct["rule_id"],
                "approval_status": direct["approval_status"],
                "hint": "APPROVED 상태의 운영 규칙을 준비하거나 공개 근거 파일럿 정책을 사용하세요.",
                "human_confirmation_required": True,
            }
        return _direct_rule_result(
            direct=direct,
            incident_cas=incident_cas,
            facility_cas=facility_cas,
            planned_actions=planned_actions,
        )

    public_policy: dict[str, Any] | None = None
    if policy_mode == PUBLIC_SOURCE_PILOT_POLICY:
        try:
            public_policy = _public_source_policy(config_dir)
        except (OSError, json.JSONDecodeError, ValueError) as error:
            return {
                "status": "VERIFY_REQUIRED",
                "severity": None,
                "reason": "공개근거 파일럿 정책 설정을 검증할 수 없습니다.",
                "configuration_error": type(error).__name__,
                "policy_mode": policy_mode,
                "expert_reviewed": False,
                "human_confirmation_required": True,
            }

    crosswalk = _crosswalk(config_dir)
    left_map = crosswalk.get(incident_cas)
    right_map = crosswalk.get(facility_cas)
    if not left_map or not right_map:
        missing_result: dict[str, Any] = {
            "status": "UNCLASSIFIED",
            "severity": None,
            "reason": "검증 대상 CAS–CAMEO 교차표가 없습니다.",
            "human_confirmation_required": True,
        }
        if public_policy is not None:
            missing_result.update(
                {
                    "policy_mode": policy_mode,
                    "expert_reviewed": False,
                }
            )
        return missing_result

    map_statuses = {left_map["verification_status"], right_map["verification_status"]}
    if public_policy is not None:
        eligible_statuses = set(public_policy["eligible_crosswalk_statuses"])
        if not map_statuses.issubset(eligible_statuses):
            return {
                "status": "VERIFY_REQUIRED",
                "severity": None,
                "reason": "공개 원자료 대조가 완료되지 않은 CAS–CAMEO 연결이 포함되어 있습니다.",
                "mapping_statuses": sorted(map_statuses),
                "policy_mode": policy_mode,
                "expert_reviewed": False,
                "human_confirmation_required": True,
            }

        provenance_errors = {
            role: errors
            for role, errors in (
                ("incident", _public_mapping_errors(left_map, public_policy)),
                ("facility", _public_mapping_errors(right_map, public_policy)),
            )
            if errors
        }
        if provenance_errors:
            return {
                "status": "VERIFY_REQUIRED",
                "severity": None,
                "reason": "공개근거 CAS–CAMEO 연결의 provenance가 불완전합니다.",
                "mapping_provenance_errors": provenance_errors,
                "policy_mode": policy_mode,
                "expert_reviewed": False,
                "human_confirmation_required": True,
            }

        screening = _cameo_screening(
            db_path,
            left_map["cameo_chemical_id"],
            right_map["cameo_chemical_id"],
        )
        if screening:
            return _public_source_cameo_result(
                incident_cas=incident_cas,
                facility_cas=facility_cas,
                screening=screening,
                left_map=left_map,
                right_map=right_map,
                planned_actions=planned_actions,
                ignored_direct_rule=direct,
            )
        return {
            "status": "UNCLASSIFIED",
            "severity": None,
            "reason": "공개근거 crosswalk는 확인됐지만 CAMEO 그룹 조합 결과가 없습니다.",
            "policy_mode": policy_mode,
            "expert_reviewed": False,
            "mapping_provenance": [
                _mapping_provenance("INCIDENT", left_map),
                _mapping_provenance("FACILITY", right_map),
            ],
            "human_confirmation_required": True,
        }

    approved = map_statuses == {"APPROVED"}
    if not approved and not allow_demo_rules:
        return {
            "status": "VERIFY_REQUIRED",
            "severity": None,
            "reason": "CAS–CAMEO 교차표가 전문가 APPROVED 상태가 아닙니다.",
            "mapping_statuses": sorted(map_statuses),
            "hint": "APPROVED 상태의 운영 매핑을 준비하거나 공개 근거 파일럿 정책을 사용하세요.",
            "human_confirmation_required": True,
        }

    screening = _cameo_screening(
        db_path,
        left_map["cameo_chemical_id"],
        right_map["cameo_chemical_id"],
    )
    if approved and screening:
        return _approved_cameo_result(
            incident_cas=incident_cas,
            facility_cas=facility_cas,
            screening=screening,
            left_map=left_map,
            right_map=right_map,
            planned_actions=planned_actions,
        )
    return {
        "status": "CAMEO_GROUP_SCREENING_ONLY" if screening else "UNCLASSIFIED",
        "severity": None,
        "screening": screening,
        "reason": (
            "CAMEO 그룹 조회 결과는 있으나 CAS–CAMEO 연결이 전문가 승인 상태가 아닙니다."
            if screening
            else "승인된 CAMEO 그룹 조회 결과가 없습니다."
        ),
        "scope": "SIMULATED_PROTOTYPE" if not approved else "APPROVED_MAPPING_ONLY",
        "human_confirmation_required": True,
    }


def validate_review_output(payload: dict[str, Any]) -> list[str]:
    """생성형 요약 뒤에도 반드시 유지할 최소 불변조건."""

    errors: list[str] = []
    completed_statuses = {"COMPLETED", "COMPLETED_DEMO", "SCREENING_COMPLETED"}
    if payload.get("status") in completed_statuses:
        severity = payload.get("severity")
        if severity not in ALLOWED_SEVERITIES:
            errors.append("severity 누락 또는 비허용 값")
        expected_levels = SEVERITY_RISK_LEVELS.get(str(severity))
        if expected_levels is None:
            errors.append("완료 severity에 대응하는 risk_level 매핑이 없습니다.")
        elif (
            payload.get("risk_level") != expected_levels[0]
            or payload.get("risk_level_ko") != expected_levels[1]
        ):
            errors.append("severity와 risk_level 매핑이 일치하지 않습니다.")

        risk_scale = payload.get("risk_scale")
        if not isinstance(risk_scale, dict):
            errors.append("risk_scale 누락 또는 형식 오류")
        else:
            if risk_scale.get("is_probability") is not False:
                errors.append("risk_scale.is_probability는 false여야 합니다.")
            if (
                "probability_percent" not in risk_scale
                or risk_scale.get("probability_percent") is not None
            ):
                errors.append("risk_scale.probability_percent는 null이어야 합니다.")

            scale_type = risk_scale.get("type")
            allowed_scale_types = {
                "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
                "ORDINAL_RULE_CLASSIFICATION",
            }
            if scale_type not in allowed_scale_types:
                errors.append("지원하지 않는 risk_scale.type입니다.")

            cameo_scopes = {
                "APPROVED_CAMEO_GROUP_SCREENING",
                PUBLIC_SOURCE_SCOPE,
            }
            cameo_scope = payload.get("scope") in cameo_scopes
            if cameo_scope and scale_type != "ORDINAL_CAMEO_COMPATIBILITY_CLASS":
                errors.append(
                    "CAMEO screening 완료 결과의 risk_scale.type이 올바르지 않습니다."
                )
            if not cameo_scope and scale_type == "ORDINAL_CAMEO_COMPATIBILITY_CLASS":
                errors.append("CAMEO risk_scale에는 승인 screening scope가 필요합니다.")
            if scale_type == "ORDINAL_CAMEO_COMPATIBILITY_CLASS":
                raw_class = risk_scale.get("raw_class_id")
                classification = CAMEO_CLASS_RESULTS.get(str(raw_class))
                if classification is None:
                    errors.append("지원하지 않거나 누락된 CAMEO raw class입니다.")
                elif any(
                    payload.get(field) != classification[field]
                    for field in ("severity", "risk_level", "risk_level_ko")
                ):
                    errors.append("CAMEO raw class와 등급 매핑이 일치하지 않습니다.")
        if not payload.get("rule_id"):
            errors.append("rule_id 누락")
        if not payload.get("evidence_urls"):
            errors.append("evidence_urls 누락")
        if payload.get("final_decision") != "현장 지휘관 판단":
            errors.append("최종 결정자 문구 변경")
        if payload.get("status") == "SCREENING_COMPLETED":
            if payload.get("scope") != PUBLIC_SOURCE_SCOPE:
                errors.append("공개근거 screening scope 누락 또는 오류")
            if payload.get("policy_mode") != PUBLIC_SOURCE_PILOT_POLICY:
                errors.append("공개근거 screening policy_mode 누락 또는 오류")
            if payload.get("expert_reviewed") is not False:
                errors.append("공개근거 screening expert_reviewed는 false여야 합니다.")
            if payload.get("human_confirmation_required") is not True:
                errors.append("공개근거 screening은 현장 확인 필수 표시가 필요합니다.")

            mapping_provenance = payload.get("mapping_provenance")
            if not isinstance(mapping_provenance, list) or len(mapping_provenance) != 2:
                errors.append(
                    "공개근거 screening mapping_provenance는 두 건이어야 합니다."
                )
            elif not all(isinstance(item, dict) for item in mapping_provenance):
                errors.append("공개근거 mapping provenance 형식이 올바르지 않습니다.")
            else:
                if {item.get("role") for item in mapping_provenance} != {
                    "INCIDENT",
                    "FACILITY",
                }:
                    errors.append(
                        "공개근거 mapping provenance 역할이 올바르지 않습니다."
                    )
                for item in mapping_provenance:
                    if item.get("verification_status") != PUBLIC_SOURCE_VERIFIED:
                        errors.append("공개근거 mapping verification_status 오류")
                    if item.get("verification_method") != PUBLIC_SOURCE_METHOD:
                        errors.append("공개근거 mapping verification_method 오류")
                    if item.get("source_product") != PUBLIC_SOURCE_PRODUCT:
                        errors.append("공개근거 mapping source_product 오류")
                    if not item.get("source_version") or not item.get("checked_at_utc"):
                        errors.append("공개근거 mapping 버전 또는 확인시각 누락")
                    expected_url = (
                        "https://cameochemicals.noaa.gov/chemical/"
                        f"{item.get('cameo_chemical_id', '')}"
                    )
                    if item.get("evidence_url") != expected_url:
                        errors.append("공개근거 mapping evidence_url 오류")

            evidence_provenance = payload.get("evidence_provenance")
            if not isinstance(evidence_provenance, dict):
                errors.append("공개근거 screening evidence_provenance 누락")
            elif (
                evidence_provenance.get("basis") != "PUBLIC_OFFICIAL_SOURCE"
                or evidence_provenance.get("source_product") != PUBLIC_SOURCE_PRODUCT
            ):
                errors.append("공개근거 screening evidence provenance 오류")
    if payload.get("severity") == "SAFE":
        errors.append("SAFE 상태 사용 금지")
    return errors
