from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

import chemiguard119.incident_source_intake as intake_module
from chemiguard119.incident_adaptation import load_incident_alias_records
from chemiguard119.incident_source_intake import (
    INTAKE_SCHEMA_VERSION,
    OFFICIAL_SOURCE_COLUMNS,
    OUTPUT_COLUMNS,
    prepare_ulsan_resolver_source,
    verify_ulsan_resolver_source_manifest,
)


def _write_source(path: Path, *, lowercase: bool = False) -> None:
    fields = list(OFFICIAL_SOURCE_COLUMNS)
    if lowercase:
        fields = [field.lower() for field in fields]
    values = {
        **{field: "" for field in OFFICIAL_SOURCE_COLUMNS},
        "SN": "1",
        "OCRN_YR": "2020",
        "CAS_NO": "64-17-5",
        "CHEM_SBSTN_KORN_NM": "  에탄올  ",
        "CHEM_SBSTN_ENG_NM": "Ethanol",
        "GNRL_KORN_NM": "에틸 알코올",
        "GNRL_ENG_NM": "Ethyl alcohol",
        "ROAD_NM": "내보내면 안 되는 도로명",
        "EMRG_RSCU_ZIP": "00000",
    }
    if lowercase:
        values = {key.lower(): value for key, value in values.items()}
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(values)


@pytest.mark.parametrize("lowercase", [False, True])
def test_projects_only_resolver_columns_and_records_provenance(
    tmp_path: Path, lowercase: bool
) -> None:
    source = tmp_path / "유해물질판단_2020_2015.csv"
    output = tmp_path / "resolver-source.csv"
    manifest = tmp_path / "resolver-source.manifest.json"
    _write_source(source, lowercase=lowercase)
    expected_raw_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()

    result = prepare_ulsan_resolver_source(source, output, manifest)

    assert result["schema_version"] == INTAKE_SCHEMA_VERSION
    assert result["source"]["row_count"] == 1
    assert result["source"]["sha256"] == expected_raw_sha256
    assert result["derived"]["columns"] == list(OUTPUT_COLUMNS)
    assert result["transformation"]["row_filtering_applied"] is False
    assert result["safety"]["git_commit_allowed"] is False
    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == list(OUTPUT_COLUMNS)
    assert rows[0]["발생연도"] == "2020"
    assert rows[0]["CAS번호"] == "64-17-5"
    assert rows[0]["화학물질명_한글"] == "  에탄올  "
    assert "내보내면 안 되는 도로명" not in output.read_text(encoding="utf-8-sig")
    assert json.loads(manifest.read_text(encoding="utf-8")) == result
    provenance = verify_ulsan_resolver_source_manifest(
        output,
        manifest,
        expected_row_count=1,
    )
    assert provenance["raw_source_sha256"] == expected_raw_sha256
    assert provenance["derived_sha256"] == result["derived"]["sha256"]


def test_rejects_tampered_derived_csv(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "output.csv"
    manifest = tmp_path / "manifest.json"
    _write_source(source)
    prepare_ulsan_resolver_source(source, output, manifest)
    output.write_text("변조됨", encoding="utf-8")

    with pytest.raises(ValueError, match="SHA-256이 다릅니다"):
        verify_ulsan_resolver_source_manifest(
            output,
            manifest,
            expected_row_count=1,
        )


def test_rejects_extra_column_even_with_matching_manifest_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "output.csv"
    manifest = tmp_path / "manifest.json"
    _write_source(source)
    payload = prepare_ulsan_resolver_source(source, output, manifest)
    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*OUTPUT_COLUMNS, "ROAD_NM"])
        writer.writeheader()
        writer.writerow({**rows[0], "ROAD_NM": "숨은 위치 열"})
    payload["derived"]["sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="정확히 같아야"):
        load_incident_alias_records(output, manifest)


def test_rejects_surplus_row_cell_with_six_column_header(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "output.csv"
    manifest = tmp_path / "manifest.json"
    _write_source(source)
    payload = prepare_ulsan_resolver_source(source, output, manifest)
    output.write_text(
        ",".join(OUTPUT_COLUMNS)
        + "\n2020,64-17-5,에탄올,Ethanol,에틸 알코올,Ethyl alcohol,숨은 위치 값\n",
        encoding="utf-8-sig",
    )
    payload["derived"]["sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="열 개수가 다른 행"):
        load_incident_alias_records(output, manifest)


def test_rejects_blank_record_in_derived_csv(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "output.csv"
    manifest = tmp_path / "manifest.json"
    _write_source(source)
    payload = prepare_ulsan_resolver_source(source, output, manifest)
    with output.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    payload["derived"]["sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="열 개수가 다른 행"):
        load_incident_alias_records(output, manifest)


def test_rejects_missing_required_source_column(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("OCRN_YR,CAS_NO\n2020,64-17-5\n", encoding="utf-8")

    with pytest.raises(ValueError, match="원본 컬럼이 부족"):
        prepare_ulsan_resolver_source(
            source,
            tmp_path / "output.csv",
            tmp_path / "manifest.json",
        )


def test_rejects_processed_six_column_csv_as_official_raw(tmp_path: Path) -> None:
    source = tmp_path / "processed.csv"
    with source.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(OUTPUT_COLUMNS))
        writer.writeheader()
        writer.writerow(
            {
                "발생연도": "2020",
                "CAS번호": "64-17-5",
                "화학물질명_한글": "에탄올",
                "화학물질명_영문": "Ethanol",
                "일반명_한글": "에틸 알코올",
                "일반명_영문": "Ethyl alcohol",
            }
        )

    with pytest.raises(ValueError, match="공식 원본 컬럼이 부족"):
        prepare_ulsan_resolver_source(
            source,
            tmp_path / "output.csv",
            tmp_path / "manifest.json",
        )


@pytest.mark.parametrize(
    "row",
    [
        "",
        "2020,64-17-5,에탄올,Ethanol,에틸 알코올",
        "2020,64-17-5,에탄올,Ethanol,에틸 알코올,Ethyl alcohol,숨은 위치 값",
    ],
)
def test_rejects_raw_row_width_mismatch_before_projection(
    tmp_path: Path,
    row: str,
) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "output.csv"
    manifest = tmp_path / "manifest.json"
    source.write_text(
        "OCRN_YR,CAS_NO,CHEM_SBSTN_KORN_NM,CHEM_SBSTN_ENG_NM,"
        "GNRL_KORN_NM,GNRL_ENG_NM\n" + row + "\n",
        encoding="utf-8-sig",
    )

    with pytest.raises(ValueError, match="열 개수가 header와 다릅니다"):
        prepare_ulsan_resolver_source(source, output, manifest)

    assert not output.exists()
    assert not manifest.exists()


def test_refuses_to_overwrite_existing_outputs(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "output.csv"
    manifest = tmp_path / "manifest.json"
    _write_source(source)
    output.write_text("기존 파일", encoding="utf-8")

    with pytest.raises(FileExistsError, match="덮어쓰지 않습니다"):
        prepare_ulsan_resolver_source(source, output, manifest)

    assert output.read_text(encoding="utf-8") == "기존 파일"


def test_does_not_publish_csv_when_manifest_staging_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "output.csv"
    manifest = tmp_path / "manifest.json"
    _write_source(source)

    def fail_manifest(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic manifest failure")

    monkeypatch.setattr(intake_module, "write_json", fail_manifest)
    with pytest.raises(OSError, match="synthetic manifest failure"):
        prepare_ulsan_resolver_source(source, output, manifest)

    assert not output.exists()
    assert not manifest.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_rolls_back_csv_when_manifest_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.csv"
    output = tmp_path / "output.csv"
    manifest = tmp_path / "manifest.json"
    _write_source(source)
    real_link = intake_module.os.link
    call_count = 0

    def fail_second_link(source_path: object, destination_path: object) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("synthetic manifest publication failure")
        real_link(source_path, destination_path)

    monkeypatch.setattr(intake_module.os, "link", fail_second_link)
    with pytest.raises(OSError, match="synthetic manifest publication failure"):
        prepare_ulsan_resolver_source(source, output, manifest)

    assert not output.exists()
    assert not manifest.exists()
    assert not list(tmp_path.glob(".*.tmp"))
