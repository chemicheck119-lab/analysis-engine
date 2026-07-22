#!/usr/bin/env python3
"""ICIS PRTR 공식 화면의 업체별·물질별 Excel 내보내기를 수집한다."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / "data" / "raw" / "ICIS_PRTR"
BASE_URL = "https://icis.mcee.go.kr"
SOURCE_PAGE = "https://icis.mcee.go.kr/prtr/prtrInfo.do"
USER_AGENT = "ChemGuard119-Research/1.0 (official public Excel export)"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", default="2024")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def validate_xls(path: Path) -> None:
    if path.stat().st_size < 1024:
        raise ValueError(f"다운로드 파일이 너무 작습니다: {path}")
    if path.read_bytes()[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
        raise ValueError(f"정상적인 XLS 파일이 아닙니다: {path}")


def download(
    endpoint: str, params: dict[str, str], output: Path, force: bool
) -> dict[str, object]:
    if not output.exists() or force:
        request = Request(
            BASE_URL + endpoint,
            data=urlencode(params).encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": SOURCE_PAGE,
            },
        )
        with urlopen(request, timeout=120) as response:
            output.write_bytes(response.read())
        status = "downloaded"
    else:
        status = "reused"
    validate_xls(output)
    return {
        "status": status,
        "file": output.name,
        "endpoint": endpoint,
        "params": params,
        "bytes": output.stat().st_size,
        "sha256": sha256(output),
    }


def main() -> None:
    args = parse_args()
    year = args.year
    output_dir = RAW_ROOT / year
    output_dir.mkdir(parents=True, exist_ok=True)
    common = {
        "searchYear": year,
        "searchYearCheck": f"{year}년",
        "searchIndutyConditonCount": "1",
        "pageIndex": "1",
    }
    material_params = {
        **common,
        "airColumn": "Y",
        "waterColumn": "Y",
        "soilColumn": "Y",
        "pyesooColumn": "Y",
        "pyegiColumn": "Y",
        "jagaColumn": "Y",
        "baechoolColumn": "Y",
        "idongColumn": "Y",
    }
    files = [
        download(
            "/prtr/prtrInfo/entrpsSearchDown.do",
            common,
            output_dir / f"ICIS_PRTR_{year}_업체별_공식Excel.xls",
            args.force,
        ),
        download(
            "/prtr/prtrInfo/mttrSearchDown.do",
            material_params,
            output_dir / f"ICIS_PRTR_{year}_물질별_공식Excel.xls",
            args.force,
        ),
    ]
    manifest = {
        "source_page": SOURCE_PAGE,
        "collection_method": "PRTR 공개화면의 공식 Excel 다운로드 기능",
        "report_year": year,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "api_key_used": False,
        "individual_detail_crawling": False,
        "files": files,
        "safety_note": "배출·이동량은 과거 연간 흐름이며 현재 재고량이 아니다.",
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {"output_dir": str(output_dir), "file_count": len(files)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
