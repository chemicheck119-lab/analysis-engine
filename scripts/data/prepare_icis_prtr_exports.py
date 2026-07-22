#!/usr/bin/env python3
"""ICIS PRTR 공식 Excel을 모델링용 UTF-8 CSV로 정리하고 검증한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "data" / "raw" / "ICIS_PRTR"
PROCESSED_ROOT = ROOT / "data" / "processed" / "ICIS_PRTR"
FINAL = ROOT / "data" / "raw"
COMPANY_SOURCE = "https://icis.mcee.go.kr/prtr/prtrInfo/entrpsSearch.do"
MATERIAL_SOURCE = "https://icis.mcee.go.kr/prtr/prtrInfo/mttrSearch.do"


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


def cas_checksum_valid(value: str) -> bool | None:
    if not re.fullmatch(r"\d{1,7}-\d{2}-\d", value):
        return None
    left, middle, check = value.split("-")
    digits = left + middle
    return sum(
        int(digit) * weight for weight, digit in enumerate(reversed(digits), 1)
    ) % 10 == int(check)


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


def stable_id(prefix: str, parts: list[str]) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:14].upper()
    return f"{prefix}{digest}"


def convert_xls(path: Path) -> pd.DataFrame:
    soffice = shutil.which("soffice")
    if not soffice:
        raise RuntimeError("LibreOffice 변환 실행파일(soffice)을 찾을 수 없습니다.")
    with tempfile.TemporaryDirectory(prefix="prtr_xls_") as temp_dir:
        subprocess.run(
            [
                soffice,
                "--headless",
                "--convert-to",
                "csv:Text - txt - csv (StarCalc):44,34,76,1",
                "--outdir",
                temp_dir,
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        csv_path = Path(temp_dir) / f"{path.stem}.csv"
        frame = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    for column in frame.columns:
        frame[column] = frame[column].map(normalize_text)
    return frame


def number(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(cleaned, errors="coerce").astype("Int64")


def write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig", na_rep="")


def main() -> None:
    args = parse_args()
    year = args.year
    raw_dir = RAW_ROOT / year
    manifest = json.loads((raw_dir / "manifest.json").read_text(encoding="utf-8"))
    collected_at = str(manifest["collected_at_utc"])

    company_path = raw_dir / f"ICIS_PRTR_{year}_업체별_공식Excel.xls"
    company_raw = convert_xls(company_path)
    company_expected = {
        "업체명",
        "주소",
        "배출량(kg/년)",
        "자가매립량(kg/년)",
        "이동량(kg/년)",
    }
    if not company_expected.issubset(company_raw.columns):
        raise ValueError(f"PRTR 업체별 컬럼 변경 감지: {list(company_raw.columns)}")
    # 상수 열(조사연도)이 모든 행에 채워지도록 원본 인덱스로 시작한다.
    companies = pd.DataFrame(index=company_raw.index)
    companies["조사연도"] = year
    companies["원본행번호"] = range(1, len(company_raw) + 1)
    companies["업체명"] = company_raw["업체명"]
    companies["주소"] = company_raw["주소"]
    companies["시도명"] = company_raw["주소"].map(province_from_address)
    companies["총배출량_kg_년"] = number(company_raw["배출량(kg/년)"])
    companies["자가매립량_kg_년"] = number(company_raw["자가매립량(kg/년)"])
    companies["총이동량_kg_년"] = number(company_raw["이동량(kg/년)"])
    companies["보고흐름합계_kg_년"] = (
        companies[["총배출량_kg_년", "자가매립량_kg_년", "총이동량_kg_년"]]
        .sum(axis=1, min_count=1)
        .astype("Int64")
    )
    companies["관측단위"] = "조사연도×PRTR업체"
    companies["자료성격"] = "연간 배출·이동 흐름"
    companies["현재보유확정여부"] = "N"
    companies["재고량아님"] = "Y"
    companies["원본파일명"] = company_path.name
    companies["원본데이터셋_URL"] = COMPANY_SOURCE
    companies["수집일시_UTC"] = collected_at
    companies.insert(
        0,
        "레코드ID",
        [
            stable_id("PRTRCOM", [year, str(row_number)])
            for row_number in companies["원본행번호"]
        ],
    )

    material_path = raw_dir / f"ICIS_PRTR_{year}_물질별_공식Excel.xls"
    material_raw = convert_xls(material_path)
    material_expected = {
        "CAS No.",
        "화학물질명",
        "배출업체수",
        "대기배출량(kg/년)",
        "수계배출량(kg/년)",
        "토양배출량(kg/년)",
        "배출량(kg/년)",
        "자가매립량(kg/년)",
        "폐수이동량(kg/년)",
        "폐기물이동량(kg/년)",
        "이동량(kg/년)",
    }
    if not material_expected.issubset(material_raw.columns):
        raise ValueError(f"PRTR 물질별 컬럼 변경 감지: {list(material_raw.columns)}")
    materials = pd.DataFrame(index=material_raw.index)
    materials["조사연도"] = year
    materials["원본행번호"] = range(1, len(material_raw) + 1)
    materials["CAS번호_원문"] = material_raw["CAS No."]
    materials["CAS번호_정규화"] = material_raw["CAS No."].map(normalize_cas)
    materials["CAS체크섬유효"] = (
        materials["CAS번호_정규화"]
        .map(cas_checksum_valid)
        .map(lambda value: "" if value is None else ("Y" if value else "N"))
    )
    materials["화학물질명"] = material_raw["화학물질명"]
    materials["배출업체수"] = number(material_raw["배출업체수"])
    source_to_output = {
        "대기배출량(kg/년)": "대기배출량_kg_년",
        "수계배출량(kg/년)": "수계배출량_kg_년",
        "토양배출량(kg/년)": "토양배출량_kg_년",
        "배출량(kg/년)": "총배출량_kg_년",
        "자가매립량(kg/년)": "자가매립량_kg_년",
        "폐수이동량(kg/년)": "폐수이동량_kg_년",
        "폐기물이동량(kg/년)": "폐기물이동량_kg_년",
        "이동량(kg/년)": "총이동량_kg_년",
    }
    for source, output in source_to_output.items():
        materials[output] = number(material_raw[source])
    materials["배출합계검산차이_kg_년"] = (
        materials["총배출량_kg_년"]
        - materials[["대기배출량_kg_년", "수계배출량_kg_년", "토양배출량_kg_년"]].sum(
            axis=1
        )
    ).astype("Int64")
    materials["이동합계검산차이_kg_년"] = (
        materials["총이동량_kg_년"]
        - materials[["폐수이동량_kg_년", "폐기물이동량_kg_년"]].sum(axis=1)
    ).astype("Int64")
    materials["관측단위"] = "조사연도×PRTR화학물질"
    materials["자료성격"] = "연간 배출·이동 흐름"
    materials["현재보유확정여부"] = "N"
    materials["재고량아님"] = "Y"
    materials["원본파일명"] = material_path.name
    materials["원본데이터셋_URL"] = MATERIAL_SOURCE
    materials["수집일시_UTC"] = collected_at
    materials.insert(
        0,
        "레코드ID",
        [
            stable_id("PRTRMAT", [year, str(row_number)])
            for row_number in materials["원본행번호"]
        ],
    )

    for label, frame in {"업체별": companies, "물질별": materials}.items():
        if not frame["조사연도"].eq(year).all():
            raise ValueError(f"PRTR {label} 조사연도 값 누락 또는 불일치")

    company_sums = companies[
        ["총배출량_kg_년", "자가매립량_kg_년", "총이동량_kg_년"]
    ].sum()
    material_sums = materials[
        ["총배출량_kg_년", "자가매립량_kg_년", "총이동량_kg_년"]
    ].sum()
    release_difference = int(
        company_sums["총배출량_kg_년"] - material_sums["총배출량_kg_년"]
    )
    landfill_difference = int(
        company_sums["자가매립량_kg_년"] - material_sums["자가매립량_kg_년"]
    )
    transfer_difference = int(
        company_sums["총이동량_kg_년"] - material_sums["총이동량_kg_년"]
    )
    checks = {
        "조사연도": year,
        "업체별_행수": len(companies),
        "업체별_조사연도불일치행수": int((companies["조사연도"] != year).sum()),
        "업체별_레코드ID중복": int(companies["레코드ID"].duplicated().sum()),
        "업체별_시도미상행수": int((companies["시도명"] == "미상").sum()),
        "물질별_행수": len(materials),
        "물질별_조사연도불일치행수": int((materials["조사연도"] != year).sum()),
        "물질별_고유CAS수": int(materials["CAS번호_정규화"].nunique()),
        "물질별_CAS체크섬불일치행수": int((materials["CAS체크섬유효"] == "N").sum()),
        "물질별_배출합계검산불일치행수": int(
            (materials["배출합계검산차이_kg_년"].abs() > 1).sum()
        ),
        "물질별_이동합계검산불일치행수": int(
            (materials["이동합계검산차이_kg_년"].abs() > 1).sum()
        ),
        "업체합계_vs_물질합계_총배출량차이_kg_년": release_difference,
        "업체합계_vs_물질합계_총배출량상대일치율": float(
            1 - abs(release_difference) / material_sums["총배출량_kg_년"]
        ),
        "업체합계_vs_물질합계_자가매립량차이_kg_년": landfill_difference,
        "업체합계_vs_물질합계_자가매립량상대일치율": float(
            1 - abs(landfill_difference) / material_sums["자가매립량_kg_년"]
        ),
        "업체합계_vs_물질합계_총이동량차이_kg_년": transfer_difference,
        "업체합계_vs_물질합계_총이동량상대일치율": float(
            1 - abs(transfer_difference) / material_sums["총이동량_kg_년"]
        ),
        "현재보유확정값_Y_행수": int((companies["현재보유확정여부"] == "Y").sum())
        + int((materials["현재보유확정여부"] == "Y").sum()),
    }

    processed_dir = PROCESSED_ROOT / year
    outputs = {
        "17_ICIS_PRTR_2024_업체별_배출이동량.csv": companies,
        "18_ICIS_PRTR_2024_물질별_배출이동량.csv": materials,
    }
    for filename, frame in outputs.items():
        write_csv(frame, processed_dir / filename)
        write_csv(frame, FINAL / filename)
    (processed_dir / "검증결과.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    readme = f"""# ICIS PRTR {year} 정리본

- 업체별: {len(companies):,}행
- 물질별: {len(materials):,}행
- 단위: kg/년
- 의미: 대기·수계·토양 배출, 자가매립, 폐수·폐기물 이동의 연간 흐름
- 주의: 취급량·생산량·현재 재고량이 아니며, 시설 후보물질의 보조 근거로만 사용합니다.
"""
    (processed_dir / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
