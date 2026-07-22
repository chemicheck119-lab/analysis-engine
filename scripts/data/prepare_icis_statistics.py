#!/usr/bin/env python3
"""ICIS 공식 Excel 내보내기를 모델링용 장문형 UTF-8 CSV로 정리한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
FINAL = ROOT / "data" / "raw"
RAW_ROOT = ROOT / "data" / "raw" / "ICIS_화학물질통계"
PROCESSED_ROOT = ROOT / "data" / "processed" / "ICIS_화학물질통계"
SOURCE_PAGE = "https://icis.mcee.go.kr/search/searchType6.do"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", default="2024")
    return parser.parse_args()


def normalize_text(value: object) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_cas(value: object) -> str:
    raw = normalize_text(value)
    match = re.fullmatch(r"0*(\d{1,7})-(\d{2})-(\d)", raw)
    if not match:
        return raw
    return f"{int(match.group(1))}-{match.group(2)}-{match.group(3)}"


def cas_format_valid(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,7}-\d{2}-\d", value))


def cas_checksum_valid(value: str) -> bool | None:
    if not cas_format_valid(value):
        return None
    left, middle, check = value.split("-")
    digits = left + middle
    checksum = (
        sum(int(digit) * weight for weight, digit in enumerate(reversed(digits), 1))
        % 10
    )
    return checksum == int(check)


PROVINCE_ALIASES = [
    ("강원특별자치도", "강원특별자치도"),
    ("강원도", "강원특별자치도"),
    ("강원", "강원특별자치도"),
    ("경기도", "경기도"),
    ("경기", "경기도"),
    ("경상남도", "경상남도"),
    ("경남", "경상남도"),
    ("경상북도", "경상북도"),
    ("경북", "경상북도"),
    ("광주광역시", "광주광역시"),
    ("광주", "광주광역시"),
    ("대구광역시", "대구광역시"),
    ("대구", "대구광역시"),
    ("대전광역시", "대전광역시"),
    ("대전", "대전광역시"),
    ("부산광역시", "부산광역시"),
    ("부산", "부산광역시"),
    ("서울특별시", "서울특별시"),
    ("서울", "서울특별시"),
    ("세종특별자치시", "세종특별자치시"),
    ("세종", "세종특별자치시"),
    ("울산광역시", "울산광역시"),
    ("울산", "울산광역시"),
    ("인천광역시", "인천광역시"),
    ("인천", "인천광역시"),
    ("전라남도", "전라남도"),
    ("전남", "전라남도"),
    ("전북특별자치도", "전북특별자치도"),
    ("전라북도", "전북특별자치도"),
    ("전북", "전북특별자치도"),
    ("제주특별자치도", "제주특별자치도"),
    ("제주도", "제주특별자치도"),
    ("제주", "제주특별자치도"),
    ("충청남도", "충청남도"),
    ("충남", "충청남도"),
    ("충청북도", "충청북도"),
    ("충북", "충청북도"),
]


def province_from_address(address: str) -> str:
    for prefix, province in PROVINCE_ALIASES:
        if address.startswith(prefix):
            return province
    return "미상"


def convert_xls_to_frame(path: Path) -> pd.DataFrame:
    soffice = shutil.which("soffice")
    if not soffice:
        raise RuntimeError("LibreOffice 변환 실행파일(soffice)을 찾을 수 없습니다.")
    with tempfile.TemporaryDirectory(prefix="icis_xls_") as temp_dir:
        command = [
            soffice,
            "--headless",
            "--convert-to",
            "csv:Text - txt - csv (StarCalc):44,34,76,1",
            "--outdir",
            temp_dir,
            str(path),
        ]
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        csv_path = Path(temp_dir) / f"{path.stem}.csv"
        if not csv_path.exists():
            raise RuntimeError(
                f"XLS→CSV 변환 결과가 없습니다: {path}\n{completed.stderr}"
            )
        return pd.read_csv(csv_path, dtype=str, keep_default_na=False)


def stable_id(prefix: str, parts: list[str]) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:14].upper()
    return f"{prefix}{digest}"


def clean_frame_strings(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in result.columns:
        result[column] = result[column].map(normalize_text)
    return result


def build_materials(raw_dir: Path, year: str, collected_at: str) -> pd.DataFrame:
    path = raw_dir / f"ICIS_{year}_전체_화학물질목록_공식Excel.xls"
    raw = clean_frame_strings(convert_xls_to_frame(path))
    expected_columns = {"번호", "물질명", "Cas No", "취급업체수"}
    if not expected_columns.issubset(raw.columns):
        raise ValueError(f"물질목록 컬럼 변경 감지: {list(raw.columns)}")
    # 원본과 같은 인덱스를 먼저 만든 뒤 상수 열을 넣어야 행 전체에 값이 채워진다.
    # 빈 DataFrame에 상수를 먼저 대입하면 이후 행 인덱스가 생길 때 해당 열이 NaN으로 남는다.
    output = pd.DataFrame(index=raw.index)
    output["조사연도"] = year
    output["화학물질명_원문"] = raw["물질명"]
    output["CAS번호_원문"] = raw["Cas No"]
    output["CAS번호_정규화"] = raw["Cas No"].map(normalize_cas)
    output["CAS형식유효"] = (
        output["CAS번호_정규화"].map(cas_format_valid).map({True: "Y", False: "N"})
    )
    output["CAS체크섬유효"] = (
        output["CAS번호_정규화"]
        .map(cas_checksum_valid)
        .map(lambda value: "" if value is None else ("Y" if value else "N"))
    )
    output["취급사업장수"] = pd.to_numeric(raw["취급업체수"], errors="coerce").astype(
        "Int64"
    )
    output["관측단위"] = "조사연도×화학물질"
    output["자료성격"] = f"{year}년 화학물질통계조사 공개 요약"
    output["현재보유확정여부"] = "N"
    output["원본파일명"] = path.name
    output["원본데이터셋_URL"] = SOURCE_PAGE
    output["수집일시_UTC"] = collected_at
    output.insert(
        0,
        "레코드ID",
        [
            stable_id("ICISMAT", [year, cas, name])
            for cas, name in zip(output["CAS번호_정규화"], output["화학물질명_원문"])
        ],
    )
    return output


def facility_frame_from_xls(path: Path) -> pd.DataFrame:
    raw = clean_frame_strings(convert_xls_to_frame(path))
    expected_columns = {"번호", "업체명", "주소", "업종"}
    if not expected_columns.issubset(raw.columns):
        raise ValueError(f"사업장목록 컬럼 변경 감지: {path.name}: {list(raw.columns)}")
    return raw[["업체명", "주소", "업종"]].copy()


def build_facilities(raw_dir: Path, year: str, collected_at: str) -> pd.DataFrame:
    path = raw_dir / f"ICIS_{year}_전체_사업장목록_공식Excel.xls"
    raw = facility_frame_from_xls(path)
    output = pd.DataFrame(index=raw.index)
    output["조사연도"] = year
    output["원본행번호"] = range(1, len(raw) + 1)
    output["업체명"] = raw["업체명"]
    output["주소"] = raw["주소"]
    output["시도명"] = raw["주소"].map(province_from_address)
    output["업종"] = raw["업종"]
    output["관측단위"] = "조사연도×사업장"
    output["자료성격"] = f"{year}년 화학물질 취급 신고 사업장"
    output["특정물질정보포함여부"] = "N"
    output["현재보유확정여부"] = "N"
    output["원본파일명"] = path.name
    output["원본데이터셋_URL"] = SOURCE_PAGE
    output["수집일시_UTC"] = collected_at
    output.insert(
        0,
        "레코드ID",
        [
            stable_id("ICISFAC", [year, str(row_number)])
            for row_number in output["원본행번호"]
        ],
    )
    return output


def build_selected_mapping(
    raw_dir: Path,
    manifest: dict[str, object],
    year: str,
    collected_at: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    selection_lookup = {
        str(item["cas_no"]): item for item in manifest.get("selected_chemicals", [])
    }
    file_records = {
        str(item.get("cas_no")): item
        for item in manifest.get("files", [])
        if item.get("cas_no")
    }
    mapping_frames: list[pd.DataFrame] = []
    status_rows: list[dict[str, object]] = []

    for cas, selection in selection_lookup.items():
        file_record = file_records.get(cas, {})
        expected_rows = int(file_record.get("expected_rows", 0) or 0)
        filename = file_record.get("file")
        reasons = "|".join(selection.get("selection_reasons", []))
        actual_rows = 0
        unique_facilities = 0
        status = str(file_record.get("status", "missing"))

        if filename:
            path = raw_dir / "핵심물질별_사업장_공식Excel" / str(filename)
            facility = facility_frame_from_xls(path)
            actual_rows = len(facility)
            unique_facilities = (
                facility[["업체명", "주소", "업종"]].drop_duplicates().shape[0]
            )
            part = facility.copy()
            part.insert(0, "조사연도", year)
            part.insert(1, "원본행번호", range(1, len(part) + 1))
            part.insert(2, "CAS번호", cas)
            part.insert(
                3, "화학물질명_선정기준", str(selection.get("chemical_name_ko", ""))
            )
            part.insert(4, "선정근거", reasons)
            part.insert(
                5,
                "울산소방_사고자료행수",
                int(selection.get("fire_incident_rows", 0) or 0),
            )
            part["시도명"] = part["주소"].map(province_from_address)
            part["관측단위"] = "조사연도×화학물질×사업장"
            part["근거유형"] = "화학물질통계조사_취급이력"
            part["시설후보물질여부"] = "Y"
            part["현재보유확정여부"] = "N"
            part["현재재고량"] = ""
            part["저장위치"] = ""
            part["안전표시문구"] = (
                f"{year}년 취급 신고 이력입니다. 현재 보유 여부·수량·저장위치는 현장에서 확인해야 합니다."
            )
            part["원본파일명"] = str(filename)
            part["원본데이터셋_URL"] = SOURCE_PAGE
            part["수집일시_UTC"] = collected_at
            part.insert(
                0,
                "레코드ID",
                [
                    stable_id("ICISMAP", [year, cas, str(row_number)])
                    for row_number in part["원본행번호"]
                ],
            )
            mapping_frames.append(part)

        status_rows.append(
            {
                "조사연도": year,
                "CAS번호": cas,
                "화학물질명_선정기준": selection.get("chemical_name_ko", ""),
                "선정근거": reasons,
                "울산소방_사고자료행수": int(
                    selection.get("fire_incident_rows", 0) or 0
                ),
                "웹검색_예상사업장수": expected_rows,
                "Excel_실제행수": actual_rows,
                "고유사업장수": unique_facilities,
                "행수검증일치": "Y" if expected_rows == actual_rows else "N",
                "2024년데이터상태": "있음" if expected_rows else "없음",
                "수집상태": status,
                "원본데이터셋_URL": SOURCE_PAGE,
            }
        )

    mapping = (
        pd.concat(mapping_frames, ignore_index=True)
        if mapping_frames
        else pd.DataFrame()
    )
    status_frame = pd.DataFrame(status_rows)
    status_frame.insert(
        0,
        "레코드ID",
        [stable_id("ICISSEL", [year, cas]) for cas in status_frame["CAS번호"]],
    )
    return mapping, status_frame


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig", na_rep="")


def main() -> None:
    args = parse_args()
    year = args.year
    raw_dir = RAW_ROOT / year
    manifest_path = raw_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            "수집 manifest가 없습니다. collect_icis_statistics.py를 먼저 실행하세요."
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    collected_at = str(
        manifest.get("collected_at_utc") or datetime.now(timezone.utc).isoformat()
    )

    materials = build_materials(raw_dir, year, collected_at)
    facilities = build_facilities(raw_dir, year, collected_at)
    mapping, status = build_selected_mapping(raw_dir, manifest, year, collected_at)

    for label, frame in {
        "물질목록": materials,
        "사업장목록": facilities,
        "선정물질매핑": mapping,
        "선정물질수집현황": status,
    }.items():
        if not frame.empty and not frame["조사연도"].eq(year).all():
            raise ValueError(f"{label} 조사연도 값 누락 또는 불일치")

    expected_by_cas = materials.set_index("CAS번호_정규화")["취급사업장수"].to_dict()
    status["전체물질목록_취급사업장수"] = (
        status["CAS번호"].map(expected_by_cas).astype("Int64")
    )
    status["전체목록과_필터행수일치"] = status.apply(
        lambda row: ""
        if pd.isna(row["전체물질목록_취급사업장수"])
        else (
            "Y"
            if int(row["전체물질목록_취급사업장수"]) == int(row["Excel_실제행수"])
            else "N"
        ),
        axis=1,
    )
    status["CAS검색검증상태"] = status.apply(
        lambda row: "검색결과없음"
        if row["2024년데이터상태"] == "없음"
        else (
            "전체목록미등재_필터결과"
            if pd.isna(row["전체물질목록_취급사업장수"])
            else (
                "전체목록집계와일치"
                if row["전체목록과_필터행수일치"] == "Y"
                else "부분일치혼입확인"
            )
        ),
        axis=1,
    )
    status["정확CAS모델링사용여부"] = status.apply(
        lambda row: "Y"
        if row["2024년데이터상태"] == "있음"
        and row["CAS검색검증상태"] == "전체목록집계와일치"
        else "N",
        axis=1,
    )

    if not mapping.empty:
        cas_validation = status.set_index("CAS번호")["CAS검색검증상태"].to_dict()
        cas_model_use = status.set_index("CAS번호")["정확CAS모델링사용여부"].to_dict()
        mapping["CAS검색검증상태"] = mapping["CAS번호"].map(cas_validation)
        mapping["정확CAS모델링사용여부"] = mapping["CAS번호"].map(cas_model_use)

    facility_keys = set(
        zip(facilities["업체명"], facilities["주소"], facilities["업종"])
    )
    if mapping.empty:
        mapping_subset_rate = 0.0
    else:
        mapping_keys = list(zip(mapping["업체명"], mapping["주소"], mapping["업종"]))
        mapping_subset_rate = sum(key in facility_keys for key in mapping_keys) / len(
            mapping_keys
        )

    outputs = {
        "13_ICIS_2024_화학물질_취급현황.csv": materials,
        "14_ICIS_2024_화학물질취급_사업장목록.csv": facilities,
        "15_ICIS_2024_선정물질_사업장매핑.csv": mapping,
        "16_ICIS_2024_선정물질_수집현황.csv": status,
    }
    processed_dir = PROCESSED_ROOT / year
    for filename, frame in outputs.items():
        write_csv(frame, processed_dir / filename)
        write_csv(frame, FINAL / filename)

    checks = {
        "조사연도": year,
        "물질목록_행수": len(materials),
        "물질목록_조사연도불일치행수": int((materials["조사연도"] != year).sum()),
        "물질목록_고유CAS수": int(materials["CAS번호_정규화"].nunique()),
        "물질목록_CAS형식유효율": float((materials["CAS형식유효"] == "Y").mean()),
        "물질목록_CAS체크섬유효율_형식유효중": float(
            (
                materials.loc[materials["CAS형식유효"] == "Y", "CAS체크섬유효"] == "Y"
            ).mean()
        ),
        "사업장목록_행수": len(facilities),
        "사업장목록_조사연도불일치행수": int((facilities["조사연도"] != year).sum()),
        "사업장목록_레코드ID중복": int(facilities["레코드ID"].duplicated().sum()),
        "사업장목록_표시값완전중복행수": int(
            facilities.duplicated(subset=["업체명", "주소", "업종"], keep=False).sum()
        ),
        "사업장목록_시도미상행수": int((facilities["시도명"] == "미상").sum()),
        "선정물질수": len(status),
        "2024년_데이터있는_선정물질수": int(
            (status["2024년데이터상태"] == "있음").sum()
        ),
        "선정물질_사업장매핑행수": len(mapping),
        "선정물질_고유CAS수": int(mapping["CAS번호"].nunique())
        if not mapping.empty
        else 0,
        "매핑_레코드ID중복": int(mapping["레코드ID"].duplicated().sum())
        if not mapping.empty
        else 0,
        "매핑_표시값완전중복행수": int(
            mapping.duplicated(
                subset=["CAS번호", "업체명", "주소", "업종"], keep=False
            ).sum()
        )
        if not mapping.empty
        else 0,
        "웹검색과_Excel행수_불일치물질수": int((status["행수검증일치"] != "Y").sum()),
        "전체물질목록과_필터행수_불일치물질수": int(
            (status["전체목록과_필터행수일치"].fillna("") == "N").sum()
        ),
        "정확CAS모델링사용가능물질수": int(
            (status["정확CAS모델링사용여부"] == "Y").sum()
        ),
        "정확CAS모델링사용가능매핑행수": int(
            (mapping["정확CAS모델링사용여부"] == "Y").sum()
        )
        if not mapping.empty
        else 0,
        "매핑행_전체사업장목록포함률": mapping_subset_rate,
        "현재보유확정값_Y_행수": int((mapping["현재보유확정여부"] == "Y").sum())
        if not mapping.empty
        else 0,
        "원본페이지": SOURCE_PAGE,
    }
    write_csv(status, processed_dir / "선정물질_검증상세.csv")
    (processed_dir / "검증결과.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    readme = f"""# ICIS 화학물질 통계정보 공개 데이터 ({year})

- 원본: ICIS 화면이 제공하는 공식 Excel 다운로드
- 전체 물질: {len(materials):,}행
- 전체 사업장: {len(facilities):,}행
- 선정물질 사업장 매핑: {len(mapping):,}행 / {mapping["CAS번호"].nunique() if not mapping.empty else 0:,}개 CAS
- 전체 물질 요약 집계와 일치해 정확 CAS 모델링에 사용 가능한 매핑: {(mapping["정확CAS모델링사용여부"] == "Y").sum() if not mapping.empty else 0:,}행
- 공개자료 의미: `{year}년 취급 신고 이력`
- 공개자료가 말하지 않는 것: 현재 보유 여부, 현재 재고량, 저장 위치

`15_ICIS_2024_선정물질_사업장매핑.csv`는 시설에서 먼저 확인할 후보물질을 만드는 보조 근거입니다.
화재·누출 현장에서 "현재 이 물질이 있다"고 단정하는 용도로 사용하면 안 됩니다.
ICIS CAS 상세검색은 문자열 부분 일치가 포함될 수 있으므로 `정확CAS모델링사용여부=Y`인 행만
정확 CAS 통계·학습·Rule Engine 근거로 사용합니다.
"""
    (processed_dir / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
