"""화학물질안전원 전국 화학사고 파일로 파서 외부 기준선을 감사한다.

원천의 업체명·주소·사고 원문은 평가 계산에만 사용하고 보고서에는 남기지 않는다.
사고물질 열에는 CAS가 없으므로 이 평가는 물질명 언급 추출과 공식 사고유형 재현을
측정하며, 화학물질 식별 정확도나 현장 성능으로 해석하지 않는다.
"""

from __future__ import annotations

import csv
import math
import statistics
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from chemiguard119.incident import INCIDENT_PARSER_POLICY_VERSION, deterministic_parse
from chemiguard119.resolver import load_resolver, resolve_substance
from chemiguard119.utils import compact_text, sha256_file, write_json


SOURCE_ID = "CSI_NATIONAL_CHEMICAL_ACCIDENTS_2014_2025"
SOURCE_PAGE_URL = "https://www.data.go.kr/data/15069200/fileData.do"
SOURCE_DOWNLOAD_URL = (
    "https://www.data.go.kr/cmm/cmm/fileDownload.do?"
    "atchFileId=FILE_000000003533190&fileDetailSn=1&insertDataPrcus=N"
)
PINNED_SOURCE_SHA256 = (
    "a1ef8e4b6b0c6ef96fb7277edce2b1cf5c1b935a88f4274c88d878713bb7fba5"
)
SOURCE_ENCODING = "cp949"
DEVELOPMENT_YEAR_MAX = 2020
LOCKED_TEST_YEAR_MIN = 2021
REQUIRED_COLUMNS = {
    "연번",
    "사고일자",
    "시도",
    "사고내용",
    "사고유형",
    "제1사고물질",
    "제2사고물질",
    "제3사고물질",
}
SENSITIVE_OR_RAW_COLUMNS = {
    "사고업체명",
    "주소",
    "사고내용",
    "소방접수시간",
}
SUBSTANCE_FIELDS = ("제1사고물질", "제2사고물질", "제3사고물질")
INCIDENT_TYPE_MAP = {
    "누출": "LEAK",
    "화재": "FIRE",
    "폭발": "EXPLOSION",
}
EvaluationSplit = Literal["development", "locked_test"]


def load_official_incidents(
    path: Path,
    *,
    expected_sha256: str | None = PINNED_SOURCE_SHA256,
) -> dict[str, Any]:
    """고정 인코딩·스키마를 검사하고 원천 행과 비식별 audit을 반환한다."""

    source_sha256 = sha256_file(path)
    if expected_sha256 is not None and source_sha256 != expected_sha256:
        raise RuntimeError(
            "전국 화학사고 원천 checksum이 고정값과 다릅니다. "
            "공식 데이터 갱신 여부를 먼저 검토하세요."
        )

    with path.open(encoding=SOURCE_ENCODING, newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - fieldnames
        if missing:
            raise RuntimeError(
                f"전국 화학사고 원천 컬럼이 부족합니다: {sorted(missing)}"
            )
        rows = list(reader)
    if not rows:
        raise RuntimeError("전국 화학사고 원천이 비어 있습니다.")

    invalid_date_count = 0
    years: Counter[str] = Counter()
    for row in rows:
        date = str(row.get("사고일자") or "").strip()
        year = date[:4]
        if len(date) < 10 or not year.isdigit():
            invalid_date_count += 1
            continue
        years[year] += 1

    return {
        "rows": rows,
        "audit": {
            "source_id": SOURCE_ID,
            "source_page_url": SOURCE_PAGE_URL,
            "source_sha256": source_sha256,
            "source_row_count": len(rows),
            "encoding": SOURCE_ENCODING,
            "year_counts": dict(sorted(years.items())),
            "invalid_date_count": invalid_date_count,
            "contains_company_or_address_fields": bool(
                SENSITIVE_OR_RAW_COLUMNS & fieldnames
            ),
            "raw_or_sensitive_fields_excluded_from_report": sorted(
                SENSITIVE_OR_RAW_COLUMNS & fieldnames
            ),
        },
    }


def _year(row: dict[str, str]) -> int | None:
    value = str(row.get("사고일자") or "").strip()[:4]
    return int(value) if value.isdigit() else None


def _select_split(
    rows: list[dict[str, str]], split: EvaluationSplit
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for row in rows:
        year = _year(row)
        if year is None:
            continue
        if split == "development" and year <= DEVELOPMENT_YEAR_MAX:
            selected.append(row)
        elif split == "locked_test" and year >= LOCKED_TEST_YEAR_MIN:
            selected.append(row)
    if not selected:
        raise RuntimeError(f"{split} 분할에 유효한 사고가 없습니다.")
    return selected


def _official_substances(row: dict[str, str]) -> list[str]:
    return [
        value
        for field in SUBSTANCE_FIELDS
        if (value := str(row.get(field) or "").strip())
    ]


def _observable_labels(row: dict[str, str]) -> list[str]:
    compact_source = compact_text(str(row.get("사고내용") or ""))
    return [
        value
        for value in _official_substances(row)
        if len(compact_text(value)) >= 2 and compact_text(value) in compact_source
    ]


def _surface_match(expected: str, actual: str, *, allow_containment: bool) -> bool:
    expected_normalized = compact_text(expected)
    actual_normalized = compact_text(actual)
    if not expected_normalized or not actual_normalized:
        return False
    if expected_normalized == actual_normalized:
        return True
    return bool(
        allow_containment
        and len(actual_normalized) >= 2
        and (
            actual_normalized in expected_normalized
            or expected_normalized in actual_normalized
        )
    )


def _rounded_rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def evaluate_official_incidents(
    source_path: Path,
    resolver_model_path: Path,
    *,
    split: EvaluationSplit,
    report_path: Path | None = None,
    expected_source_sha256: str | None = PINNED_SOURCE_SHA256,
) -> dict[str, Any]:
    """시간·기관 분리 외부 기준선 평가를 수행한다.

    locked_test에서는 실패 물질명과 원문을 반환하지 않아 결과를 보고 모델을 조정하는
    누수를 줄인다. 해당 원천에는 CAS 정답이 없으므로 Resolver CAS 정확도는 주장하지 않는다.
    """

    loaded = load_official_incidents(
        source_path,
        expected_sha256=expected_source_sha256,
    )
    rows = _select_split(loaded["rows"], split)
    resolver = load_resolver(resolver_model_path)
    type_case_count = 0
    type_hit_count = 0
    exact_type_set_count = 0
    additional_type_count = 0
    official_label_count = 0
    observable_label_count = 0
    exact_surface_hit_count = 0
    containment_surface_hit_count = 0
    observable_case_count = 0
    any_surface_case_hit_count = 0
    all_surface_case_hit_count = 0
    resolver_reachable_label_count = 0
    resolver_reachable_hit_count = 0
    unsafe_rule_eligible_mention_count = 0
    type_counts: Counter[str] = Counter()
    province_counts: Counter[str] = Counter()
    missed_labels: Counter[str] = Counter()
    latencies: list[float] = []
    resolution_cache: dict[str, dict[str, Any]] = {}
    allowed_incident_types = set(INCIDENT_TYPE_MAP.values())

    for row in rows:
        source_text = str(row.get("사고내용") or "")
        started = time.perf_counter()
        parsed = deterministic_parse(source_text, resolver)
        latencies.append((time.perf_counter() - started) * 1_000)
        predicted_types = {
            value
            for value in parsed.get("incident_types") or []
            if value in allowed_incident_types
        }
        official_type = str(row.get("사고유형") or "").strip()
        type_counts[official_type or "<EMPTY>"] += 1
        province_counts[str(row.get("시도") or "").strip() or "<EMPTY>"] += 1
        expected_type = INCIDENT_TYPE_MAP.get(official_type)
        if expected_type is not None:
            type_case_count += 1
            if expected_type in predicted_types:
                type_hit_count += 1
            if predicted_types == {expected_type}:
                exact_type_set_count += 1
            if predicted_types - {expected_type}:
                additional_type_count += 1

        official_labels = _official_substances(row)
        official_label_count += len(official_labels)
        observable = _observable_labels(row)
        mentions = [
            str(item.get("surface_text") or "")
            for item in parsed.get("substance_mentions") or []
        ]
        observable_label_count += len(observable)
        if observable:
            observable_case_count += 1
        exact_hits = 0
        containment_hits = 0
        for label in observable:
            exact = any(
                _surface_match(label, mention, allow_containment=False)
                for mention in mentions
            )
            containment = any(
                _surface_match(label, mention, allow_containment=True)
                for mention in mentions
            )
            exact_surface_hit_count += int(exact)
            containment_surface_hit_count += int(containment)
            exact_hits += int(exact)
            containment_hits += int(containment)
            if split == "development" and not containment:
                missed_labels[label] += 1

            expected_resolution = resolution_cache.get(label)
            if expected_resolution is None:
                expected_resolution = resolve_substance(label, resolver, top_k=10)
                resolution_cache[label] = expected_resolution
            expected_cas = {
                str(candidate.get("cas_number") or "")
                for candidate in expected_resolution.get("candidates") or []
                if candidate.get("cas_number")
            }
            if (
                expected_resolution.get("status")
                in {
                    "EXACT_ALIAS_CANDIDATE",
                    "AMBIGUOUS_ALIAS",
                }
                and expected_cas
            ):
                resolver_reachable_label_count += 1
                mention_cas = {
                    str(candidate.get("cas_number") or "")
                    for item in parsed.get("substance_mentions") or []
                    for candidate in (item.get("resolver") or {}).get("candidates")
                    or []
                    if candidate.get("cas_number")
                }
                if expected_cas & mention_cas:
                    resolver_reachable_hit_count += 1

        if observable and containment_hits > 0:
            any_surface_case_hit_count += 1
        if observable and containment_hits == len(observable):
            all_surface_case_hit_count += 1
        unsafe_rule_eligible_mention_count += sum(
            bool((item.get("resolver") or {}).get("rule_input_eligible"))
            for item in parsed.get("substance_mentions") or []
        )

    ordered_latency = sorted(latencies)
    p95_index = min(
        len(ordered_latency) - 1, math.ceil(len(ordered_latency) * 0.95) - 1
    )
    payload: dict[str, Any] = {
        "metrics_version": "official-national-incident-parser-audit-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "parser_policy_version": INCIDENT_PARSER_POLICY_VERSION,
        "resolver_artifact_sha256": sha256_file(resolver_model_path),
        "expert_reviewed": False,
        "source_audit": loaded["audit"],
        "split": split,
        "split_policy": {
            "development_year_max": DEVELOPMENT_YEAR_MAX,
            "locked_test_year_min": LOCKED_TEST_YEAR_MIN,
            "locked_test_used_for_training": False,
            "locked_test_diagnostics_exposed": False,
        },
        "case_count": len(rows),
        "year_counts": dict(sorted(Counter(str(_year(row)) for row in rows).items())),
        "province_counts": dict(sorted(province_counts.items())),
        "official_incident_type_counts": dict(sorted(type_counts.items())),
        "incident_type": {
            "eligible_case_count": type_case_count,
            "recall": _rounded_rate(type_hit_count, type_case_count),
            "exact_set_accuracy": _rounded_rate(exact_type_set_count, type_case_count),
            "additional_type_case_rate": _rounded_rate(
                additional_type_count, type_case_count
            ),
        },
        "substance_mention": {
            "official_label_count": official_label_count,
            "observable_exact_label_count": observable_label_count,
            "observable_case_count": observable_case_count,
            "exact_surface_recall": _rounded_rate(
                exact_surface_hit_count, observable_label_count
            ),
            "containment_surface_recall": _rounded_rate(
                containment_surface_hit_count, observable_label_count
            ),
            "case_any_surface_recall": _rounded_rate(
                any_surface_case_hit_count, observable_case_count
            ),
            "case_all_surface_recall": _rounded_rate(
                all_surface_case_hit_count, observable_case_count
            ),
            "resolver_reachable_label_count": resolver_reachable_label_count,
            "resolver_reachable_recall": _rounded_rate(
                resolver_reachable_hit_count, resolver_reachable_label_count
            ),
            "metric_scope": (
                "공식 사고물질 문자열이 사고내용에 관찰 가능한 사례의 언급 추출; "
                "CAS 식별 정확도 아님"
            ),
        },
        "safety": {
            "unsafe_rule_eligible_mention_count": (unsafe_rule_eligible_mention_count),
            "raw_text_in_report": False,
            "company_or_address_in_report": False,
            "auto_confirmation_allowed": False,
        },
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 6),
            "p95": round(ordered_latency[p95_index], 6),
        },
        "claim_scope": "OFFICIAL_CROSS_SOURCE_PARSER_AUDIT_NOT_FIELD_ACCURACY",
        "limitations": [
            "공식 원천의 사고물질 열에는 CAS 정답이 없어 물질 식별 정확도를 측정하지 않습니다.",
            "사고내용에 공식 사고물질 문자열이 직접 관찰되는 사례만 언급 추출 분모에 포함합니다.",
            "공식 사고유형은 단일 라벨이지만 사고내용에는 복합 사건 표현이 있을 수 있습니다.",
            "실제 119 신고 음성 전사가 아니라 사고 정리 문장입니다.",
        ],
    }
    if split == "development":
        payload["development_diagnostics"] = {
            "top_missed_official_labels": [
                {"label": label, "count": count}
                for label, count in missed_labels.most_common(30)
            ]
        }
    if report_path is not None:
        write_json(report_path, payload)
    return payload


__all__ = [
    "DEVELOPMENT_YEAR_MAX",
    "LOCKED_TEST_YEAR_MIN",
    "PINNED_SOURCE_SHA256",
    "SOURCE_DOWNLOAD_URL",
    "SOURCE_ID",
    "SOURCE_PAGE_URL",
    "evaluate_official_incidents",
    "load_official_incidents",
]
