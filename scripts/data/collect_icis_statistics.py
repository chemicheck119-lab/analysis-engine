#!/usr/bin/env python3
"""ICIS 화학물질 통계정보 공개화면의 공식 Excel 내보내기를 수집한다.

대규모 개별 페이지 크롤링은 하지 않는다. 화면에 제공된 Excel 다운로드 기능만
사용해 전체 물질목록·전체 사업장목록과 프로젝트 핵심 CAS 필터 결과를 받는다.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
FINAL = ROOT / "data" / "raw"
RAW_ROOT = ROOT / "data" / "raw" / "ICIS_화학물질통계"
SOURCE_PAGE = "https://icis.mcee.go.kr/search/searchType6.do"
BASE_URL = "https://icis.mcee.go.kr"
USER_AGENT = "ChemGuard119-Research/1.0 (official public Excel export)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", default="2024")
    parser.add_argument(
        "--accident-top",
        type=int,
        default=30,
        help="울산 소방 사고자료에서 빈도가 높은 CAS를 추가로 수집할 개수",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Excel 다운로드 요청 사이의 최소 대기시간(초)",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def normalize_cas(value: str) -> str:
    value = value.strip()
    match = re.fullmatch(r"0*(\d{1,7})-(\d{2})-(\d)", value)
    if not match:
        return value
    return f"{int(match.group(1))}-{match.group(2)}-{match.group(3)}"


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def selected_chemicals(accident_top: int) -> list[dict[str, object]]:
    selected: dict[str, dict[str, object]] = {}

    kosha_path = FINAL / "01_KOSHA_물질안전보건자료.csv"
    for row in read_csv_rows(kosha_path):
        cas = normalize_cas(row.get("CAS번호", ""))
        if not re.fullmatch(r"\d{1,7}-\d{2}-\d", cas):
            continue
        item = selected.setdefault(
            cas,
            {
                "cas_no": cas,
                "chemical_name_ko": row.get("검색기준_화학물질명")
                or row.get("화학물질명_국문")
                or "",
                "selection_reasons": [],
                "fire_incident_rows": 0,
            },
        )
        if "KOSHA_핵심9물질" not in item["selection_reasons"]:
            item["selection_reasons"].append("KOSHA_핵심9물질")

    accident_path = FINAL / "07_울산소방_화학사고별_유해물질판단.csv"
    accident_rows = read_csv_rows(accident_path)
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for row in accident_rows:
        cas = normalize_cas(row.get("CAS번호", ""))
        if not re.fullmatch(r"\d{1,7}-\d{2}-\d", cas):
            continue
        counts[cas] += 1
        names.setdefault(cas, row.get("화학물질명_한글", ""))

    for cas, count in counts.most_common(max(0, accident_top)):
        item = selected.setdefault(
            cas,
            {
                "cas_no": cas,
                "chemical_name_ko": names.get(cas, ""),
                "selection_reasons": [],
                "fire_incident_rows": 0,
            },
        )
        if not item["chemical_name_ko"]:
            item["chemical_name_ko"] = names.get(cas, "")
        item["fire_incident_rows"] = count
        item["selection_reasons"].append(f"울산소방_사고빈도_TOP{accident_top}")

    return sorted(selected.values(), key=lambda item: str(item["cas_no"]))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_xls(path: Path) -> None:
    if path.stat().st_size < 1024:
        raise ValueError(f"다운로드 파일이 너무 작습니다: {path}")
    with path.open("rb") as handle:
        magic = handle.read(8)
    if magic != bytes.fromhex("D0CF11E0A1B11AE1"):
        raise ValueError(f"정상적인 XLS 파일이 아닙니다: {path}")


def download_xls(
    endpoint: str,
    params: dict[str, str],
    output: Path,
    force: bool,
) -> dict[str, object]:
    if output.exists() and not force:
        validate_xls(output)
        return {
            "status": "reused",
            "file": output.name,
            "bytes": output.stat().st_size,
            "sha256": sha256(output),
            "endpoint": endpoint,
            "params": params,
        }

    data = urlencode(params).encode("utf-8")
    request = Request(
        BASE_URL + endpoint,
        data=data,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": SOURCE_PAGE,
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urlopen(request, timeout=120) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "")
            output.write_bytes(payload)
            validate_xls(output)
            return {
                "status": "downloaded",
                "file": output.name,
                "bytes": len(payload),
                "sha256": sha256(output),
                "content_type": content_type,
                "endpoint": endpoint,
                "params": params,
            }
        except (HTTPError, URLError, TimeoutError, ValueError) as error:
            last_error = error
            output.unlink(missing_ok=True)
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"다운로드 실패: {endpoint} -> {output}") from last_error


def query_facility_count(year: str, cas_no: str) -> int:
    params = facility_params(year, cas_no)
    params["pageNo"] = "1"
    request = Request(
        BASE_URL + "/iprtr/cdrInfoDetailListJson.do",
        data=urlencode(params).encode("utf-8"),
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": SOURCE_PAGE,
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("result") != "SUCCESS":
                raise ValueError(f"검색 실패 응답: {payload.get('result')}")
            return int(payload.get("totalCount", 0))
        except (
            HTTPError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"CAS 검색 건수 확인 실패: {cas_no}") from last_error


def material_params(year: str) -> dict[str, str]:
    return {
        "pageNo": "1",
        "sortType": "",
        "sortOrder": "",
        "casNo": "",
        "searchYear": year,
        "searchAdres1": "",
        "searchAdres2": "",
        "searchType": "all",
        "searchWord": "",
    }


def facility_params(year: str, cas_no: str = "") -> dict[str, str]:
    return {
        "pageNo": "1",
        "searchAdres1Text": "전체지역",
        "searchAdres2Text": "전체지역",
        "bplcId": "",
        "streNo": "",
        "searchYear": year,
        "searchAdres1": "",
        "searchAdres2": "",
        "mttrGroup": "",
        "searchCategory": "03" if cas_no else "",
        "searchMttrWord": cas_no,
        "indutyCode": "",
        "indutyCode2": "",
        "indutyCode3": "",
        "indutyCode4": "",
        "irsttList": "",
        "bplcNm": "",
    }


def main() -> None:
    args = parse_args()
    output_dir = RAW_ROOT / args.year
    filtered_dir = output_dir / "핵심물질별_사업장_공식Excel"
    output_dir.mkdir(parents=True, exist_ok=True)
    filtered_dir.mkdir(parents=True, exist_ok=True)

    selections = selected_chemicals(args.accident_top)
    records: list[dict[str, object]] = []

    records.append(
        download_xls(
            "/iprtr/cdrInfoExcelDown.do",
            material_params(args.year),
            output_dir / f"ICIS_{args.year}_전체_화학물질목록_공식Excel.xls",
            args.force,
        )
    )
    time.sleep(max(0.0, args.delay))
    records.append(
        download_xls(
            "/iprtr/cdrDetailInfoExcelDown.do",
            facility_params(args.year),
            output_dir / f"ICIS_{args.year}_전체_사업장목록_공식Excel.xls",
            args.force,
        )
    )

    selection_by_file: dict[str, dict[str, object]] = {}
    for item in selections:
        time.sleep(max(0.0, args.delay))
        cas = str(item["cas_no"])
        filename = f"ICIS_{args.year}_CAS_{cas}_취급사업장_공식Excel.xls"
        expected_rows = query_facility_count(args.year, cas)
        if expected_rows == 0:
            records.append(
                {
                    "status": "no_data",
                    "file": None,
                    "endpoint": "/iprtr/cdrInfoDetailListJson.do",
                    "cas_no": cas,
                    "chemical_name_ko": item["chemical_name_ko"],
                    "selection_reasons": item["selection_reasons"],
                    "fire_incident_rows": item["fire_incident_rows"],
                    "expected_rows": 0,
                }
            )
            continue
        time.sleep(max(0.0, args.delay))
        record = download_xls(
            "/iprtr/cdrDetailInfoExcelDown.do",
            facility_params(args.year, cas),
            filtered_dir / filename,
            args.force,
        )
        record["cas_no"] = cas
        record["chemical_name_ko"] = item["chemical_name_ko"]
        record["selection_reasons"] = item["selection_reasons"]
        record["fire_incident_rows"] = item["fire_incident_rows"]
        record["expected_rows"] = expected_rows
        records.append(record)
        selection_by_file[filename] = item

    manifest = {
        "source_page": SOURCE_PAGE,
        "collection_method": "공개화면의 공식 Excel 다운로드 기능",
        "collection_scope": "전체 물질목록, 전체 사업장목록, 핵심 CAS 필터 사업장목록",
        "individual_page_crawling": False,
        "report_year": args.year,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "selected_chemical_count": len(selections),
        "selected_chemicals": selections,
        "files": records,
        "safety_note": (
            "사업장-물질 결과는 조사연도의 취급 이력이며 현재 보유·재고·저장위치를 확정하지 않는다."
        ),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output_dir": str(output_dir),
                "file_count": len(records),
                "selected_chemical_count": len(selections),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
