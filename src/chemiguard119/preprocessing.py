"""검증된 공개자료를 검색·규칙 조회용 SQLite로 변환한다.

이 모듈은 위험등급을 학습하거나 시설의 현재 재고를 예측하지 않는다. KOSHA와
CAMEO는 각각 근거 검색과 결정적 lookup에 사용한다. ICIS 물질 목록은 일반 물질
식별용 후보 카탈로그로, ICIS/PRTR 결합 자료는 과거 취급 이력 기반의 확인 후보로만
적재한다.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from chemiguard119.utils import (
    compact_text,
    normalize_cas,
    require_materialized_files,
    sha256_file,
    valid_cas_checksum,
)


csv.field_size_limit(100 * 1024 * 1024)


SCHEMA_VERSION = "chemiguard119-preprocessing-1.3.0"

MINIMUM_KOSHA_SUBSTANCE_COUNT = 9
EXPECTED_ICIS_VALID_CAS_COUNT = 4_299
EXPECTED_ICIS_SPLIT_ALIAS_COUNT = 5_331
EXPECTED_CAMEO_CHEMICAL_COUNT = 5_094
EXPECTED_REACTIVE_GROUP_COUNT = 68
EXPECTED_CAMEO_MAPPING_COUNT = 9_231
EXPECTED_COMPATIBILITY_PAIR_COUNT = 2_346

SOURCE_FILES: Mapping[str, str] = {
    "kosha": "01_KOSHA_물질안전보건자료.csv",
    "cameo_chemical": "02_CAMEO_화학물질_반응성.csv",
    "cameo_mapping": "03_CAMEO_화학물질_반응성그룹_매핑.csv",
    "cameo_group": "04_CAMEO_반응성그룹_목록.csv",
    "compatibility": "05_CAMEO_반응성그룹_호환성_고유조합.csv",
    "ulsan_substance": "06_울산소방_화학물정보.csv",
    "icis_material": "13_ICIS_2024_화학물질_취급현황.csv",
    "facility_candidate": "19_ICIS_2024_시설후보_통합모델입력.csv",
}

OVERRIDE_FILE = "substance_overrides.csv"
CROSSWALK_FILE = "cameo_crosswalk.csv"
FEATURE_FILE = "facility_material_features.csv"
MANIFEST_FILE = "preprocessing_manifest.json"
DEFAULT_DB_FILE = "chemiguard119.sqlite"


class PreprocessingError(ValueError):
    """입력 스냅샷이 안전한 전처리 계약을 만족하지 않을 때 발생한다."""


def _read_dicts(path: Path) -> Iterator[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _require_columns(path: Path, required: Iterable[str]) -> None:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle), [])
    missing = sorted(set(required) - set(header))
    if missing:
        raise PreprocessingError(f"{path.name} 필수 컬럼 누락: {', '.join(missing)}")


def _stable_id(prefix: str, *parts: str) -> str:
    payload = "|".join(parts).encode("utf-8")
    return f"{prefix}{hashlib.sha256(payload).hexdigest()[:24].upper()}"


def _required_paths(data_dir: Path) -> dict[str, Path]:
    return {key: data_dir / filename for key, filename in SOURCE_FILES.items()}


def _as_int(value: str | None, *, default: int = 0) -> int:
    text = (value or "").strip().replace(",", "")
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError as error:
        raise PreprocessingError(f"정수 변환 실패: {value!r}") from error


def _optional_number(value: str | None) -> int | float | None:
    """결측을 0으로 바꾸지 않고 SQLite 숫자 또는 NULL로 보존한다."""

    text = (value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        number = float(text)
    except ValueError as error:
        raise PreprocessingError(f"숫자 변환 실패: {value!r}") from error
    return int(number) if number.is_integer() else number


def _flag(value: str | None) -> int:
    return int((value or "").strip().upper() == "Y")


def _load_overrides(path: Path) -> dict[str, dict[str, str]]:
    _require_columns(
        path,
        {
            "cas_number",
            "canonical_name_ko",
            "canonical_name_en",
            "formula",
            "un_number",
            "scenario_role",
            "aliases",
        },
    )
    overrides: dict[str, dict[str, str]] = {}
    for row in _read_dicts(path):
        cas = normalize_cas(row.get("cas_number"))
        if not valid_cas_checksum(cas):
            raise PreprocessingError(f"설정 파일의 CAS가 유효하지 않습니다: {cas!r}")
        if cas in overrides:
            raise PreprocessingError(f"설정 파일의 CAS가 중복됩니다: {cas}")
        overrides[cas] = {key: (value or "").strip() for key, value in row.items()}
    return overrides


def _load_cameo_crosswalk(
    path: Path,
    kosha_cas_numbers: Iterable[str],
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """CAS↔CAMEO 연결과 공개 출처 검증 상태를 읽는다.

    교차표는 CAMEO evidence의 CAS 검색 필드를 채우고, 별도 Rule Engine이
    ``PUBLIC_SOURCE_VERIFIED`` 행만 공개근거 파일럿에 사용할 수 있게 한다.
    이 상태는 전문가 승인이나 현장 물질 확인을 의미하지 않는다.
    """

    _require_columns(
        path,
        {
            "cas_number",
            "cameo_chemical_id",
            "selected_form",
            "verification_status",
            "evidence_url",
            "notes",
        },
    )
    allowed_cas = set(kosha_cas_numbers)
    crosswalk: dict[str, dict[str, str]] = {}
    status_counts: dict[str, int] = {}
    for row in _read_dicts(path):
        cas = normalize_cas(row.get("cas_number"))
        chemical_id = (row.get("cameo_chemical_id") or "").strip()
        selected_form = (row.get("selected_form") or "").strip()
        status = (row.get("verification_status") or "").strip()
        if not valid_cas_checksum(cas):
            raise PreprocessingError(f"CAMEO 교차표의 CAS가 유효하지 않습니다: {cas!r}")
        if cas not in allowed_cas:
            raise PreprocessingError(
                f"CAMEO 교차표에 KOSHA 상세 근거가 없는 CAS가 있습니다: {cas}"
            )
        if not chemical_id or not selected_form or not status:
            raise PreprocessingError(
                "CAMEO 교차표의 ID·선택형태·검증상태는 필수입니다."
            )
        if chemical_id in crosswalk:
            raise PreprocessingError(
                f"CAMEO 교차표의 물질 ID가 중복됩니다: {chemical_id}"
            )
        crosswalk[chemical_id] = {
            "cas_number": cas,
            "selected_form": selected_form,
            "verification_status": status,
            "evidence_url": (row.get("evidence_url") or "").strip(),
            "notes": (row.get("notes") or "").strip(),
        }
        status_counts[status] = status_counts.get(status, 0) + 1
    return crosswalk, status_counts


def _load_kosha_master(
    path: Path,
    overrides: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    required = {
        "레코드ID",
        "화학물질ID",
        "CAS번호",
        "화학물질명_국문",
        "MSDS_장번호",
        "MSDS_항목명_국문",
        "상세내용",
        "최종개정일",
        "시나리오역할",
        "검색기준_화학물질명",
        "UN번호",
        "자료출처",
    }
    _require_columns(path, required)
    rows = list(_read_dicts(path))
    master: dict[str, dict[str, str]] = {}
    for row in rows:
        cas = normalize_cas(row.get("CAS번호"))
        if not valid_cas_checksum(cas):
            raise PreprocessingError(
                f"KOSHA 핵심 물질의 CAS가 유효하지 않습니다: {cas!r}"
            )
        existing = master.setdefault(
            cas,
            {
                "chemical_id": (row.get("화학물질ID") or "").strip(),
                "kosha_name": (row.get("화학물질명_국문") or "").strip(),
                "search_name": (row.get("검색기준_화학물질명") or "").strip(),
                "scenario_role": (row.get("시나리오역할") or "").strip(),
                "un_number": (row.get("UN번호") or "").strip(),
            },
        )
        if not existing["chemical_id"] or not existing["kosha_name"]:
            raise PreprocessingError(
                f"KOSHA {cas}의 화학물질ID 또는 국문명이 비어 있습니다."
            )
        for key, column in {
            "chemical_id": "화학물질ID",
            "kosha_name": "화학물질명_국문",
            "search_name": "검색기준_화학물질명",
            "scenario_role": "시나리오역할",
        }.items():
            value = (row.get(column) or "").strip()
            if value and existing[key] and value != existing[key]:
                raise PreprocessingError(
                    f"KOSHA {cas}의 {column} 값이 일관되지 않습니다."
                )

    if len(master) < MINIMUM_KOSHA_SUBSTANCE_COUNT:
        raise PreprocessingError(
            "KOSHA 상세 물질 수가 검증된 최소 기준보다 작습니다: "
            f"minimum={MINIMUM_KOSHA_SUBSTANCE_COUNT}, actual={len(master)}"
        )
    unexpected_overrides = sorted(set(overrides) - set(master))
    if unexpected_overrides:
        raise PreprocessingError(
            "substance_overrides에 KOSHA 상세 근거가 없는 CAS가 있습니다. "
            f"unexpected={unexpected_overrides}"
        )
    return master, rows


def _load_icis_catalog(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """ICIS 공개 물질 목록을 일반 식별 후보 카탈로그로 읽는다.

    CAS 체크섬이 실제로 유효하고 원본의 체크섬 플래그도 ``Y``인 행만 사용한다.
    세미콜론으로 병기된 이름은 개별 별칭으로 나누되, 같은 CAS 안에서 정규화한
    문자열이 같은 경우 하나만 유지한다. 이 카탈로그는 현재 재고나 CAMEO 규칙
    매핑을 뜻하지 않는다.
    """

    _require_columns(
        path,
        {
            "레코드ID",
            "조사연도",
            "화학물질명_원문",
            "CAS번호_정규화",
            "CAS체크섬유효",
            "자료성격",
            "현재보유확정여부",
            "원본데이터셋_URL",
        },
    )
    catalog: dict[str, dict[str, Any]] = {}
    stats = {
        "icis_source_rows": 0,
        "icis_valid_cas_rows": 0,
        "icis_invalid_cas_rows": 0,
        "icis_checksum_flag_mismatches": 0,
        "icis_split_alias_rows": 0,
        "icis_unique_aliases": 0,
        "icis_source_current_inventory_y_rows": 0,
    }
    for row in _read_dicts(path):
        stats["icis_source_rows"] += 1
        cas = normalize_cas(row.get("CAS번호_정규화"))
        declared_valid = (row.get("CAS체크섬유효") or "").strip().upper() == "Y"
        checksum_valid = valid_cas_checksum(cas)
        if declared_valid != checksum_valid:
            stats["icis_checksum_flag_mismatches"] += 1
        if not declared_valid or not checksum_valid:
            stats["icis_invalid_cas_rows"] += 1
            continue

        raw_names = [
            value.strip()
            for value in (row.get("화학물질명_원문") or "").split(";")
            if value.strip()
        ]
        if not raw_names:
            raise PreprocessingError(f"ICIS 유효 CAS 행에 물질명이 없습니다: {cas}")
        stats["icis_valid_cas_rows"] += 1
        stats["icis_split_alias_rows"] += len(raw_names)
        stats["icis_source_current_inventory_y_rows"] += _flag(
            row.get("현재보유확정여부")
        )

        entry = catalog.setdefault(
            cas,
            {
                "preferred_name": raw_names[0],
                "aliases": [],
                "record_ids": [],
                "survey_year": (row.get("조사연도") or "").strip(),
                "data_character": (row.get("자료성격") or "").strip(),
                "source_url": (row.get("원본데이터셋_URL") or "").strip(),
            },
        )
        record_id = (row.get("레코드ID") or "").strip()
        if record_id:
            entry["record_ids"].append(record_id)
        known = {compact_text(item["text"]) for item in entry["aliases"]}
        for index, name in enumerate(raw_names):
            normalized = compact_text(name)
            if not normalized or normalized in known:
                continue
            entry["aliases"].append(
                {
                    "text": name,
                    "alias_type": (
                        "icis_primary_name" if index == 0 else "icis_reported_alias"
                    ),
                }
            )
            known.add(normalized)

    if len(catalog) != EXPECTED_ICIS_VALID_CAS_COUNT:
        raise PreprocessingError(
            "ICIS 체크섬 유효 CAS 수 불일치: "
            f"expected={EXPECTED_ICIS_VALID_CAS_COUNT}, actual={len(catalog)}"
        )
    if stats["icis_split_alias_rows"] != EXPECTED_ICIS_SPLIT_ALIAS_COUNT:
        raise PreprocessingError(
            "ICIS 세미콜론 분리 별칭 수 불일치: "
            f"expected={EXPECTED_ICIS_SPLIT_ALIAS_COUNT}, "
            f"actual={stats['icis_split_alias_rows']}"
        )
    stats["icis_unique_aliases"] = sum(
        len(entry["aliases"]) for entry in catalog.values()
    )
    return catalog, stats


def _add_alias(
    aliases: dict[tuple[str, str], tuple[int, dict[str, str]]],
    *,
    cas: str,
    text: str | None,
    alias_type: str,
    source: str,
    verification_status: str,
    priority: int,
) -> None:
    alias_text = (text or "").strip()
    normalized = compact_text(alias_text)
    if not normalized:
        return
    key = (cas, normalized)
    row = {
        "alias_id": _stable_id("ALS", cas, normalized),
        "cas_number": cas,
        "alias_text": alias_text,
        "normalized_text": normalized,
        "alias_type": alias_type,
        "source": source,
        "verification_status": verification_status,
    }
    existing = aliases.get(key)
    if existing is None or priority > existing[0]:
        aliases[key] = (priority, row)


def _build_aliases(
    kosha_master: Mapping[str, Mapping[str, str]],
    overrides: Mapping[str, Mapping[str, str]],
    icis_catalog: Mapping[str, Mapping[str, Any]],
    ulsan_path: Path,
) -> tuple[list[dict[str, str]], dict[str, int]]:
    _require_columns(ulsan_path, {"CAS번호", "화학물질명_한글", "화학물질명_영문"})
    aliases: dict[tuple[str, str], tuple[int, dict[str, str]]] = {}
    stats = {"ulsan_invalid_cas_rows": 0, "ulsan_non_kosha_rows": 0}

    for cas, material in icis_catalog.items():
        _add_alias(
            aliases,
            cas=cas,
            text=cas,
            alias_type="cas",
            source=SOURCE_FILES["icis_material"],
            verification_status="PUBLIC_CATALOG_CANDIDATE",
            priority=110,
        )
        for item in material.get("aliases", []):
            alias_type = str(item.get("alias_type") or "icis_reported_alias")
            _add_alias(
                aliases,
                cas=cas,
                text=str(item.get("text") or ""),
                alias_type=alias_type,
                source=SOURCE_FILES["icis_material"],
                verification_status="PUBLIC_CATALOG_CANDIDATE",
                priority=110,
            )

    for cas, master in kosha_master.items():
        _add_alias(
            aliases,
            cas=cas,
            text=cas,
            alias_type="cas",
            source=SOURCE_FILES["kosha"],
            verification_status="SOURCE_EXACT",
            priority=120,
        )
        for key, alias_type in (
            ("kosha_name", "kosha_name"),
            ("search_name", "search_name"),
        ):
            _add_alias(
                aliases,
                cas=cas,
                text=master.get(key),
                alias_type=alias_type,
                source=SOURCE_FILES["kosha"],
                verification_status="SOURCE_EXACT",
                priority=120,
            )

        override = overrides.get(cas, {})
        for key, alias_type in (
            ("canonical_name_ko", "canonical_name_ko"),
            ("canonical_name_en", "canonical_name_en"),
            ("formula", "formula"),
        ):
            _add_alias(
                aliases,
                cas=cas,
                text=override.get(key),
                alias_type=alias_type,
                source=OVERRIDE_FILE,
                verification_status="PROJECT_CONFIG_CANDIDATE",
                priority=80,
            )
        un_number = override.get("un_number")
        for un_text in (un_number, f"UN {un_number}" if un_number else ""):
            _add_alias(
                aliases,
                cas=cas,
                text=un_text,
                alias_type="un_number",
                source=OVERRIDE_FILE,
                verification_status="PROJECT_CONFIG_CANDIDATE",
                priority=80,
            )
        for value in (override.get("aliases") or "").split("|"):
            _add_alias(
                aliases,
                cas=cas,
                text=value,
                alias_type="configured_alias",
                source=OVERRIDE_FILE,
                verification_status="PROJECT_CONFIG_CANDIDATE",
                priority=80,
            )

    for row in _read_dicts(ulsan_path):
        cas = normalize_cas(row.get("CAS번호"))
        if not cas:
            continue
        if not valid_cas_checksum(cas):
            stats["ulsan_invalid_cas_rows"] += 1
            continue
        if cas not in kosha_master:
            stats["ulsan_non_kosha_rows"] += 1
            continue
        for column, alias_type in (
            ("화학물질명_한글", "ulsan_name_ko"),
            ("화학물질명_영문", "ulsan_name_en"),
        ):
            _add_alias(
                aliases,
                cas=cas,
                text=row.get(column),
                alias_type=alias_type,
                source=SOURCE_FILES["ulsan_substance"],
                verification_status="SOURCE_EXACT_VALID_CAS",
                priority=100,
            )

    return [value[1] for _, value in sorted(aliases.items())], stats


def _create_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = FULL;

            CREATE TABLE substance (
                cas_number TEXT PRIMARY KEY,
                canonical_name_ko TEXT NOT NULL,
                canonical_name_en TEXT NOT NULL,
                formula TEXT NOT NULL,
                un_number TEXT NOT NULL,
                scenario_role TEXT NOT NULL,
                catalog_scope TEXT NOT NULL CHECK(
                    catalog_scope IN (
                        'KOSHA_CORE_WITH_DETAIL',
                        'ICIS_PUBLIC_CATALOG_CANDIDATE'
                    )
                ),
                has_kosha_detail INTEGER NOT NULL CHECK(has_kosha_detail IN (0, 1)),
                resolver_candidate_only INTEGER NOT NULL CHECK(
                    resolver_candidate_only IN (0, 1)
                )
            );

            CREATE TABLE alias (
                alias_id TEXT PRIMARY KEY,
                cas_number TEXT NOT NULL REFERENCES substance(cas_number),
                alias_text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                alias_type TEXT NOT NULL,
                source TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                UNIQUE(cas_number, normalized_text)
            );
            CREATE INDEX alias_normalized_idx ON alias(normalized_text);

            CREATE TABLE evidence (
                evidence_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                cas_number TEXT REFERENCES substance(cas_number),
                cameo_chemical_id TEXT,
                title TEXT NOT NULL,
                body TEXT NOT NULL CHECK(length(trim(body)) > 0),
                source_url TEXT,
                document_version TEXT,
                cas_link_status TEXT NOT NULL
            );
            CREATE INDEX evidence_cas_idx ON evidence(cas_number);
            CREATE INDEX evidence_cameo_idx ON evidence(cameo_chemical_id);

            CREATE VIRTUAL TABLE evidence_fts USING fts5(
                evidence_id UNINDEXED,
                title,
                body,
                tokenize = 'unicode61'
            );

            CREATE TABLE cameo_chemical (
                cameo_chemical_id TEXT PRIMARY KEY,
                chemical_name TEXT NOT NULL,
                reactive_group_count INTEGER NOT NULL,
                source_url TEXT,
                document_version TEXT
            );

            CREATE TABLE cameo_mapping (
                cameo_chemical_id TEXT NOT NULL REFERENCES cameo_chemical(cameo_chemical_id),
                reactive_group_id TEXT NOT NULL,
                reactive_group_name TEXT NOT NULL,
                PRIMARY KEY(cameo_chemical_id, reactive_group_id)
            );
            CREATE INDEX cameo_mapping_group_idx ON cameo_mapping(reactive_group_id);

            CREATE TABLE compatibility (
                pair_id TEXT PRIMARY KEY,
                group_a_id TEXT NOT NULL,
                group_b_id TEXT NOT NULL,
                compatibility_label TEXT NOT NULL,
                compatibility_class_id TEXT NOT NULL,
                hazard_codes TEXT,
                hazard_text TEXT,
                gases TEXT,
                source_url TEXT,
                UNIQUE(group_a_id, group_b_id)
            );
            CREATE INDEX compatibility_groups_idx
                ON compatibility(group_a_id, group_b_id);

            CREATE TABLE facility_candidate (
                candidate_id TEXT PRIMARY KEY,
                source_mapping_id TEXT,
                survey_year TEXT NOT NULL,
                cas_number TEXT NOT NULL,
                chemical_name TEXT NOT NULL,
                facility_name TEXT NOT NULL,
                address TEXT NOT NULL,
                province TEXT,
                industry TEXT,
                selection_basis TEXT,
                fire_incident_row_count INTEGER NOT NULL,
                cas_search_status TEXT,
                exact_cas_usable INTEGER NOT NULL CHECK(exact_cas_usable = 1),
                kosha_msds_exact_match INTEGER NOT NULL,
                prtr_company_exact_match INTEGER NOT NULL,
                prtr_material_exact_match INTEGER NOT NULL,
                current_inventory_confirmed INTEGER NOT NULL CHECK(current_inventory_confirmed = 0),
                evidence_class TEXT NOT NULL CHECK(evidence_class = 'REPORTED_HANDLING_HISTORY'),
                source_url TEXT,
                prtr_facility_total_release_kg_year NUMERIC,
                prtr_facility_self_landfill_kg_year NUMERIC,
                prtr_facility_total_transfer_kg_year NUMERIC,
                prtr_facility_reported_flow_kg_year NUMERIC,
                prtr_material_reporting_company_count NUMERIC,
                prtr_material_national_release_kg_year NUMERIC,
                prtr_material_national_self_landfill_kg_year NUMERIC,
                prtr_material_national_transfer_kg_year NUMERIC,
                model_output_purpose TEXT,
                safety_constraint TEXT NOT NULL
            );
            CREATE INDEX facility_candidate_name_idx
                ON facility_candidate(facility_name, address);
            CREATE INDEX facility_candidate_cas_idx ON facility_candidate(cas_number);
            """
        )
    except sqlite3.OperationalError as error:
        if "fts5" in str(error).lower():
            raise RuntimeError(
                "이 Python SQLite 빌드에는 필수 FTS5가 없습니다."
            ) from error
        raise


def _insert_substances_and_aliases(
    connection: sqlite3.Connection,
    kosha_master: Mapping[str, Mapping[str, str]],
    overrides: Mapping[str, Mapping[str, str]],
    icis_catalog: Mapping[str, Mapping[str, Any]],
    aliases: Sequence[Mapping[str, str]],
) -> None:
    substance_rows = []
    for cas in sorted(set(kosha_master) | set(icis_catalog)):
        override = overrides.get(cas, {})
        kosha_material = kosha_master.get(cas, {})
        icis_material = icis_catalog.get(cas, {})
        has_kosha_detail = int(cas in kosha_master)
        substance_rows.append(
            (
                cas,
                override.get("canonical_name_ko", "")
                or kosha_material.get("kosha_name", "")
                or str(icis_material.get("preferred_name") or ""),
                override.get("canonical_name_en", ""),
                override.get("formula", ""),
                override.get("un_number", "")
                or kosha_material.get("un_number", ""),
                override.get("scenario_role", "")
                or kosha_material.get("scenario_role", ""),
                (
                    "KOSHA_CORE_WITH_DETAIL"
                    if has_kosha_detail
                    else "ICIS_PUBLIC_CATALOG_CANDIDATE"
                ),
                has_kosha_detail,
                int(not has_kosha_detail),
            )
        )
    connection.executemany(
        """
        INSERT INTO substance(
            cas_number, canonical_name_ko, canonical_name_en,
            formula, un_number, scenario_role, catalog_scope,
            has_kosha_detail, resolver_candidate_only
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        substance_rows,
    )
    connection.executemany(
        """
        INSERT INTO alias(
            alias_id, cas_number, alias_text, normalized_text,
            alias_type, source, verification_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["alias_id"],
                row["cas_number"],
                row["alias_text"],
                row["normalized_text"],
                row["alias_type"],
                row["source"],
                row["verification_status"],
            )
            for row in aliases
        ],
    )


def _insert_evidence(
    connection: sqlite3.Connection,
    row: Sequence[str | None],
) -> None:
    connection.execute(
        """
        INSERT INTO evidence(
            evidence_id, source, source_record_id, cas_number,
            cameo_chemical_id, title, body, source_url, document_version,
            cas_link_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        row,
    )
    connection.execute(
        "INSERT INTO evidence_fts(evidence_id, title, body) VALUES (?, ?, ?)",
        (row[0], row[5], row[6]),
    )


def _load_kosha_evidence(
    connection: sqlite3.Connection,
    rows: Sequence[Mapping[str, str]],
    kosha_master: Mapping[str, Mapping[str, str]],
    overrides: Mapping[str, Mapping[str, str]],
) -> tuple[int, int, int]:
    inserted = 0
    blank = 0
    no_information = 0
    for source_row in rows:
        body = (source_row.get("상세내용") or "").strip()
        if not body:
            blank += 1
            continue
        if compact_text(body) in {
            "자료없음",
            "자료없다",
            "해당없음",
            "해당사항없음",
            "없음",
            "없다",
        }:
            no_information += 1
            continue
        cas = normalize_cas(source_row.get("CAS번호"))
        if cas not in kosha_master:
            raise PreprocessingError(f"KOSHA evidence의 허용되지 않은 CAS: {cas}")
        source_record_id = (source_row.get("레코드ID") or "").strip()
        if not source_record_id:
            source_record_id = _stable_id(
                "KSR",
                cas,
                source_row.get("MSDS_장번호", ""),
                source_row.get("MSDS_항목명_국문", ""),
                body,
            )
        title = " ".join(
            part
            for part in (
                overrides.get(cas, {}).get("canonical_name_ko", "")
                or kosha_master[cas].get("kosha_name", ""),
                f"MSDS {source_row.get('MSDS_장번호', '').strip()}장",
                (source_row.get("MSDS_항목명_국문") or "").strip(),
            )
            if part
        )
        _insert_evidence(
            connection,
            (
                f"KOSHA:{source_record_id}",
                "KOSHA",
                source_record_id,
                cas,
                None,
                title,
                body,
                (source_row.get("자료출처") or "").strip() or None,
                (source_row.get("최종개정일") or "").strip() or None,
                "SOURCE_EXACT",
            ),
        )
        inserted += 1
    return inserted, blank, no_information


CAMEO_BODY_FIELDS: Sequence[tuple[str, str]] = (
    ("물질설명", "물질 설명"),
    ("건강유해성", "건강 유해성"),
    ("응급조치", "응급조치"),
    ("화재위험", "화재 위험"),
    ("소화대응", "소화 대응"),
    ("비화재사고대응", "비화재 사고 대응"),
    ("공기·물반응성", "공기·물 반응성"),
    ("화학반응성_상세", "화학 반응성"),
    ("특수위험", "특수 위험"),
    ("격리정보", "격리 정보"),
    ("비호환_흡수재", "비호환 흡수재"),
)


def _load_cameo_chemicals(
    connection: sqlite3.Connection,
    path: Path,
    crosswalk: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, int], int, int]:
    required = {
        "CAMEO_화학물질ID",
        "화학물질명_원문",
        "반응성그룹수",
        "원본_상세URL",
        "출처버전",
        *(column for column, _ in CAMEO_BODY_FIELDS),
    }
    _require_columns(path, required)
    expected_group_counts: dict[str, int] = {}
    count = 0
    linked_evidence_count = 0
    for source_row in _read_dicts(path):
        chemical_id = (source_row.get("CAMEO_화학물질ID") or "").strip()
        chemical_name = (source_row.get("화학물질명_원문") or "").strip()
        if not chemical_id or not chemical_name:
            raise PreprocessingError("CAMEO 물질 ID 또는 이름이 비어 있습니다.")
        group_count = _as_int(source_row.get("반응성그룹수"))
        if chemical_id in expected_group_counts:
            raise PreprocessingError(f"CAMEO 물질 ID 중복: {chemical_id}")
        expected_group_counts[chemical_id] = group_count
        source_url = (source_row.get("원본_상세URL") or "").strip() or None
        version = (source_row.get("출처버전") or "").strip() or None
        connection.execute(
            """
            INSERT INTO cameo_chemical(
                cameo_chemical_id, chemical_name, reactive_group_count,
                source_url, document_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (chemical_id, chemical_name, group_count, source_url, version),
        )
        sections = []
        for column, label in CAMEO_BODY_FIELDS:
            value = (source_row.get(column) or "").strip()
            if value:
                sections.append(f"[{label}]\n{value}")
        body = "\n\n".join(sections) or chemical_name
        crosswalk_row = crosswalk.get(chemical_id)
        linked_cas = crosswalk_row.get("cas_number") if crosswalk_row else None
        cas_link_status = (
            crosswalk_row.get("verification_status", "UNLINKED")
            if crosswalk_row
            else "UNLINKED"
        )
        title_terms: list[str] = []
        if linked_cas:
            linked_evidence_count += 1
            linked_substance = connection.execute(
                """
                SELECT canonical_name_ko, formula
                FROM substance
                WHERE cas_number = ?
                """,
                (linked_cas,),
            ).fetchone()
            if linked_substance:
                title_terms.extend(
                    value
                    for value in linked_substance
                    if value and value not in title_terms
                )
        title_terms.append(chemical_name)
        evidence_title = " | ".join(title_terms) + " — CAMEO 반응성"
        _insert_evidence(
            connection,
            (
                f"CAMEO:{chemical_id}",
                "CAMEO",
                chemical_id,
                linked_cas,
                chemical_id,
                evidence_title,
                body,
                source_url,
                version,
                cas_link_status,
            ),
        )
        count += 1

    if count != EXPECTED_CAMEO_CHEMICAL_COUNT:
        raise PreprocessingError(
            "CAMEO 물질 수 불일치: "
            f"expected={EXPECTED_CAMEO_CHEMICAL_COUNT}, actual={count}"
        )
    missing_chemical_ids = sorted(set(crosswalk) - set(expected_group_counts))
    if missing_chemical_ids:
        raise PreprocessingError(
            "CAMEO 교차표 ID가 02 물질 목록에 없습니다: "
            + ", ".join(missing_chemical_ids[:10])
        )
    return expected_group_counts, count, linked_evidence_count


def _load_reactive_groups(path: Path) -> dict[str, str]:
    _require_columns(path, {"반응성그룹ID", "반응성그룹명_원문"})
    groups: dict[str, str] = {}
    for row in _read_dicts(path):
        group_id = (row.get("반응성그룹ID") or "").strip()
        name = (row.get("반응성그룹명_원문") or "").strip()
        if not group_id or not name:
            raise PreprocessingError("CAMEO 반응성 그룹 ID 또는 이름이 비어 있습니다.")
        if group_id in groups:
            raise PreprocessingError(f"CAMEO 반응성 그룹 ID 중복: {group_id}")
        groups[group_id] = name
    if len(groups) != EXPECTED_REACTIVE_GROUP_COUNT:
        raise PreprocessingError(
            "CAMEO 반응성 그룹 수 불일치: "
            f"expected={EXPECTED_REACTIVE_GROUP_COUNT}, actual={len(groups)}"
        )
    return groups


def _load_cameo_mappings(
    connection: sqlite3.Connection,
    path: Path,
    chemical_group_counts: Mapping[str, int],
    reactive_groups: Mapping[str, str],
) -> int:
    _require_columns(
        path,
        {"CAMEO_화학물질ID", "반응성그룹ID", "반응성그룹명_원문"},
    )
    actual_counts: dict[str, int] = {}
    count = 0
    for row in _read_dicts(path):
        chemical_id = (row.get("CAMEO_화학물질ID") or "").strip()
        group_id = (row.get("반응성그룹ID") or "").strip()
        group_name = (row.get("반응성그룹명_원문") or "").strip()
        if chemical_id not in chemical_group_counts:
            raise PreprocessingError(
                f"03에 알 수 없는 CAMEO 물질 ID가 있습니다: {chemical_id}"
            )
        if group_id not in reactive_groups:
            raise PreprocessingError(
                f"03에 알 수 없는 반응성 그룹 ID가 있습니다: {group_id}"
            )
        if group_name != reactive_groups[group_id]:
            raise PreprocessingError(
                f"반응성 그룹 {group_id}의 이름이 03과 04에서 다릅니다."
            )
        connection.execute(
            """
            INSERT INTO cameo_mapping(
                cameo_chemical_id, reactive_group_id, reactive_group_name
            ) VALUES (?, ?, ?)
            """,
            (chemical_id, group_id, group_name),
        )
        actual_counts[chemical_id] = actual_counts.get(chemical_id, 0) + 1
        count += 1

    if count != EXPECTED_CAMEO_MAPPING_COUNT:
        raise PreprocessingError(
            "CAMEO 매핑 수 불일치: "
            f"expected={EXPECTED_CAMEO_MAPPING_COUNT}, actual={count}"
        )
    mismatches = [
        (chemical_id, expected, actual_counts.get(chemical_id, 0))
        for chemical_id, expected in chemical_group_counts.items()
        if actual_counts.get(chemical_id, 0) != expected
    ]
    if mismatches:
        raise PreprocessingError(
            f"CAMEO 보고 그룹 수와 03 매핑 수 불일치: {mismatches[:5]}"
        )
    return count


def _load_compatibility(
    connection: sqlite3.Connection,
    path: Path,
    reactive_groups: Mapping[str, str],
) -> int:
    _require_columns(
        path,
        {
            "고유조합ID",
            "그룹A_ID",
            "그룹B_ID",
            "호환성_판정",
            "호환성_클래스ID",
            "위험코드",
            "위험문구",
            "발생가스",
            "원본URL",
        },
    )
    count = 0
    for row in _read_dicts(path):
        group_a = (row.get("그룹A_ID") or "").strip()
        group_b = (row.get("그룹B_ID") or "").strip()
        if group_a not in reactive_groups or group_b not in reactive_groups:
            raise PreprocessingError(
                f"05에 알 수 없는 그룹쌍이 있습니다: {group_a}, {group_b}"
            )
        try:
            ordered = int(group_a) <= int(group_b)
        except ValueError:
            ordered = group_a <= group_b
        if not ordered:
            raise PreprocessingError(
                f"05 그룹쌍이 정규 순서가 아닙니다: {group_a}, {group_b}"
            )
        pair_id = (row.get("고유조합ID") or "").strip()
        if not pair_id:
            pair_id = _stable_id("PAIR", group_a, group_b)
        connection.execute(
            """
            INSERT INTO compatibility(
                pair_id, group_a_id, group_b_id, compatibility_label,
                compatibility_class_id, hazard_codes, hazard_text,
                gases, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pair_id,
                group_a,
                group_b,
                (row.get("호환성_판정") or "").strip(),
                (row.get("호환성_클래스ID") or "").strip(),
                (row.get("위험코드") or "").strip() or None,
                (row.get("위험문구") or "").strip() or None,
                (row.get("발생가스") or "").strip() or None,
                (row.get("원본URL") or "").strip() or None,
            ),
        )
        count += 1
    if count != EXPECTED_COMPATIBILITY_PAIR_COUNT:
        raise PreprocessingError(
            "CAMEO 호환성 고유조합 수 불일치: "
            f"expected={EXPECTED_COMPATIBILITY_PAIR_COUNT}, actual={count}"
        )
    return count


FACILITY_REQUIRED_COLUMNS = {
    "레코드ID",
    "기준매핑_레코드ID",
    "조사연도",
    "CAS번호",
    "화학물질명_선정기준",
    "업체명",
    "주소",
    "시도명",
    "업종",
    "선정근거",
    "울산소방_사고자료행수",
    "CAS검색검증상태",
    "정확CAS모델링사용여부",
    "KOSHA_MSDS_CAS정확매칭",
    "PRTR_업체_업체명주소정확매칭",
    "PRTR_물질_CAS정확매칭",
    "현재보유확정여부",
    "원본데이터셋_URL",
    "PRTR_시설전체_총배출량_kg_년",
    "PRTR_시설전체_자가매립량_kg_년",
    "PRTR_시설전체_총이동량_kg_년",
    "PRTR_시설전체_보고흐름합계_kg_년",
    "PRTR_해당물질_배출업체수",
    "PRTR_해당물질_전국총배출량_kg_년",
    "PRTR_해당물질_전국자가매립량_kg_년",
    "PRTR_해당물질_전국총이동량_kg_년",
    "모델출력용도",
    "안전제약",
}


def _load_facility_candidates(
    connection: sqlite3.Connection,
    path: Path,
) -> dict[str, int]:
    _require_columns(path, FACILITY_REQUIRED_COLUMNS)
    stats = {
        "source_rows": 0,
        "inserted_rows": 0,
        "excluded_non_exact_cas_rows": 0,
        "source_current_inventory_y_rows": 0,
    }
    sql = """
        INSERT INTO facility_candidate(
            candidate_id, source_mapping_id, survey_year, cas_number,
            chemical_name, facility_name, address, province, industry,
            selection_basis, fire_incident_row_count, cas_search_status,
            exact_cas_usable, kosha_msds_exact_match,
            prtr_company_exact_match, prtr_material_exact_match,
            current_inventory_confirmed, evidence_class, source_url,
            prtr_facility_total_release_kg_year,
            prtr_facility_self_landfill_kg_year,
            prtr_facility_total_transfer_kg_year,
            prtr_facility_reported_flow_kg_year,
            prtr_material_reporting_company_count,
            prtr_material_national_release_kg_year,
            prtr_material_national_self_landfill_kg_year,
            prtr_material_national_transfer_kg_year,
            model_output_purpose, safety_constraint
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """
    batch: list[tuple[Any, ...]] = []
    for row in _read_dicts(path):
        stats["source_rows"] += 1
        if (row.get("정확CAS모델링사용여부") or "").strip().upper() != "Y":
            stats["excluded_non_exact_cas_rows"] += 1
            continue
        cas = normalize_cas(row.get("CAS번호"))
        if not valid_cas_checksum(cas):
            raise PreprocessingError(
                f"19의 exact-CAS 허용 행에 유효하지 않은 CAS가 있습니다: {cas!r}"
            )
        stats["source_current_inventory_y_rows"] += _flag(row.get("현재보유확정여부"))
        candidate_id = (row.get("레코드ID") or "").strip()
        if not candidate_id:
            candidate_id = _stable_id(
                "FAC",
                (row.get("조사연도") or "").strip(),
                cas,
                (row.get("업체명") or "").strip(),
                (row.get("주소") or "").strip(),
            )
        safety_constraint = (row.get("안전제약") or "").strip()
        if not safety_constraint:
            safety_constraint = (
                "과거 취급 이력 후보이며 현재 존재·수량·저장위치는 현장에서 확인해야 함"
            )
        batch.append(
            (
                candidate_id,
                (row.get("기준매핑_레코드ID") or "").strip() or None,
                (row.get("조사연도") or "").strip(),
                cas,
                (row.get("화학물질명_선정기준") or "").strip(),
                (row.get("업체명") or "").strip(),
                (row.get("주소") or "").strip(),
                (row.get("시도명") or "").strip() or None,
                (row.get("업종") or "").strip() or None,
                (row.get("선정근거") or "").strip() or None,
                _as_int(row.get("울산소방_사고자료행수")),
                (row.get("CAS검색검증상태") or "").strip() or None,
                1,
                _flag(row.get("KOSHA_MSDS_CAS정확매칭")),
                _flag(row.get("PRTR_업체_업체명주소정확매칭")),
                _flag(row.get("PRTR_물질_CAS정확매칭")),
                0,
                "REPORTED_HANDLING_HISTORY",
                (row.get("원본데이터셋_URL") or "").strip() or None,
                _optional_number(row.get("PRTR_시설전체_총배출량_kg_년")),
                _optional_number(row.get("PRTR_시설전체_자가매립량_kg_년")),
                _optional_number(row.get("PRTR_시설전체_총이동량_kg_년")),
                _optional_number(row.get("PRTR_시설전체_보고흐름합계_kg_년")),
                _optional_number(row.get("PRTR_해당물질_배출업체수")),
                _optional_number(row.get("PRTR_해당물질_전국총배출량_kg_년")),
                _optional_number(row.get("PRTR_해당물질_전국자가매립량_kg_년")),
                _optional_number(row.get("PRTR_해당물질_전국총이동량_kg_년")),
                (row.get("모델출력용도") or "").strip() or None,
                safety_constraint,
            )
        )
        if len(batch) >= 1_000:
            connection.executemany(sql, batch)
            stats["inserted_rows"] += len(batch)
            batch.clear()
    if batch:
        connection.executemany(sql, batch)
        stats["inserted_rows"] += len(batch)
    return stats


FEATURE_COLUMNS = (
    "survey_year",
    "facility_name",
    "address",
    "province",
    "industry",
    "cas_number",
    "chemical_name",
    "candidate_record_count",
    "fire_incident_row_count",
    "kosha_msds_exact_match",
    "prtr_company_exact_match",
    "prtr_material_exact_match",
    "prtr_facility_total_release_kg_year",
    "prtr_facility_self_landfill_kg_year",
    "prtr_facility_total_transfer_kg_year",
    "prtr_facility_reported_flow_kg_year",
    "prtr_material_reporting_company_count",
    "prtr_material_national_release_kg_year",
    "prtr_material_national_self_landfill_kg_year",
    "prtr_material_national_transfer_kg_year",
    "current_inventory_confirmed",
    "evidence_class",
)


FEATURE_QUERY = """
    SELECT
        survey_year,
        facility_name,
        address,
        MAX(province) AS province,
        MAX(industry) AS industry,
        cas_number,
        MAX(chemical_name) AS chemical_name,
        COUNT(*) AS candidate_record_count,
        MAX(fire_incident_row_count) AS fire_incident_row_count,
        MAX(kosha_msds_exact_match) AS kosha_msds_exact_match,
        MAX(prtr_company_exact_match) AS prtr_company_exact_match,
        MAX(prtr_material_exact_match) AS prtr_material_exact_match,
        MAX(prtr_facility_total_release_kg_year) AS prtr_facility_total_release_kg_year,
        MAX(prtr_facility_self_landfill_kg_year) AS prtr_facility_self_landfill_kg_year,
        MAX(prtr_facility_total_transfer_kg_year) AS prtr_facility_total_transfer_kg_year,
        MAX(prtr_facility_reported_flow_kg_year) AS prtr_facility_reported_flow_kg_year,
        MAX(prtr_material_reporting_company_count) AS prtr_material_reporting_company_count,
        MAX(prtr_material_national_release_kg_year) AS prtr_material_national_release_kg_year,
        MAX(prtr_material_national_self_landfill_kg_year) AS prtr_material_national_self_landfill_kg_year,
        MAX(prtr_material_national_transfer_kg_year) AS prtr_material_national_transfer_kg_year,
        0 AS current_inventory_confirmed,
        'REPORTED_HANDLING_HISTORY' AS evidence_class
    FROM facility_candidate
    GROUP BY survey_year, facility_name, address, cas_number
    ORDER BY survey_year, facility_name, address, cas_number
"""


def _make_temp_path(directory: Path, prefix: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=prefix, suffix=suffix)
    os.close(descriptor)
    return Path(name)


def _write_feature_csv(connection: sqlite3.Connection, path: Path) -> int:
    count = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FEATURE_COLUMNS)
        for row in connection.execute(FEATURE_QUERY):
            writer.writerow(["" if value is None else value for value in row])
            count += 1
        handle.flush()
        os.fsync(handle.fileno())
    return count


def _write_json_atomic_temp(path: Path, payload: Mapping[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "substance",
        "alias",
        "evidence",
        "evidence_fts",
        "cameo_chemical",
        "cameo_mapping",
        "compatibility",
        "facility_candidate",
    )
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def prepare_dataset(
    data_dir: str | Path,
    config_dir: str | Path,
    artifact_dir: str | Path,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """검색·규칙 조회용 SQLite와 설명 가능한 집계 피처를 원자적으로 만든다.

    Args:
        data_dir: `01`~`06`, `13`, `19` 최종 CSV가 있는 디렉터리.
        config_dir: 수동 별칭·CAMEO 공개 검증 설정 CSV가 있는 디렉터리.
        artifact_dir: 집계 CSV와 manifest JSON을 저장할 디렉터리.
        db_path: SQLite 출력 경로. 생략하면 artifact_dir 아래에 생성한다.

    Returns:
        저장된 manifest와 동일한 dict.
    """

    data_dir = Path(data_dir).expanduser().resolve()
    config_dir = Path(config_dir).expanduser().resolve()
    artifact_dir = Path(artifact_dir).expanduser().resolve()
    final_db_path = (
        Path(db_path).expanduser().resolve()
        if db_path is not None
        else artifact_dir / DEFAULT_DB_FILE
    )
    feature_path = artifact_dir / FEATURE_FILE
    manifest_path = artifact_dir / MANIFEST_FILE

    paths = _required_paths(data_dir)
    override_path = config_dir / OVERRIDE_FILE
    crosswalk_path = config_dir / CROSSWALK_FILE
    require_materialized_files([*paths.values(), override_path, crosswalk_path])

    overrides = _load_overrides(override_path)
    kosha_master, kosha_rows = _load_kosha_master(paths["kosha"], overrides)
    icis_catalog, icis_stats = _load_icis_catalog(paths["icis_material"])
    cameo_crosswalk, crosswalk_status_counts = _load_cameo_crosswalk(
        crosswalk_path,
        kosha_master,
    )
    aliases, alias_stats = _build_aliases(
        kosha_master,
        overrides,
        icis_catalog,
        paths["ulsan_substance"],
    )

    final_db_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    temp_db = _make_temp_path(final_db_path.parent, f".{final_db_path.name}.", ".tmp")
    temp_feature = _make_temp_path(artifact_dir, f".{FEATURE_FILE}.", ".tmp")
    temp_manifest = _make_temp_path(artifact_dir, f".{MANIFEST_FILE}.", ".tmp")

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temp_db)
        _create_schema(connection)
        _insert_substances_and_aliases(
            connection,
            kosha_master,
            overrides,
            icis_catalog,
            aliases,
        )
        (
            kosha_evidence_count,
            blank_kosha_details,
            no_information_kosha_details,
        ) = _load_kosha_evidence(
            connection,
            kosha_rows,
            kosha_master,
            overrides,
        )
        cameo_group_counts, cameo_count, crosswalk_evidence_links = (
            _load_cameo_chemicals(
                connection,
                paths["cameo_chemical"],
                cameo_crosswalk,
            )
        )
        reactive_groups = _load_reactive_groups(paths["cameo_group"])
        mapping_count = _load_cameo_mappings(
            connection,
            paths["cameo_mapping"],
            cameo_group_counts,
            reactive_groups,
        )
        compatibility_count = _load_compatibility(
            connection,
            paths["compatibility"],
            reactive_groups,
        )
        facility_stats = _load_facility_candidates(
            connection, paths["facility_candidate"]
        )
        connection.commit()

        table_counts = _table_counts(connection)
        if table_counts["evidence_fts"] != table_counts["evidence"]:
            raise PreprocessingError("evidence와 evidence_fts 행 수가 다릅니다.")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise PreprocessingError(f"SQLite 무결성 검사 실패: {integrity}")
        feature_count = _write_feature_csv(connection, temp_feature)
        connection.close()
        connection = None

        source_manifest = {
            key: {
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for key, path in paths.items()
        }
        config_manifest = {
            "substance_overrides": {
                "path": str(override_path),
                "bytes": override_path.stat().st_size,
                "sha256": sha256_file(override_path),
            },
            "cameo_crosswalk": {
                "path": str(crosswalk_path),
                "bytes": crosswalk_path.stat().st_size,
                "sha256": sha256_file(crosswalk_path),
                "verification_status_counts": crosswalk_status_counts,
                "usage": "SEARCH_METADATA_AND_PUBLIC_SOURCE_SCREENING",
            },
        }
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "data_dir": str(data_dir),
            "config_dir": str(config_dir),
            "artifacts": {
                "sqlite": str(final_db_path),
                "facility_material_features": str(feature_path),
                "manifest": str(manifest_path),
            },
            "source_files": source_manifest,
            "config_files": config_manifest,
            "counts": {
                **table_counts,
                "kosha_evidence": kosha_evidence_count,
                "kosha_blank_details_excluded": blank_kosha_details,
                "kosha_no_information_details_excluded": no_information_kosha_details,
                "cameo_evidence": cameo_count,
                "cameo_crosswalk_rows": len(cameo_crosswalk),
                "cameo_crosswalk_evidence_links": crosswalk_evidence_links,
                "reactive_group": len(reactive_groups),
                "cameo_mapping_loaded": mapping_count,
                "compatibility_loaded": compatibility_count,
                "facility_material_feature_rows": feature_count,
                **icis_stats,
                **alias_stats,
                **facility_stats,
            },
            "safety_constraints": {
                "risk_level_training_included": False,
                "current_inventory_prediction_included": False,
                "facility_candidates_are_current_inventory": False,
                "facility_candidate_evidence_class": "REPORTED_HANDLING_HISTORY",
                "missing_prtr_values_imputed_to_zero": False,
                "cameo_ids_preserved_as_text": True,
                "cameo_crosswalk_public_source_screening_enabled": True,
                "cameo_crosswalk_implies_expert_approval": False,
                "icis_catalog_candidates_are_current_inventory": False,
                "icis_catalog_aliases_used_for_rule_promotion": False,
                "icis_catalog_has_kosha_detail_only_when_explicitly_joined": True,
                "notes": [
                    "CAMEO 호환성은 결정적 lookup이며 학습 라벨이 아닙니다.",
                    "PUBLIC_SOURCE_VERIFIED 교차표는 공개근거 CAMEO 스크리닝에 사용하며 전문가 승인을 뜻하지 않습니다.",
                    "ICIS 물질명은 일반 물질 식별 후보이며 현장 존재나 Rule 입력을 확정하지 않습니다.",
                    "KOSHA 상세 근거 물질과 ICIS 일반 후보 카탈로그는 substance 범위 필드로 구분합니다.",
                    "시설 후보는 과거 취급 이력으로, 현재 존재·수량·저장위치를 확정하지 않습니다.",
                    "PRTR 결측은 NULL로 보존하며 배출·이동량을 재고량이나 사고확률로 해석하지 않습니다.",
                ],
            },
        }
        _write_json_atomic_temp(temp_manifest, manifest)

        os.replace(temp_db, final_db_path)
        os.replace(temp_feature, feature_path)
        os.replace(temp_manifest, manifest_path)
        return manifest
    except Exception:
        if connection is not None:
            connection.close()
        raise
    finally:
        for path in (temp_db, temp_feature, temp_manifest):
            path.unlink(missing_ok=True)


__all__ = ["PreprocessingError", "prepare_dataset"]
