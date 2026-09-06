"""울산 사고–CAS 원본을 Resolver 평가용 최소 컬럼으로 축약한다.

공식 원본에는 관할서·도로명·우편번호 같은 위치 관련 열이 함께 들어 있다.
Resolver source adaptation에는 이 열이 필요하지 않으므로, 이 모듈은 학습과
평가에 필요한 연도·CAS·물질명 여섯 열만 private 경로로 투영한다. 원본 행을
필터링하거나 정답을 수정하지 않으며, 원본과 파생 파일의 SHA-256을 manifest에
기록한다.
"""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chemiguard119.incident_adaptation import SOURCE_DATASET_ID, SOURCE_URL
from chemiguard119.utils import sha256_file, write_json


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


def _read_source(path: Path) -> tuple[str, list[str], list[dict[str, str]]]:
    last_error: UnicodeDecodeError | None = None
    for encoding in SUPPORTED_ENCODINGS:
        try:
            with path.open(encoding=encoding, newline="") as handle:
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

    encoding, fieldnames, source_rows = _read_source(source_path)
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
                "sha256": sha256_file(source_path),
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
    "prepare_ulsan_resolver_source",
]
