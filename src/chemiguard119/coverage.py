"""배포 artifact의 시설 이력 지리 범위를 계산한다."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from chemiguard119.database import connect_readonly


EXPECTED_KOREA_FIRST_LEVEL_DIVISIONS = 17


def facility_history_coverage(db_path: Path) -> dict[str, Any]:
    """시설 후보가 전국 자료인지 행 수와 시·도 분포로 확인한다.

    이 통계는 현재 재고 범위가 아니라 ``REPORTED_HANDLING_HISTORY`` 후보의
    지리적 범위다. 테이블이 없는 테스트·불완전 artifact에서는 예외 대신
    명시적인 unavailable 상태를 반환한다.
    """

    unavailable = {
        "ready": False,
        "scope": "UNAVAILABLE",
        "evidence_class": "REPORTED_HANDLING_HISTORY",
        "current_inventory_confirmed": False,
        "candidate_row_count": 0,
        "distinct_facility_count": 0,
        "distinct_cas_count": 0,
        "covered_province_count": 0,
        "covered_provinces": [],
        "province_breakdown": [],
        "unknown_location_facility_count": 0,
        "warning": "시설 이력 후보는 현재 재고·수량·저장 위치의 확정 정보가 아닙니다.",
        "reason": "facility_candidate 테이블을 확인하지 못했습니다.",
    }
    if not db_path.is_file():
        return unavailable

    try:
        with connect_readonly(db_path) as connection:
            connection.row_factory = sqlite3.Row
            totals = connection.execute(
                """
                SELECT
                    COUNT(*) AS candidate_row_count,
                    COUNT(DISTINCT facility_name || char(31) || address)
                        AS distinct_facility_count,
                    COUNT(DISTINCT cas_number) AS distinct_cas_count
                FROM facility_candidate
                """
            ).fetchone()
            breakdown = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(TRIM(province), ''), '미상') AS province,
                        COUNT(*) AS candidate_row_count,
                        COUNT(DISTINCT facility_name || char(31) || address)
                            AS distinct_facility_count
                    FROM facility_candidate
                    GROUP BY COALESCE(NULLIF(TRIM(province), ''), '미상')
                    ORDER BY candidate_row_count DESC, province
                    """
                )
            ]
    except sqlite3.OperationalError as error:
        if "no such table: facility_candidate" in str(error).lower():
            return unavailable
        raise

    known = [item for item in breakdown if item["province"] != "미상"]
    unknown_facilities = sum(
        int(item["distinct_facility_count"])
        for item in breakdown
        if item["province"] == "미상"
    )
    nationwide = len(known) >= EXPECTED_KOREA_FIRST_LEVEL_DIVISIONS
    return {
        "ready": bool(totals["candidate_row_count"]),
        "scope": (
            "NATIONWIDE_KOREA_HISTORICAL_CANDIDATES"
            if nationwide
            else "PARTIAL_KOREA_HISTORICAL_CANDIDATES"
        ),
        "evidence_class": "REPORTED_HANDLING_HISTORY",
        "current_inventory_confirmed": False,
        "candidate_row_count": int(totals["candidate_row_count"]),
        "distinct_facility_count": int(totals["distinct_facility_count"]),
        "distinct_cas_count": int(totals["distinct_cas_count"]),
        "covered_province_count": len(known),
        "covered_provinces": sorted(item["province"] for item in known),
        "province_breakdown": breakdown,
        "unknown_location_facility_count": unknown_facilities,
        "warning": "시설 이력 후보는 현재 재고·수량·저장 위치의 확정 정보가 아닙니다.",
        "reason": None,
    }


__all__ = ["facility_history_coverage"]
