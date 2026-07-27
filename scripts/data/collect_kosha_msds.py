#!/usr/bin/env python3
"""KOSHA 공식 OpenAPI에서 CAS별 MSDS를 검토용 staging CSV로 수집한다."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chemiguard119.kosha_client import (
    KOSHA_API_BASE_URL,
    KOSHA_DETAIL_SECTIONS,
    KOSHA_SOURCE_PAGE,
    KOSHA_STAGING_COLUMNS,
    KoshaApiError,
    KoshaMsdsClient,
)
from chemiguard119.utils import normalize_cas, sha256_file, valid_cas_checksum


SERVICE_KEY_ENV = "KOSHA_API_SERVICE_KEY"
COLLECTION_SCHEMA_VERSION = "chemicheck119-kosha-collection-v1"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("1 이상의 정수여야 합니다.")
    return parsed


def _parse_sections(value: str) -> tuple[int, ...]:
    try:
        sections = tuple(sorted({int(item.strip()) for item in value.split(",")}))
    except ValueError as error:
        raise argparse.ArgumentTypeError("장번호는 쉼표로 구분한 정수여야 합니다.") from error
    if not sections or any(section not in KOSHA_DETAIL_SECTIONS for section in sections):
        raise argparse.ArgumentTypeError("장번호는 1~16 범위여야 합니다.")
    return sections


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "KOSHA MSDS 공식 XML API를 정확 CAS로 조회합니다. 결과는 즉시 운영 "
            "데이터로 병합하지 않고 staging CSV와 수집 manifest로 저장합니다."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--cas",
        action="append",
        help="수집할 CAS 번호. 여러 번 지정할 수 있습니다.",
    )
    source.add_argument(
        "--priority-csv",
        type=Path,
        help="build_support_material_priority.py가 만든 전체 우선순위 CSV",
    )
    parser.add_argument("--limit", type=_positive_int, default=10)
    parser.add_argument(
        "--skip-cas",
        action="append",
        default=[],
        help="수집에서 제외할 CAS. 여러 번 지정할 수 있습니다.",
    )
    parser.add_argument(
        "--sections",
        type=_parse_sections,
        default=KOSHA_DETAIL_SECTIONS,
        help="수집할 MSDS 장번호. 기본값: 1~16 전체",
    )
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--request-interval-seconds", type=float, default=0.1)
    return parser.parse_args()


def _validated_cas(value: str) -> str:
    cas = normalize_cas(value)
    if not valid_cas_checksum(cas):
        raise ValueError(f"CAS 형식 또는 체크섬이 유효하지 않습니다: {cas!r}")
    return cas


def select_priority_cas(
    path: Path,
    *,
    limit: int,
    skip_cas: set[str] | None = None,
) -> list[str]:
    """미적재 KOSHA 근거가 있는 CAS를 expansion_rank 순으로 선택한다."""

    if not path.is_file():
        raise FileNotFoundError(f"지원 물질 우선순위 CSV를 찾을 수 없습니다: {path}")
    skip = skip_cas or set()
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "cas_number",
            "expansion_rank",
            "missing_official_evidence",
        }
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(
                "지원 물질 우선순위 CSV 필수 컬럼 누락: " + ", ".join(missing)
            )
        candidates: list[tuple[int, str]] = []
        for row in reader:
            missing_evidence = set(
                filter(None, (row.get("missing_official_evidence") or "").split("|"))
            )
            if "KOSHA_MSDS" not in missing_evidence:
                continue
            cas = _validated_cas(row.get("cas_number") or "")
            if cas in skip:
                continue
            try:
                rank = int(row.get("expansion_rank") or "")
            except ValueError as error:
                raise ValueError(f"CAS {cas}의 expansion_rank가 정수가 아닙니다.") from error
            candidates.append((rank, cas))
    return [cas for _, cas in sorted(set(candidates))[:limit]]


def _atomic_write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8-sig",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=KOSHA_STAGING_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(path)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temp_path.replace(path)


def collect_batch(
    client: KoshaMsdsClient,
    cas_numbers: list[str],
    *,
    sections: tuple[int, ...],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    rows: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []
    record_ids: set[str] = set()
    for cas in cas_numbers:
        try:
            result = client.collect_cas(cas, sections=sections)
        except KoshaApiError as error:
            results.append(
                {
                    "cas_number": cas,
                    "status": "FAILED",
                    "error": error.as_dict(),
                }
            )
            continue
        records = result.pop("records")
        for row in records:
            record_id = row["레코드ID"]
            if record_id in record_ids:
                raise ValueError(f"KOSHA staging 레코드ID가 중복됩니다: {record_id}")
            record_ids.add(record_id)
            rows.append(row)
        results.append({**result, "record_count": len(records)})
    return rows, results


def main() -> None:
    args = parse_args()
    service_key = os.environ.get(SERVICE_KEY_ENV, "").strip()
    if not service_key:
        raise SystemExit(
            f"{SERVICE_KEY_ENV}가 설정되지 않았습니다. 키는 명령행이나 Git에 "
            "기록하지 말고 환경변수로 주입하세요."
        )

    skipped = {_validated_cas(value) for value in args.skip_cas}
    if args.priority_csv:
        cas_numbers = select_priority_cas(
            args.priority_csv,
            limit=args.limit,
            skip_cas=skipped,
        )
        selection = {
            "method": "SUPPORT_PRIORITY_EXPANSION_RANK",
            "input_file": args.priority_csv.name,
            "input_sha256": sha256_file(args.priority_csv),
        }
    else:
        cas_numbers = []
        for value in args.cas or []:
            cas = _validated_cas(value)
            if cas not in skipped and cas not in cas_numbers:
                cas_numbers.append(cas)
        cas_numbers = cas_numbers[: args.limit]
        selection = {"method": "EXPLICIT_CAS"}
    if not cas_numbers:
        raise SystemExit("수집할 유효 CAS가 없습니다.")

    client = KoshaMsdsClient(
        service_key,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        request_interval_seconds=args.request_interval_seconds,
    )
    rows, results = collect_batch(client, cas_numbers, sections=args.sections)
    output_csv = args.output_csv.expanduser().resolve()
    manifest_path = (
        args.manifest.expanduser().resolve()
        if args.manifest
        else output_csv.with_suffix(".manifest.json")
    )
    _atomic_write_csv(output_csv, rows)

    status_counts = Counter(str(result["status"]) for result in results)
    manifest = {
        "schema_version": COLLECTION_SCHEMA_VERSION,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": {
            "organization": "한국산업안전보건공단",
            "data_portal_url": KOSHA_SOURCE_PAGE,
            "api_base_url": KOSHA_API_BASE_URL,
            "response_format": "XML",
        },
        "selection": {
            **selection,
            "requested_cas": cas_numbers,
            "skipped_cas": sorted(skipped),
        },
        "sections": list(args.sections),
        "api_key_env": SERVICE_KEY_ENV,
        "api_key_recorded": False,
        "request_count_including_retries": client.request_count,
        "status_counts": dict(sorted(status_counts.items())),
        "record_count": len(rows),
        "results": results,
        "output": {
            "file": output_csv.name,
            "bytes": output_csv.stat().st_size,
            "sha256": sha256_file(output_csv),
        },
        "review_required_before_merge": True,
        "safety_notes": [
            "KOSHA 공개자료는 참고용이며 제조사·수입자의 최신 MSDS를 대체하지 않는다.",
            "정확 CAS 검색 결과가 여러 chemId이면 임의 선택하지 않고 AMBIGUOUS_EXACT_CAS로 남긴다.",
            "이 수집 단계는 CAMEO 반응성 형태를 자동 확정하지 않는다.",
        ],
    }
    _atomic_write_json(manifest_path, manifest)
    print(
        json.dumps(
            {
                "output_csv": str(output_csv),
                "manifest": str(manifest_path),
                "requested_cas_count": len(cas_numbers),
                "record_count": len(rows),
                "status_counts": dict(sorted(status_counts.items())),
            },
            ensure_ascii=False,
        )
    )
    if status_counts.get("FAILED", 0):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
