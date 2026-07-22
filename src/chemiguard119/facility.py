"""시설명·주소에서 과거 취급 이력 후보를 조회한다.

이 결과는 현재 재고 또는 실제 저장 위치를 뜻하지 않으며 Rule Engine 입력으로
자동 승격할 수 없다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from chemiguard119.database import connect_readonly


def _like_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_facility_history(
    query: str,
    db_path: Path,
    *,
    province: str | None = None,
    top_k: int = 10,
) -> dict[str, Any]:
    normalized = query.strip()
    if len(normalized) < 2:
        raise ValueError("시설명 또는 주소 검색어는 2자 이상이어야 합니다.")
    if not 1 <= top_k <= 50:
        raise ValueError("top_k는 1~50이어야 합니다.")

    conditions = [
        "(facility_name = ? OR facility_name LIKE ? ESCAPE '\\' "
        "OR address LIKE ? ESCAPE '\\')"
    ]
    parameters: list[Any] = [
        normalized,
        _like_value(normalized),
        _like_value(normalized),
    ]
    if province and province.strip():
        conditions.append("province = ?")
        parameters.append(province.strip())
    parameters.append(top_k)

    sql = f"""
        SELECT
            facility_name,
            address,
            province,
            industry,
            cas_number,
            GROUP_CONCAT(DISTINCT chemical_name) AS chemical_names,
            MAX(survey_year) AS latest_survey_year,
            COUNT(*) AS source_record_count,
            MAX(fire_incident_row_count) AS fire_incident_row_count,
            MAX(kosha_msds_exact_match) AS kosha_msds_exact_match,
            MAX(prtr_company_exact_match) AS prtr_company_exact_match,
            MAX(prtr_material_exact_match) AS prtr_material_exact_match,
            MAX(source_url) AS source_url,
            CASE WHEN facility_name = ? THEN 'EXACT_FACILITY_NAME' ELSE 'CONTAINS_TEXT' END AS match_type
        FROM facility_candidate
        WHERE {" AND ".join(conditions)}
        GROUP BY facility_name, address, province, industry, cas_number
        ORDER BY
            CASE WHEN facility_name = ? THEN 0 ELSE 1 END,
            prtr_company_exact_match DESC,
            fire_incident_row_count DESC,
            facility_name,
            cas_number
        LIMIT ?
    """
    # CASE 표현 두 곳에서 exact 검색어를 사용한다.
    sql_parameters = [normalized, *parameters[:-1], normalized, parameters[-1]]
    with connect_readonly(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(sql, sql_parameters)]

    for row in rows:
        row["current_inventory_confirmed"] = False
        row["evidence_class"] = "REPORTED_HANDLING_HISTORY"
        row["rule_eligible"] = False
        row["requires_on_site_confirmation"] = True

    return {
        "query": normalized,
        "province": province.strip() if province and province.strip() else None,
        "status": "CANDIDATES_FOUND" if rows else "NO_HISTORY_MATCH",
        "warning": (
            "시설 후보는 과거 신고·취급 이력입니다. 현재 존재·수량·저장위치를 의미하지 않으며 "
            "현장 확인 전 Rule Engine에 사용할 수 없습니다."
        ),
        "results": rows,
    }


__all__ = ["search_facility_history"]
