from __future__ import annotations

import sqlite3
from pathlib import Path

from chemiguard119.coverage import facility_history_coverage


def test_facility_history_coverage_reports_nationwide_without_inventory_claim(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "coverage.sqlite"
    provinces = [f"시도-{index:02d}" for index in range(17)]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE facility_candidate(
                facility_name TEXT NOT NULL,
                address TEXT NOT NULL,
                province TEXT,
                cas_number TEXT NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO facility_candidate VALUES (?, ?, ?, ?)",
            [
                (f"사업장-{index}", f"주소-{index}", province, "7647-01-0")
                for index, province in enumerate(provinces)
            ]
            + [("미상 사업장", "미상 주소", None, "7681-52-9")],
        )

    coverage = facility_history_coverage(db_path)

    assert coverage["ready"] is True
    assert coverage["scope"] == "NATIONWIDE_KOREA_HISTORICAL_CANDIDATES"
    assert coverage["covered_province_count"] == 17
    assert coverage["candidate_row_count"] == 18
    assert coverage["distinct_facility_count"] == 18
    assert coverage["distinct_cas_count"] == 2
    assert coverage["unknown_location_facility_count"] == 1
    assert coverage["current_inventory_confirmed"] is False
    assert coverage["evidence_class"] == "REPORTED_HANDLING_HISTORY"


def test_facility_history_coverage_is_unavailable_without_table(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "empty.sqlite"
    sqlite3.connect(db_path).close()

    coverage = facility_history_coverage(db_path)

    assert coverage["ready"] is False
    assert coverage["scope"] == "UNAVAILABLE"
    assert coverage["current_inventory_confirmed"] is False
