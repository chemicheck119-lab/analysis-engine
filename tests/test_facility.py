from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chemiguard119.facility import search_facility_history


@pytest.fixture()
def facility_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "facility.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE facility_candidate (
                facility_name TEXT,
                address TEXT,
                province TEXT,
                industry TEXT,
                cas_number TEXT,
                chemical_name TEXT,
                survey_year TEXT,
                fire_incident_row_count INTEGER,
                kosha_msds_exact_match INTEGER,
                prtr_company_exact_match INTEGER,
                prtr_material_exact_match INTEGER,
                source_url TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO facility_candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "테스트전자 화성공장",
                    "경기도 화성시 팔탄면 119",
                    "경기도",
                    "전자부품 제조업",
                    "7681-52-9",
                    "차아염소산나트륨",
                    "2024",
                    2,
                    1,
                    1,
                    1,
                    "https://example.test/facility",
                ),
                (
                    "테스트전자 화성공장",
                    "경기도 화성시 팔탄면 119",
                    "경기도",
                    "전자부품 제조업",
                    "7647-01-0",
                    "염산",
                    "2023",
                    1,
                    1,
                    0,
                    1,
                    "https://example.test/facility",
                ),
            ],
        )
    return db_path


def test_facility_history_is_never_current_inventory_or_rule_input(
    facility_db: Path,
) -> None:
    result = search_facility_history("테스트전자 화성공장", facility_db, top_k=10)

    assert result["status"] == "CANDIDATES_FOUND"
    assert {row["cas_number"] for row in result["results"]} == {
        "7681-52-9",
        "7647-01-0",
    }
    assert all(row["current_inventory_confirmed"] is False for row in result["results"])
    assert all(row["rule_eligible"] is False for row in result["results"])
    assert all(
        row["requires_on_site_confirmation"] is True for row in result["results"]
    )
    assert all(
        row["evidence_class"] == "REPORTED_HANDLING_HISTORY"
        for row in result["results"]
    )


def test_facility_search_escapes_sql_like_wildcards(facility_db: Path) -> None:
    result = search_facility_history("테스트%", facility_db)

    assert result["status"] == "NO_HISTORY_MATCH"
    assert result["results"] == []


def test_facility_search_rejects_too_short_query(facility_db: Path) -> None:
    with pytest.raises(ValueError, match="2자 이상"):
        search_facility_history("가", facility_db)
