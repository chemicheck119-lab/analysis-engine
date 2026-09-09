from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from chemiguard119.asr_whitespace_review import (
    SCHEMA_VERSION,
    build_asr_whitespace_review_queue,
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
            "사고업체명": "검수 파일에 넣으면 안 되는 회사",
            "주소": "검수 파일에 넣으면 안 되는 주소",
            "시도": "광주",
            "시군구": "북구",
            "사고내용": "차아 염소산 나트륨 탱크에서 누출",
            "사고원인": "설비 결함",
            "사고유형": "누출",
            "제1사고물질": "차아염소산나트륨",
            "제2사고물질": "",
            "제3사고물질": "",
        },
        {
            "연번": "2021-001",
            "사고일자": "2021-06-01",
            "소방접수시간": "11:00:00",
            "사고업체명": "잠금 회사",
            "주소": "잠금 주소",
            "시도": "서울",
            "시군구": "중구",
            "사고내용": "차아 염소산 나트륨 탱크에서 누출",
            "사고원인": "설비 결함",
            "사고유형": "누출",
            "제1사고물질": "차아염소산나트륨",
            "제2사고물질": "",
            "제3사고물질": "",
        },
    ]
    with path.open("w", encoding="cp949", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _write_resolver(path: Path) -> None:
    fit_resolver_rows(
        [
            {
                "cas_number": "7681-52-9",
                "alias_text": "차아염소산나트륨",
                "normalized_text": "차아염소산나트륨",
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


def test_builds_unlabeled_private_review_queue_from_development_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "official.csv"
    resolver = tmp_path / "resolver.joblib"
    report_path = tmp_path / "review.json"
    _write_source(source)
    _write_resolver(resolver)

    report = build_asr_whitespace_review_queue(
        source,
        resolver,
        report_path=report_path,
        expected_source_sha256=None,
    )
    serialized = json.dumps(report, ensure_ascii=False)

    assert report["schema_version"] == SCHEMA_VERSION
    assert set(report["source_code_sha256"]) == {
        "review_queue",
        "incident_parser",
        "resolver",
    }
    assert all(len(value) == 64 for value in report["source_code_sha256"].values())
    assert report["development_case_count"] == 1
    assert report["changed_case_count"] == 1
    assert report["official_label_metric_effect_case_counts"]["exact_improved"] == 1
    assert report["review_status"] == "NOT_STARTED"
    assert report["reviewed_case_count"] == 0
    assert report["machine_generated_human_labels"] is False
    assert report["runtime_default_change_allowed"] is False
    assert report["git_commit_allowed"] is False
    assert report["candidate_unsafe_rule_eligible_mention_count"] == 0
    assert report["entries"][0]["year"] == 2020
    assert report["entries"][0]["human_review"] == {
        "status": "NOT_REVIEWED",
        "decision": None,
        "reviewer": None,
        "notes": None,
    }
    assert (
        report["entries"][0]["candidate_only_mentions"][0]["surface_text"]
        == "차아 염소산 나트륨"
    )
    assert "검수 파일에 넣으면 안 되는 회사" not in serialized
    assert "검수 파일에 넣으면 안 되는 주소" not in serialized
    assert "잠금 회사" not in serialized
    assert json.loads(report_path.read_text(encoding="utf-8")) == report


def test_review_queue_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "official.csv"
    resolver = tmp_path / "resolver.joblib"
    _write_source(source)
    _write_resolver(resolver)

    first = build_asr_whitespace_review_queue(
        source,
        resolver,
        expected_source_sha256=None,
    )
    second = build_asr_whitespace_review_queue(
        source,
        resolver,
        expected_source_sha256=None,
    )

    assert first == second


def test_rejects_report_path_inside_git_repository(tmp_path: Path) -> None:
    source = tmp_path / "official.csv"
    resolver = tmp_path / "resolver.joblib"
    _write_source(source)
    _write_resolver(resolver)
    repository_report = Path(__file__).resolve().parents[1] / "review.private.json"

    with pytest.raises(ValueError, match="Git 저장소 내부"):
        build_asr_whitespace_review_queue(
            source,
            resolver,
            report_path=repository_report,
            expected_source_sha256=None,
        )
