"""공식 데이터 커버리지를 이용해 지원 물질 구축 순서를 만든다.

이 모듈이 만드는 순위는 위험 확률이나 업체의 현재 재고 확률이 아니다. 소방 데이터의
사고 신호, ICIS·PRTR 과거 이력, KOSHA MSDS 적재 여부, 공개 검증 CAMEO 연결 여부를
분리해서 집계하고 다음 두 작업 순서를 재현 가능하게 만든다.

* ``demo_rank``: 현재 근거로 종단간 시연하기 좋은 물질 순서
* ``expansion_rank``: 공식 근거를 다음으로 확장할 물질 순서
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from chemiguard119.utils import normalize_cas, valid_cas_checksum


SCHEMA_VERSION = "chemicheck119-support-priority-v1"
PUBLIC_VERIFIED_STATUS = "PUBLIC_SOURCE_VERIFIED"


class SupportPriorityError(ValueError):
    """우선순위 입력 데이터 계약이 올바르지 않을 때 발생한다."""


@dataclass
class SubstanceSignals:
    cas_number: str
    names: set[str] = field(default_factory=set)
    fire_incident_rows: int = 0
    facility_records: int = 0
    facility_keys: set[str] = field(default_factory=set)
    prtr_company_exact_records: int = 0
    prtr_material_exact_records: int = 0
    kosha_msds_loaded: bool = False
    cameo_public_verified: bool = False

    @property
    def facility_count(self) -> int:
        return len(self.facility_keys)

    @property
    def operational_signal(self) -> bool:
        return self.fire_incident_rows > 0 or self.facility_count > 0


def _read_rows(path: Path) -> Iterator[dict[str, str]]:
    if not path.is_file():
        raise SupportPriorityError(f"입력 파일을 찾을 수 없습니다: {path}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SupportPriorityError(f"CSV 헤더가 없습니다: {path}")
        yield from reader


def _require_columns(path: Path, rows: list[dict[str, str]], required: set[str]) -> None:
    columns = set(rows[0]) if rows else set()
    missing = sorted(required - columns)
    if missing:
        raise SupportPriorityError(
            f"{path.name} 필수 컬럼 누락: {', '.join(missing)}"
        )


def _cas(value: str | None, *, source: Path, row_number: int) -> str:
    cas_number = normalize_cas(value)
    if not valid_cas_checksum(cas_number):
        raise SupportPriorityError(
            f"{source.name} {row_number}행의 CAS가 유효하지 않습니다: {value!r}"
        )
    return cas_number


def _text(value: str | None) -> str:
    return " ".join((value or "").split())


def _as_nonnegative_int(
    value: str | None,
    *,
    source: Path,
    row_number: int,
    column: str,
) -> int:
    text = _text(value).replace(",", "")
    if not text:
        return 0
    try:
        number = int(float(text))
    except ValueError as error:
        raise SupportPriorityError(
            f"{source.name} {row_number}행의 {column} 값이 숫자가 아닙니다: {value!r}"
        ) from error
    if number < 0:
        raise SupportPriorityError(
            f"{source.name} {row_number}행의 {column} 값은 음수일 수 없습니다."
        )
    return number


def _signal(
    signals: dict[str, SubstanceSignals],
    cas_number: str,
) -> SubstanceSignals:
    return signals.setdefault(cas_number, SubstanceSignals(cas_number=cas_number))


def _load_fire_incidents(
    paths: Iterable[Path],
    signals: dict[str, SubstanceSignals],
) -> None:
    """CAS가 확인된 소방 사고 행을 집계한다.

    파일 하나의 행이 하나의 사고 단위를 뜻하는지는 수집 문서에서 별도로 보장해야 한다.
    이 함수는 행 수를 사고 확률이나 전국 빈도로 변환하지 않는다.
    """

    for path in paths:
        rows = list(_read_rows(path))
        if not rows:
            continue
        _require_columns(path, rows, {"CAS번호"})
        for row_number, row in enumerate(rows, start=2):
            cas_number = _cas(
                row.get("CAS번호"),
                source=path,
                row_number=row_number,
            )
            item = _signal(signals, cas_number)
            item.fire_incident_rows += 1
            for column in (
                "화학물질명_한글",
                "화학물질명_국문",
                "기본화학물질명",
            ):
                name = _text(row.get(column))
                if name:
                    item.names.add(name)


def _load_facilities(
    path: Path,
    signals: dict[str, SubstanceSignals],
) -> None:
    rows = list(_read_rows(path))
    if not rows:
        return
    _require_columns(path, rows, {"CAS번호", "업체명", "주소"})
    for row_number, row in enumerate(rows, start=2):
        if (
            "정확CAS모델링사용여부" in row
            and _text(row.get("정확CAS모델링사용여부")).upper() != "Y"
        ):
            continue

        cas_number = _cas(
            row.get("CAS번호"),
            source=path,
            row_number=row_number,
        )
        item = _signal(signals, cas_number)
        facility_name = _text(row.get("업체명"))
        address = _text(row.get("주소"))
        item.facility_records += 1
        item.facility_keys.add(f"{facility_name}|{address}")

        name = _text(
            row.get("화학물질명_선정기준")
            or row.get("화학물질명")
            or row.get("chemical_name")
        )
        if name:
            item.names.add(name)

        if _text(row.get("PRTR_업체_업체명주소정확매칭")).upper() == "Y":
            item.prtr_company_exact_records += 1
        if _text(row.get("PRTR_물질_CAS정확매칭")).upper() == "Y":
            item.prtr_material_exact_records += 1

        # 기존 통합 입력에만 소방 사고 집계가 있을 때 사용하는 보조 신호다.
        fire_count = _as_nonnegative_int(
            row.get("울산소방_사고자료행수"),
            source=path,
            row_number=row_number,
            column="울산소방_사고자료행수",
        )
        item.fire_incident_rows = max(item.fire_incident_rows, fire_count)


def _load_kosha(
    path: Path,
    signals: dict[str, SubstanceSignals],
) -> None:
    rows = list(_read_rows(path))
    if not rows:
        return
    _require_columns(path, rows, {"CAS번호"})
    for row_number, row in enumerate(rows, start=2):
        cas_number = _cas(
            row.get("CAS번호"),
            source=path,
            row_number=row_number,
        )
        item = _signal(signals, cas_number)
        item.kosha_msds_loaded = True
        name = _text(row.get("화학물질명_국문"))
        if name:
            item.names.add(name)


def _load_crosswalk(
    path: Path,
    signals: dict[str, SubstanceSignals],
) -> None:
    rows = list(_read_rows(path))
    if not rows:
        return
    _require_columns(path, rows, {"cas_number", "verification_status"})
    for row_number, row in enumerate(rows, start=2):
        cas_number = _cas(
            row.get("cas_number"),
            source=path,
            row_number=row_number,
        )
        item = _signal(signals, cas_number)
        if _text(row.get("verification_status")) == PUBLIC_VERIFIED_STATUS:
            item.cameo_public_verified = True


def _coverage_tier(item: SubstanceSignals) -> str:
    if item.operational_signal and item.kosha_msds_loaded and item.cameo_public_verified:
        return "END_TO_END_READY"
    if item.operational_signal and not item.kosha_msds_loaded and not item.cameo_public_verified:
        return "MSDS_AND_CAMEO_GAP"
    if item.operational_signal and not item.kosha_msds_loaded:
        return "MSDS_GAP"
    if item.operational_signal and not item.cameo_public_verified:
        return "CAMEO_GAP"
    if item.kosha_msds_loaded and item.cameo_public_verified:
        return "EVIDENCE_READY_LOW_OPERATIONAL_SIGNAL"
    return "SEARCH_ONLY"


def _missing_evidence(item: SubstanceSignals) -> list[str]:
    missing: list[str] = []
    if not item.kosha_msds_loaded:
        missing.append("KOSHA_MSDS")
    if not item.cameo_public_verified:
        missing.append("CAMEO_PUBLIC_CROSSWALK")
    return missing


def _demo_key(item: SubstanceSignals) -> tuple[Any, ...]:
    complete = item.kosha_msds_loaded and item.cameo_public_verified
    return (
        -int(complete),
        -int(item.fire_incident_rows > 0),
        -item.fire_incident_rows,
        -item.facility_count,
        -item.prtr_material_exact_records,
        item.cas_number,
    )


def _expansion_key(item: SubstanceSignals) -> tuple[Any, ...]:
    missing_count = len(_missing_evidence(item))
    return (
        -int(item.operational_signal),
        -int(item.fire_incident_rows > 0),
        -item.fire_incident_rows,
        -item.facility_count,
        -item.prtr_material_exact_records,
        -missing_count,
        item.cas_number,
    )


def build_support_priority(
    *,
    facility_path: Path,
    kosha_path: Path,
    crosswalk_path: Path,
    fire_incident_paths: Iterable[Path] = (),
) -> list[dict[str, Any]]:
    """공식 데이터 신호를 집계해 두 가지 지원 물질 순위를 반환한다."""

    signals: dict[str, SubstanceSignals] = {}
    _load_fire_incidents(fire_incident_paths, signals)
    _load_facilities(facility_path, signals)
    _load_kosha(kosha_path, signals)
    _load_crosswalk(crosswalk_path, signals)

    demo_ranks = {
        item.cas_number: index
        for index, item in enumerate(sorted(signals.values(), key=_demo_key), start=1)
    }
    expansion_ranks = {
        item.cas_number: index
        for index, item in enumerate(
            sorted(signals.values(), key=_expansion_key),
            start=1,
        )
    }

    results: list[dict[str, Any]] = []
    for item in sorted(signals.values(), key=_expansion_key):
        missing = _missing_evidence(item)
        results.append(
            {
                "schema_version": SCHEMA_VERSION,
                "cas_number": item.cas_number,
                "chemical_names": sorted(item.names),
                "coverage_tier": _coverage_tier(item),
                "demo_rank": demo_ranks[item.cas_number],
                "expansion_rank": expansion_ranks[item.cas_number],
                "fire_incident_rows": item.fire_incident_rows,
                "facility_record_count": item.facility_records,
                "facility_count": item.facility_count,
                "prtr_company_exact_record_count": item.prtr_company_exact_records,
                "prtr_material_exact_record_count": item.prtr_material_exact_records,
                "kosha_msds_loaded": item.kosha_msds_loaded,
                "cameo_public_verified": item.cameo_public_verified,
                "missing_official_evidence": missing,
                "is_probability": False,
                "current_inventory_confirmed": False,
                "interpretation": (
                    "공식 데이터 구축·시연 우선순위이며 위험 확률이나 현재 재고 확률이 아님"
                ),
            }
        )
    return results


__all__ = [
    "PUBLIC_VERIFIED_STATUS",
    "SCHEMA_VERSION",
    "SupportPriorityError",
    "build_support_priority",
]
