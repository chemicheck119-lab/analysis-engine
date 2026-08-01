from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.evaluation.evaluate_material_ranker import evaluate


def test_self_retrieval_report_discloses_scope_and_non_probability(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "profiles.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE substance_profile(
                cas_number TEXT PRIMARY KEY,
                canonical_name_ko TEXT,
                canonical_name_en TEXT,
                physical_state TEXT,
                color TEXT,
                odor TEXT,
                use_description TEXT,
                source_url TEXT,
                document_version TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE substance_profile_fts USING fts5(
                cas_number UNINDEXED,
                canonical_name_ko,
                canonical_name_en,
                physical_state,
                color,
                odor,
                use_description,
                tokenize = 'unicode61'
            )
            """
        )
        rows = [
            (
                "64-17-5",
                "에탄올",
                "Ethanol",
                "휘발성 액체",
                "무색",
                "알코올 냄새",
                "용제",
            ),
            (
                "67-56-1",
                "메탄올",
                "Methanol",
                "휘발성 액체",
                "투명",
                "자극성 냄새",
                "연료",
            ),
        ]
        connection.executemany(
            "INSERT INTO substance_profile VALUES (?, ?, ?, ?, ?, ?, ?, '', '')",
            rows,
        )
        connection.executemany(
            "INSERT INTO substance_profile_fts VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    report = evaluate(db_path, max_cases=2)

    assert report["case_count"] >= 1
    assert report["evaluation_scope"] == "PROFILE_SELF_RETRIEVAL_REGRESSION_ONLY"
    assert report["is_independent_field_accuracy"] is False
    assert report["safety"]["candidate_score_is_probability"] is False
    assert report["safety"]["auto_confirmation_allowed"] is False
