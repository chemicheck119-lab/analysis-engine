"""울산 사고–CAS 원본을 Resolver 평가용 최소 컬럼으로 축약한다.

공식 원본에는 관할서·도로명·우편번호 같은 위치 관련 열이 함께 들어 있다.
Resolver source adaptation에는 이 열이 필요하지 않으므로, 이 모듈은 학습과
평가에 필요한 연도·CAS·물질명 여섯 열만 private 경로로 투영한다. 원본 행을
필터링하거나 정답을 수정하지 않으며, 원본과 파생 파일의 SHA-256을 manifest에
기록한다.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chemiguard119.utils import sha256_file, write_json


SOURCE_DATASET_ID = "NFA_BIGDATA119_ULSAN_HAZARDOUS_SUBSTANCE_JUDGMENT_2015_2020"
SOURCE_URL = "https://bigdata-119.kr/goods/goodsInfo?goods_mng_sn=5"
INTAKE_SCHEMA_VERSION = "ulsan-resolver-source-intake-v1"
OUTPUT_COLUMNS = (
    "발생연도",
    "CAS번호",
    "화학물질명_한글",
    "화학물질명_영문",
    "일반명_한글",
    "일반명_영문",
)
SOURCE_COLUMNS = {
    "OCRN_YR": "발생연도",
    "CAS_NO": "CAS번호",
    "CHEM_SBSTN_KORN_NM": "화학물질명_한글",
    "CHEM_SBSTN_ENG_NM": "화학물질명_영문",
    "GNRL_KORN_NM": "일반명_한글",
    "GNRL_ENG_NM": "일반명_영문",
}
SUPPORTED_ENCODINGS = ("utf-8-sig", "cp949")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _read_source(source_bytes: bytes) -> tuple[str, list[str], list[dict[str, str]]]:
    last_error: UnicodeDecodeError | None = None
    for encoding in SUPPORTED_ENCODINGS:
        try:
            decoded = source_bytes.decode(encoding)
            with io.StringIO(decoded, newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = [
                    str(item or "").strip() for item in reader.fieldnames or []
                ]
                rows: list[dict[str, str]] = []
                for row in reader:
                    preserved: dict[str, str] = {}
                    for key, value in row.items():
                        if key is None:
                            continue
                        preserved[str(key).strip()] = (
                            value if isinstance(value, str) else ""
                        )
                    rows.append(preserved)
            return encoding, fieldnames, rows
        except UnicodeDecodeError as error:
            last_error = error
    if last_error is not None:
        raise ValueError(
            "원본 CSV를 utf-8-sig 또는 cp949로 해석할 수 없습니다."
        ) from last_error
    raise ValueError("원본 CSV를 읽을 수 없습니다.")


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"intake manifest의 {label} 객체가 필요합니다.")
    return value


def verify_ulsan_resolver_source_manifest(
    derived_path: Path,
    manifest_path: Path,
    *,
    expected_row_count: int,
    observed_derived_sha256: str | None = None,
) -> dict[str, Any]:
    """파생 CSV와 sidecar가 같은 승인 원본 계보를 가리키는지 검증한다."""

    derived_path = Path(derived_path)
    manifest_path = Path(manifest_path)
    try:
        manifest_bytes = manifest_path.read_bytes()
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"intake manifest를 읽을 수 없습니다: {manifest_path}"
        ) from error
    if not isinstance(payload, Mapping):
        raise ValueError("intake manifest 최상위는 객체여야 합니다.")
    if payload.get("schema_version") != INTAKE_SCHEMA_VERSION:
        raise ValueError("지원하지 않는 intake manifest schema_version입니다.")
    if payload.get("source_dataset_id") != SOURCE_DATASET_ID:
        raise ValueError("intake manifest source_dataset_id가 다릅니다.")
    if payload.get("source_url") != SOURCE_URL:
        raise ValueError("intake manifest source_url이 다릅니다.")

    source = _mapping(payload.get("source"), "source")
    derived = _mapping(payload.get("derived"), "derived")
    transformation = _mapping(payload.get("transformation"), "transformation")
    safety = _mapping(payload.get("safety"), "safety")
    source_sha256 = str(source.get("sha256") or "")
    if not SHA256_PATTERN.fullmatch(source_sha256):
        raise ValueError("intake manifest 원본 SHA-256이 유효하지 않습니다.")
    expected_derived_sha256 = str(derived.get("sha256") or "")
    if not SHA256_PATTERN.fullmatch(expected_derived_sha256):
        raise ValueError("intake manifest 파생 CSV SHA-256이 유효하지 않습니다.")
    actual_derived_sha256 = observed_derived_sha256 or sha256_file(derived_path)
    if actual_derived_sha256 != expected_derived_sha256:
        raise ValueError("intake manifest와 파생 CSV의 SHA-256이 다릅니다.")
    if derived.get("file_name") != derived_path.name:
        raise ValueError("intake manifest와 파생 CSV 파일명이 다릅니다.")
    if derived.get("row_count") != expected_row_count:
        raise ValueError("intake manifest와 파생 CSV 행 수가 다릅니다.")
    if source.get("row_count") != expected_row_count:
        raise ValueError("intake manifest의 원본·파생 행 수가 다릅니다.")
    if derived.get("columns") != list(OUTPUT_COLUMNS):
        raise ValueError("intake manifest의 파생 컬럼 계약이 다릅니다.")
    if transformation != {
        "type": "COLUMN_PROJECTION_ONLY",
        "row_filtering_applied": False,
        "value_relabeling_applied": False,
        "training_split_applied": False,
    }:
        raise ValueError("intake manifest의 변환 계약이 다릅니다.")
    if (
        source.get("contains_location_or_response_fields") is not True
        or derived.get("contains_location_or_response_fields") is not False
        or safety.get("git_commit_allowed") is not False
        or safety.get("private_storage_required") is not True
        or safety.get("claim_scope") != "SOURCE_INTAKE_ONLY_NOT_RESOLVER_PERFORMANCE"
    ):
        raise ValueError("intake manifest의 private 저장·주장 범위 계약이 다릅니다.")
    return {
        "schema_version": INTAKE_SCHEMA_VERSION,
        "manifest_file": manifest_path.name,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "raw_source_file": source.get("file_name"),
        "raw_source_sha256": source_sha256,
        "raw_source_row_count": source.get("row_count"),
        "derived_file": derived_path.name,
        "derived_sha256": expected_derived_sha256,
        "derived_row_count": expected_row_count,
        "private_storage_required": True,
        "claim_scope": "SOURCE_INTAKE_ONLY_NOT_RESOLVER_PERFORMANCE",
    }


def _source_projection(fieldnames: list[str]) -> dict[str, str]:
    normalized = {field.strip().upper(): field for field in fieldnames}
    missing = sorted(set(SOURCE_COLUMNS) - set(normalized))
    if missing:
        raise ValueError(f"울산 사고–CAS 원본 컬럼이 부족합니다: {missing}")
    return {output: normalized[source] for source, output in SOURCE_COLUMNS.items()}


def _write_projected_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def prepare_ulsan_resolver_source(
    source_path: Path,
    output_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    """공식 원본에서 Resolver에 필요한 열만 private 파생 파일로 만든다."""

    source_path = Path(source_path).resolve()
    output_path = Path(output_path).resolve()
    manifest_path = Path(manifest_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"울산 사고–CAS 원본이 없습니다: {source_path}")
    if len({source_path, output_path, manifest_path}) != 3:
        raise ValueError("원본·파생 CSV·manifest 경로는 서로 달라야 합니다.")
    existing = [path for path in (output_path, manifest_path) if path.exists()]
    if existing:
        raise FileExistsError(
            "기존 파생 파일을 덮어쓰지 않습니다: "
            + ", ".join(str(path) for path in existing)
        )

    source_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    encoding, fieldnames, source_rows = _read_source(source_bytes)
    projection = _source_projection(fieldnames)
    projected_rows = [
        {
            column: row.get(source_column, "")
            for column, source_column in projection.items()
        }
        for row in source_rows
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    output_descriptor, output_temporary_name = tempfile.mkstemp(
        dir=output_path.parent,
        prefix=f".{output_path.name}.",
        suffix=".tmp",
    )
    os.close(output_descriptor)
    manifest_descriptor, manifest_temporary_name = tempfile.mkstemp(
        dir=manifest_path.parent,
        prefix=f".{manifest_path.name}.",
        suffix=".tmp",
    )
    os.close(manifest_descriptor)
    temporary_output = Path(output_temporary_name)
    temporary_manifest = Path(manifest_temporary_name)
    manifest: dict[str, Any]
    try:
        _write_projected_csv(temporary_output, projected_rows)
        manifest = {
            "schema_version": INTAKE_SCHEMA_VERSION,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_dataset_id": SOURCE_DATASET_ID,
            "source_url": SOURCE_URL,
            "source": {
                "file_name": source_path.name,
                "sha256": source_sha256,
                "encoding": encoding,
                "row_count": len(source_rows),
                "contains_location_or_response_fields": True,
            },
            "derived": {
                "file_name": output_path.name,
                "sha256": sha256_file(temporary_output),
                "encoding": "utf-8-sig",
                "row_count": len(projected_rows),
                "columns": list(OUTPUT_COLUMNS),
                "contains_location_or_response_fields": False,
            },
            "transformation": {
                "type": "COLUMN_PROJECTION_ONLY",
                "row_filtering_applied": False,
                "value_relabeling_applied": False,
                "training_split_applied": False,
            },
            "safety": {
                "git_commit_allowed": False,
                "private_storage_required": True,
                "claim_scope": "SOURCE_INTAKE_ONLY_NOT_RESOLVER_PERFORMANCE",
            },
        }
        write_json(temporary_manifest, manifest)
        with temporary_manifest.open("rb") as handle:
            os.fsync(handle.fileno())
        output_published = False
        try:
            # 같은 디렉터리에 만든 임시 파일을 hard link로 게시하면, 목적지가
            # 늦게 생긴 경우에도 기존 파일을 덮어쓰지 않고 원자적으로 실패한다.
            os.link(temporary_output, output_path)
            output_published = True
            os.link(temporary_manifest, manifest_path)
        except Exception:
            if output_published:
                output_path.unlink(missing_ok=True)
            raise
    finally:
        temporary_output.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
    return manifest


__all__ = [
    "INTAKE_SCHEMA_VERSION",
    "OUTPUT_COLUMNS",
    "SOURCE_DATASET_ID",
    "SOURCE_URL",
    "prepare_ulsan_resolver_source",
    "verify_ulsan_resolver_source_manifest",
]
