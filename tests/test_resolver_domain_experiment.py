import csv
import io
import json
from pathlib import Path

import numpy as np
import pytest

from chemiguard119.resolver_domain_experiment import (
    SOURCE_COLUMNS,
    apply_projection,
    candidate_safety,
    choose_epoch,
    enrich_corpus,
    make_benchmark,
    rank_metrics,
    rerank_pool,
    source_aliases,
    unambiguous_aliases,
)
from chemiguard119.utils import compact_text


def alias(cas="64-17-5", text="ethanol", kind="icis_primary_name"):
    return {"cas_number": cas, "alias_text": text, "alias_type": kind}


def test_source_intake_rejects_composite_and_shared_names():
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(SOURCE_COLUMNS)
    writer.writerows(
        [
            ["64-17-5", "에탄올", "ethanol;ethyl alcohol"],
            ["67-56-1", "메탄올", "shared"],
            ["64-17-5", "shared", "ethanol"],
            ["64-17-5|67-56-1", "혼합", "mixture"],
            ["64-17-6", "오류", "invalid"],
        ]
    )
    rows, audit = source_aliases(buf.getvalue().encode("cp949"))
    assert audit["invalid_or_composite_cas_rows"] == 2
    assert audit["ambiguous_surface_count"] == 1
    assert all(r["alias_text"] != "shared" for r in rows)
    assert len(rows) == 4
    assert audit["chemical_expert_reviewed"] is False


@pytest.mark.parametrize(
    "payload", [b"", b"wrong,columns\na,b\n", b"x" * (10 * 1024 * 1024 + 1)]
)
def test_source_intake_fails_closed(payload):
    with pytest.raises(ValueError):
        source_aliases(payload)


def test_enrichment_never_promotes_new_or_exact_only_cas():
    base = [alias()]
    artifact = {"rows": base + [alias("67-56-1", "methanol")]}
    official = [
        alias(text="에탄올"),
        alias("67-56-1", "메탄올"),
        alias("7732-18-5", "물"),
    ]
    enriched, audit = enrich_corpus(base, official, artifact)
    assert len(enriched) == 2
    assert audit["exact_only_aliases_quarantined"] == 1
    assert audit["new_cas_count_quarantined"] == 1
    assert audit["exact_gate_modified"] is False
    assert artifact["rows"] == base + [alias("67-56-1", "methanol")]


def test_conflicting_additions_are_quarantined_without_changing_base():
    base = [alias(), alias("67-56-1", "methanol")]
    enriched, audit = enrich_corpus(base, [alias("67-56-1", "ethanol")], {"rows": base})
    assert enriched == base
    assert audit["conflicting_aliases_quarantined"] == 1


def test_normalization_collisions_are_removed():
    clean, audit = unambiguous_aliases([alias(), alias("67-56-1", "ETHANOL")])
    assert not clean
    assert audit["ambiguous_surface_count"] == 1


def test_benchmark_is_cas_disjoint_and_has_no_self_retrieval():
    # CAS 문자열은 split key용 fixture다. 실제 intake의 checksum 검증과 별도 테스트.
    rows = [
        alias(f"fixture-{i}", f"name_{i}_{j}") for i in range(300) for j in range(3)
    ]
    result = make_benchmark(rows)
    assert result == make_benchmark(list(reversed(rows)))
    training_cas = {r["cas_number"] for r in result["training"]}
    dev_cas = {q["expected_cas"] for q in result["queries"]["dev"]}
    test_cas = {q["expected_cas"] for q in result["queries"]["test"]}
    assert not (training_cas & dev_cas or training_cas & test_cas or dev_cas & test_cas)
    heldout = {
        compact_text(q["query"]) for qs in result["queries"].values() for q in qs
    }
    corpus_text = {compact_text(r["alias_text"]) for r in result["corpus"]}
    assert not heldout & corpus_text
    assert result["audit"]["uses_incident_records_for_training"] is False


def test_reranker_uses_names_not_expected_cas_and_preserves_ties():
    pool = [
        {"cas_number": "64-17-5", "matched_alias": "ethanol", "rule_eligible": False},
        {"cas_number": "67-56-1", "matched_alias": "methanol", "rule_eligible": False},
    ]
    observed = []

    def score(pairs):
        observed.extend(pairs)
        return [0.5, 0.5]

    ranked = rerank_pool("불확실", pool, score)
    assert observed == [("불확실", "ethanol"), ("불확실", "methanol")]
    assert [r["cas_number"] for r in ranked] == [r["cas_number"] for r in pool]
    assert all(not r["rule_eligible"] for r in ranked)


@pytest.mark.parametrize("scores", [[float("nan")], [], [1.0, 2.0]])
def test_reranker_rejects_invalid_scores(scores):
    with pytest.raises(ValueError):
        rerank_pool(
            "query",
            [{"cas_number": "64-17-5", "matched_alias": "ethanol"}],
            lambda _: scores,
        )


def test_metrics_denominators_and_duplicate_cas():
    metrics = rank_metrics([[{"cas_number": "a"}, {"cas_number": "b"}], []], ["b", "a"])
    assert metrics["count"] == 2
    assert metrics["top1_hits"] == 0 and metrics["top3_hits"] == 1
    assert metrics["mrr_at_20"] == 0.25
    with pytest.raises(ValueError):
        rank_metrics([[{"cas_number": "a"}] * 2], ["a"])


def test_identity_projection_and_epoch_selection():
    x = np.eye(4, dtype=np.float32)
    assert np.array_equal(apply_projection(x, np.ones((4, 2)), np.zeros((2, 4))), x)
    metric = {"top20_hits": 3, "top3_hits": 2}
    assert choose_epoch({0: metric, 5: metric}) == 0
    assert choose_epoch({0: metric, 5: {"top20_hits": 4, "top3_hits": 1}}) == 5


def test_safety_counts_violations():
    assert candidate_safety(
        [
            [
                {
                    "cas_number": "bad",
                    "rule_eligible": True,
                    "current_inventory_confirmed": True,
                }
            ]
        ],
        {"ok"},
    ) == {
        "rule_eligibility_violation_count": 1,
        "fuzzy_catalog_escape_count": 1,
        "current_inventory_violation_count": 1,
    }


def test_recorded_actual_results_preserve_denominators_and_limitations():
    path = Path(__file__).parents[1] / "data/evaluation/resolver_domain_2026-09-11.json"
    report = json.loads(path.read_text())
    assert report["runtime_changed"] is False
    assert report["server_cost_krw"] == 0
    assert report["enrichment"]["baseline"]["unseen_60"]["top3_hits"] == 24
    assert report["enrichment"]["enriched"]["unseen_60"]["top3_hits"] == 43
    assert report["reranker"]["reranked"]["unseen_60"]["top3_hits"] == 31
    assert report["projection"]["test_frozen"]["count"] == 264
    assert report["projection"]["test_selected"]["top3_hits"] == 145
    assert report["projection"]["learning"]["selected_epoch"] == 5
    audit = report["audit"]
    assert (
        audit["posthoc_2020_exposure"]["unseen_60"][
            "query_expected_pair_in_projection_training_count"
        ]
        == 27
    )
    assert audit["synonym_test_paired"]["top3"] == {"gained": 50, "lost": 11}
    assert not any(audit["checks"].values())
    assert audit["new_cas_activated"] is False
    for section in ("enrichment", "reranker", "projection"):
        assert not any(report[section]["safety"].values())
