from __future__ import annotations

import csv
from pathlib import Path

import pytest

from chemiguard119.incident_adaptation import (
    evaluate_temporal_adaptation,
    load_incident_alias_records,
    train_incident_adapted_resolver,
)
from chemiguard119.resolver import (
    INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION,
    fit_resolver_rows,
    load_resolver,
    resolve_substance,
)


HEADERS = [
    "발생연도",
    "CAS번호",
    "화학물질명_한글",
    "화학물질명_영문",
    "일반명_한글",
    "일반명_영문",
]


def _write_incidents(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)


def _row(year: int, cas: str, name: str, *, common: str = "") -> dict[str, str]:
    return {
        "발생연도": str(year),
        "CAS번호": cas,
        "화학물질명_한글": name,
        "화학물질명_영문": "",
        "일반명_한글": common,
        "일반명_영문": "",
    }


def _base_model(path: Path) -> None:
    rows = [
        {
            "cas_number": "7647-01-0",
            "alias_text": "Hydrogen chloride",
            "normalized_text": "hydrogenchloride",
            "alias_type": "canonical_en",
            "source": "fixture",
            "verification_status": "SOURCE_EXACT_VALID_CAS",
            "catalog_scope": "TEST",
            "has_kosha_detail": 1,
            "resolver_candidate_only": 0,
        },
        {
            "cas_number": "1310-73-2",
            "alias_text": "Sodium hydroxide",
            "normalized_text": "sodiumhydroxide",
            "alias_type": "canonical_en",
            "source": "fixture",
            "verification_status": "SOURCE_EXACT_VALID_CAS",
            "catalog_scope": "TEST",
            "has_kosha_detail": 1,
            "resolver_candidate_only": 0,
        },
    ]
    fit_resolver_rows(rows, path)


def test_source_audit_filters_invalid_cas_and_ambiguous_surface(tmp_path: Path) -> None:
    source = tmp_path / "incidents.csv"
    _write_incidents(
        source,
        [
            _row(2018, "7647-01-0", "염화수소", common="공통명"),
            _row(2018, "1310-73-2", "수산화나트륨", common="공통명"),
            _row(2020, "7647-01-0, 1310-73-2", "복합물질"),
        ],
    )

    result = load_incident_alias_records(source)

    assert result["audit"]["source_row_count"] == 3
    assert result["audit"]["invalid_or_composite_cas_row_count"] == 1
    assert result["audit"]["ambiguous_surface_count"] == 1
    assert all(row["alias_text"] != "공통명" for row in result["records"])


def test_incident_adaptation_adds_only_pre_cutoff_aliases(tmp_path: Path) -> None:
    base_model = tmp_path / "base.joblib"
    adapted_model = tmp_path / "adapted.joblib"
    source = tmp_path / "incidents.csv"
    _base_model(base_model)
    _write_incidents(
        source,
        [
            _row(2018, "7647-01-0", "염산"),
            _row(2020, "1310-73-2", "가성소다"),
        ],
    )

    report = train_incident_adapted_resolver(
        base_model,
        source,
        adapted_model,
        training_year_max=2019,
    )
    artifact = load_resolver(adapted_model)

    assert report["schema_version"] == INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION
    assert report["added_alias_count"] == 1
    assert report["excluded_after_cutoff_count"] == 1
    assert artifact["training_metadata"]["fine_tuned"] is True
    resolution = resolve_substance("염산", artifact)
    assert resolution["candidates"][0]["cas_number"] == "7647-01-0"
    assert resolution["input_class"] == "REPORTED_ALIAS"
    assert resolve_substance("가성소다", artifact)["status"] != (
        "EXACT_ALIAS_CANDIDATE"
    )


def test_temporal_evaluation_requires_validation_and_locked_years(
    tmp_path: Path,
) -> None:
    model = tmp_path / "resolver.joblib"
    source = tmp_path / "incidents.csv"
    _base_model(model)
    _write_incidents(source, [_row(2018, "7647-01-0", "염산")])

    with pytest.raises(RuntimeError, match="검증 연도와 잠금 테스트 연도"):
        evaluate_temporal_adaptation(model, model, model, source)
