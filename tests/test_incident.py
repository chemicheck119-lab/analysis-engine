from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chemiguard119.incident import deterministic_parse, validate_parser_output
from chemiguard119.resolver import load_resolver, train_resolver


@pytest.fixture()
def resolver_artifact(tmp_path: Path) -> dict:
    db_path = tmp_path / "incident-resolver.sqlite"
    with sqlite3.connect(db_path) as connection:
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
                    "차아염소산 나트륨",
                    "차아염소산나트륨",
                    "CANONICAL_KO",
                    "TEST",
                    "VERIFIED",
                ),
                (
                    "7758-19-2",
                    "아염소산나트륨",
                    "아염소산나트륨",
                    "CANONICAL_KO",
                    "TEST",
                    "VERIFIED",
                ),
                ("7440-23-5", "나트륨", "나트륨", "CANONICAL_KO", "TEST", "VERIFIED"),
                ("7647-01-0", "염산", "염산", "ALIAS", "TEST", "VERIFIED"),
            ],
        )
    model_path = tmp_path / "incident-resolver.joblib"
    train_resolver(db_path, model_path)
    return load_resolver(model_path)


def _mentions_by_surface(payload: dict) -> dict[str, dict]:
    return {item["surface_text"]: item for item in payload["substance_mentions"]}


def test_parser_preserves_negated_substance_mention(resolver_artifact: dict) -> None:
    source = "염산은 없습니다. 차아염소산나트륨 탱크에서 누출 중입니다."

    parsed = deterministic_parse(source, resolver_artifact)
    mentions = _mentions_by_surface(parsed)

    assert parsed["incident_types"] == ["LEAK"]
    assert mentions["염산"]["role"] == "NEGATED"
    assert mentions["염산"]["assertion"] == "NEGATED"
    assert mentions["차아염소산나트륨"]["role"] == "INCIDENT"
    assert mentions["차아염소산나트륨"]["assertion"] == "AFFIRMED"
    assert validate_parser_output(parsed, source) == []


def test_parser_separates_incident_and_nearby_facility_substances(
    resolver_artifact: dict,
) -> None:
    source = "차아염소산나트륨 탱크에서 누출 중이며, 옆 저장고에 염산이 있습니다."

    parsed = deterministic_parse(source, resolver_artifact)
    mentions = _mentions_by_surface(parsed)

    assert mentions["차아염소산나트륨"]["role"] == "INCIDENT"
    assert mentions["염산"]["role"] == "FACILITY"


def test_parser_does_not_extract_nested_shorter_substance_alias(
    resolver_artifact: dict,
) -> None:
    source = "차아염소산나트륨 저장탱크에서 누출 중입니다."

    parsed = deterministic_parse(source, resolver_artifact)

    assert [item["surface_text"] for item in parsed["substance_mentions"]] == [
        "차아염소산나트륨"
    ]


def test_parser_prefers_longest_exact_alias_despite_source_spacing(
    resolver_artifact: dict,
) -> None:
    source = "차아염소산나트륨 저장탱크 누출 중입니다."

    parsed = deterministic_parse(source, resolver_artifact)

    assert len(parsed["substance_mentions"]) == 1
    mention = parsed["substance_mentions"][0]
    assert mention["surface_text"] == "차아염소산나트륨"
    assert mention["resolver"]["candidates"][0]["cas_number"] == "7681-52-9"


def test_parser_validator_blocks_mentions_not_grounded_in_source() -> None:
    source = "염산 누출 의심"
    hallucinated = {
        "incident_types": ["LEAK"],
        "substance_mentions": [
            {
                "surface_text": "차아염소산나트륨",
                "role": "FACILITY",
                "assertion": "AFFIRMED",
            }
        ],
    }

    errors = validate_parser_output(hallucinated, source)

    assert errors == ["원문에 없는 물질 표현: 차아염소산나트륨"]


def test_parser_validator_blocks_risk_or_decision_fields() -> None:
    payload = {
        "incident_types": ["LEAK"],
        "substance_mentions": [],
        "severity": "HIGH_RISK",
        "recommended_response": "주수",
    }

    errors = validate_parser_output(payload, "물질 누출")

    assert "parser가 금지된 위험판정·결정 필드를 출력했습니다." in errors
