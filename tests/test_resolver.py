from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from chemiguard119.resolver import (
    RUNTIME_INDEX_KEY,
    evaluate_resolver,
    load_resolver,
    resolve_substance,
    select_evidence_cas_hint,
    select_evidence_cas_hint_from_text,
    train_resolver,
)


@pytest.fixture()
def resolver_artifact(tmp_path: Path) -> dict:
    db_path = tmp_path / "resolver.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE substance (
                cas_number TEXT PRIMARY KEY,
                catalog_scope TEXT NOT NULL,
                has_kosha_detail INTEGER NOT NULL,
                resolver_candidate_only INTEGER NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO substance VALUES (?, ?, ?, ?)",
            [
                ("7681-52-9", "KOSHA_CORE_WITH_DETAIL", 1, 0),
                ("7647-01-0", "KOSHA_CORE_WITH_DETAIL", 1, 0),
                ("64-17-5", "KOSHA_CORE_WITH_DETAIL", 1, 0),
                ("67-56-1", "KOSHA_CORE_WITH_DETAIL", 1, 0),
                ("7732-18-5", "ICIS_PUBLIC_CATALOG_CANDIDATE", 0, 1),
            ],
        )
        connection.execute(
            """
            CREATE TABLE alias (
                cas_number TEXT NOT NULL,
                alias_text TEXT NOT NULL,
                normalized_text TEXT NOT NULL,
                alias_type TEXT NOT NULL,
                source TEXT,
                verification_status TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO alias VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "7681-52-9",
                    "차아염소산나트륨",
                    "차아염소산나트륨",
                    "canonical_name_ko",
                    "KOSHA",
                    "SOURCE_EXACT",
                ),
                (
                    "7681-52-9",
                    "상용표백제",
                    "상용표백제",
                    "configured_alias",
                    "PROJECT",
                    "PROJECT_CONFIG_CANDIDATE",
                ),
                ("7647-01-0", "염산", "염산", "search_name", "KOSHA", "SOURCE_EXACT"),
                (
                    "7647-01-0",
                    "염화수소",
                    "염화수소",
                    "kosha_name",
                    "KOSHA",
                    "SOURCE_EXACT",
                ),
                (
                    "64-17-5",
                    "에탄올",
                    "에탄올",
                    "canonical_name_ko",
                    "KOSHA",
                    "SOURCE_EXACT",
                ),
                ("64-17-5", "알코올", "알코올", "common_name", "TEST", "VERIFIED"),
                (
                    "67-56-1",
                    "메탄올",
                    "메탄올",
                    "canonical_name_ko",
                    "KOSHA",
                    "SOURCE_EXACT",
                ),
                ("67-56-1", "알코올", "알코올", "common_name", "TEST", "VERIFIED"),
                (
                    "7732-18-5",
                    "물",
                    "물",
                    "icis_primary_name",
                    "13_ICIS.csv",
                    "PUBLIC_CATALOG_CANDIDATE",
                ),
            ],
        )

    model_path = tmp_path / "resolver.joblib"
    summary = train_resolver(db_path, model_path)
    assert summary["alias_count"] == 9
    assert summary["substance_count"] == 5
    assert model_path.is_file()
    return load_resolver(model_path)


def test_resolver_accepts_checksum_valid_cas_as_exact_identifier(
    resolver_artifact: dict,
) -> None:
    result = resolve_substance("7681-52-9", resolver_artifact)

    assert result["status"] == "EXACT_IDENTIFIER_MATCH"
    assert result["input_class"] == "AUTHORITATIVE_IDENTIFIER"
    assert result["requires_responder_confirmation"] is True
    assert result["confirmation_reason"] == "IDENTITY_EXACT_PRESENCE_UNCONFIRMED"
    assert result["rule_input_eligible"] is False
    assert result["candidates"][0]["cas_number"] == "7681-52-9"
    assert result["candidates"][0]["match_type"] == "CAS_EXACT"
    assert result["candidates"][0]["has_kosha_detail"] is True
    assert result["candidates"][0]["rule_eligible"] is False


def test_loaded_resolver_reuses_one_runtime_normalization_index(
    resolver_artifact: dict,
) -> None:
    runtime_index = resolver_artifact[RUNTIME_INDEX_KEY]

    resolve_substance("염산", resolver_artifact)
    resolve_substance("7647-01-0", resolver_artifact)

    assert resolver_artifact[RUNTIME_INDEX_KEY] is runtime_index
    assert runtime_index["cas_rows"]["7647-01-0"]
    assert runtime_index["exact_aliases"]["염산"]


def test_resolver_keeps_name_exact_match_as_confirmation_candidate(
    resolver_artifact: dict,
) -> None:
    result = resolve_substance("차아염소산 나트륨", resolver_artifact)

    assert result["status"] == "EXACT_ALIAS_CANDIDATE"
    assert result["input_class"] == "AUTHORITATIVE_ALIAS"
    assert result["requires_responder_confirmation"] is True
    assert result["candidates"][0]["cas_number"] == "7681-52-9"
    assert result["candidates"][0]["match_type"] == "UNIQUE_ALIAS_EXACT"
    assert result["candidates"][0]["authority_level"] == "PUBLIC_AUTHORITY_SOURCE"


def test_resolver_classifies_generic_configured_alias_without_word_hardcoding(
    resolver_artifact: dict,
) -> None:
    result = resolve_substance("상용표백제", resolver_artifact)

    assert result["status"] == "EXACT_ALIAS_CANDIDATE"
    assert result["input_class"] == "PRODUCT_OR_COMMON_NAME"
    assert result["candidates"][0]["matched_alias_type"] == "configured_alias"
    assert result["candidates"][0]["matched_alias_class"] == "PRODUCT_OR_COMMON_NAME"
    assert result["requires_responder_confirmation"] is True
    assert select_evidence_cas_hint(result) is None


def test_resolver_reports_one_expression_mapped_to_multiple_cas_as_ambiguous(
    resolver_artifact: dict,
) -> None:
    result = resolve_substance("알코올", resolver_artifact)

    assert result["status"] == "AMBIGUOUS_ALIAS"
    assert result["input_class"] == "AMBIGUOUS_EXPRESSION"
    assert result["confirmation_reason"] == "MULTIPLE_CAS_FOR_EXPRESSION"
    assert {item["cas_number"] for item in result["candidates"]} == {
        "64-17-5",
        "67-56-1",
    }
    assert select_evidence_cas_hint(result) is None


def test_only_authoritative_single_exact_match_can_narrow_evidence_search(
    resolver_artifact: dict,
) -> None:
    exact_name = resolve_substance("염산", resolver_artifact)
    exact_cas = resolve_substance("7647-01-0", resolver_artifact)
    fuzzy = resolve_substance("염산 누출 대응", resolver_artifact)

    assert select_evidence_cas_hint(exact_name) == "7647-01-0"
    assert select_evidence_cas_hint(exact_cas) == "7647-01-0"
    assert select_evidence_cas_hint(fuzzy) is None
    assert (
        select_evidence_cas_hint_from_text("염산 누출 대응", resolver_artifact)
        == "7647-01-0"
    )
    assert (
        select_evidence_cas_hint_from_text("알코올 누출 대응", resolver_artifact)
        is None
    )
    assert (
        select_evidence_cas_hint_from_text("상용표백제 누출 대응", resolver_artifact)
        is None
    )


def test_embedded_ascii_authoritative_name_requires_token_boundary() -> None:
    artifact = {
        "rows": [
            {
                "cas_number": "64-17-5",
                "alias_text": "Ethanol",
                "alias_type": "canonical_en",
                "source": "KOSHA",
                "verification_status": "SOURCE_EXACT",
            }
        ]
    }

    assert select_evidence_cas_hint_from_text("Ethanol leak", artifact) == "64-17-5"
    assert select_evidence_cas_hint_from_text("methanolic solvent", artifact) is None


def test_mixed_primary_and_reported_alias_never_selects_one_evidence_cas() -> None:
    artifact = {
        "rows": [
            {
                "cas_number": "20654-88-0",
                "alias_text": "삼불화붕소",
                "alias_type": "icis_primary_name",
                "source": "13_ICIS.csv",
                "verification_status": "PUBLIC_CATALOG_CANDIDATE",
            },
            {
                "cas_number": "7637-07-2",
                "alias_text": "삼불화붕소",
                "alias_type": "icis_reported_alias",
                "source": "13_ICIS.csv",
                "verification_status": "PUBLIC_CATALOG_CANDIDATE",
            },
        ]
    }

    exact = resolve_substance("삼불화붕소", artifact)

    assert exact["status"] == "AMBIGUOUS_ALIAS"
    assert select_evidence_cas_hint(exact) is None
    assert select_evidence_cas_hint_from_text("삼불화붕소", artifact) is None
    assert select_evidence_cas_hint_from_text("삼불화붕소 누출 대응", artifact) is None


def test_internal_demo_authoritative_alias_cannot_narrow_evidence_search() -> None:
    artifact = {
        "rows": [
            {
                "cas_number": "7681-52-9",
                "alias_text": "SYNTHETIC-INTERNAL-FORMULA",
                "alias_type": "formula",
                "source": "substance_overrides.csv",
                "verification_status": "PROJECT_CONFIG_CANDIDATE",
            }
        ]
    }

    exact = resolve_substance("SYNTHETIC-INTERNAL-FORMULA", artifact)

    assert exact["status"] == "EXACT_ALIAS_CANDIDATE"
    assert exact["input_class"] == "AUTHORITATIVE_ALIAS"
    assert exact["candidates"][0]["authority_level"] == "PROJECT_CONFIG_CANDIDATE"
    assert select_evidence_cas_hint(exact) is None
    assert (
        select_evidence_cas_hint_from_text(
            "SYNTHETIC-INTERNAL-FORMULA leak response",
            artifact,
        )
        is None
    )


def test_resolver_exposes_icis_name_as_catalog_candidate_not_rule_input(
    resolver_artifact: dict,
) -> None:
    result = resolve_substance("물", resolver_artifact)

    candidate = result["candidates"][0]
    assert result["input_class"] == "AUTHORITATIVE_ALIAS"
    assert candidate["authority_level"] == "PUBLIC_CATALOG_CANDIDATE"
    assert candidate["catalog_scope"] == "ICIS_PUBLIC_CATALOG_CANDIDATE"
    assert candidate["has_kosha_detail"] is False
    assert candidate["catalog_candidate_only"] is True
    assert candidate["rule_eligible"] is False


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("7681-52-0", "INVALID_CAS_IDENTIFIER"),
        ("71-43-2", "VALID_CAS_NOT_IN_CATALOG"),
    ],
)
def test_resolver_does_not_fuzzy_match_invalid_or_unregistered_cas(
    resolver_artifact: dict,
    query: str,
    reason: str,
) -> None:
    result = resolve_substance(query, resolver_artifact)

    assert result["status"] == "UNRESOLVED"
    assert result["input_class"] == "UNRESOLVED"
    assert result["confirmation_reason"] == reason
    assert result["candidates"] == []


def test_resolver_returns_ranked_fuzzy_candidate_without_auto_confirmation(
    resolver_artifact: dict,
) -> None:
    result = resolve_substance("차아염소산나트륨 저장탱크 누출", resolver_artifact)

    assert result["status"] == "FUZZY_CANDIDATE"
    assert result["requires_responder_confirmation"] is True
    assert result["candidates"][0]["cas_number"] == "7681-52-9"
    assert result["candidates"][0]["match_type"] == "CHAR_TFIDF_CANDIDATE"
    # TF-IDF vocabulary outside the tiny fixture can be ignored, so a fuzzy
    # query may still have cosine score 1.0. Safety is conveyed by match_type
    # and the mandatory confirmation flag, not by treating the score as truth.
    assert 0.20 <= result["candidates"][0]["score"] <= 1.0


def test_evaluation_separates_candidate_hit_from_unique_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = {
        "rows": [
            {
                "cas_number": "64-17-5",
                "alias_text": "공통명",
                "alias_type": "icis_primary_name",
                "source": "13_ICIS.csv",
                "verification_status": "PUBLIC_CATALOG_CANDIDATE",
            },
            {
                "cas_number": "67-56-1",
                "alias_text": "공통명",
                "alias_type": "icis_reported_alias",
                "source": "13_ICIS.csv",
                "verification_status": "PUBLIC_CATALOG_CANDIDATE",
            },
        ]
    }
    evaluation_path = tmp_path / "resolver-evaluation.csv"
    with evaluation_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("query", "expected_cas", "query_type", "review_status"),
        )
        writer.writeheader()
        writer.writerow(
            {
                "query": "공통명",
                "expected_cas": "64-17-5",
                "query_type": "ambiguous_alias",
                "review_status": "TEST",
            }
        )
    monkeypatch.setattr(
        "chemiguard119.resolver.load_resolver",
        lambda _model_path: artifact,
    )

    report = evaluate_resolver(tmp_path / "unused.joblib", evaluation_path)

    assert report["metrics_version"] == "resolver-evaluation-v2"
    assert report["candidate_top1_hit_rate"] == 1.0
    assert report["candidate_top3_recall"] == 1.0
    assert report["unique_resolution_accuracy"] == 0.0
    assert report["top1_accuracy"] == 0.0
    assert report["ambiguous_case_count"] == 1
    assert report["rows"][0]["candidate_top1_hit"] is True
    assert report["rows"][0]["unique_resolution_correct"] is False
