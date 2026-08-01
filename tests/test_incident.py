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
                ("7782-50-5", "염소", "염소", "CANONICAL_KO", "TEST", "VERIFIED"),
                ("7697-37-2", "질산", "질산", "CANONICAL_KO", "TEST", "VERIFIED"),
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


@pytest.mark.parametrize(
    "source",
    [
        "염산염 누출 신고",
        "염산성 세척제 누출",
    ],
)
def test_parser_does_not_promote_embedded_korean_alias(
    resolver_artifact: dict,
    source: str,
) -> None:
    parsed = deterministic_parse(source, resolver_artifact)

    assert parsed["substance_mentions"] == []
    assert parsed["missing_fields"] == ["substance"]
    assert parsed["needs_substance_confirmation"] is True


@pytest.mark.parametrize(
    ("source", "surface"),
    [
        ("염소가스가 누출됐습니다.", "염소"),
        ("질산용기에서 유출됐습니다.", "질산"),
    ],
)
def test_parser_accepts_safe_material_equipment_compounds(
    resolver_artifact: dict,
    source: str,
    surface: str,
) -> None:
    parsed = deterministic_parse(source, resolver_artifact)

    assert [item["surface_text"] for item in parsed["substance_mentions"]] == [surface]
    assert parsed["needs_substance_confirmation"] is True


def test_parser_does_not_treat_product_class_as_element_identity(
    resolver_artifact: dict,
) -> None:
    parsed = deterministic_parse("염소소독제 냄새가 납니다.", resolver_artifact)

    assert parsed["substance_mentions"] == []


@pytest.mark.parametrize(
    "source",
    [
        "약품이 작업자에게 비산됐습니다.",
        "용액이 탱크 밖으로 넘쳐 바닥에 흘렀습니다.",
        "배관에서 가스가 누설됐습니다.",
    ],
)
def test_parser_recognizes_direct_release_expressions(
    resolver_artifact: dict,
    source: str,
) -> None:
    parsed = deterministic_parse(source, resolver_artifact)

    assert "LEAK" in parsed["incident_types"]


@pytest.mark.parametrize("source", ["누출 없음", "화재 미발생", "폭발은 아닙니다"])
def test_parser_does_not_promote_negated_incident_types(
    resolver_artifact: dict,
    source: str,
) -> None:
    parsed = deterministic_parse(source, resolver_artifact)

    assert parsed["incident_types"] == ["UNKNOWN"]


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
