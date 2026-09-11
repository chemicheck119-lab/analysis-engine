from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from chemiguard119.resolver import fit_resolver_rows, load_resolver, resolve_substance
from chemiguard119.resolver_dense_experiment import (
    _normalize_embeddings,
    _rank_dense,
    _rrf_fuse,
    build_dense_alias_corpus,
)
from chemiguard119.ulsan_resolver_comparison import (
    SYSTEMS,
    assert_baseline,
    compare_cases,
    comparison_decisions,
    fallback_result,
    metrics,
    paired_counts,
    preserve_sparse_gate,
    run_comparison,
)
from chemiguard119.utils import sha256_file


@pytest.fixture
def artifact(tmp_path: Path):
    rows = [
        {"cas_number": "64-17-5", "alias_text": "에탄올", "alias_type": "canonical_ko"},
        {"cas_number": "67-56-1", "alias_text": "메탄올", "alias_type": "canonical_ko"},
        {"cas_number": "64-17-5", "alias_text": "알코올", "alias_type": "alias"},
        {"cas_number": "67-56-1", "alias_text": "알코올", "alias_type": "alias"},
        {
            "cas_number": "67-64-1",
            "alias_text": "기록전용명",
            "alias_type": "source",
            "fuzzy_search_eligible": False,
        },
    ]
    model = tmp_path / "resolver.joblib"
    fit_resolver_rows(rows, model, training_metadata={"training_year_max": 2019})
    return load_resolver(model)


@pytest.mark.parametrize(
    "query",
    ["에탄올", "알코올", "64-17-5", "64-17-6", "7732-18-5", "", "  ", "기록전용명"],
)
def test_shared_gate_cannot_be_replaced_by_dense(artifact, query):
    sparse = resolve_substance(query, artifact)
    assert preserve_sparse_gate(query, sparse)


def test_fuzzy_query_is_not_treated_as_confirmation(artifact):
    sparse = resolve_substance("에탄오올", artifact)
    assert not preserve_sparse_gate("에탄오올", sparse)
    output = fallback_result(
        sparse,
        [
            {
                "cas_number": "67-56-1",
                "rule_eligible": True,
                "current_inventory_confirmed": True,
            }
        ],
    )
    assert output["requires_responder_confirmation"]
    assert not output["rule_input_eligible"]
    assert not output["current_inventory_confirmed"]
    assert not output["candidates"][0]["rule_eligible"]
    assert not output["candidates"][0]["current_inventory_confirmed"]
    assert output["status"] == "FUZZY_CANDIDATE"
    assert sparse["query"] == output["query"]


def test_corpus_excludes_source_only_numeric_cas_invalid_and_duplicates(artifact):
    artifact["rows"].extend(
        [
            {"cas_number": "64-17-5", "alias_text": "64-17-5", "alias_type": "cas"},
            {"cas_number": "64-17-6", "alias_text": "잘못된CAS"},
            {"cas_number": "64-17-5", "alias_text": "에탄올", "alias_type": "alias"},
        ]
    )
    corpus = build_dense_alias_corpus(artifact)
    assert len(corpus) == 4
    assert all(row["alias_type"] != "cas" for row in corpus)
    assert {row["cas_number"] for row in corpus} == {"64-17-5", "67-56-1"}


@pytest.mark.parametrize(
    "values",
    [
        np.zeros((2, 3)),
        np.full((2, 3), np.nan),
        np.full((2, 3), np.inf),
        np.ones((1, 3)),
        np.ones(2),
    ],
)
def test_embedding_invalid_values_are_rejected(values):
    with pytest.raises(ValueError):
        _normalize_embeddings(values, expected_rows=2)


def test_dense_ranking_uses_best_alias_per_cas_and_deterministic_tie():
    corpus = [
        {"cas_number": "67-56-1", "alias_text": "m"},
        {"cas_number": "64-17-5", "alias_text": "e1"},
        {"cas_number": "64-17-5", "alias_text": "e2"},
    ]
    result = _rank_dense(
        np.array([1, 0]), np.array([[1, 0], [0.5, 0.5], [1, 0]]), corpus, top_k=3
    )
    assert [row["cas_number"] for row in result] == ["64-17-5", "67-56-1"]
    assert result[0]["matched_alias"] == "e2"
    assert not any(row["rule_eligible"] for row in result)


def test_rrf_uses_ranks_not_incomparable_model_scores():
    sparse = [{"cas_number": "A", "score": 1000}, {"cas_number": "B", "score": 0}]
    dense = [{"cas_number": "B", "score": -1000}, {"cas_number": "A", "score": -9999}]
    result = _rrf_fuse(sparse, dense, top_k=3)
    assert [row["cas_number"] for row in result] == ["A", "B"]
    assert result[0]["score"] == round(1 / 61 + 1 / 62, 8)
    assert all(row["sources"] == ["DENSE", "SPARSE"] for row in result)


def test_full_comparison_preserves_gate_keeps_rows_private_and_separates_groups(
    artifact,
):
    calls = []

    def embed(texts):
        calls.append(len(texts))
        return np.array(
            [[1, 0] if "에탄" in text else [0, 1] for text in texts], dtype=np.float32
        )

    cases = [
        {
            "query": query,
            "expected_cas": cas,
            "cohort": "official_2020",
            "unseen_surface": index > 0,
            "unseen_cas": False,
        }
        for index, (query, cas) in enumerate(
            [
                ("에탄올", "64-17-5"),
                ("에탄오올", "64-17-5"),
                ("비공개시험명", "67-56-1"),
                ("기록전용명", "67-64-1"),
                ("64-17-6", "64-17-5"),
                ("알코올", "64-17-5"),
            ]
        )
    ]
    cases.append(
        {"query": "메탄올", "expected_cas": "67-56-1", "cohort": "internal_regression"}
    )
    report, rows, vectors = compare_cases(artifact, cases, embed)
    assert calls == [4, 7]
    assert vectors.shape == (4, 2)
    assert report["metrics_by_cohort"]["all"][SYSTEMS[0]]["case_count"] == 6
    assert (
        report["metrics_by_cohort"]["internal_regression"][SYSTEMS[0]]["case_count"]
        == 1
    )
    for row in rows:
        if row["preserved_gate"]:
            assert (
                row["systems"][SYSTEMS[0]]["candidates"]
                == row["systems"][SYSTEMS[1]]["candidates"]
                == row["systems"][SYSTEMS[2]]["candidates"]
            )
    for system in SYSTEMS:
        group = report["metrics_by_cohort"]["all"][system]
        assert group["rule_eligibility_violation_count"] == 0
        assert group["source_only_fuzzy_leak_count"] == 0
        assert group["common_gate_change_count"] == 0
        assert group["confirmation_policy_violation_count"] == 0
    assert "비공개시험명" not in json.dumps(report, ensure_ascii=False)
    assert "비공개시험명" in json.dumps(rows, ensure_ascii=False)
    assert not report["timing"]["is_service_latency_comparison"]


def test_mrr_and_paired_counts_are_at_three_not_full_depth():
    rows = []
    for a, b in [(1, None), (None, 2), (3, 1)]:
        row = {"cas_in_artifact": True, "systems": {}}
        for system, rank in zip(SYSTEMS, (a, b, a), strict=True):
            row["systems"][system] = {
                "rank": rank,
                "candidate_count": 3,
                "wrong_unique_exact": False,
                "rule_violation": False,
                "confirmation_violation": False,
                "source_only_fuzzy_leak": False,
                "common_gate_changed": False,
            }
        rows.append(row)
    assert metrics(rows, SYSTEMS[0])["mrr_at_3"] == round((1 + 1 / 3) / 3, 6)
    assert paired_counts(rows, SYSTEMS[1]) == {
        "top1": {"corrected_from_sparse": 1, "lost_from_sparse": 1},
        "top3": {"corrected_from_sparse": 1, "lost_from_sparse": 1},
    }
    assert metrics([], SYSTEMS[0])["top1_accuracy"] is None


@pytest.mark.parametrize(
    "regression,safety,expected",
    [
        (False, False, "CONDITIONAL_FURTHER_VALIDATION_ONLY"),
        (True, False, "REJECT_THIS_CONFIGURATION_KEEP_SPARSE"),
        (False, True, "REJECT_THIS_CONFIGURATION_KEEP_SPARSE"),
    ],
)
def test_adoption_never_ignores_regression_or_policy_violation(
    regression, safety, expected
):
    groups = {}
    for group in ("all", "unseen_surface", "internal_regression"):
        groups[group] = {}
        for system in SYSTEMS:
            groups[group][system] = {
                "top1_hit_count": 1,
                "top3_hit_count": 1 if system == SYSTEMS[0] else 2,
                "wrong_unique_exact_candidate_count": 0,
                "rule_eligibility_violation_count": 0,
                "confirmation_policy_violation_count": 0,
                "source_only_fuzzy_leak_count": 0,
                "common_gate_change_count": 0,
            }
    if regression:
        groups["all"][SYSTEMS[1]]["top1_hit_count"] = 0
    if safety:
        groups["all"][SYSTEMS[1]]["rule_eligibility_violation_count"] = 1
    result = comparison_decisions(groups)
    assert result[SYSTEMS[1]]["decision"] == expected
    assert not result[SYSTEMS[1]]["runtime_change_allowed"]


def test_baseline_mismatch_is_not_silently_reported_as_improvement():
    report = {
        "metrics_by_cohort": {
            "all": {
                SYSTEMS[0]: {
                    "case_count": 419,
                    "top1_hit_count": 375,
                    "top3_hit_count": 378,
                }
            }
        }
    }
    with pytest.raises(ValueError, match="기준선 불일치"):
        assert_baseline(report)


def test_modified_source_fails_before_model_loading_or_creating_output(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    archive = source / "source.zip"
    archive.write_bytes(b"wrong archive")
    before = sha256_file(archive)
    with pytest.raises(ValueError, match="hash 불일치"):
        run_comparison(
            source,
            tmp_path / "no-model",
            tmp_path / "no-embedding",
            tmp_path / "regression",
            tmp_path / "safety",
            tmp_path / "output",
        )
    assert not (tmp_path / "output").exists()
    assert sha256_file(archive) == before
