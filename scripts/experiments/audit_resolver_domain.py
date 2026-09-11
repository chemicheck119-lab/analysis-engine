#!/usr/bin/env python3
"""고정 실험 후 누수·회귀·별칭 직접 노출을 감사한다. 재학습/설정 선택 없음."""

import argparse
import json
from pathlib import Path

from chemiguard119.incident_replay import _private_json
from chemiguard119.resolver_domain_experiment import choose_epoch, rank_metrics
from chemiguard119.utils import compact_text, sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument(
        "--report-name",
        choices=("final-audit.json", "final-audit-v2.json"),
        default="final-audit.json",
    )
    args = parser.parse_args()
    out = args.output.resolve()
    if "private-data" not in out.parts or out.is_relative_to(
        Path(__file__).resolve().parents[2]
    ):
        raise ValueError("Git 밖 private-data 출력만 허용")

    def read(name):
        return json.loads((out / name).read_text())

    prepared = read("prepared-manifest.json")
    for name, expected in prepared["files"].items():
        if sha256_file(out / name) != expected:
            raise ValueError("준비 artifact 변조")
    if (
        sha256_file(args.previous / "rows.private.json")
        != prepared["input_hashes"]["rows.private.json"]
    ):
        raise ValueError("이전 사례 hash 불일치")
    rows = json.loads((args.previous / "rows.private.json").read_text())["rows"]
    ds = read("datasets.private.json")
    pairs = read("rankings.private.json")
    summary = read("enrichment-report.json")
    learning = read("projection-report.json")
    benchmark = ds["benchmark"]
    train_cas = {r["cas_number"] for r in benchmark["training"]}
    train_text = {compact_text(r["alias_text"]) for r in benchmark["training"]}
    train_pairs = {
        (r["cas_number"], compact_text(r["alias_text"])) for r in benchmark["training"]
    }
    corpus_text = {compact_text(r["alias_text"]) for r in benchmark["corpus"]}
    dev_cas = {r["expected_cas"] for r in benchmark["queries"]["dev"]}
    test_cas = {r["expected_cas"] for r in benchmark["queries"]["test"]}
    heldout_text = {
        compact_text(r["query"]) for q in benchmark["queries"].values() for r in q
    }
    additions = ds["enriched"][summary["enrichment"]["base_alias_count"] :]
    added_pairs = {(r["cas_number"], compact_text(r["alias_text"])) for r in additions}
    official_cas = {r["cas_number"] for r in ds["official"]}
    official_rows = [r for r in rows if r["cohort"] == "official_2020"]
    missing = [
        r for r in official_rows if r["unseen_surface"] and not r["cas_in_artifact"]
    ]
    exposures = {}
    for group in ("all_419", "unseen_60"):
        indices = [
            i
            for i, r in enumerate(rows)
            if r["cohort"] == "official_2020"
            and (group == "all_419" or r["unseen_surface"])
        ]
        exposed = [
            i
            for i in indices
            if (rows[i]["expected_cas"], compact_text(rows[i]["query"])) in added_pairs
        ]
        gained = [
            i
            for i in indices
            if rows[i]["expected_cas"]
            not in [r["cas_number"] for r in pairs["baseline"][i][:3]]
            and rows[i]["expected_cas"]
            in [r["cas_number"] for r in pairs["enriched"][i][:3]]
        ]
        exposures[group] = {
            "new_alias_direct_match_count": len(exposed),
            "top3_gained_count": len(gained),
            "gained_with_direct_match_count": len(set(gained) & set(exposed)),
            "gained_without_direct_match_count": len(set(gained) - set(exposed)),
            "query_expected_pair_in_projection_training_count": sum(
                (rows[i]["expected_cas"], compact_text(rows[i]["query"])) in train_pairs
                for i in indices
            ),
        }
    learning_rows = read("projection-rows.private.json")
    posthoc_slices = {}
    for label, match in (
        ("training_expression_exposed", True),
        ("training_expression_not_exposed", False),
    ):
        indices = [
            i
            for i, r in enumerate(rows)
            if r["cohort"] == "official_2020"
            and r["unseen_surface"]
            and (
                ((r["expected_cas"], compact_text(r["query"])) in train_pairs) == match
            )
        ]
        posthoc_slices[label] = {
            name: rank_metrics(
                [ranking[i] for i in indices],
                [rows[i]["expected_cas"] for i in indices],
            )
            for name, ranking in (
                ("frozen", pairs["baseline"]),
                ("projection", learning_rows["official_adapted"]),
            )
        }
    language_slices = {}
    for label, korean in (("contains_hangul", True), ("no_hangul", False)):
        indices = [
            i
            for i, r in enumerate(benchmark["queries"]["test"])
            if any("가" <= c <= "힣" for c in r["query"]) == korean
        ]
        language_slices[label] = {
            name: rank_metrics(
                [ranking[i] for i in indices],
                [benchmark["queries"]["test"][i]["expected_cas"] for i in indices],
            )
            for name, ranking in (
                ("frozen", learning_rows["benchmark_frozen"]),
                ("projection", learning_rows["benchmark_selected"]),
            )
        }
    test_paired = {}
    for k in (1, 3, 20):
        before = [
            q["expected_cas"] in [r["cas_number"] for r in ranking[:k]]
            for q, ranking in zip(
                benchmark["queries"]["test"],
                learning_rows["benchmark_frozen"],
                strict=True,
            )
        ]
        after = [
            q["expected_cas"] in [r["cas_number"] for r in ranking[:k]]
            for q, ranking in zip(
                benchmark["queries"]["test"],
                learning_rows["benchmark_selected"],
                strict=True,
            )
        ]
        test_paired[f"top{k}"] = {
            "gained": sum(b and not a for a, b in zip(before, after, strict=True)),
            "lost": sum(a and not b for a, b in zip(before, after, strict=True)),
        }
    checks = {
        "cas_split_overlap_count": len(train_cas & (dev_cas | test_cas))
        + len(dev_cas & test_cas),
        "heldout_expression_in_training_count": len(heldout_text & train_text),
        "heldout_expression_in_corpus_count": len(heldout_text & corpus_text),
        "epoch_selection_mismatch_count": int(
            choose_epoch(
                {int(k): v for k, v in learning["learning"]["dev_checkpoints"].items()}
            )
            != learning["learning"]["selected_epoch"]
        ),
    }
    if any(checks.values()):
        raise ValueError("독립 감사 누수/선택 검사 실패")
    report = {
        "fact_status": "부분 구현 또는 개발용 데모",
        "checks": checks,
        "posthoc_2020_exposure": exposures,
        "synonym_test_paired": test_paired,
        "posthoc_unseen_training_exposure_slices": posthoc_slices,
        "posthoc_synonym_test_language_slices": language_slices,
        "slice_results_used_for_selection": False,
        "unseen_catalog_missing_count": len(missing),
        "missing_cas_found_in_new_official_source_count": sum(
            r["expected_cas"] in official_cas for r in missing
        ),
        "new_cas_activated": False,
        "audit_is_human_chemical_review": False,
        "artifact_hashes": {
            p.name: sha256_file(p) for p in sorted(out.iterdir()) if p.is_file()
        },
        "source_code_revision": "6ab87f5",
        "audit_script_sha256": sha256_file(Path(__file__)),
    }
    _private_json(out / args.report_name, report)
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "artifact_hashes"},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
