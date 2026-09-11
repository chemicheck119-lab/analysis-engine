"""공식 울산 2020 고정 모집단의 오프라인 Resolver 비교. 원문은 private에만 쓴다."""

from __future__ import annotations

import csv
import platform
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from chemiguard119.incident_adaptation import (
    _cases_for_year,
    load_incident_alias_records,
)
from chemiguard119.incident_replay import _fingerprint, _private_bytes, _private_json
from chemiguard119.resolver import (
    evaluate_resolver_hint_safety,
    load_resolver,
    resolve_substance,
)
from chemiguard119.resolver_dense_experiment import (
    EmbeddingFunction,
    _candidate_rank,
    _normalize_embeddings,
    _rank_dense,
    _rrf_fuse,
    build_dense_alias_corpus,
    make_transformer_cls_encoder,
)
from chemiguard119.utils import sha256_file


SYSTEMS = ("A_SPARSE", "B_DENSE_FALLBACK", "C_RRF_FALLBACK")
EXACT_STATUSES = {"EXACT_IDENTIFIER_MATCH", "EXACT_ALIAS_CANDIDATE", "AMBIGUOUS_ALIAS"}
LOCKED_HASHES = {
    "source.zip": "d0ee041579a953084f8037763ac7d0a3d901d11278a6854d348374c69363f976",
    "resolver-source.csv": "5237194a1a22bf9fb3666a4009237d6bd243095e262baba3468874283f51d11c",
    "resolver-source.manifest.json": "5cf643ece3a6de06e4807695459c193a5d989b21e84f728b96a231c1d322c1ae",
}
MODEL_HASH = "2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff"
TEST_HASH = "217bfa4aa6ff3ac2b98b20049096c76df7249747b8ae2d3c524ee6a0499f2f6a"
UNSEEN_HASH = "6c17105cecc2c30254ef2297b94b2d414a8f11166215703d00d552bbd8b6a24a"
MODEL_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


def preserve_sparse_gate(query: str, result: dict[str, Any]) -> bool:
    """exact/모호함/식별자 거절/빈 입력은 의미 유사도로 우회하지 않는다."""
    return (
        not query.strip()
        or result["status"] in EXACT_STATUSES
        or result["confirmation_reason"]
        in {"VALID_CAS_NOT_IN_CATALOG", "INVALID_CAS_IDENTIFIER"}
    )


def fallback_result(
    sparse: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        **sparse,
        "status": "FUZZY_CANDIDATE" if candidates else "UNRESOLVED",
        "input_class": "UNCONFIRMED_CANDIDATE" if candidates else "UNRESOLVED",
        "confirmation_reason": "EXPERIMENTAL_RANKING_REQUIRES_RESPONDER_CONFIRMATION",
        "requires_responder_confirmation": True,
        "rule_input_eligible": False,
        "current_inventory_confirmed": False,
        "candidates": [
            {**item, "rule_eligible": False, "current_inventory_confirmed": False}
            for item in candidates
        ],
    }


def metrics(rows: list[dict[str, Any]], system: str) -> dict[str, Any]:
    count = len(rows)
    outputs = [row["systems"][system] for row in rows]
    ranks = [item["rank"] for item in outputs]
    returned = sum(bool(item["candidate_count"]) for item in outputs)
    top1 = sum(rank == 1 for rank in ranks)
    top3 = sum(rank is not None for rank in ranks)

    def ratio(value: float) -> float | None:
        return round(value / count, 6) if count else None

    return {
        "case_count": count,
        "top1_hit_count": top1,
        "top3_hit_count": top3,
        "top1_accuracy": ratio(top1),
        "top3_recall": ratio(top3),
        "mrr_at_3": ratio(sum(1 / rank if rank else 0 for rank in ranks)),
        "candidate_return_count": returned,
        "candidate_return_rate": ratio(returned),
        "empty_candidate_count": count - returned,
        "empty_candidate_rate": ratio(count - returned),
        "top1_accuracy_when_candidates_returned": round(top1 / returned, 6)
        if returned
        else None,
        "catalog_missing_case_count": sum(not row["cas_in_artifact"] for row in rows),
        "catalog_coverage_rate": ratio(sum(row["cas_in_artifact"] for row in rows)),
        "wrong_unique_exact_candidate_count": sum(
            item["wrong_unique_exact"] for item in outputs
        ),
        "rule_eligibility_violation_count": sum(
            item["rule_violation"] for item in outputs
        ),
        "confirmation_policy_violation_count": sum(
            item["confirmation_violation"] for item in outputs
        ),
        "source_only_fuzzy_leak_count": sum(
            item["source_only_fuzzy_leak"] for item in outputs
        ),
        "common_gate_change_count": sum(
            item["common_gate_changed"] for item in outputs
        ),
    }


def paired_counts(rows: list[dict[str, Any]], system: str) -> dict[str, Any]:
    result = {}
    for k in (1, 3):
        better = worse = 0
        for row in rows:
            a = row["systems"][SYSTEMS[0]]["rank"]
            b = row["systems"][system]["rank"]
            a_hit = a is not None and a <= k
            b_hit = b is not None and b <= k
            better += b_hit and not a_hit
            worse += a_hit and not b_hit
        result[f"top{k}"] = {"corrected_from_sparse": better, "lost_from_sparse": worse}
    return result


def comparison_decisions(groups: dict[str, Any]) -> dict[str, Any]:
    decisions = {}
    for system in SYSTEMS[1:]:
        improved = (
            groups["unseen_surface"][system]["top3_hit_count"]
            > groups["unseen_surface"][SYSTEMS[0]]["top3_hit_count"]
        )
        nonregression = all(
            groups[group][system][metric] >= groups[group][SYSTEMS[0]][metric]
            for group in ("all", "unseen_surface", "internal_regression")
            for metric in ("top1_hit_count", "top3_hit_count")
        )
        safety = all(
            groups[group][system][metric] == 0
            for group in ("all", "internal_regression")
            for metric in (
                "wrong_unique_exact_candidate_count",
                "rule_eligibility_violation_count",
                "confirmation_policy_violation_count",
                "source_only_fuzzy_leak_count",
                "common_gate_change_count",
            )
        )
        decisions[system] = {
            "unseen_top3_improved": improved,
            "all_required_accuracy_nonregression": nonregression,
            "structural_safety_passed": safety,
            "decision": "CONDITIONAL_FURTHER_VALIDATION_ONLY"
            if improved and nonregression and safety
            else "REJECT_THIS_CONFIGURATION_KEEP_SPARSE",
            "runtime_change_allowed": False,
        }
    return decisions


def compare_cases(
    artifact: dict[str, Any], cases: list[dict[str, Any]], embed: EmbeddingFunction
) -> tuple[dict[str, Any], list[dict[str, Any]], np.ndarray]:
    """예측과 정답은 이 함수의 private rows에만 남는다. 보고서는 집계만 반환한다."""
    if not cases:
        raise ValueError("평가 사례가 비어 있습니다.")
    corpus = build_dense_alias_corpus(artifact)
    started = time.perf_counter()
    corpus_vectors = _normalize_embeddings(
        embed([item["alias_text"] for item in corpus]), expected_rows=len(corpus)
    )
    corpus_seconds = time.perf_counter() - started
    started = time.perf_counter()
    query_vectors = _normalize_embeddings(
        embed([item["query"] for item in cases]), expected_rows=len(cases)
    )
    query_seconds = time.perf_counter() - started
    if query_vectors.shape[1] != corpus_vectors.shape[1]:
        raise ValueError("질의와 corpus embedding 차원이 다릅니다.")
    catalog = {row["cas_number"] for row in artifact["rows"]}
    fuzzy_catalog = {
        row["cas_number"]
        for row in artifact["rows"]
        if row.get("fuzzy_search_eligible") is not False
    }
    source_only = catalog - fuzzy_catalog
    rows = []
    timings: dict[str, list[float]] = {
        name: []
        for name in (
            "sparse_top3",
            "sparse_pool100",
            "dense_cached_ranking",
            "rrf_fusion",
        )
    }
    for case, vector in zip(cases, query_vectors, strict=True):
        started = time.perf_counter()
        sparse = resolve_substance(case["query"], artifact, top_k=3, minimum_score=0.20)
        timings["sparse_top3"].append(time.perf_counter() - started)
        preserve = preserve_sparse_gate(case["query"], sparse)
        outputs = {SYSTEMS[0]: sparse}
        if preserve:
            outputs.update({name: sparse for name in SYSTEMS[1:]})
        else:
            started = time.perf_counter()
            pool = resolve_substance(
                case["query"], artifact, top_k=100, minimum_score=0.20
            )
            timings["sparse_pool100"].append(time.perf_counter() - started)
            started = time.perf_counter()
            dense = _rank_dense(vector, corpus_vectors, corpus, top_k=100)
            timings["dense_cached_ranking"].append(time.perf_counter() - started)
            started = time.perf_counter()
            fused = _rrf_fuse(pool["candidates"], dense, top_k=3, rrf_k=60)
            timings["rrf_fusion"].append(time.perf_counter() - started)
            outputs[SYSTEMS[1]] = fallback_result(sparse, dense[:3])
            outputs[SYSTEMS[2]] = fallback_result(sparse, fused)
        row = {
            **case,
            "cas_in_artifact": case["expected_cas"] in catalog,
            "preserved_gate": preserve,
            "sparse_status": sparse["status"],
            "systems": {},
        }
        for name, output in outputs.items():
            candidates = output["candidates"]
            rank = _candidate_rank(candidates, case["expected_cas"])
            row["systems"][name] = {
                "rank": rank,
                "candidate_count": len(candidates),
                "candidates": candidates,
                "wrong_unique_exact": len(candidates) == 1
                and rank != 1
                and output["status"] in EXACT_STATUSES,
                "rule_violation": bool(output.get("rule_input_eligible"))
                or any(item.get("rule_eligible") for item in candidates),
                "confirmation_violation": output.get("requires_responder_confirmation")
                is not True
                or bool(output.get("current_inventory_confirmed"))
                or any(item.get("current_inventory_confirmed") for item in candidates),
                "source_only_fuzzy_leak": not preserve
                and any(item["cas_number"] in source_only for item in candidates),
                "common_gate_changed": preserve and output != sparse,
            }
        rows.append(row)
    official = [row for row in rows if row["cohort"] == "official_2020"]
    selections = {
        "all": official,
        "unseen_surface": [row for row in official if row["unseen_surface"]],
        "unseen_cas": [row for row in official if row["unseen_cas"]],
        "internal_regression": [
            row for row in rows if row["cohort"] == "internal_regression"
        ],
    }
    groups = {
        group: {system: metrics(selected, system) for system in SYSTEMS}
        for group, selected in selections.items()
    }
    report = {
        "metrics_by_cohort": groups,
        "paired_changes": {
            group: {system: paired_counts(selected, system) for system in SYSTEMS[1:]}
            for group, selected in selections.items()
        },
        "decision": comparison_decisions(groups),
        "corpus": {
            "dense_alias_count": len(corpus),
            "dense_cas_count": len({row["cas_number"] for row in corpus}),
            "sha256": _fingerprint(corpus),
        },
        "routing": {
            group: {
                "preserved_gate_count": sum(row["preserved_gate"] for row in selected),
                "fallback_count": sum(not row["preserved_gate"] for row in selected),
                "sparse_status_counts": dict(
                    Counter(row["sparse_status"] for row in selected)
                ),
            }
            for group, selected in selections.items()
        },
        "timing": {
            "corpus_embedding_seconds": round(corpus_seconds, 6),
            "all_query_batch_embedding_seconds": round(query_seconds, 6),
            "cached_operations": {
                name: {
                    "count": len(values),
                    "total_seconds": round(sum(values), 6),
                    "mean_ms": round(float(np.mean(values)) * 1000, 6)
                    if values
                    else None,
                }
                for name, values in timings.items()
            },
            "is_service_latency_comparison": False,
            "includes_http_stt_or_cold_model_load": False,
        },
    }
    return report, rows, corpus_vectors


def assert_baseline(report: dict[str, Any]) -> None:
    for group, count, top1, top3 in (
        ("all", 419, 376, 378),
        ("unseen_surface", 60, 17, 19),
    ):
        actual = report["metrics_by_cohort"][group][SYSTEMS[0]]
        if (
            actual["case_count"],
            actual["top1_hit_count"],
            actual["top3_hit_count"],
        ) != (count, top1, top3):
            raise ValueError(
                "고정 Sparse 기준선 불일치: 결과를 채택하지 않고 원본/버전을 확인해야 합니다."
            )


def run_comparison(
    source_dir: Path,
    model_path: Path,
    embedding_path: Path,
    regression_path: Path,
    safety_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    for name, expected in LOCKED_HASHES.items():
        if sha256_file(source_dir / name) != expected:
            raise ValueError(f"사전 고정 입력 hash 불일치: {name}")
    if sha256_file(model_path) != MODEL_HASH:
        raise ValueError("사전 고정 Resolver hash 불일치")
    if embedding_path.name != MODEL_REVISION:
        raise ValueError("사전 고정 BGE-M3 revision 경로가 아닙니다.")
    artifact = load_resolver(model_path)
    if artifact.get("training_metadata", {}).get("training_year_max") != 2019:
        raise ValueError("2019년 학습 cutoff가 아닙니다.")
    source = load_incident_alias_records(
        source_dir / "resolver-source.csv", source_dir / "resolver-source.manifest.json"
    )
    records = source["records"]
    cases = _cases_for_year(records, 2020)
    past = [row for row in records if row["year"] <= 2019]
    past_pairs = {(row["normalized_text"], row["cas_number"]) for row in past}
    past_cas = {cas for _, cas in past_pairs}
    unseen = [
        case
        for case in cases
        if (case["normalized_query"], case["expected_cas"]) not in past_pairs
    ]
    if _fingerprint(cases) != TEST_HASH or _fingerprint(unseen) != UNSEEN_HASH:
        raise ValueError("기존 419건/60건과 평가 모집단이 다릅니다.")
    for case in cases:
        case.update(
            cohort="official_2020",
            unseen_surface=case in unseen,
            unseen_cas=case["expected_cas"] not in past_cas,
        )
    with regression_path.open(encoding="utf-8-sig", newline="") as handle:
        regression = [
            {
                "query": row["query"],
                "expected_cas": row["expected_cas"],
                "cohort": "internal_regression",
            }
            for row in csv.DictReader(handle)
        ]
    if len(regression) != 21:
        raise ValueError("고정 내부 회귀 사례 수가 다릅니다.")
    output_dir = output_dir.resolve()
    if any((parent / ".git").exists() for parent in (output_dir, *output_dir.parents)):
        raise ValueError("상세 결과는 Git worktree 밖 private 경로만 허용합니다.")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    print(
        "공식 419건/미관측 60건 분할·hash 확인 완료; 로컬 임베딩을 시작합니다.",
        flush=True,
    )
    embed, metadata = make_transformer_cls_encoder(
        embedding_path, batch_size=64, max_length=32, device_name="mps"
    )
    report, rows, vectors = compare_cases(artifact, cases + regression, embed)
    _private_json(output_dir / "rows.private.json", {"rows": rows})
    _private_bytes(
        output_dir / "corpus-vectors.float32", vectors.astype("<f4").tobytes()
    )
    _private_json(output_dir / "comparison.raw.json", report)
    assert_baseline(report)
    hint_safety = evaluate_resolver_hint_safety(model_path, safety_path)
    _private_json(output_dir / "hint-safety.private.json", hint_safety)
    report.update(
        {
            "schema_version": "ulsan-shared-gate-resolver-comparison-v1",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "fact_status": "부분 구현 또는 개발용 데모",
            "claim_scope": "OFFICIAL_2020_EXPRESSION_CAS_PAIRS_NOT_FIELD_VALIDATION",
            "runtime_changed": False,
            "training_performed": False,
            "cost": {
                "additional_server_cost_krw": 0,
                "cumulative_prior_cost_known": False,
            },
            "environment": {
                "system": platform.system(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "embedding": metadata,
            },
            "config": {
                "top_k": 3,
                "retrieval_depth": 100,
                "sparse_minimum_score": 0.20,
                "rrf_k": 60,
                "rrf_weights": [1, 1],
                "dense_confidence_threshold": None,
                "shared_exact_and_identifier_gate": True,
            },
            "provenance": {
                **LOCKED_HASHES,
                "resolver_sha256": MODEL_HASH,
                "canonical_test_cases_sha256": TEST_HASH,
                "canonical_unseen_cases_sha256": UNSEEN_HASH,
                "regression_sha256": sha256_file(regression_path),
                "safety_sha256": sha256_file(safety_path),
                "evaluator_source_sha256": sha256_file(Path(__file__)),
                "dense_helper_source_sha256": sha256_file(
                    Path(__file__).with_name("resolver_dense_experiment.py")
                ),
                "private_rows_sha256": sha256_file(output_dir / "rows.private.json"),
                "corpus_vectors_sha256": sha256_file(
                    output_dir / "corpus-vectors.float32"
                ),
            },
            "split": {
                "test_year": 2020,
                "past_year_max": 2019,
                "past_record_count": len(past),
                "repeated_past_pair_count": len(cases) - len(unseen),
                "new_secret_test": False,
                "global_period_ambiguity_filter_preserved": True,
            },
            "existing_sparse_hint_safety": {
                key: hint_safety[key]
                for key in (
                    "case_count",
                    "unsafe_auto_hint_count",
                    "wrong_cas_auto_hint_count",
                    "resolver_rule_eligibility_violation_count",
                    "deployment_gate",
                )
            },
            "limitations": [
                "공식 표의 표현-CAS 관계이며 독립 전문가 검수 정답이 아니다.",
                "419건은 독립 사고 건수가 아니며 이미 관찰한 test이다.",
                "확인 정책 검사는 Rule 실행/현장 안전성 검증이 아니다.",
                "Dense/RRF 후보 반환률은 안전한 기권 성능이 아니다.",
                "속도는 batch/offline 분해 측정이며 API 응답속도 비교가 아니다.",
                "Retriever qrel 독립 검수는 보류 중이며 이 결과와 무관하다.",
            ],
        }
    )
    if not hint_safety["deployment_gate"]["passed"]:
        for decision in report["decision"].values():
            decision["decision"] = "REJECT_SAFETY_REGRESSION_KEEP_SPARSE"
    _private_json(output_dir / "summary.json", report)
    return report
