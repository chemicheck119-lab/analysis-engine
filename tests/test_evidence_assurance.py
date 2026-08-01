from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from chemiguard119.evidence_assurance import (
    ReferenceAssuranceError,
    build_reference_assurance,
    reference_assurance_configuration_status,
    validate_reference_assurance,
)
from chemiguard119.dashboard_contract import DashboardReferenceAssurance
from chemiguard119.paths import CONFIG_DIR


def _rule_result(
    incident_cas: str = "7681-52-9",
    facility_cas: str = "7647-01-0",
    gas_products: list[str] | None = None,
) -> dict:
    return {
        "incident_cas": incident_cas,
        "facility_cas": facility_cas,
        "gas_products": ["Cl2"] if gas_products is None else gas_products,
    }


def _copy_registry(tmp_path: Path) -> tuple[Path, dict]:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    payload = json.loads(
        (CONFIG_DIR / "reference_assurance_registry.json").read_text(encoding="utf-8")
    )
    (config_dir / "reference_assurance_registry.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return config_dir, payload


def test_demo_pair_is_triangulated_by_four_independent_authorities() -> None:
    result = build_reference_assurance(_rule_result(), CONFIG_DIR)

    assert result["status"] == "REFERENCE_TRIANGULATED"
    assert result["claim_id"] == "HYPOCHLORITE_ACID_CHLORINE_RELEASE_V1"
    assert result["reference_count"] == 5
    assert result["independent_authority_count"] == 4
    assert result["machine_checked"] is True
    assert result["expert_reviewed"] is False
    assert result["human_expert_substitute"] is False
    assert validate_reference_assurance(result, "7681-52-9", "7647-01-0") == []


def test_configuration_status_exposes_scope_without_claiming_expert_review() -> None:
    status = reference_assurance_configuration_status(CONFIG_DIR)

    assert status["ready"] is True
    assert status["authority_count"] == 5
    assert status["triangulated_pair_count"] == 2
    assert status["registry_sha256"]
    assert status["expert_reviewed"] is False
    assert status["human_expert_substitute"] is False


def test_pair_order_does_not_change_reference_claim() -> None:
    forward = build_reference_assurance(_rule_result(), CONFIG_DIR)
    reverse = build_reference_assurance(
        _rule_result("7647-01-0", "7681-52-9"), CONFIG_DIR
    )

    assert reverse["claim_id"] == forward["claim_id"]
    assert reverse["cas_pair"] == ["7647-01-0", "7681-52-9"]


def test_unregistered_pair_stays_primary_authority_only() -> None:
    result = build_reference_assurance(
        _rule_result("64-17-5", "67-64-1", gas_products=[]), CONFIG_DIR
    )

    assert result["status"] == "PRIMARY_AUTHORITY_ONLY"
    assert result["independent_authority_count"] == 1
    assert result["claim_id"] is None
    assert result["expert_reviewed"] is False
    assert validate_reference_assurance(result, "64-17-5", "67-64-1") == []


def test_sodium_hydrochloric_acid_pair_is_triangulated() -> None:
    result = build_reference_assurance(
        _rule_result("7440-23-5", "7647-01-0", gas_products=["H2"]), CONFIG_DIR
    )

    assert result["status"] == "REFERENCE_TRIANGULATED"
    assert result["claim_id"] == "SODIUM_HYDROCHLORIC_ACID_HYDROGEN_FIRE_V1"
    assert result["reference_count"] == 3
    assert result["independent_authority_count"] == 3
    assert result["expected_gas_products"] == ["H2"]
    assert result["expert_reviewed"] is False
    assert validate_reference_assurance(result, "7440-23-5", "7647-01-0") == []
    assert (
        DashboardReferenceAssurance.model_validate(result).claim_id
        == result["claim_id"]
    )


def test_claim_gas_mismatch_fails_closed() -> None:
    with pytest.raises(ReferenceAssuranceError, match="예상 생성물"):
        build_reference_assurance(_rule_result(gas_products=[]), CONFIG_DIR)


def test_untrusted_source_host_invalidates_registry(tmp_path: Path) -> None:
    config_dir, payload = _copy_registry(tmp_path)
    forged = deepcopy(payload)
    forged["pair_claims"][0]["sources"][0]["source_url"] = "https://example.com/forged"
    (config_dir / "reference_assurance_registry.json").write_text(
        json.dumps(forged, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ReferenceAssuranceError, match="allowlist"):
        build_reference_assurance(_rule_result(), config_dir)


def test_html_source_without_locator_probe_invalidates_registry(tmp_path: Path) -> None:
    config_dir, payload = _copy_registry(tmp_path)
    forged = deepcopy(payload)
    forged["pair_claims"][0]["sources"][0].pop("content_probe")
    (config_dir / "reference_assurance_registry.json").write_text(
        json.dumps(forged, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ReferenceAssuranceError, match="문서 위치 probe"):
        build_reference_assurance(_rule_result(), config_dir)


def test_ai_audit_cannot_promote_itself_to_human_expert(tmp_path: Path) -> None:
    config_dir, payload = _copy_registry(tmp_path)
    forged = deepcopy(payload)
    forged["expert_reviewed"] = True
    (config_dir / "reference_assurance_registry.json").write_text(
        json.dumps(forged, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ReferenceAssuranceError, match="전문가 검토 완료"):
        build_reference_assurance(_rule_result(), config_dir)

    status = reference_assurance_configuration_status(config_dir)
    assert status["ready"] is False
    assert status["expert_reviewed"] is False


def test_validator_rejects_forged_expert_and_field_claims() -> None:
    result = build_reference_assurance(_rule_result(), CONFIG_DIR)
    forged = deepcopy(result)
    forged["expert_reviewed"] = True
    forged["human_expert_substitute"] = True
    for check in forged["claim_checks"]:
        if check["claim"] == "CURRENT_SITE_INVENTORY":
            check["status"] = "PASSED"

    errors = validate_reference_assurance(forged, "7681-52-9", "7647-01-0")

    assert any("전문가 검토 완료" in error for error in errors)
    assert any("사람 전문가 대체" in error for error in errors)
    assert any("현재 재고" in error for error in errors)
