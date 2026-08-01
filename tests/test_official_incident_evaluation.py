from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from chemiguard119.official_incident_evaluation import (
    evaluate_official_incidents,
    load_official_incidents,
)
from chemiguard119.resolver import fit_resolver_rows


FIELDS = [
    "연번",
    "사고일자",
    "소방접수시간",
    "사고업체명",
    "주소",
    "시도",
    "시군구",
    "사고내용",
    "사고원인",
    "사고유형",
    "제1사고물질",
    "제2사고물질",
    "제3사고물질",
]


def _write_source(path: Path) -> None:
    rows = [
        {
            "연번": "2020-001",
            "사고일자": "2020-06-01",
            "소방접수시간": "10:00:00",
            "사고업체명": "개발용 회사",
            "주소": "개발용 상세주소",
            "시도": "울산",
            "시군구": "남구",
            "사고내용": "염산 저장탱크에서 누출",
            "사고원인": "시설 결함",
            "사고유형": "누출",
            "제1사고물질": "염산",
            "제2사고물질": "",
            "제3사고물질": "",
        },
        {
            "연번": "2021-001",
            "사고일자": "2021-07-01",
            "소방접수시간": "11:00:00",
            "사고업체명": "잠금 회사",
            "주소": "잠금 상세주소",
            "시도": "서울",
            "시군구": "중구",
            "사고내용": "염산 배관에서 누출",
            "사고원인": "시설 결함",
            "사고유형": "누출",
            "제1사고물질": "염산",
            "제2사고물질": "",
            "제3사고물질": "",
        },
    ]
    with path.open("w", encoding="cp949", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _resolver(path: Path) -> None:
    fit_resolver_rows(
        [
            {
                "cas_number": "7647-01-0",
                "alias_text": "염산",
                "normalized_text": "염산",
                "alias_type": "kosha_name",
                "source": "fixture",
                "verification_status": "SOURCE_EXACT",
                "catalog_scope": "TEST",
                "has_kosha_detail": 1,
                "resolver_candidate_only": 0,
            }
        ],
        path,
    )


def test_official_source_loader_audits_without_copying_raw_fields(
    tmp_path: Path,
) -> None:
    source = tmp_path / "official.csv"
    _write_source(source)

    loaded = load_official_incidents(source, expected_sha256=None)

    assert loaded["audit"]["source_row_count"] == 2
    assert loaded["audit"]["year_counts"] == {"2020": 1, "2021": 1}
    assert loaded["audit"]["contains_company_or_address_fields"] is True
    assert (
        "사고업체명" in loaded["audit"]["raw_or_sensitive_fields_excluded_from_report"]
    )


def test_locked_external_evaluation_hides_raw_text_and_diagnostics(
    tmp_path: Path,
) -> None:
    source = tmp_path / "official.csv"
    model = tmp_path / "resolver.joblib"
    _write_source(source)
    _resolver(model)

    report = evaluate_official_incidents(
        source,
        model,
        split="locked_test",
        expected_source_sha256=None,
    )
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["case_count"] == 1
    assert report["incident_type"]["recall"] == 1.0
    assert report["substance_mention"]["exact_surface_recall"] == 1.0
    assert report["safety"]["unsafe_rule_eligible_mention_count"] == 0
    assert "development_diagnostics" not in report
    assert "잠금 회사" not in serialized
    assert "잠금 상세주소" not in serialized
    assert "염산 배관에서 누출" not in serialized


def test_development_evaluation_can_expose_only_missed_label_counts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "official.csv"
    model = tmp_path / "resolver.joblib"
    _write_source(source)
    _resolver(model)

    report = evaluate_official_incidents(
        source,
        model,
        split="development",
        expected_source_sha256=None,
    )

    assert report["case_count"] == 1
    assert report["development_diagnostics"]["top_missed_official_labels"] == []


def test_official_source_loader_rejects_unpinned_content(tmp_path: Path) -> None:
    source = tmp_path / "official.csv"
    _write_source(source)

    with pytest.raises(RuntimeError, match="checksum"):
        load_official_incidents(source)
