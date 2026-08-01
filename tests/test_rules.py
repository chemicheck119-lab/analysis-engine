from __future__ import annotations

import csv
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest

from chemiguard119.paths import CONFIG_DIR
from chemiguard119.rules import (
    ALLOWED_SEVERITIES,
    PUBLIC_SOURCE_PILOT_POLICY,
    review_pair,
    validate_review_output,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _demo_rule_fixture(tmp_path: Path) -> tuple[Path, Path]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_csv(
        config_dir / "cameo_crosswalk_demo.csv",
        [
            "cas_number",
            "cameo_chemical_id",
            "selected_form",
            "verification_status",
            "evidence_url",
            "notes",
        ],
        [
            {
                "cas_number": "7681-52-9",
                "cameo_chemical_id": "4503",
                "selected_form": "SODIUM HYPOCHLORITE",
                "verification_status": "SIMULATED_PROTOTYPE",
                "evidence_url": "https://example.test/4503",
                "notes": "test-only",
            },
            {
                "cas_number": "7647-01-0",
                "cameo_chemical_id": "3598",
                "selected_form": "HYDROCHLORIC ACID, SOLUTION",
                "verification_status": "SIMULATED_PROTOTYPE",
                "evidence_url": "https://example.test/3598",
                "notes": "test-only",
            },
        ],
    )
    _write_csv(
        config_dir / "demo_pair_rules.csv",
        [
            "rule_id",
            "rule_version",
            "cas_a",
            "cas_b",
            "severity",
            "hazard_codes",
            "brief_text_ko",
            "required_checks",
            "evidence_urls",
            "approval_status",
        ],
        [
            {
                "rule_id": "TEST-ACID-HYPOCHLORITE-001",
                "rule_version": "0.1.0-demo",
                "cas_a": "7647-01-0",
                "cas_b": "7681-52-9",
                "severity": "HIGH_RISK",
                "hazard_codes": "TOXIC_GAS|EXOTHERMIC",
                "brief_text_ko": "산성 물질과 접촉하면 염소가스 발생 가능",
                "required_checks": "저장구역 확인|배수로 연결 확인|실제 혼합 확인",
                "evidence_urls": "https://example.test/4503|https://example.test/reactivity",
                "approval_status": "SIMULATED_PROTOTYPE",
            }
        ],
    )

    db_path = tmp_path / "rules.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE cameo_mapping (cameo_chemical_id TEXT, reactive_group_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO cameo_mapping VALUES (?, ?)",
            [("4503", "10"), ("3598", "20")],
        )
        connection.execute(
            """
            CREATE TABLE compatibility (
                pair_id TEXT,
                group_a_id TEXT,
                group_b_id TEXT,
                compatibility_label TEXT,
                compatibility_class_id TEXT,
                hazard_codes TEXT,
                hazard_text TEXT,
                gases TEXT,
                source_url TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO compatibility VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "10-20",
                "10",
                "20",
                "INCOMPATIBLE",
                "3",
                "TOXIC_GAS",
                "Toxic gas generation possible",
                "Chlorine",
                "https://example.test/reactivity",
            ),
        )
    return db_path, config_dir


def test_demo_rule_is_blocked_by_default_and_requires_explicit_gate(
    tmp_path: Path,
) -> None:
    db_path, config_dir = _demo_rule_fixture(tmp_path)

    result = review_pair("7681-52-9", "7647-01-0", db_path, config_dir=config_dir)

    assert result["status"] == "VERIFY_REQUIRED"
    assert result["severity"] is None
    assert result["human_confirmation_required"] is True
    assert "공개 근거 파일럿" in result["hint"]


def test_allow_demo_gate_returns_labeled_demo_output_only(tmp_path: Path) -> None:
    db_path, config_dir = _demo_rule_fixture(tmp_path)

    result = review_pair(
        "7681-52-9",
        "7647-01-0",
        db_path,
        planned_actions=["주수 검토"],
        allow_demo_rules=True,
        config_dir=config_dir,
    )

    assert result["status"] == "COMPLETED_DEMO"
    assert result["scope"] == "SIMULATED_PROTOTYPE"
    assert result["severity"] == "HIGH_RISK"
    assert result["rule_id"] == "TEST-ACID-HYPOCHLORITE-001"
    assert result["final_decision"] == "현장 지휘관 판단"
    assert result["human_confirmation_required"] is True
    assert result["planned_actions"] == [
        {"raw_text": "주수 검토", "status": "UNVALIDATED_ACTION_INPUT"}
    ]
    assert result["cameo_group_screening"] == []
    assert validate_review_output(result) == []


def test_approved_direct_rule_is_independent_of_crosswalk_and_cameo_database(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_csv(
        config_dir / "demo_pair_rules.csv",
        [
            "rule_id",
            "rule_version",
            "cas_a",
            "cas_b",
            "severity",
            "hazard_codes",
            "brief_text_ko",
            "required_checks",
            "evidence_urls",
            "approval_status",
        ],
        [
            {
                "rule_id": "APPROVED-DIRECT-001",
                "rule_version": "1.0.0",
                "cas_a": "64-17-5",
                "cas_b": "67-64-1",
                "severity": "CAUTION",
                "hazard_codes": "DIRECT_RULE",
                "brief_text_ko": "전문가가 승인한 직접 물질쌍 Rule",
                "required_checks": "현장 상태 재확인",
                "evidence_urls": "https://example.test/approved-direct-rule",
                "approval_status": "APPROVED",
            }
        ],
    )

    result = review_pair(
        "64-17-5",
        "67-64-1",
        tmp_path / "missing-and-must-not-be-opened.sqlite",
        config_dir=config_dir,
    )

    assert result["status"] == "COMPLETED"
    assert result["scope"] == "APPROVED"
    assert result["rule_id"] == "APPROVED-DIRECT-001"
    assert result["severity"] == "CAUTION"
    assert result["risk_level"] == "MEDIUM"
    assert result["cameo_group_screening"] == []
    assert validate_review_output(result) == []


def test_safe_is_not_an_allowed_or_validated_rule_output() -> None:
    assert "SAFE" not in ALLOWED_SEVERITIES
    unsafe_payload = {
        "status": "COMPLETED",
        "severity": "SAFE",
        "rule_id": "SHOULD-NOT-PASS",
        "evidence_urls": ["https://example.test/evidence"],
        "final_decision": "현장 지휘관 판단",
    }

    errors = validate_review_output(unsafe_payload)

    assert "severity 누락 또는 비허용 값" in errors
    assert "SAFE 상태 사용 금지" in errors


def _valid_completed_cameo_payload() -> dict:
    return {
        "status": "COMPLETED",
        "scope": "APPROVED_CAMEO_GROUP_SCREENING",
        "severity": "HIGH_RISK",
        "risk_level": "HIGH",
        "risk_level_ko": "높음",
        "risk_scale": {
            "type": "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
            "raw_class_id": 2,
            "is_probability": False,
            "probability_percent": None,
        },
        "rule_id": "CAMEO-REACTIVE-GROUP-COMPATIBILITY-MATRIX",
        "evidence_urls": ["https://example.test/reactivity"],
        "final_decision": "현장 지휘관 판단",
    }


def test_validate_completed_output_rejects_probability_semantics() -> None:
    valid = _valid_completed_cameo_payload()
    assert validate_review_output(valid) == []

    probability = deepcopy(valid)
    probability["risk_scale"]["is_probability"] = True
    probability["risk_scale"]["probability_percent"] = 90
    errors = validate_review_output(probability)

    assert "risk_scale.is_probability는 false여야 합니다." in errors
    assert "risk_scale.probability_percent는 null이어야 합니다." in errors

    missing_percent = deepcopy(valid)
    missing_percent["risk_scale"].pop("probability_percent")
    assert (
        "risk_scale.probability_percent는 null이어야 합니다."
        in validate_review_output(missing_percent)
    )


def test_validate_completed_output_rejects_severity_and_raw_cameo_class_mismatch() -> (
    None
):
    valid = _valid_completed_cameo_payload()

    severity_mismatch = deepcopy(valid)
    severity_mismatch["risk_level"] = "LOW"
    assert "severity와 risk_level 매핑이 일치하지 않습니다." in validate_review_output(
        severity_mismatch
    )

    raw_class_mismatch = deepcopy(valid)
    raw_class_mismatch["risk_scale"]["raw_class_id"] = 0
    assert "CAMEO raw class와 등급 매핑이 일치하지 않습니다." in validate_review_output(
        raw_class_mismatch
    )

    unsupported_class = deepcopy(valid)
    unsupported_class["risk_scale"]["raw_class_id"] = 3
    assert "지원하지 않거나 누락된 CAMEO raw class입니다." in validate_review_output(
        unsupported_class
    )

    wrong_scope = deepcopy(valid)
    wrong_scope["scope"] = "APPROVED"
    assert (
        "CAMEO risk_scale에는 승인 screening scope가 필요합니다."
        in validate_review_output(wrong_scope)
    )


@pytest.mark.parametrize(
    ("class_id", "label", "expected_severity", "expected_level", "expected_level_ko"),
    [
        ("0", "호환", "NO_KNOWN_HAZARDOUS_REACTION", "LOW", "낮음"),
        ("1", "주의", "CAUTION", "MEDIUM", "중간"),
        ("2", "비호환", "HIGH_RISK", "HIGH", "높음"),
    ],
)
def test_approved_cameo_mapping_returns_non_probability_ordinal_level(
    tmp_path: Path,
    class_id: str,
    label: str,
    expected_severity: str,
    expected_level: str,
    expected_level_ko: str,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_csv(
        config_dir / "cameo_crosswalk_demo.csv",
        [
            "cas_number",
            "cameo_chemical_id",
            "selected_form",
            "verification_status",
            "evidence_url",
            "notes",
        ],
        [
            {
                "cas_number": "64-17-5",
                "cameo_chemical_id": "667",
                "selected_form": "ETHANOL",
                "verification_status": "APPROVED",
                "evidence_url": "https://example.test/667",
                "notes": "expert-reviewed test fixture",
            },
            {
                "cas_number": "67-64-1",
                "cameo_chemical_id": "8",
                "selected_form": "ACETONE",
                "verification_status": "APPROVED",
                "evidence_url": "https://example.test/8",
                "notes": "expert-reviewed test fixture",
            },
        ],
    )
    _write_csv(
        config_dir / "demo_pair_rules.csv",
        [
            "rule_id",
            "rule_version",
            "cas_a",
            "cas_b",
            "severity",
            "hazard_codes",
            "brief_text_ko",
            "required_checks",
            "evidence_urls",
            "approval_status",
        ],
        [],
    )
    db_path = tmp_path / "approved.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE cameo_mapping (cameo_chemical_id TEXT, reactive_group_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO cameo_mapping VALUES (?, ?)",
            [("667", "4"), ("8", "5")],
        )
        connection.execute(
            """
            CREATE TABLE compatibility (
                pair_id TEXT,
                group_a_id TEXT,
                group_b_id TEXT,
                compatibility_label TEXT,
                compatibility_class_id TEXT,
                hazard_codes TEXT,
                hazard_text TEXT,
                gases TEXT,
                source_url TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO compatibility VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "4-5",
                "4",
                "5",
                label,
                class_id,
                "NR" if class_id == "0" else "R1",
                "No known reaction" if class_id == "0" else "Generates heat",
                "",
                "https://example.test/reactivity",
            ),
        )

    result = review_pair("64-17-5", "67-64-1", db_path, config_dir=config_dir)

    assert result["status"] == "COMPLETED"
    assert result["scope"] == "APPROVED_CAMEO_GROUP_SCREENING"
    assert result["severity"] == expected_severity
    assert result["risk_level"] == expected_level
    assert result["risk_level_ko"] == expected_level_ko
    assert result["risk_scale"] == {
        "type": "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
        "raw_class_id": int(class_id),
        "is_probability": False,
        "probability_percent": None,
    }
    assert validate_review_output(result) == []
    if class_id == "0":
        assert any("안전 보장" in item for item in result["limitations"])


def _public_source_fixture(
    tmp_path: Path,
    *,
    facility_status: str = "PUBLIC_SOURCE_VERIFIED",
) -> tuple[Path, Path]:
    config_dir = tmp_path / "public-config"
    config_dir.mkdir()
    fields = [
        "cas_number",
        "cameo_chemical_id",
        "selected_form",
        "verification_status",
        "verification_method",
        "evidence_url",
        "source_product",
        "source_version",
        "checked_at_utc",
        "notes",
    ]
    _write_csv(
        config_dir / "cameo_crosswalk.csv",
        fields,
        [
            {
                "cas_number": "7681-52-9",
                "cameo_chemical_id": "4503",
                "selected_form": "SODIUM HYPOCHLORITE",
                "verification_status": "PUBLIC_SOURCE_VERIFIED",
                "verification_method": "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET",
                "evidence_url": "https://cameochemicals.noaa.gov/chemical/4503",
                "source_product": "NOAA/EPA CAMEO Chemicals",
                "source_version": "3.1.0",
                "checked_at_utc": "2026-07-22T00:00:00+00:00",
                "notes": "official source fixture",
            },
            {
                "cas_number": "7647-01-0",
                "cameo_chemical_id": "3598",
                "selected_form": "HYDROCHLORIC ACID, SOLUTION",
                "verification_status": facility_status,
                "verification_method": "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET",
                "evidence_url": "https://cameochemicals.noaa.gov/chemical/3598",
                "source_product": "NOAA/EPA CAMEO Chemicals",
                "source_version": "3.1.0",
                "checked_at_utc": "2026-07-22T00:00:00+00:00",
                "notes": "official source fixture",
            },
        ],
    )
    _write_csv(
        config_dir / "pair_rules.csv",
        [
            "rule_id",
            "rule_version",
            "cas_a",
            "cas_b",
            "severity",
            "hazard_codes",
            "brief_text_ko",
            "required_checks",
            "evidence_urls",
            "approval_status",
        ],
        [
            {
                "rule_id": "MUST-BE-IGNORED-DEMO",
                "rule_version": "0.1.0-demo",
                "cas_a": "7647-01-0",
                "cas_b": "7681-52-9",
                "severity": "CAUTION",
                "hazard_codes": "FORGED_DEMO",
                "brief_text_ko": "파일럿에서 사용하면 안 되는 직접 Rule",
                "required_checks": "사용 금지",
                "evidence_urls": "https://example.test/demo",
                "approval_status": "SIMULATED_PROTOTYPE",
            }
        ],
    )
    (config_dir / "conflict_policy.json").write_text(
        json.dumps(
            {
                "schema_version": "chemicheck119-conflict-policy-v1",
                "policy_id": "PUBLIC_SOURCE_PILOT_V1",
                "eligible_crosswalk_statuses": ["PUBLIC_SOURCE_VERIFIED"],
                "required_verification_method": (
                    "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET"
                ),
                "required_source_product": "NOAA/EPA CAMEO Chemicals",
                "allow_direct_rules": False,
                "require_two_responder_confirmed_cas": True,
                "decision_support_only": True,
                "expert_review_required": False,
                "probability_output_allowed": False,
                "final_decision_authority": "현장 지휘관 판단",
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "reference_assurance_registry.json").write_text(
        (CONFIG_DIR / "reference_assurance_registry.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    db_path = tmp_path / "public-rules.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE cameo_mapping (cameo_chemical_id TEXT, reactive_group_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO cameo_mapping VALUES (?, ?)",
            [("4503", "44"), ("3598", "1")],
        )
        connection.execute(
            """
            CREATE TABLE compatibility (
                pair_id TEXT,
                group_a_id TEXT,
                group_b_id TEXT,
                compatibility_label TEXT,
                compatibility_class_id TEXT,
                hazard_codes TEXT,
                hazard_text TEXT,
                gases TEXT,
                source_url TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO compatibility VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1-44",
                "1",
                "44",
                "INCOMPATIBLE",
                "2",
                "G|R1|T",
                "Generates toxic chlorine gas",
                "Cl2",
                "https://cameochemicals.noaa.gov/reactivity",
            ),
        )
    return db_path, config_dir


def test_public_source_pilot_returns_labeled_non_probability_screening(
    tmp_path: Path,
) -> None:
    db_path, config_dir = _public_source_fixture(tmp_path)

    result = review_pair(
        "7681-52-9",
        "7647-01-0",
        db_path,
        planned_actions=["누출구역 통제"],
        allow_demo_rules=True,
        config_dir=config_dir,
        policy_mode=PUBLIC_SOURCE_PILOT_POLICY,
    )

    assert result["status"] == "SCREENING_COMPLETED"
    assert result["scope"] == "PUBLIC_SOURCE_CAMEO_SCREENING"
    assert result["policy_mode"] == "PUBLIC_SOURCE_PILOT_V1"
    assert result["expert_reviewed"] is False
    assert result["severity"] == "HIGH_RISK"
    assert result["risk_level"] == "HIGH"
    assert result["risk_scale"] == {
        "type": "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
        "raw_class_id": 2,
        "is_probability": False,
        "probability_percent": None,
    }
    assert result["gas_products"] == ["Cl2"]
    assert result["ignored_direct_rule_ids"] == ["MUST-BE-IGNORED-DEMO"]
    assert result["planned_actions"] == [
        {"raw_text": "누출구역 통제", "status": "UNVALIDATED_ACTION_INPUT"}
    ]
    assert {item["role"] for item in result["mapping_provenance"]} == {
        "INCIDENT",
        "FACILITY",
    }
    assert all(
        item["verification_status"] == "PUBLIC_SOURCE_VERIFIED"
        for item in result["mapping_provenance"]
    )
    assert result["evidence_provenance"] == {
        "basis": "PUBLIC_OFFICIAL_SOURCE",
        "source_product": "NOAA/EPA CAMEO Chemicals",
        "source_versions": ["3.1.0"],
        "mapping_evidence_urls": [
            "https://cameochemicals.noaa.gov/chemical/4503",
            "https://cameochemicals.noaa.gov/chemical/3598",
        ],
        "compatibility_evidence_urls": ["https://cameochemicals.noaa.gov/reactivity"],
    }
    assurance = result["reference_assurance"]
    assert assurance["status"] == "REFERENCE_TRIANGULATED"
    assert assurance["reference_count"] == 5
    assert assurance["independent_authority_count"] == 4
    assert assurance["expert_reviewed"] is False
    assert assurance["human_expert_substitute"] is False
    assert assurance["expected_gas_products"] == ["Cl2"]
    assert validate_review_output(result) == []


def test_public_source_pilot_does_not_promote_unverified_candidate(
    tmp_path: Path,
) -> None:
    db_path, config_dir = _public_source_fixture(
        tmp_path,
        facility_status="CANDIDATE_UNVERIFIED",
    )

    result = review_pair(
        "7681-52-9",
        "7647-01-0",
        db_path,
        config_dir=config_dir,
        policy_mode=PUBLIC_SOURCE_PILOT_POLICY,
    )

    assert result["status"] == "VERIFY_REQUIRED"
    assert result["severity"] is None
    assert result["mapping_statuses"] == [
        "CANDIDATE_UNVERIFIED",
        "PUBLIC_SOURCE_VERIFIED",
    ]
    assert result["expert_reviewed"] is False


def test_public_source_crosswalk_is_not_used_by_default_policy(tmp_path: Path) -> None:
    db_path, config_dir = _public_source_fixture(tmp_path)

    result = review_pair(
        "7681-52-9",
        "7647-01-0",
        db_path,
        config_dir=config_dir,
    )

    assert result["status"] == "VERIFY_REQUIRED"
    assert result["severity"] is None
    assert result["approval_status"] == "SIMULATED_PROTOTYPE"


def test_public_source_completed_output_requires_provenance_and_false_expert_flag(
    tmp_path: Path,
) -> None:
    db_path, config_dir = _public_source_fixture(tmp_path)
    result = review_pair(
        "7681-52-9",
        "7647-01-0",
        db_path,
        config_dir=config_dir,
        policy_mode=PUBLIC_SOURCE_PILOT_POLICY,
    )

    forged = deepcopy(result)
    forged["expert_reviewed"] = True
    forged.pop("evidence_provenance")
    errors = validate_review_output(forged)

    assert "공개근거 screening expert_reviewed는 false여야 합니다." in errors
    assert "공개근거 screening evidence_provenance 누락" in errors


def test_public_source_reference_registry_failure_blocks_risk_output(
    tmp_path: Path,
) -> None:
    db_path, config_dir = _public_source_fixture(tmp_path)
    (config_dir / "reference_assurance_registry.json").write_text(
        '{"schema_version":"forged","expert_reviewed":true}',
        encoding="utf-8",
    )

    result = review_pair(
        "7681-52-9",
        "7647-01-0",
        db_path,
        config_dir=config_dir,
        policy_mode=PUBLIC_SOURCE_PILOT_POLICY,
    )

    assert result["status"] == "VERIFY_REQUIRED"
    assert result["severity"] is None
    assert result["configuration_error"] == "ReferenceAssuranceError"
    assert result["expert_reviewed"] is False


def test_unknown_policy_mode_is_rejected_before_rule_execution(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="지원하지 않는 conflict policy_mode"):
        review_pair(
            "7681-52-9",
            "7647-01-0",
            tmp_path / "must-not-open.sqlite",
            policy_mode="UNSAFE_POLICY",
        )
