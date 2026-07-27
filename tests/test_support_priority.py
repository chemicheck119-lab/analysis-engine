from __future__ import annotations

import csv
from pathlib import Path

import pytest

from chemiguard119.support_priority import (
    SCHEMA_VERSION,
    SupportPriorityError,
    build_support_priority,
)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    facility = tmp_path / "facility.csv"
    kosha = tmp_path / "kosha.csv"
    crosswalk = tmp_path / "crosswalk.csv"
    fire = tmp_path / "fire.csv"

    _write_csv(
        facility,
        [
            "CAS번호",
            "화학물질명_선정기준",
            "업체명",
            "주소",
            "정확CAS모델링사용여부",
            "PRTR_업체_업체명주소정확매칭",
            "PRTR_물질_CAS정확매칭",
            "울산소방_사고자료행수",
        ],
        [
            {
                "CAS번호": "7681-52-9",
                "화학물질명_선정기준": "차아염소산나트륨",
                "업체명": "테스트전자",
                "주소": "경기도 화성시",
                "정확CAS모델링사용여부": "Y",
                "PRTR_업체_업체명주소정확매칭": "Y",
                "PRTR_물질_CAS정확매칭": "Y",
                "울산소방_사고자료행수": "2",
            },
            {
                "CAS번호": "7647-01-0",
                "화학물질명_선정기준": "염산",
                "업체명": "테스트전자",
                "주소": "경기도 화성시",
                "정확CAS모델링사용여부": "Y",
                "PRTR_업체_업체명주소정확매칭": "N",
                "PRTR_물질_CAS정확매칭": "Y",
                "울산소방_사고자료행수": "1",
            },
            {
                "CAS번호": "67-64-1",
                "화학물질명_선정기준": "아세톤",
                "업체명": "테스트화학",
                "주소": "울산광역시",
                "정확CAS모델링사용여부": "Y",
                "PRTR_업체_업체명주소정확매칭": "N",
                "PRTR_물질_CAS정확매칭": "N",
                "울산소방_사고자료행수": "0",
            },
            {
                "CAS번호": "64-17-5",
                "화학물질명_선정기준": "에탄올",
                "업체명": "부분일치업체",
                "주소": "울산광역시",
                "정확CAS모델링사용여부": "N",
                "PRTR_업체_업체명주소정확매칭": "Y",
                "PRTR_물질_CAS정확매칭": "Y",
                "울산소방_사고자료행수": "99",
            },
        ],
    )
    _write_csv(
        kosha,
        ["CAS번호", "화학물질명_국문"],
        [
            {"CAS번호": "7681-52-9", "화학물질명_국문": "차아염소산나트륨"},
            {"CAS번호": "7647-01-0", "화학물질명_국문": "염산"},
        ],
    )
    _write_csv(
        crosswalk,
        ["cas_number", "verification_status"],
        [
            {
                "cas_number": "7681-52-9",
                "verification_status": "PUBLIC_SOURCE_VERIFIED",
            },
            {
                "cas_number": "7647-01-0",
                "verification_status": "PUBLIC_SOURCE_VERIFIED",
            },
            {
                "cas_number": "67-64-1",
                "verification_status": "CANDIDATE_UNVERIFIED",
            },
            {
                "cas_number": "64-17-5",
                "verification_status": "CANDIDATE_UNVERIFIED",
            },
        ],
    )
    _write_csv(
        fire,
        ["CAS번호", "화학물질명_한글"],
        [
            {"CAS번호": "7681-52-9", "화학물질명_한글": "차아염소산나트륨"},
            {"CAS번호": "67-64-1", "화학물질명_한글": "아세톤"},
            {"CAS번호": "67-64-1", "화학물질명_한글": "아세톤"},
            {"CAS번호": "67-64-1", "화학물질명_한글": "아세톤"},
        ],
    )
    return facility, kosha, crosswalk, fire


def test_priority_separates_demo_readiness_from_data_expansion(
    tmp_path: Path,
) -> None:
    facility, kosha, crosswalk, fire = _inputs(tmp_path)

    rows = build_support_priority(
        facility_path=facility,
        kosha_path=kosha,
        crosswalk_path=crosswalk,
        fire_incident_paths=[fire],
    )
    by_cas = {row["cas_number"]: row for row in rows}

    assert by_cas["7681-52-9"]["coverage_tier"] == "END_TO_END_READY"
    assert by_cas["7681-52-9"]["demo_rank"] == 1
    assert by_cas["67-64-1"]["coverage_tier"] == "MSDS_AND_CAMEO_GAP"
    assert by_cas["67-64-1"]["expansion_rank"] == 1
    assert (
        by_cas["67-64-1"]["expansion_rank"]
        < by_cas["7681-52-9"]["expansion_rank"]
    )
    assert by_cas["67-64-1"]["missing_official_evidence"] == [
        "KOSHA_MSDS",
        "CAMEO_PUBLIC_CROSSWALK",
    ]


def test_priority_is_not_probability_and_does_not_confirm_inventory(
    tmp_path: Path,
) -> None:
    facility, kosha, crosswalk, fire = _inputs(tmp_path)

    rows = build_support_priority(
        facility_path=facility,
        kosha_path=kosha,
        crosswalk_path=crosswalk,
        fire_incident_paths=[fire],
    )

    assert all(row["schema_version"] == SCHEMA_VERSION for row in rows)
    assert all(row["is_probability"] is False for row in rows)
    assert all(row["current_inventory_confirmed"] is False for row in rows)
    assert all("확률" in row["interpretation"] for row in rows)


def test_unverified_cameo_candidate_is_not_counted_as_ready(
    tmp_path: Path,
) -> None:
    facility, kosha, crosswalk, fire = _inputs(tmp_path)

    rows = build_support_priority(
        facility_path=facility,
        kosha_path=kosha,
        crosswalk_path=crosswalk,
        fire_incident_paths=[fire],
    )
    acetone = next(row for row in rows if row["cas_number"] == "67-64-1")

    assert acetone["cameo_public_verified"] is False
    assert acetone["coverage_tier"] == "MSDS_AND_CAMEO_GAP"


def test_facility_rows_without_exact_cas_permission_are_excluded(
    tmp_path: Path,
) -> None:
    facility, kosha, crosswalk, fire = _inputs(tmp_path)

    rows = build_support_priority(
        facility_path=facility,
        kosha_path=kosha,
        crosswalk_path=crosswalk,
        fire_incident_paths=[fire],
    )
    ethanol = next(row for row in rows if row["cas_number"] == "64-17-5")

    assert ethanol["facility_count"] == 0
    assert ethanol["fire_incident_rows"] == 0


def test_invalid_fire_cas_is_excluded_and_reported(tmp_path: Path) -> None:
    facility, kosha, crosswalk, _ = _inputs(tmp_path)
    invalid_fire = tmp_path / "invalid_fire.csv"
    _write_csv(
        invalid_fire,
        ["CAS번호"],
        [
            {"CAS번호": "1234-INVALID"},
            {"CAS번호": "67-64-1"},
        ],
    )
    diagnostics: dict[str, object] = {}

    rows = build_support_priority(
        facility_path=facility,
        kosha_path=kosha,
        crosswalk_path=crosswalk,
        fire_incident_paths=[invalid_fire],
        diagnostics=diagnostics,
    )

    acetone = next(row for row in rows if row["cas_number"] == "67-64-1")
    assert acetone["fire_incident_rows"] == 1
    assert diagnostics["fire_incident_sources"] == [
        {
            "file": "invalid_fire.csv",
            "total_rows": 2,
            "valid_cas_rows": 1,
            "invalid_or_blank_cas_rows": 1,
            "invalid_cas_policy": "EXCLUDE_AND_REPORT",
        }
    ]


def test_invalid_crosswalk_cas_still_fails_the_offline_pipeline(
    tmp_path: Path,
) -> None:
    facility, kosha, _, fire = _inputs(tmp_path)
    invalid_crosswalk = tmp_path / "invalid_crosswalk.csv"
    _write_csv(
        invalid_crosswalk,
        ["cas_number", "verification_status"],
        [
            {
                "cas_number": "1234-INVALID",
                "verification_status": "PUBLIC_SOURCE_VERIFIED",
            }
        ],
    )

    with pytest.raises(SupportPriorityError, match="CAS가 유효하지 않습니다"):
        build_support_priority(
            facility_path=facility,
            kosha_path=kosha,
            crosswalk_path=invalid_crosswalk,
            fire_incident_paths=[fire],
        )
