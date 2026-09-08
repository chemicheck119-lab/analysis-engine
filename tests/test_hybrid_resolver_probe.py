from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import joblib
import numpy as np

from chemiguard119.hybrid_resolver_probe import (
    build_dense_alias_corpus,
    evaluate_hybrid_probe,
    load_probe_cases,
)
from chemiguard119.resolver import load_resolver, train_resolver


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    db_path = tmp_path / "resolver.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE substance (cas_number TEXT PRIMARY KEY, catalog_scope TEXT, "
            "has_kosha_detail INTEGER, resolver_candidate_only INTEGER)"
        )
        connection.executemany(
            "INSERT INTO substance VALUES (?, ?, ?, ?)",
            [
                ("100-42-5", "PUBLIC", 1, 0),
                ("64-17-5", "PUBLIC", 1, 0),
                ("67-56-1", "PUBLIC", 1, 0),
            ],
        )
        connection.execute(
            "CREATE TABLE alias (cas_number TEXT, alias_text TEXT, normalized_text TEXT, "
            "alias_type TEXT, source TEXT, verification_status TEXT)"
        )
        connection.executemany(
            "INSERT INTO alias VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "100-42-5",
                    "Styrene",
                    "styrene",
                    "canonical_en",
                    "PUBLIC",
                    "SOURCE_EXACT",
                ),
                (
                    "64-17-5",
                    "에탄올",
                    "에탄올",
                    "canonical_ko",
                    "PUBLIC",
                    "SOURCE_EXACT",
                ),
                (
                    "67-56-1",
                    "메탄올",
                    "메탄올",
                    "canonical_ko",
                    "PUBLIC",
                    "SOURCE_EXACT",
                ),
            ],
        )
    model_path = tmp_path / "resolver.joblib"
    train_resolver(db_path, model_path)

    temporal = tmp_path / "temporal.json"
    temporal.write_text(
        json.dumps(
            {
                "validation": {
                    "adapted": {
                        "failure_examples": [
                            {"query": "Stylene", "expected_cas": "100-42-5"}
                        ]
                    }
                },
                "locked_test": {
                    "adapted": {
                        "failure_examples": [
                            {"query": "스틸렌", "expected_cas": "100-42-5"}
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    regression = tmp_path / "regression.csv"
    _write_csv(
        regression,
        ["query", "query_type", "expected_cas"],
        [{"query": "에탄올", "query_type": "name", "expected_cas": "64-17-5"}],
    )
    safety = tmp_path / "safety.csv"
    fields = [
        "case_id",
        "query",
        "query_type",
        "expected_behavior",
        "expected_cas",
        "review_status",
        "source_type",
        "source_reference",
        "split",
        "duplicate_group",
    ]
    _write_csv(
        safety,
        fields,
        [
            {
                "case_id": "S1",
                "query": "에탄올",
                "query_type": "exact",
                "expected_behavior": "ALLOW_EXACT_HINT",
                "expected_cas": "64-17-5",
                "review_status": "DRAFT",
                "source_type": "SYNTHETIC",
                "source_reference": "fixture",
                "split": "regression",
                "duplicate_group": "s1",
            },
            {
                "case_id": "S2",
                "query": "에탄올성 용제",
                "query_type": "boundary",
                "expected_behavior": "WITHHOLD_AUTO_HINT",
                "expected_cas": "",
                "review_status": "DRAFT",
                "source_type": "SYNTHETIC",
                "source_reference": "fixture",
                "split": "regression",
                "duplicate_group": "s2",
            },
            {
                "case_id": "S3",
                "query": "알코올",
                "query_type": "ambiguous",
                "expected_behavior": "PRESERVE_AMBIGUITY",
                "expected_cas": "64-17-5|67-56-1",
                "review_status": "DRAFT",
                "source_type": "SYNTHETIC",
                "source_reference": "fixture",
                "split": "regression",
                "duplicate_group": "s3",
            },
        ],
    )
    artifact = joblib.load(model_path)
    artifact["rows"].extend(
        [
            {
                "cas_number": "64-17-5",
                "alias_text": "알코올",
                "normalized_text": "알코올",
                "alias_type": "common_name",
                "source": "TEST",
                "verification_status": "VERIFIED",
            },
            {
                "cas_number": "67-56-1",
                "alias_text": "알코올",
                "normalized_text": "알코올",
                "alias_type": "common_name",
                "source": "TEST",
                "verification_status": "VERIFIED",
            },
            {
                "cas_number": "100-42-5",
                "alias_text": "source only",
                "normalized_text": "source only",
                "alias_type": "fire_incident_name_en",
                "source": "TEST",
                "verification_status": "SOURCE_EXACT_VALID_CAS",
                "fuzzy_search_eligible": False,
            },
        ]
    )
    joblib.dump(artifact, model_path)
    return model_path, temporal, regression, safety


def test_load_probe_cases_keeps_cohorts_separate(tmp_path: Path) -> None:
    model, temporal, regression, safety = _fixture(tmp_path)
    del model, safety

    cases = load_probe_cases(temporal, regression)

    assert [case["cohort"] for case in cases] == [
        "DEVELOPMENT_FAILURE_SAMPLE",
        "LOCKED_RESULT_EXPOSED_FAILURE_SAMPLE",
        "INTERNAL_REGRESSION",
    ]


def test_dense_corpus_excludes_cas_and_source_only_aliases(tmp_path: Path) -> None:
    model, _temporal, _regression, _safety = _fixture(tmp_path)

    corpus = build_dense_alias_corpus(load_resolver(model))

    assert "source only" not in {row["alias_text"] for row in corpus}
    assert all(row["alias_type"] != "cas" for row in corpus)


def test_probe_preserves_exact_contract_and_blocks_runtime_adoption(
    tmp_path: Path,
) -> None:
    model, temporal, regression, safety = _fixture(tmp_path)
    vectors = {
        "Styrene": np.array([1.0, 0.0, 0.0]),
        "에탄올": np.array([0.0, 1.0, 0.0]),
        "메탄올": np.array([0.0, 0.0, 1.0]),
        "알코올": np.array([0.0, 0.5, 0.5]),
        "Stylene": np.array([1.0, 0.0, 0.0]),
        "스틸렌": np.array([1.0, 0.0, 0.0]),
    }

    def embed(texts: list[str]) -> np.ndarray:
        return np.stack([vectors[text] for text in texts])

    report = evaluate_hybrid_probe(
        resolver_model_path=model,
        temporal_snapshot_path=temporal,
        regression_path=regression,
        safety_evaluation_path=safety,
        embed=embed,
        embedding_model={"model_reference": "fixture"},
    )

    assert (
        report["metrics_by_cohort"]["DEVELOPMENT_FAILURE_SAMPLE"]["B_DENSE"][
            "top1_accuracy"
        ]
        == 1.0
    )
    assert (
        report["metrics_by_cohort"]["INTERNAL_REGRESSION"]["C_SPARSE_DENSE_RRF"][
            "top1_accuracy"
        ]
        == 1.0
    )
    assert (
        report["gate"]["runtime_adoption_decision"]
        == "NOT_ELIGIBLE_MISSING_FULL_TEMPORAL_SOURCE"
    )
    assert report["safety"]["existing_hint_gate_passed"] is True
    assert report["safety"]["runtime_default_changed"] is False
