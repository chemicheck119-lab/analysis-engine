from __future__ import annotations

import csv
from pathlib import Path

import pytest

from chemiguard119.incident_adaptation import (
    SOURCE_ONLY_CATALOG_SCOPE,
    evaluate_temporal_adaptation,
    load_incident_alias_records,
    train_incident_adapted_resolver,
)
from chemiguard119.incident_source_intake import prepare_ulsan_resolver_source
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


def _write_official_incidents(path: Path, rows: list[dict[str, str]]) -> None:
    field_map = {
        "발생연도": "ocrn_yr",
        "CAS번호": "cas_no",
        "화학물질명_한글": "chem_sbstn_korn_nm",
        "화학물질명_영문": "chem_sbstn_eng_nm",
        "일반명_한글": "gnrl_korn_nm",
        "일반명_영문": "gnrl_eng_nm",
    }
    fields = [*field_map.values(), "road_nm"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **{raw: row[column] for column, raw in field_map.items()},
                    "road_nm": "비공개 경로에만 남을 위치 열",
                }
            )


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
    assert sum(row["alias_text"] == "공통명" for row in result["training_records"]) == 2


def test_training_binds_verified_raw_source_provenance(tmp_path: Path) -> None:
    base_model = tmp_path / "base.joblib"
    raw_source = tmp_path / "유해물질판단_2020_2015.csv"
    derived_source = tmp_path / "resolver-source.csv"
    manifest = tmp_path / "resolver-source.manifest.json"
    adapted_model = tmp_path / "adapted.joblib"
    _base_model(base_model)
    _write_official_incidents(raw_source, [_row(2018, "7647-01-0", "염산")])
    intake = prepare_ulsan_resolver_source(
        raw_source,
        derived_source,
        manifest,
    )

    report = train_incident_adapted_resolver(
        base_model,
        derived_source,
        adapted_model,
        training_year_max=2019,
        source_manifest_path=manifest,
    )

    provenance = report["source_audit"]["intake_provenance"]
    assert provenance["raw_source_sha256"] == intake["source"]["sha256"]
    assert provenance["derived_sha256"] == intake["derived"]["sha256"]
    assert len(provenance["manifest_sha256"]) == 64
    artifact = load_resolver(adapted_model)
    assert (
        artifact["training_metadata"]["source_audit"]["intake_provenance"]
        == provenance
    )


def test_training_ambiguity_filter_does_not_look_at_future_year(tmp_path: Path) -> None:
    base_model = tmp_path / "base.joblib"
    adapted_model = tmp_path / "adapted.joblib"
    source = tmp_path / "incidents.csv"
    _base_model(base_model)
    _write_incidents(
        source,
        [
            _row(2018, "7647-01-0", "염화수소", common="과거공통명"),
            _row(2019, "7647-01-0", "염산"),
            _row(2020, "1310-73-2", "수산화나트륨", common="과거공통명"),
        ],
    )

    report = train_incident_adapted_resolver(
        base_model,
        source,
        adapted_model,
        training_year_max=2018,
    )
    result = resolve_substance("과거공통명", load_resolver(adapted_model))

    assert report["ambiguity_filter_uses_future_records"] is False
    assert report["excluded_ambiguous_record_count"] == 0
    assert result["candidates"][0]["cas_number"] == "7647-01-0"


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


def test_incident_source_can_expand_catalog_without_enabling_fuzzy_guess(
    tmp_path: Path,
) -> None:
    base_model = tmp_path / "base.joblib"
    adapted_model = tmp_path / "adapted.joblib"
    source = tmp_path / "incidents.csv"
    _base_model(base_model)
    _write_incidents(source, [_row(2018, "67-64-1", "아세톤")])

    report = train_incident_adapted_resolver(
        base_model,
        source,
        adapted_model,
        training_year_max=2019,
    )
    artifact = load_resolver(adapted_model)

    exact = resolve_substance("아세톤", artifact)
    fuzzy = resolve_substance("아세톤느", artifact)

    assert report["added_source_only_cas_count"] == 1
    assert report["added_source_only_alias_count"] == 1
    assert report["excluded_cas_not_in_catalog_count"] == 0
    assert exact["status"] == "EXACT_ALIAS_CANDIDATE"
    assert exact["input_class"] == "REPORTED_ALIAS"
    assert exact["candidates"][0]["cas_number"] == "67-64-1"
    assert exact["candidates"][0]["catalog_scope"] == SOURCE_ONLY_CATALOG_SCOPE
    assert exact["candidates"][0]["catalog_candidate_only"] is True
    assert exact["candidates"][0]["rule_eligible"] is False
    assert "67-64-1" not in {
        candidate["cas_number"] for candidate in fuzzy["candidates"]
    }

    # 이미 확장된 artifact를 다시 입력으로 사용해도 source-only CAS가 일반
    # 카탈로그 물질로 승격되면 안 된다.
    second_source = tmp_path / "second-incidents.csv"
    second_model = tmp_path / "second-adapted.joblib"
    _write_incidents(second_source, [_row(2019, "67-64-1", "프로판온")])
    train_incident_adapted_resolver(
        adapted_model,
        second_source,
        second_model,
        training_year_max=2019,
    )
    repeated = resolve_substance("프로판온", load_resolver(second_model))
    assert repeated["candidates"][0]["catalog_candidate_only"] is True
    assert repeated["candidates"][0]["rule_eligible"] is False


def test_temporal_evaluation_requires_validation_and_locked_years(
    tmp_path: Path,
) -> None:
    model = tmp_path / "resolver.joblib"
    source = tmp_path / "incidents.csv"
    _base_model(model)
    _write_incidents(source, [_row(2018, "7647-01-0", "염산")])

    with pytest.raises(RuntimeError, match="검증 연도와 잠금 테스트 연도"):
        evaluate_temporal_adaptation(model, model, model, source)
