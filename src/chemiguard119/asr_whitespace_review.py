"""ASR 내부 공백 후보 규칙의 개발 구간 사람 검수 큐를 만든다.

공식 사고 원문은 검수에 필요하지만 Git 보고서에는 넣지 않는다. 이 모듈의 출력은
private-data에만 저장하며 사람의 정답을 자동으로 채우지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from chemiguard119.incident import deterministic_parse
from chemiguard119.official_incident_evaluation import (
    DEVELOPMENT_YEAR_MAX,
    PINNED_SOURCE_SHA256,
    SOURCE_ID,
    load_official_incidents,
)
from chemiguard119.resolver import load_resolver
from chemiguard119.utils import compact_text, sha256_file, write_json


SCHEMA_VERSION = "chemicheck119-asr-whitespace-human-review-queue-v1"
FACT_STATUS = "부분 구현 또는 개발용 데모"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _year(row: Mapping[str, str]) -> int | None:
    value = str(row.get("사고일자") or "").strip()[:4]
    return int(value) if value.isdigit() else None


def _observable_labels(row: Mapping[str, str], incident_text: str) -> list[str]:
    compact_source = compact_text(incident_text)
    labels: list[str] = []
    for field in ("제1사고물질", "제2사고물질", "제3사고물질"):
        label = str(row.get(field) or "").strip()
        compact_label = compact_text(label)
        if len(compact_label) >= 2 and compact_label in compact_source:
            labels.append(label)
    return labels


def _mention_summary(mention: Mapping[str, Any]) -> dict[str, Any]:
    resolver = mention.get("resolver")
    resolver_payload = resolver if isinstance(resolver, Mapping) else {}
    candidates = resolver_payload.get("candidates")
    candidate_rows = candidates if isinstance(candidates, list) else []
    return {
        "surface_text": str(mention.get("surface_text") or ""),
        "role": str(mention.get("role") or ""),
        "assertion": str(mention.get("assertion") or ""),
        "resolver_status": str(resolver_payload.get("status") or ""),
        "candidate_cas_numbers": [
            str(candidate.get("cas_number"))
            for candidate in candidate_rows
            if isinstance(candidate, Mapping) and candidate.get("cas_number")
        ],
        "requires_responder_confirmation": bool(
            resolver_payload.get("requires_responder_confirmation")
        ),
        "rule_input_eligible": bool(resolver_payload.get("rule_input_eligible")),
    }


def _mention_key(mention: Mapping[str, Any]) -> str:
    return json.dumps(mention, ensure_ascii=False, sort_keys=True)


def _surface_hits(labels: list[str], mentions: list[dict[str, Any]]) -> tuple[int, int]:
    exact_hits = 0
    containment_hits = 0
    surfaces = [
        compact_text(str(mention.get("surface_text") or "")) for mention in mentions
    ]
    for label in labels:
        expected = compact_text(label)
        exact_hits += int(any(expected == surface for surface in surfaces))
        containment_hits += int(
            any(
                surface and (expected in surface or surface in expected)
                for surface in surfaces
            )
        )
    return exact_hits, containment_hits


def _case_id(row: Mapping[str, str], incident_text: str) -> str:
    identity = "|".join(
        (
            SOURCE_ID,
            str(row.get("연번") or ""),
            str(row.get("사고일자") or ""),
            incident_text,
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def build_asr_whitespace_review_queue(
    source_path: Path,
    resolver_model_path: Path,
    *,
    report_path: Path | None = None,
    expected_source_sha256: str | None = PINNED_SOURCE_SHA256,
) -> dict[str, Any]:
    """2014~2020 개발 구간의 baseline/candidate 변경 사례만 반환한다."""

    source_path = Path(source_path)
    resolver_model_path = Path(resolver_model_path)
    if report_path is not None and Path(report_path).resolve().is_relative_to(
        REPOSITORY_ROOT
    ):
        raise ValueError(
            "검수 큐에는 공식 사고 원문이 포함되므로 Git 저장소 내부에 저장할 수 없습니다."
        )
    loaded = load_official_incidents(
        source_path,
        expected_sha256=expected_source_sha256,
    )
    resolver = load_resolver(resolver_model_path)
    entries: list[dict[str, Any]] = []
    development_case_count = 0
    baseline_unsafe_count = 0
    candidate_unsafe_count = 0
    exact_improved_count = 0
    exact_regressed_count = 0
    containment_improved_count = 0
    containment_regressed_count = 0

    for row in loaded["rows"]:
        year = _year(row)
        if year is None or year > DEVELOPMENT_YEAR_MAX:
            continue
        development_case_count += 1
        incident_text = str(row.get("사고내용") or "")
        baseline = deterministic_parse(
            incident_text,
            resolver,
            allow_internal_asr_whitespace=False,
        )
        candidate = deterministic_parse(
            incident_text,
            resolver,
            allow_internal_asr_whitespace=True,
        )
        baseline_mentions = [
            _mention_summary(item)
            for item in baseline.get("substance_mentions") or []
            if isinstance(item, Mapping)
        ]
        candidate_mentions = [
            _mention_summary(item)
            for item in candidate.get("substance_mentions") or []
            if isinstance(item, Mapping)
        ]
        baseline_unsafe_count += sum(
            mention["rule_input_eligible"] for mention in baseline_mentions
        )
        candidate_unsafe_count += sum(
            mention["rule_input_eligible"] for mention in candidate_mentions
        )
        baseline_keys = {_mention_key(mention) for mention in baseline_mentions}
        candidate_keys = {_mention_key(mention) for mention in candidate_mentions}
        if baseline_keys == candidate_keys:
            continue

        labels = _observable_labels(row, incident_text)
        baseline_exact, baseline_containment = _surface_hits(labels, baseline_mentions)
        candidate_exact, candidate_containment = _surface_hits(
            labels, candidate_mentions
        )
        exact_improved_count += int(candidate_exact > baseline_exact)
        exact_regressed_count += int(candidate_exact < baseline_exact)
        containment_improved_count += int(candidate_containment > baseline_containment)
        containment_regressed_count += int(candidate_containment < baseline_containment)
        entries.append(
            {
                "case_id": _case_id(row, incident_text),
                "year": year,
                "incident_text": incident_text,
                "official_observable_labels": labels,
                "baseline_mentions": baseline_mentions,
                "candidate_mentions": candidate_mentions,
                "candidate_only_mentions": [
                    mention
                    for mention in candidate_mentions
                    if _mention_key(mention) not in baseline_keys
                ],
                "removed_mentions": [
                    mention
                    for mention in baseline_mentions
                    if _mention_key(mention) not in candidate_keys
                ],
                "official_label_metric_effect": {
                    "baseline_exact_hits": baseline_exact,
                    "candidate_exact_hits": candidate_exact,
                    "baseline_containment_hits": baseline_containment,
                    "candidate_containment_hits": candidate_containment,
                },
                "human_review": {
                    "status": "NOT_REVIEWED",
                    "decision": None,
                    "reviewer": None,
                    "notes": None,
                },
            }
        )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "fact_status": FACT_STATUS,
        "source_id": SOURCE_ID,
        "source_sha256": loaded["audit"]["source_sha256"],
        "resolver_artifact_sha256": sha256_file(resolver_model_path),
        "source_code_sha256": {
            "review_queue": sha256_file(Path(__file__)),
            "incident_parser": sha256_file(Path(__file__).with_name("incident.py")),
            "resolver": sha256_file(Path(__file__).with_name("resolver.py")),
        },
        "split": "development_2014_2020",
        "development_case_count": development_case_count,
        "changed_case_count": len(entries),
        "baseline_unsafe_rule_eligible_mention_count": baseline_unsafe_count,
        "candidate_unsafe_rule_eligible_mention_count": candidate_unsafe_count,
        "official_label_metric_effect_case_counts": {
            "exact_improved": exact_improved_count,
            "exact_regressed": exact_regressed_count,
            "containment_improved": containment_improved_count,
            "containment_regressed": containment_regressed_count,
        },
        "contains_raw_official_incident_text": True,
        "contains_company_or_address_fields": False,
        "contains_potential_facility_information_in_incident_text": True,
        "git_commit_allowed": False,
        "human_review_required": True,
        "machine_generated_human_labels": False,
        "review_status": "NOT_STARTED",
        "reviewed_case_count": 0,
        "runtime_default_change_allowed": False,
        "claim_scope": "OFFICIAL_DEVELOPMENT_DIFF_REVIEW_NOT_FIELD_ACCURACY",
        "review_protocol": {
            "allowed_decisions": ["ACCEPT", "REJECT", "AMBIGUOUS"],
            "question": (
                "후보 규칙이 새로 선택한 물질 span이 원문에서 실제 물질 언급인가?"
            ),
            "do_not_use_locked_2021_2025_failures_for_tuning": True,
        },
        "entries": entries,
    }
    if report_path is not None:
        write_json(Path(report_path), payload)
    return payload


__all__ = [
    "FACT_STATUS",
    "REPOSITORY_ROOT",
    "SCHEMA_VERSION",
    "build_asr_whitespace_review_queue",
]
