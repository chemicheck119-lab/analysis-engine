"""전처리에 필요한 원천 CSV를 스트리밍으로 점검한다."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from chemiguard119.paths import FINAL_DATA_DIR
from chemiguard119.preprocessing import SOURCE_FILES
from chemiguard119.utils import (
    is_lfs_pointer,
    normalize_cas,
    require_materialized_files,
    sha256_file,
    valid_cas_checksum,
)


csv.field_size_limit(100 * 1024 * 1024)

REQUIRED_SOURCE_FILENAMES = tuple(SOURCE_FILES.values())


def required_csv_paths(data_dir: Path = FINAL_DATA_DIR) -> list[Path]:
    """전처리 계약에 명시된 8개 입력 경로를 고정된 순서로 반환한다."""

    return [data_dir / filename for filename in REQUIRED_SOURCE_FILENAMES]


def final_csv_paths(data_dir: Path = FINAL_DATA_DIR) -> list[Path]:
    """현재 존재하는 필수 입력만 반환한다.

    CLI ``doctor``와의 하위 호환을 위해 기존 함수명을 유지하되, 디렉터리의
    임의 CSV가 아니라 실제 전처리 입력 계약만 센다.
    """

    return [path for path in required_csv_paths(data_dir) if path.is_file()]


def profile_csv(path: Path, include_hash: bool = False) -> dict[str, Any]:
    """한 파일의 구조·결측·키 중복을 메모리 폭증 없이 점검한다."""

    if is_lfs_pointer(path):
        raise FileNotFoundError(
            f"{path}는 실제 CSV가 아닌 Git LFS 포인터입니다. 데이터 번들을 다시 준비하세요."
        )

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        missing = [0] * len(header)
        row_count = 0
        malformed = 0
        duplicate_first_key = 0
        first_keys: set[str] = set()
        for row in reader:
            row_count += 1
            if len(row) != len(header):
                malformed += 1
                continue
            for index, value in enumerate(row):
                if not value.strip():
                    missing[index] += 1
            if row:
                key = row[0].strip()
                if key and key in first_keys:
                    duplicate_first_key += 1
                elif key:
                    first_keys.add(key)

    profile: dict[str, Any] = {
        "file": path.name,
        "bytes": path.stat().st_size,
        "rows": row_count,
        "columns": len(header),
        "column_names": header,
        "malformed_column_count_rows": malformed,
        "duplicate_first_key_rows": duplicate_first_key,
        "missing_by_column": dict(zip(header, missing)),
    }
    if include_hash:
        profile["sha256"] = sha256_file(path)
    return profile


def _read_dicts(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _semantic_checks(data_dir: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {}

    kosha_path = data_dir / "01_KOSHA_물질안전보건자료.csv"
    kosha_rows = list(_read_dicts(kosha_path))
    kosha_cas = {
        normalize_cas(row.get("CAS번호")) for row in kosha_rows if row.get("CAS번호")
    }
    checks["kosha"] = {
        "substance_count": len(kosha_cas),
        "record_count": len(kosha_rows),
        "blank_detail_count": sum(
            not (row.get("상세내용") or "").strip() for row in kosha_rows
        ),
        "invalid_cas_count": sum(not valid_cas_checksum(cas) for cas in kosha_cas),
        "interpretation": "행 수는 물질 수가 아니라 MSDS 세부 항목 수입니다.",
    }

    compatibility_path = data_dir / "05_CAMEO_반응성그룹_호환성_고유조합.csv"
    compatibility = Counter()
    gas_blank = 0
    pair_count = 0
    for row in _read_dicts(compatibility_path):
        pair_count += 1
        compatibility[(row.get("호환성_판정") or "").strip()] += 1
        gas_blank += not (row.get("발생가스") or "").strip()
    checks["cameo_group_pairs"] = {
        "pair_count": pair_count,
        "compatibility_distribution": dict(compatibility),
        "blank_gas_count": gas_blank,
        "interpretation": "그룹쌍 결과이며 개별 물질쌍 실험 결과가 아닙니다. 발생가스 공란도 안전을 뜻하지 않습니다.",
    }

    ulsan_path = data_dir / "06_울산소방_화학물정보.csv"
    ulsan_rows = list(_read_dicts(ulsan_path))
    ulsan_cas = [normalize_cas(row.get("CAS번호")) for row in ulsan_rows]
    checks["ulsan_substances"] = {
        "record_count": len(ulsan_rows),
        "blank_cas_count": sum(not cas for cas in ulsan_cas),
        "invalid_nonblank_cas_count": sum(
            bool(cas) and not valid_cas_checksum(cas) for cas in ulsan_cas
        ),
        "interpretation": "CAS 결측·훼손 의심 행은 자동 식별 학습에서 제외해야 합니다.",
    }

    model_input_path = data_dir / "19_ICIS_2024_시설후보_통합모델입력.csv"
    exact_cas = 0
    current_inventory = 0
    automatic_rule = 0
    record_count = 0
    for row in _read_dicts(model_input_path):
        record_count += 1
        exact_cas += row.get("정확CAS모델링사용여부") == "Y"
        current_inventory += row.get("현재보유확정여부") == "Y"
        automatic_rule += row.get("RuleEngine_자동판정가능여부") == "Y"
    checks["facility_candidates"] = {
        "record_count": record_count,
        "exact_cas_usable_count": exact_cas,
        "current_inventory_confirmed_count": current_inventory,
        "automatic_rule_allowed_count": automatic_rule,
        "allowed_use": "과거 취급 이력 기반 시설 후보 조회와 확인 우선순위 피처",
        "prohibited_target": "현재 보유 여부 또는 사고 확률",
    }
    return checks


def audit_dataset(
    data_dir: Path = FINAL_DATA_DIR, include_hash: bool = False
) -> dict[str, Any]:
    paths = required_csv_paths(data_dir)
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "전처리 필수 CSV가 누락되었습니다: " + ", ".join(missing)
        )
    require_materialized_files(paths)
    profiles = [profile_csv(path, include_hash=include_hash) for path in paths]
    report = {
        "dataset_dir": str(data_dir),
        "file_count": len(profiles),
        "total_rows": sum(profile["rows"] for profile in profiles),
        "total_bytes": sum(profile["bytes"] for profile in profiles),
        "malformed_row_count": sum(
            profile["malformed_column_count_rows"] for profile in profiles
        ),
        "duplicate_first_key_row_count": sum(
            profile["duplicate_first_key_rows"] for profile in profiles
        ),
        "files": profiles,
        "semantic_checks": _semantic_checks(data_dir),
        "modeling_decision": {
            "train_now": [
                "ICIS 중심 4,300개 공개 카탈로그의 물질·CAS 후보 검색 모델",
                "KOSHA·CAMEO 문서 검색 기준선",
            ],
            "deterministic_only": [
                "CAS 체크섬과 exact 식별",
                "공개 근거로 검증된 CAMEO 교차표와 Rule Engine",
                "출력 정합성 검사",
            ],
            "do_not_train": [
                "시설 현재 보유 여부",
                "화학사고 발생 확률",
                "LLM 단독 위험등급",
            ],
        },
    }
    return report


__all__ = [
    "REQUIRED_SOURCE_FILENAMES",
    "audit_dataset",
    "final_csv_paths",
    "profile_csv",
    "required_csv_paths",
]
