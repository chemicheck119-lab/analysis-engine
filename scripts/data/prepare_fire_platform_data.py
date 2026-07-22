#!/usr/bin/env python3
"""소방안전 빅데이터 플랫폼 원본을 모델링용 UTF-8 CSV로 정리한다."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "소방빅데이터플랫폼"
PLATFORM_RAW = RAW / "플랫폼_원본"
PORTAL_RAW = RAW / "공공데이터포털_원본"
FINAL = ROOT / "data" / "raw"
PROCESSED = ROOT / "data" / "processed" / "소방빅데이터플랫폼"
COLLECTED_DATE = "2026-07-15"


CHEMICAL_MAP = {
    "화학물질 일련번호": "화학물질_일련번호",
    "화학물질 한글명": "화학물질명_한글",
    "화학물질 영문명": "화학물질명_영문",
    "cas_등록번호": "CAS번호",
    "EC번호": "EC번호",
    "UN번호": "UN번호",
    "상온상태": "상온상태",
    "색상": "색상",
    "냄새": "냄새",
    "사용용도 설명": "사용용도_설명",
}

DISASTER_MAP = {
    "일련번호": "재난화학물_일련번호",
    "재난화학물 등록일": "재난화학물_등록일",
    "화학물질 한글명": "화학물질명_한글",
    "화학물질 영문명": "화학물질명_영문",
    "cas등록번호": "CAS번호",
    "긴급구조 우편번호": "긴급구조_우편번호",
    "긴급구조 시도명": "긴급구조_시도명",
    "긴급구조 구군명": "긴급구조_구군명",
    "긴급구조 동명": "긴급구조_동명",
    "긴급구조 리명": "긴급구조_리명",
    "긴급구조 종별명": "긴급구조_종별명",
    "긴급구조 분류명": "긴급구조_분류명",
    "긴급구조 규모명": "긴급구조_규모명",
    "상황종료일시": "상황종료일시",
    "관할서명": "관할서명",
    "서센터명": "센터명",
    "도로명": "도로명",
    "읍면동 일련번호": "읍면동_일련번호",
    "지하여부": "지상지하구분",
}

HARMFUL_MAP = {
    "SN": "일련번호",
    "OCRN_YR": "발생연도",
    "OCRN_MM": "발생월",
    "CAS_NO": "CAS번호",
    "CHEM_SBSTN_ENG_NM": "화학물질명_영문",
    "CHEM_SBSTN_KORN_NM": "화학물질명_한글",
    "CHEM_SBSTN_IDNTY_DT": "화학물질_확인일시",
    "EMRG_RSCU_CLSF_NM": "긴급구조_분류명",
    "EMRG_RSCU_CTPV_NM": "긴급구조_시도명",
    "EMRG_RSCU_GUGUN_NM": "긴급구조_구군명",
    "EMRG_RSCU_EMD_NM": "긴급구조_읍면동명",
    "EMRG_RSCU_KND_NM": "긴급구조_종류명",
    "EMRG_RSCU_SCL_NM": "긴급구조_규모명",
    "EMRG_RSCU_ZIP": "긴급구조_우편번호",
    "EMD_SN": "읍면동_일련번호",
    "SITTN_END_DT": "상황종료일시",
    "PLCSCN_NM": "관할서명",
    "CNTR_NM": "센터명",
    "ROAD_NM": "도로명",
    "UDGD_YN": "지상지하구분",
    "GNRL_KORN_NM": "일반명_한글",
    "GNRL_ENG_NM": "일반명_영문",
    "BSC_CHEM_SBSTN_NM": "기본화학물질명",
    "HFLS_CHEM_SBSTN_YN": "유해화학물질_여부",
    "EMPHS_MNG_YN": "중점관리_여부",
    "CRCNS_SBSTN_YN": "발암돌연변이_여부",
    "ACDNT_PRPR_SBSTN_YN": "사고대비물질_여부",
    "HFLS_SBSTN_NO": "유해물질번호",
    "HFLS_SBSTN_TYPE_NM": "유해물질_유형",
    "HFLS_SBSTN_SPCQLT_NM": "유해물질_특성",
    "HFLS_SBSTN_RMRK_CN": "유해물질_비고",
}

INCIDENT_MAP = {
    "SN": "일련번호",
    "RCPT_PATH_NM": "접수경로명",
    "RCPT_DT": "접수일시",
    "OTR_CTPV_DCLR_YN": "타시도신고_여부",
    "EMRG_RSCU_ZIP": "긴급구조_우편번호",
    "EMRG_RSCU_CTPV_NM": "긴급구조_시도명",
    "EMRG_RSCU_GUGUN_NM": "긴급구조_구군명",
    "EMRG_RSCU_EMD_NM": "긴급구조_읍면동명",
    "LI_NM": "리명",
    "EMRG_RSCU_KND_NM": "긴급구조_종류명",
    "EMRG_RSCU_CLSF_NM": "긴급구조_분류명",
    "EMRG_RSCU_SCL_NM": "긴급구조_규모명",
    "SITTN_END_DT": "상황종료일시",
    "PLCSCN_NM": "관할서명",
    "CNTR_NM": "센터명",
    "ROAD_NM": "도로명",
    "EMD_SN": "읍면동_일련번호",
    "UDGD_YN": "지상지하구분",
    "JNT_CRSP_YN": "공동대응_여부",
    "HIWY_ACDNT_YN": "고속도로사고_여부",
    "CHEM_ACDNT_YN": "화학사고_여부",
    "OTR_CTPV_DSPT_YN": "타시도출동_여부",
}

FREQUENCY_MAP = {
    "ACDNT_FREQ_UNQ_NO": "사고빈도_고유번호",
    "ACDNT_FREQ_NO": "사고빈도_번호",
    "ACDNT_YMD_NM": "사고일자명",
    "PIPE_NO": "배관번호",
    "PIPE_LNKG_INFO": "배관연결정보",
    "CO_INFO": "회사정보",
    "ACDNT_TYPE": "사고유형",
    "CIPS_ON_ELCPTT_VL": "좁은간격상세전위_온전위값",
    "CIPS_OFF_ELCPTT_VL": "좁은간격상세전위_오프전위값",
    "COAT_DAMG_OBSRVN_VL": "코팅손상_관측값",
    "SLD_RS_VL": "토양저항값",
    "SLD_TYPE_VL": "토양유형값",
    "INSD_CRRSN_RT": "내부부식률",
    "CRRSN_RT": "부식률",
    "NOW_THICK_VL": "현재두께값",
    "BKDN_FREQ_RT": "고장빈도율",
    "BKDN_FREQ_GRD": "고장빈도등급",
    "PIPE_PPDMT": "배관관경",
    "PIPE_LEN": "배관길이",
    "PIPE_MATRL_CD": "배관재료코드",
    "PIPE_COAT_MATRL_CD": "배관코팅재료코드",
    "PIPE_DESIGN_THICK_VL": "배관설계두께값",
    "PIPE_USE_TP": "배관사용온도",
    "PIPE_INSD_FLUID_STTS_CD": "배관내부유체상태코드",
    "SBSTN_CD": "물질코드",
    "PIPE_PRSR_VL": "배관압력값",
    "PIPE_FLUX_VL": "배관유량값",
    "PIPE_INSTL_YR": "배관설치연도",
}

DAMAGE_MAP = {
    "ACDNT_DAM_SCL_UNQ_NO": "사고피해규모_고유번호",
    "ACDNT_DAM_SCL_NO": "사고피해규모_번호",
    "PIPE_NO": "배관번호",
    "CO_INFO": "회사정보",
    "ACDNT_TYPE": "사고유형",
    "TXCT_DSTNC": "독성거리",
    "RADI_DSTNC": "방사거리",
    "EXPL_DSTNC": "폭발거리",
    "DAM_TYPE_VL": "피해유형값",
    "PIPE_ACDNT_DAM_AREA": "배관사고_피해면적",
    "PIPE_ACDNT_DAM_AREA_GRD": "배관사고_피해면적등급",
    "PIPE_PPDMT": "배관관경",
    "PIPE_LEN": "배관길이",
    "PIPE_MATRL_CD": "배관재료코드",
    "PIPE_COAT_MATRL_CD": "배관코팅재료코드",
    "PIPE_DESIGN_THICK_VL": "배관설계두께값",
    "PIPE_USE_TP": "배관사용온도",
    "PIPE_INSD_FLUID_STTS_CD": "배관내부유체상태코드",
    "SBSTN_CD": "물질코드",
    "PIPE_PRSR_VL": "배관압력값",
    "PIPE_FLUX_VL": "배관유량값",
    "PIPE_INSTL_YR": "배관설치연도",
}

RISK_MAP = {
    "ACDNT_DGDGR_EVL_UNQ_NO": "사고위험도평가_고유번호",
    "ACDNT_DGDGR_EVL_NO": "사고위험도평가_번호",
    "PIPE_NO": "배관번호",
    "CO_INFO": "회사정보",
    "ACDNT_TYPE": "사고유형",
    "ACDNT_DAM_KND": "사고피해종류",
    "DAM_AREA_CRTR_DGDGR_VL": "피해면적기준_위험도값",
    "DAM_AREA_CRTR_DGDGR_GRD": "피해면적기준_위험도등급",
    "DCSD_CRTR_DAM_FREQ_RT": "사망자기준_피해빈도",
    "DCSD_CRTR_DAM_IDCT_VL": "사망자기준_피해지표값",
    "DCSD_CRTR_DGDGR_VL": "사망자기준_위험도값",
    "DCSD_CRTR_DGDGR_GRD": "사망자기준_위험도등급",
    "PIPE_PPDMT": "배관관경",
    "PIPE_LEN": "배관길이",
    "PIPE_MATRL_CD": "배관재료코드",
    "PIPE_COAT_MATRL_CD": "배관코팅재료코드",
    "PIPE_DESIGN_THICK_VL": "배관설계두께값",
    "PIPE_USE_TP": "배관사용온도",
    "PIPE_INSD_FLUID_STTS_CD": "배관내부유체상태코드",
    "SBSTN_CD": "물질코드",
    "PIPE_PRSR_VL": "배관압력값",
    "PIPE_FLUX_VL": "배관유량값",
    "PIPE_INSTL_YR": "배관설치연도",
}


def nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def clean_text(df: pd.DataFrame) -> pd.DataFrame:
    for column in df.columns:
        df[column] = df[column].map(
            lambda value: nfc(value.strip()) if isinstance(value, str) else value
        )
    return df


def read_csv(path: Path, encoding: str = "utf-8-sig") -> pd.DataFrame:
    return clean_text(pd.read_csv(path, encoding=encoding, dtype=str, low_memory=False))


def period_from_filename(filename: str) -> tuple[str, str, str]:
    normalized = nfc(filename)
    match = re.search(r"_(\d{4})(?:_(\d)분기)?\.csv$", normalized)
    if not match:
        if "_0000.csv" in normalized:
            return "기존누적", "", "누적"
        return "미상", "", "미상"
    year = match.group(1)
    quarter = match.group(2)
    if quarter:
        return f"{year}-Q{quarter}", year, f"{quarter}분기"
    return year, year, "연간"


def provenance(
    df: pd.DataFrame,
    source_file: str,
    source_url: str,
    period: str | None = None,
    year: str | None = None,
    quarter: str | None = None,
) -> pd.DataFrame:
    if period is not None:
        df["자료기간"] = period
    if year is not None:
        df["자료연도"] = year
    if quarter is not None:
        df["자료분기"] = quarter
    df["원본파일명"] = nfc(source_file)
    df["원본데이터셋_URL"] = source_url
    df["수집일자"] = COLLECTED_DATE
    return df


def write_final(df: pd.DataFrame, filename: str) -> dict[str, object]:
    output = FINAL / filename
    df.to_csv(output, index=False, encoding="utf-8-sig", na_rep="")
    return {
        "파일명": filename,
        "행수": int(len(df)),
        "열수": int(len(df.columns)),
        "완전중복행수": int(df.duplicated().sum()),
        "레코드ID중복수": int(df["레코드ID"].duplicated().sum()),
        "결측셀수": int(df.isna().sum().sum()),
        "파일크기_바이트": output.stat().st_size,
    }


def prepare_single_sources() -> list[dict[str, object]]:
    results: list[dict[str, object]] = []

    chemical_path = PORTAL_RAW / "소방청_울산시 화학물 데이터_20210115.csv"
    chemical = read_csv(chemical_path, "cp949").rename(columns=CHEMICAL_MAP)
    chemical.insert(
        0,
        "레코드ID",
        "ULSAN-CHEM-" + chemical["화학물질_일련번호"].fillna("MISSING"),
    )
    provenance(
        chemical,
        chemical_path.name,
        "https://www.data.go.kr/data/15081005/fileData.do",
        period="2021-01-15 기준",
        year="2021",
        quarter="기준일",
    )
    results.append(write_final(chemical, "06_울산소방_화학물정보.csv"))

    harmful_path = (
        PLATFORM_RAW / "울산_화학사고별_유해물질판단" / "유해물질판단_2020_2015.csv"
    )
    harmful = read_csv(harmful_path).rename(columns=HARMFUL_MAP)
    harmful.insert(
        0,
        "레코드ID",
        "ULSAN-HAZARD-JUDGE-" + harmful["일련번호"].fillna("MISSING"),
    )
    provenance(
        harmful,
        harmful_path.name,
        "https://bigdata-119.kr/goods/goodsInfo?goods_mng_sn=5",
        period="2015-2020",
        year="2015-2020",
        quarter="전체",
    )
    results.append(write_final(harmful, "07_울산소방_화학사고별_유해물질판단.csv"))

    disaster_path = PORTAL_RAW / "울산시 재난별 화학물질 데이터.csv"
    disaster = read_csv(disaster_path, "cp949").rename(columns=DISASTER_MAP)
    disaster.insert(
        0,
        "레코드ID",
        "ULSAN-DISASTER-" + disaster["재난화학물_일련번호"].fillna("MISSING"),
    )
    provenance(
        disaster,
        disaster_path.name,
        "https://www.data.go.kr/data/15080955/fileData.do",
        period="2015-2020",
        year="2015-2020",
        quarter="전체",
    )
    results.append(write_final(disaster, "08_울산소방_재난별_화학물질현황.csv"))

    return results


def prepare_partitioned(
    folder: Path,
    mapping: dict[str, str],
    source_url: str,
    key_column: str,
    record_prefix: str,
    output_name: str,
) -> dict[str, object]:
    parts: list[pd.DataFrame] = []
    for path in sorted(folder.glob("*.csv"), key=lambda item: nfc(item.name)):
        frame = read_csv(path).rename(columns=mapping)
        period, year, quarter = period_from_filename(path.name)
        frame.insert(
            0,
            "레코드ID",
            record_prefix + "-" + period + "-" + frame[key_column].fillna("MISSING"),
        )
        provenance(
            frame,
            path.name,
            source_url,
            period=period,
            year=year,
            quarter=quarter,
        )
        parts.append(frame)

    combined = pd.concat(parts, ignore_index=True)
    return write_final(combined, output_name)


def prepare_incidents() -> dict[str, object]:
    folder = PLATFORM_RAW / "울산_유해화학물질사고현황"
    parts: list[pd.DataFrame] = []
    for path in sorted(folder.glob("*.csv"), key=lambda item: nfc(item.name)):
        frame = read_csv(path).rename(columns=INCIDENT_MAP)
        period, year, quarter = period_from_filename(path.name)
        frame.insert(
            0,
            "레코드ID",
            "ULSAN-HAZ-INCIDENT-" + frame["일련번호"].fillna("MISSING"),
        )
        provenance(
            frame,
            path.name,
            "https://bigdata-119.kr/goods/goodsInfo?goods_mng_sn=19",
            period=period,
            year=year,
            quarter=quarter,
        )
        parts.append(frame)
    combined = pd.concat(parts, ignore_index=True)
    return write_final(combined, "12_울산소방_유해화학물질사고현황.csv")


def write_data_dictionary() -> None:
    rows: list[dict[str, str]] = []
    datasets = {
        "06_울산소방_화학물정보.csv": CHEMICAL_MAP,
        "07_울산소방_화학사고별_유해물질판단.csv": HARMFUL_MAP,
        "08_울산소방_재난별_화학물질현황.csv": DISASTER_MAP,
        "09_화학배관_사고발생빈도.csv": FREQUENCY_MAP,
        "10_화학배관_사고피해규모.csv": DAMAGE_MAP,
        "11_화학배관_사고위험성평가.csv": RISK_MAP,
        "12_울산소방_유해화학물질사고현황.csv": INCIDENT_MAP,
    }
    for dataset, mapping in datasets.items():
        for original, korean in mapping.items():
            rows.append(
                {
                    "최종파일명": dataset,
                    "원본컬럼명": original,
                    "정리컬럼명": korean,
                }
            )
    pd.DataFrame(rows).to_csv(
        PROCESSED / "소방빅데이터플랫폼_컬럼사전.csv",
        index=False,
        encoding="utf-8-sig",
    )


def main() -> None:
    FINAL.mkdir(parents=True, exist_ok=True)
    PROCESSED.mkdir(parents=True, exist_ok=True)

    validation = prepare_single_sources()
    validation.append(
        prepare_partitioned(
            PLATFORM_RAW / "화학배관_사고발생빈도",
            FREQUENCY_MAP,
            "https://bigdata-119.kr/goods/goodsInfo?goods_mng_sn=192",
            "사고빈도_고유번호",
            "PIPE-FREQ",
            "09_화학배관_사고발생빈도.csv",
        )
    )
    validation.append(
        prepare_partitioned(
            PLATFORM_RAW / "화학배관_사고피해규모",
            DAMAGE_MAP,
            "https://bigdata-119.kr/goods/goodsInfo?goods_mng_sn=193",
            "사고피해규모_고유번호",
            "PIPE-DAMAGE",
            "10_화학배관_사고피해규모.csv",
        )
    )
    validation.append(
        prepare_partitioned(
            PLATFORM_RAW / "화학배관_사고위험성평가",
            RISK_MAP,
            "https://bigdata-119.kr/goods/goodsInfo?goods_mng_sn=194",
            "사고위험도평가_고유번호",
            "PIPE-RISK",
            "11_화학배관_사고위험성평가.csv",
        )
    )
    validation.append(prepare_incidents())

    write_data_dictionary()
    with (PROCESSED / "검증결과.json").open("w", encoding="utf-8") as handle:
        json.dump(validation, handle, ensure_ascii=False, indent=2)

    print(json.dumps(validation, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
