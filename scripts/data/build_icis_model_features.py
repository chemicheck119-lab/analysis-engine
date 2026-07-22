#!/usr/bin/env python3
"""ICIS 시설후보, PRTR, KOSHA를 안전한 모델 입력 피처로 결합한다."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
FINAL = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed" / "ICIS_통합모델입력" / "2024"
OUTPUT_NAME = "19_ICIS_2024_시설후보_통합모델입력.csv"


def normalize_text(series: pd.Series) -> pd.Series:
    return (
        series.fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    )


def stable_id(value: str) -> str:
    return "ICISMOD" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:14].upper()


def yes_no(series: pd.Series) -> pd.Series:
    return series.fillna(False).astype(bool).map({True: "Y", False: "N"})


def main() -> None:
    mapping = pd.read_csv(
        FINAL / "15_ICIS_2024_선정물질_사업장매핑.csv", dtype=str, low_memory=False
    )
    prtr_company = pd.read_csv(
        FINAL / "17_ICIS_PRTR_2024_업체별_배출이동량.csv", dtype=str, low_memory=False
    )
    prtr_material = pd.read_csv(
        FINAL / "18_ICIS_PRTR_2024_물질별_배출이동량.csv", dtype=str, low_memory=False
    )
    kosha = pd.read_csv(
        FINAL / "01_KOSHA_물질안전보건자료.csv", dtype=str, low_memory=False
    )

    mapping["_업체키"] = (
        normalize_text(mapping["업체명"]) + "|" + normalize_text(mapping["주소"])
    )
    prtr_company["_업체키"] = (
        normalize_text(prtr_company["업체명"])
        + "|"
        + normalize_text(prtr_company["주소"])
    )
    prtr_company = prtr_company.rename(
        columns={
            "총배출량_kg_년": "PRTR_시설전체_총배출량_kg_년",
            "자가매립량_kg_년": "PRTR_시설전체_자가매립량_kg_년",
            "총이동량_kg_년": "PRTR_시설전체_총이동량_kg_년",
            "보고흐름합계_kg_년": "PRTR_시설전체_보고흐름합계_kg_년",
            "원본데이터셋_URL": "PRTR_업체별_출처URL",
        }
    )
    company_columns = [
        "_업체키",
        "PRTR_시설전체_총배출량_kg_년",
        "PRTR_시설전체_자가매립량_kg_년",
        "PRTR_시설전체_총이동량_kg_년",
        "PRTR_시설전체_보고흐름합계_kg_년",
        "PRTR_업체별_출처URL",
    ]
    combined = mapping.merge(
        prtr_company[company_columns], on="_업체키", how="left", validate="many_to_one"
    )
    combined["PRTR_업체_업체명주소정확매칭"] = yes_no(
        combined["PRTR_업체별_출처URL"].notna()
    )

    prtr_material = prtr_material.rename(
        columns={
            "CAS번호_정규화": "CAS번호",
            "배출업체수": "PRTR_해당물질_배출업체수",
            "총배출량_kg_년": "PRTR_해당물질_전국총배출량_kg_년",
            "자가매립량_kg_년": "PRTR_해당물질_전국자가매립량_kg_년",
            "총이동량_kg_년": "PRTR_해당물질_전국총이동량_kg_년",
            "원본데이터셋_URL": "PRTR_물질별_출처URL",
        }
    )
    material_columns = [
        "CAS번호",
        "PRTR_해당물질_배출업체수",
        "PRTR_해당물질_전국총배출량_kg_년",
        "PRTR_해당물질_전국자가매립량_kg_년",
        "PRTR_해당물질_전국총이동량_kg_년",
        "PRTR_물질별_출처URL",
    ]
    combined = combined.merge(
        prtr_material[material_columns],
        on="CAS번호",
        how="left",
        validate="many_to_one",
    )
    combined["PRTR_물질_CAS정확매칭"] = yes_no(combined["PRTR_물질별_출처URL"].notna())

    kosha_cas = set(kosha["CAS번호"].dropna().astype(str).str.strip())
    combined["KOSHA_MSDS_CAS정확매칭"] = (
        combined["CAS번호"].isin(kosha_cas).map({True: "Y", False: "N"})
    )
    combined["CAMEO_CAS교차표상태"] = "미구축_검증된_CAS-CAMEO_ID_교차표필요"
    combined["RuleEngine_자동판정가능여부"] = "N"
    combined["현재보유확정여부"] = "N"
    combined["모델출력용도"] = "시설후보물질_확인우선순위"
    combined.loc[combined["정확CAS모델링사용여부"] != "Y", "모델출력용도"] = (
        "정확CAS검증전_후보조회참고용"
    )
    combined["안전제약"] = (
        "2024년 취급 이력 기반 후보이며 현재 존재·수량·저장위치는 현장에서 확인해야 함"
    )
    combined.loc[combined["정확CAS모델링사용여부"] != "Y", "안전제약"] += (
        "; ICIS 부분일치 검색 가능성이 있어 정확 CAS 근거로 자동 사용 금지"
    )
    combined["기준매핑_레코드ID"] = combined["레코드ID"]
    combined["레코드ID"] = combined["기준매핑_레코드ID"].map(stable_id)
    combined = combined.drop(columns=["_업체키"])

    front = [
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
    ]
    remaining = [column for column in combined.columns if column not in front]
    combined = combined[front + remaining]

    PROCESSED.mkdir(parents=True, exist_ok=True)
    combined.to_csv(
        PROCESSED / OUTPUT_NAME, index=False, encoding="utf-8-sig", na_rep=""
    )
    combined.to_csv(FINAL / OUTPUT_NAME, index=False, encoding="utf-8-sig", na_rep="")

    qa = pd.Series(
        {
            "행수": len(combined),
            "레코드ID중복": int(combined["레코드ID"].duplicated().sum()),
            "KOSHA_MSDS_CAS매칭행수": int(
                (combined["KOSHA_MSDS_CAS정확매칭"] == "Y").sum()
            ),
            "PRTR_업체정확매칭행수": int(
                (combined["PRTR_업체_업체명주소정확매칭"] == "Y").sum()
            ),
            "PRTR_물질CAS매칭행수": int(
                (combined["PRTR_물질_CAS정확매칭"] == "Y").sum()
            ),
            "정확CAS모델링사용가능행수": int(
                (combined["정확CAS모델링사용여부"] == "Y").sum()
            ),
            "RuleEngine자동판정Y행수": int(
                (combined["RuleEngine_자동판정가능여부"] == "Y").sum()
            ),
            "현재보유확정Y행수": int((combined["현재보유확정여부"] == "Y").sum()),
        },
        name="값",
    )
    qa.to_csv(PROCESSED / "검증요약.csv", encoding="utf-8-sig")
    print(qa.to_string())


if __name__ == "__main__":
    main()
