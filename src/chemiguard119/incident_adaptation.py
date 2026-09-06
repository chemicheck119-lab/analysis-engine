"""소방 사고–CAS 공개 기록으로 Resolver를 source-adaptive fine-tuning 한다.

이 모듈은 화학 위험이나 대응 절차를 학습하지 않는다. 과거 사고 기록에 CAS와
함께 공개된 물질 표현만 기존 문자 TF-IDF 별칭 공간에 추가하고, 미래 연도
잠금셋에서 후보 검색 성능을 비교한다.
"""

from __future__ import annotations

import csv
import hashlib
import io
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from chemiguard119.incident_source_intake import (
    OUTPUT_COLUMNS,
    SOURCE_DATASET_ID,
    SOURCE_URL,
    verify_ulsan_resolver_source_manifest,
)
from chemiguard119.resolver import (
    INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION,
    evaluate_resolver,
    evaluate_resolver_hint_safety,
    fit_resolver_rows,
    load_resolver,
    resolve_substance,
)
from chemiguard119.utils import (
    compact_text,
    normalize_cas,
    sha256_file,
    valid_cas_checksum,
    write_json,
)
from chemiguard119.paths import EVALUATION_DIR


TRAINING_POLICY_VERSION = "incident-alias-temporal-adaptation-v2-source-catalog"
DEFAULT_TRAINING_YEAR_MAX = 2019
DEFAULT_LOCKED_TEST_YEAR = 2020
SOURCE_ONLY_CATALOG_SCOPE = "NFA_BIGDATA119_INCIDENT_CATALOG_CANDIDATE"

SURFACE_FIELDS = {
    "화학물질명_한글": "fire_incident_name_ko",
    "화학물질명_영문": "fire_incident_name_en",
    "일반명_한글": "fire_incident_common_name_ko",
    "일반명_영문": "fire_incident_common_name_en",
}
REQUIRED_COLUMNS = {"발생연도", "CAS번호", *SURFACE_FIELDS}


def _surface_values(value: str) -> list[str]:
    """하나의 CAS에 연결된 세미콜론 구분 동의어만 안전하게 분리한다."""

    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def load_incident_alias_records(
    path: Path,
    source_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """공개 사고 CSV를 검증하고 비식별 물질명–CAS 학습 레코드로 축약한다."""

    source_bytes = path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    with io.StringIO(source_bytes.decode("utf-8-sig"), newline="") as handle:
        reader = csv.reader(handle)
        actual_columns = next(reader, [])
        if actual_columns != list(OUTPUT_COLUMNS):
            missing = sorted(REQUIRED_COLUMNS - set(actual_columns))
            unexpected = sorted(set(actual_columns) - REQUIRED_COLUMNS)
            raise RuntimeError(
                "사고–CAS 파생 CSV는 manifest에 고정된 6개 컬럼과 순서가 "
                f"정확히 같아야 합니다: missing={missing}, unexpected={unexpected}"
            )
        source_rows: list[dict[str, str]] = []
        for row_number, values in enumerate(reader, start=2):
            if len(values) != len(OUTPUT_COLUMNS):
                raise RuntimeError(
                    "사고–CAS 파생 CSV에 header와 열 개수가 다른 행이 있습니다: "
                    f"{row_number}행"
                )
            source_rows.append(dict(zip(OUTPUT_COLUMNS, values, strict=True)))

    invalid_year = 0
    invalid_cas = 0
    extracted: list[dict[str, Any]] = []
    for row in source_rows:
        try:
            year = int(str(row.get("발생연도") or ""))
        except ValueError:
            invalid_year += 1
            continue
        cas_number = normalize_cas(str(row.get("CAS번호") or ""))
        if not valid_cas_checksum(cas_number):
            invalid_cas += 1
            continue
        for field, alias_type in SURFACE_FIELDS.items():
            for surface in _surface_values(str(row.get(field) or "")):
                normalized = compact_text(surface)
                if len(normalized) < 2:
                    continue
                extracted.append(
                    {
                        "year": year,
                        "cas_number": cas_number,
                        "alias_text": surface,
                        "normalized_text": normalized,
                        "alias_type": alias_type,
                    }
                )

    # 같은 정규화 표현이 서로 다른 CAS로 공개된 경우 exact alias 학습에서 제외한다.
    surface_cas: dict[str, set[str]] = defaultdict(set)
    for row in extracted:
        surface_cas[row["normalized_text"]].add(row["cas_number"])
    ambiguous_surfaces = {
        surface for surface, cas_values in surface_cas.items() if len(cas_values) > 1
    }

    deduplicated_all: dict[tuple[int, str, str], dict[str, Any]] = {}
    for row in extracted:
        key = (row["year"], row["cas_number"], row["normalized_text"])
        deduplicated_all.setdefault(key, row)
    training_records = sorted(
        deduplicated_all.values(),
        key=lambda row: (row["year"], row["cas_number"], row["normalized_text"]),
    )
    # 하나의 정답 CAS를 가질 수 없는 표현은 평가 사례에서는 제외한다. 학습은
    # 미래 연도를 보지 않도록 아래 전체 레코드에서 cutoff 시점의 모호성만 다시
    # 계산한다.
    records = sorted(
        (
            row
            for row in training_records
            if row["normalized_text"] not in ambiguous_surfaces
        ),
        key=lambda row: (row["year"], row["cas_number"], row["normalized_text"]),
    )
    audit: dict[str, Any] = {
        "source_dataset_id": SOURCE_DATASET_ID,
        "source_url": SOURCE_URL,
        "source_file": path.name,
        "source_sha256": source_sha256,
        "source_row_count": len(source_rows),
        "valid_alias_record_count": len(records),
        "pre_ambiguity_filter_record_count": len(training_records),
        "invalid_year_row_count": invalid_year,
        "invalid_or_composite_cas_row_count": invalid_cas,
        "ambiguous_surface_count": len(ambiguous_surfaces),
        "year_counts": dict(Counter(row["year"] for row in records)),
        "contains_personal_data": False,
        "released_fields": ["발생연도", "CAS번호", *SURFACE_FIELDS],
    }
    if source_manifest_path is not None:
        audit["intake_provenance"] = verify_ulsan_resolver_source_manifest(
            path,
            source_manifest_path,
            expected_row_count=len(source_rows),
            observed_derived_sha256=source_sha256,
        )
    return {
        "records": records,
        "training_records": training_records,
        "audit": audit,
    }


def _base_catalog(artifact: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_cas: dict[str, dict[str, Any]] = {}
    for row in artifact.get("rows", []):
        cas_number = str(row.get("cas_number") or "")
        current = by_cas.setdefault(
            cas_number,
            {
                "has_kosha_detail": False,
                "catalog_scope": "LEGACY_OR_TEST_REGISTRY",
                "resolver_candidate_only": False,
            },
        )
        current["has_kosha_detail"] = bool(current["has_kosha_detail"]) or bool(
            row.get("has_kosha_detail")
        )
        if row.get("catalog_scope"):
            current["catalog_scope"] = str(row["catalog_scope"])
        current["resolver_candidate_only"] = bool(
            current["resolver_candidate_only"]
        ) or bool(row.get("resolver_candidate_only"))
    return by_cas


def _training_alias_rows(
    base_artifact: dict[str, Any],
    records: Iterable[dict[str, Any]],
    *,
    training_year_max: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base_rows = [dict(row) for row in base_artifact.get("rows", [])]
    catalog = _base_catalog(base_artifact)
    existing = {
        (
            str(row.get("cas_number") or ""),
            compact_text(str(row.get("alias_text") or "")),
        )
        for row in base_rows
    }
    additions: list[dict[str, Any]] = []
    source_only_alias_count = 0
    source_only_cas: set[str] = set()
    excluded_after_cutoff = 0
    cutoff_records = [
        record for record in records if int(record["year"]) <= training_year_max
    ]
    cutoff_surface_cas: dict[str, set[str]] = defaultdict(set)
    for record in cutoff_records:
        cutoff_surface_cas[str(record["normalized_text"])].add(
            str(record["cas_number"])
        )
    ambiguous_at_cutoff = {
        surface
        for surface, cas_values in cutoff_surface_cas.items()
        if len(cas_values) > 1
    }
    excluded_ambiguous_records = 0
    for record in records:
        if int(record["year"]) > training_year_max:
            excluded_after_cutoff += 1
            continue
        if str(record["normalized_text"]) in ambiguous_at_cutoff:
            excluded_ambiguous_records += 1
            continue
        cas_number = str(record["cas_number"])
        key = (cas_number, str(record["normalized_text"]))
        if key in existing:
            continue
        existing.add(key)
        source_only = cas_number not in catalog or (
            catalog[cas_number].get("catalog_scope") == SOURCE_ONLY_CATALOG_SCOPE
        )
        if source_only:
            source_only_alias_count += 1
            source_only_cas.add(cas_number)
        scope = catalog.get(
            cas_number,
            {
                "has_kosha_detail": False,
                "catalog_scope": SOURCE_ONLY_CATALOG_SCOPE,
                "resolver_candidate_only": True,
            },
        )
        additions.append(
            {
                "cas_number": cas_number,
                "alias_text": str(record["alias_text"]),
                "normalized_text": str(record["normalized_text"]),
                "alias_type": str(record["alias_type"]),
                "source": "07_울산소방_화학사고별_유해물질판단.csv",
                "verification_status": "SOURCE_EXACT_VALID_CAS",
                "catalog_scope": str(scope["catalog_scope"]),
                "has_kosha_detail": int(bool(scope["has_kosha_detail"])),
                # 원천에는 CAS-물질명 관계가 있지만 현재 재고나 상세 MSDS까지
                # 보장하지 않는다. 따라서 항상 현장 확인 후보로만 반환한다.
                "resolver_candidate_only": int(
                    source_only or bool(scope.get("resolver_candidate_only"))
                ),
                # source-only CAS는 정확히 다시 등장한 표현만 인식한다. 유사도
                # 검색에서 제외해 철자가 비슷한 다른 물질을 오답 후보로 올리지 않는다.
                "fuzzy_search_eligible": not source_only,
            }
        )
    return base_rows + additions, {
        "base_alias_count": len(base_rows),
        "added_alias_count": len(additions),
        "added_source_only_alias_count": source_only_alias_count,
        "added_source_only_cas_count": len(source_only_cas),
        "excluded_cas_not_in_catalog_count": 0,
        "source_only_exact_match_required": True,
        "excluded_ambiguous_record_count": excluded_ambiguous_records,
        "ambiguity_filter_uses_future_records": False,
        "excluded_after_cutoff_count": excluded_after_cutoff,
        "training_year_max": training_year_max,
        "training_source": SOURCE_DATASET_ID,
        "training_policy_version": TRAINING_POLICY_VERSION,
        "fine_tuned": True,
        "task": "substance_alias_candidate_retrieval_only",
        "risk_or_response_training_included": False,
    }


def train_incident_adapted_resolver(
    base_model_path: Path,
    incident_csv_path: Path,
    output_model_path: Path,
    *,
    training_year_max: int = DEFAULT_TRAINING_YEAR_MAX,
    source_manifest_path: Path | None = None,
    source_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_artifact = load_resolver(base_model_path)
    source = (
        source_snapshot
        if source_snapshot is not None
        else load_incident_alias_records(incident_csv_path, source_manifest_path)
    )
    rows, metadata = _training_alias_rows(
        base_artifact,
        source["training_records"],
        training_year_max=training_year_max,
    )
    trained = fit_resolver_rows(
        rows,
        output_model_path,
        schema_version=INCIDENT_ADAPTED_MODEL_SCHEMA_VERSION,
        training_metadata={**metadata, "source_audit": source["audit"]},
    )
    return {
        **trained,
        **metadata,
        "source_audit": source["audit"],
        "artifact_sha256": sha256_file(output_model_path),
    }


def _cases_for_year(
    records: Iterable[dict[str, Any]],
    year: int,
) -> list[dict[str, Any]]:
    cases: dict[tuple[str, str], dict[str, Any]] = {}
    for row in records:
        if int(row["year"]) != year:
            continue
        key = (str(row["normalized_text"]), str(row["cas_number"]))
        cases.setdefault(
            key,
            {
                "query": str(row["alias_text"]),
                "normalized_query": str(row["normalized_text"]),
                "expected_cas": str(row["cas_number"]),
                "year": year,
            },
        )
    return sorted(cases.values(), key=lambda row: (row["expected_cas"], row["query"]))


def _evaluate_cases(
    artifact: dict[str, Any],
    cases: list[dict[str, Any]],
    *,
    training_records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    training_pairs = {
        (str(row["normalized_text"]), str(row["cas_number"]))
        for row in training_records
    }
    training_cas = {cas for _surface, cas in training_pairs}
    artifact_cas = {
        str(row.get("cas_number") or "") for row in artifact.get("rows", [])
    }
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in cases:
        started = time.perf_counter()
        result = resolve_substance(str(case["query"]), artifact, top_k=3)
        latencies.append((time.perf_counter() - started) * 1_000)
        ranking = [str(item["cas_number"]) for item in result.get("candidates", [])]
        expected = str(case["expected_cas"])
        rank = ranking.index(expected) + 1 if expected in ranking else None
        rows.append(
            {
                **case,
                "ranking": ranking,
                "rank": rank,
                "status": str(result.get("status") or ""),
                "surface_seen_in_training": (str(case["normalized_query"]), expected)
                in training_pairs,
                "cas_seen_in_training": expected in training_cas,
                "cas_present_in_artifact": expected in artifact_cas,
                "wrong_unique_resolution": bool(
                    len(ranking) == 1
                    and rank != 1
                    and result.get("status")
                    in {"EXACT_IDENTIFIER_MATCH", "EXACT_ALIAS_CANDIDATE"}
                ),
            }
        )

    def metrics(selected: list[dict[str, Any]]) -> dict[str, Any]:
        if not selected:
            return {
                "case_count": 0,
                "top1_accuracy": None,
                "top3_recall": None,
                "mrr": None,
            }
        count = len(selected)
        return {
            "case_count": count,
            "top1_accuracy": round(
                sum(row["rank"] == 1 for row in selected) / count, 6
            ),
            "top3_recall": round(
                sum(row["rank"] is not None for row in selected) / count, 6
            ),
            "mrr": round(
                sum(1.0 / row["rank"] if row["rank"] else 0.0 for row in selected)
                / count,
                6,
            ),
            "wrong_unique_resolution_rate": round(
                sum(row["wrong_unique_resolution"] for row in selected) / count, 6
            ),
            "catalog_coverage_rate": round(
                sum(row["cas_present_in_artifact"] for row in selected) / count, 6
            ),
            "catalog_missing_case_count": sum(
                not row["cas_present_in_artifact"] for row in selected
            ),
        }

    ordered = sorted(latencies)
    p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95)) if ordered else 0
    return {
        "all": metrics(rows),
        "unseen_surface": metrics(
            [row for row in rows if not row["surface_seen_in_training"]]
        ),
        "unseen_cas": metrics([row for row in rows if not row["cas_seen_in_training"]]),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 6) if latencies else None,
            "p95": round(ordered[p95_index], 6) if ordered else None,
        },
        "failure_examples": [
            {
                "query": row["query"],
                "expected_cas": row["expected_cas"],
                "ranking": row["ranking"],
                "status": row["status"],
            }
            for row in rows
            if row["rank"] != 1
        ][:20],
    }


def evaluate_temporal_adaptation(
    base_model_path: Path,
    validation_model_path: Path,
    final_model_path: Path,
    incident_csv_path: Path,
    *,
    validation_year: int = 2019,
    locked_test_year: int = DEFAULT_LOCKED_TEST_YEAR,
    source_manifest_path: Path | None = None,
    source_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = (
        source_snapshot
        if source_snapshot is not None
        else load_incident_alias_records(incident_csv_path, source_manifest_path)
    )
    records = list(source["records"])
    base = load_resolver(base_model_path)
    validation_model = load_resolver(validation_model_path)
    final_model = load_resolver(final_model_path)
    validation_cases = _cases_for_year(records, validation_year)
    test_cases = _cases_for_year(records, locked_test_year)
    if not validation_cases or not test_cases:
        raise RuntimeError(
            "시간 분할 평가에는 검증 연도와 잠금 테스트 연도의 유효 사례가 모두 필요합니다."
        )
    validation_training = [row for row in records if int(row["year"]) < validation_year]
    final_training = [row for row in records if int(row["year"]) < locked_test_year]

    base_validation = _evaluate_cases(
        base, validation_cases, training_records=validation_training
    )
    adapted_validation = _evaluate_cases(
        validation_model, validation_cases, training_records=validation_training
    )
    base_test = _evaluate_cases(base, test_cases, training_records=final_training)
    adapted_test = _evaluate_cases(
        final_model, test_cases, training_records=final_training
    )
    test_top1_delta = round(
        float(adapted_test["all"]["top1_accuracy"])
        - float(base_test["all"]["top1_accuracy"]),
        6,
    )
    test_top3_delta = round(
        float(adapted_test["all"]["top3_recall"])
        - float(base_test["all"]["top3_recall"]),
        6,
    )
    safety_passed = (
        adapted_test["all"]["wrong_unique_resolution_rate"]
        <= base_test["all"]["wrong_unique_resolution_rate"]
    )
    adoption_passed = test_top1_delta >= 0 and test_top3_delta >= 0 and safety_passed
    return {
        "metrics_version": "incident-adapted-resolver-temporal-evaluation-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "training_policy_version": TRAINING_POLICY_VERSION,
        "task": "substance_candidate_retrieval_only",
        "source_audit": source["audit"],
        "split_policy": {
            "validation_train_year_max": validation_year - 1,
            "validation_year": validation_year,
            "final_train_year_max": locked_test_year - 1,
            "locked_test_year": locked_test_year,
            "locked_test_used_for_tuning": False,
        },
        "validation": {"base": base_validation, "adapted": adapted_validation},
        "locked_test": {"base": base_test, "adapted": adapted_test},
        "locked_test_top1_delta": test_top1_delta,
        "locked_test_top3_delta": test_top3_delta,
        "adoption_gate": {
            "passed": adoption_passed,
            "requires_non_regression_top1": True,
            "requires_non_regression_top3": True,
            "requires_no_wrong_unique_resolution_increase": True,
            "runtime_default_change_allowed": adoption_passed,
        },
        "claim_scope": "TEMPORAL_PUBLIC_RECORD_EVALUATION_NOT_FIELD_ACCURACY",
        "safety": {
            "risk_or_response_training_included": False,
            "candidate_score_is_probability": False,
            "auto_confirmation_allowed": False,
            "rule_execution_allowed": False,
        },
        "limitations": [
            "동일 물질과 동일 표현이 여러 연도에 반복될 수 있는 시간 분할 평가입니다.",
            "울산 2015~2020 공개 기록이므로 전국 현장 정확도를 의미하지 않습니다.",
            "유효 checksum 단일 CAS와 비모호 물질 표현만 사용했습니다.",
            "기본 카탈로그 밖 CAS는 과거 소방 기록의 정확 표현에만 후보로 노출하며 유사도 검색에서는 제외합니다.",
        ],
    }


def run_training_and_evaluation(
    base_model_path: Path,
    incident_csv_path: Path,
    output_dir: Path,
    report_path: Path | None = None,
    *,
    source_manifest_path: Path,
) -> dict[str, Any]:
    source_snapshot = load_incident_alias_records(
        incident_csv_path,
        source_manifest_path,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    validation_model = output_dir / "resolver_incident_adapted_through_2018.joblib"
    final_model = output_dir / "resolver_incident_adapted_through_2019.joblib"
    validation_training = train_incident_adapted_resolver(
        base_model_path,
        incident_csv_path,
        validation_model,
        training_year_max=2018,
        source_snapshot=source_snapshot,
    )
    final_training = train_incident_adapted_resolver(
        base_model_path,
        incident_csv_path,
        final_model,
        training_year_max=2019,
        source_snapshot=source_snapshot,
    )
    evaluation = evaluate_temporal_adaptation(
        base_model_path,
        validation_model,
        final_model,
        incident_csv_path,
        source_snapshot=source_snapshot,
    )
    resolver_regression = evaluate_resolver(
        final_model,
        EVALUATION_DIR / "resolver_regression_queries.csv",
    )
    hint_safety = evaluate_resolver_hint_safety(
        final_model,
        EVALUATION_DIR / "resolver_hint_safety_queries.csv",
    )
    # 저장소에 고정하는 평가 스냅샷에는 실행자 로컬 절대 경로를 남기지 않는다.
    resolver_regression["dataset"] = "resolver_regression_queries.csv"
    hint_safety["dataset"] = "resolver_hint_safety_queries.csv"
    hint_safety["model"] = final_model.name
    regression_gate_passed = bool(
        resolver_regression["candidate_top3_recall"] >= 1.0
        and hint_safety["deployment_gate"]["passed"] is True
    )
    temporal_gate = evaluation["adoption_gate"]
    base_unseen_surface = evaluation["locked_test"]["base"]["unseen_surface"]
    adapted_unseen_surface = evaluation["locked_test"]["adapted"]["unseen_surface"]
    base_unseen_cas = evaluation["locked_test"]["base"]["unseen_cas"]
    adapted_unseen_cas = evaluation["locked_test"]["adapted"]["unseen_cas"]
    unseen_non_regression = bool(
        base_unseen_surface["case_count"] > 0
        and base_unseen_cas["case_count"] > 0
        and adapted_unseen_surface["top1_accuracy"]
        >= base_unseen_surface["top1_accuracy"]
        and adapted_unseen_cas["top3_recall"] >= base_unseen_cas["top3_recall"]
    )
    temporal_gate["requires_unseen_surface_and_cas_non_regression"] = True
    temporal_gate["unseen_surface_and_cas_non_regression"] = unseen_non_regression
    temporal_gate["legacy_regression_and_hint_safety_passed"] = regression_gate_passed
    temporal_gate["passed"] = bool(
        temporal_gate["passed"] and unseen_non_regression and regression_gate_passed
    )
    temporal_gate["runtime_default_change_allowed"] = temporal_gate["passed"]
    payload = {
        **evaluation,
        "legacy_regression": {
            "resolver": resolver_regression,
            "hint_safety": hint_safety,
        },
        "artifacts": {
            "validation": {
                **validation_training,
                "model_path": validation_model.name,
            },
            "final": {
                **final_training,
                "model_path": final_model.name,
            },
        },
    }
    if report_path:
        write_json(report_path, payload)
    return payload


__all__ = [
    "DEFAULT_LOCKED_TEST_YEAR",
    "DEFAULT_TRAINING_YEAR_MAX",
    "SOURCE_DATASET_ID",
    "SOURCE_URL",
    "TRAINING_POLICY_VERSION",
    "evaluate_temporal_adaptation",
    "load_incident_alias_records",
    "run_training_and_evaluation",
    "train_incident_adapted_resolver",
]
