from __future__ import annotations

import csv
from pathlib import Path

import pytest

from chemiguard119.audit import (
    REQUIRED_SOURCE_FILENAMES,
    audit_dataset,
    final_csv_paths,
    required_csv_paths,
)
from chemiguard119.preprocessing import SOURCE_FILES


def _write_csv(path: Path, fieldnames: list[str], row: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(row)


def _write_minimal_bundle(data_dir: Path) -> None:
    data_dir.mkdir()
    for filename in REQUIRED_SOURCE_FILENAMES:
        _write_csv(data_dir / filename, ["id"], {"id": "1"})

    _write_csv(
        data_dir / SOURCE_FILES["kosha"],
        ["CAS번호", "상세내용"],
        {"CAS번호": "64-17-5", "상세내용": "응급조치"},
    )
    _write_csv(
        data_dir / SOURCE_FILES["compatibility"],
        ["호환성_판정", "발생가스"],
        {"호환성_판정": "1", "발생가스": ""},
    )
    _write_csv(
        data_dir / SOURCE_FILES["ulsan_substance"],
        ["CAS번호"],
        {"CAS번호": "64-17-5"},
    )
    _write_csv(
        data_dir / SOURCE_FILES["facility_candidate"],
        ["정확CAS모델링사용여부", "현재보유확정여부", "RuleEngine_자동판정가능여부"],
        {
            "정확CAS모델링사용여부": "Y",
            "현재보유확정여부": "N",
            "RuleEngine_자동판정가능여부": "N",
        },
    )


def test_audit_uses_only_preprocessing_source_contract(tmp_path: Path) -> None:
    data_dir = tmp_path / "raw"
    _write_minimal_bundle(data_dir)
    _write_csv(data_dir / "99_사용하지않는파일.csv", ["id"], {"id": "extra"})

    assert REQUIRED_SOURCE_FILENAMES == tuple(SOURCE_FILES.values())
    assert required_csv_paths(data_dir) == [
        data_dir / filename for filename in SOURCE_FILES.values()
    ]
    assert len(final_csv_paths(data_dir)) == 8

    report = audit_dataset(data_dir)
    assert report["file_count"] == 8
    assert {item["file"] for item in report["files"]} == set(SOURCE_FILES.values())
    assert "incident_labels" not in report["semantic_checks"]


def test_audit_reports_exact_missing_required_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "raw"
    _write_minimal_bundle(data_dir)
    missing_name = SOURCE_FILES["cameo_mapping"]
    (data_dir / missing_name).unlink()

    with pytest.raises(FileNotFoundError, match=missing_name):
        audit_dataset(data_dir)
