from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from chemiguard119.incident import deterministic_parse, validate_parser_output
from chemiguard119.parser_uncertainty_cases import cases as uncertainty_cases
from chemiguard119.parser_uncertainty_evaluation import score_case
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
                (
                    "7664-41-7",
                    "암모니아",
                    "암모니아",
                    "CANONICAL_KO",
                    "TEST",
                    "VERIFIED",
                ),
                (
                    "67-64-1",
                    "가상의 제품 명칭",
                    "가상의제품명칭",
                    "ALIAS",
                    "TEST",
                    "VERIFIED",
                ),
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


def test_parser_recovers_long_authoritative_alias_with_asr_internal_spacing(
    resolver_artifact: dict,
) -> None:
    source = "차아 염소산 나트륨 탱크에서 누출 중이며, 옆 저장고에 염산이 있습니다."

    parsed = deterministic_parse(source, resolver_artifact)

    assert len(parsed["substance_mentions"]) == 2
    mention = parsed["substance_mentions"][0]
    assert mention["surface_text"] == "차아 염소산 나트륨"
    assert mention["resolver"]["candidates"][0]["cas_number"] == "7681-52-9"
    assert mention["resolver"]["requires_responder_confirmation"] is True
    assert mention["resolver"]["rule_input_eligible"] is False
    assert validate_parser_output(parsed, source) == []


def test_parser_does_not_apply_internal_spacing_to_short_alias(
    resolver_artifact: dict,
) -> None:
    parsed = deterministic_parse("나 트륨 누출 신고", resolver_artifact)

    assert parsed["substance_mentions"] == []


def test_parser_does_not_apply_internal_spacing_to_common_alias(
    resolver_artifact: dict,
) -> None:
    parsed = deterministic_parse("가 상의 제품 명칭 누출 신고", resolver_artifact)

    assert parsed["substance_mentions"] == []


def test_parser_does_not_promote_spaced_alias_embedded_in_product_class(
    resolver_artifact: dict,
) -> None:
    parsed = deterministic_parse("차아 염소산 나트륨성 세척제 누출", resolver_artifact)

    assert parsed["substance_mentions"] == []


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


@pytest.mark.parametrize("case", uncertainty_cases(), ids=lambda case: case["id"])
def test_uncertainty_design_contract_with_fixture_artifact(case, resolver_artifact):
    parsed = deterministic_parse(case["text"], resolver_artifact)
    assert score_case(case, parsed)["all_correct"]
    assert parsed["source_text"] == case["text"]
    assert validate_parser_output(parsed, case["text"]) == []
    assert all(
        m["resolver"]["requires_responder_confirmation"]
        for m in parsed["substance_mentions"]
    )
    assert not any(
        m["resolver"]["rule_input_eligible"] for m in parsed["substance_mentions"]
    )


def test_repeated_negative_and_positive_are_not_verified_contradiction(
    resolver_artifact,
):
    parsed = deterministic_parse(
        "염산은 없습니다. 염산이 누출됩니다.", resolver_artifact
    )
    assert len(parsed["substance_mentions"]) == 2
    assert parsed["requires_statement_clarification"]
    assert parsed["statement_conflicts"][0]["not_a_verified_contradiction"]


def test_same_material_in_different_roles_is_not_by_itself_conflict(resolver_artifact):
    parsed = deterministic_parse(
        "염산이 누출됩니다. 옆 창고에 염산이 있습니다.", resolver_artifact
    )
    assert len(parsed["substance_mentions"]) == 2
    assert not parsed["requires_statement_clarification"]


@pytest.mark.parametrize("ending", ["없지만", "없으나", "없는데", "없으며"])
def test_concessive_negation_preserves_conflict_and_blocks_pair(
    resolver_artifact, ending
):
    from chemiguard119.action_examples import BOTH_CONFIRMED
    from chemiguard119.action_models import BriefRequest
    from chemiguard119.action_policy import BriefHeld, before_rule

    text = f"염산은 {ending} 염산이 누출됩니다."
    parsed = deterministic_parse(text, resolver_artifact)
    assert [m["assertion"] for m in parsed["substance_mentions"]] == [
        "NEGATED",
        "AFFIRMED",
    ]
    assert parsed["requires_statement_clarification"]
    assert parsed["source_text"] == text
    with pytest.raises(BriefHeld, match="STATEMENT_CLARIFICATION_REQUIRED"):
        before_rule(
            BriefRequest.model_validate(BOTH_CONFIRMED).effective_analysis(),
            {"parsed_report": parsed},
        )


@pytest.mark.parametrize("connector", ["이 아닌", "이 아니라", "은 아닌"])
def test_alternative_material_does_not_inherit_previous_negation(
    resolver_artifact, connector
):
    text = f"염산{connector} 질산이 누출됩니다."
    parsed = deterministic_parse(text, resolver_artifact)
    first, second = parsed["substance_mentions"]
    assert (first["surface_text"], first["assertion"]) == ("염산", "NEGATED")
    assert (second["surface_text"], second["assertion"], second["role"]) == (
        "질산",
        "AFFIRMED",
        "INCIDENT",
    )
    assert not parsed["requires_statement_clarification"]
    assert validate_parser_output(parsed, text) == []
    assert second["resolver"]["requires_responder_confirmation"]
    assert not second["resolver"]["rule_input_eligible"]


def test_unicode_expansion_and_emoji_keep_original_offsets(resolver_artifact):
    text = "ß 📞 차아 염소산 나트륨 탱크에서 누출됩니다."
    parsed = deterministic_parse(text, resolver_artifact)
    assert len(parsed["substance_mentions"]) == 1
    mention = parsed["substance_mentions"][0]
    assert text[mention["start"] : mention["end"]] == "차아 염소산 나트륨"
    assert validate_parser_output(parsed, text) == []


def test_validator_rejects_incorrect_offsets(resolver_artifact):
    parsed = deterministic_parse("염산 누출", resolver_artifact)
    parsed["substance_mentions"][0]["end"] += 1
    assert "물질 표현의 원문 구간이 일치하지 않습니다." in validate_parser_output(
        parsed, "염산 누출"
    )


def test_repeated_claims_preserve_mentions_but_bound_conflict_list(resolver_artifact):
    text = ("염산 없음. 염산. " * 400)[:4000]
    parsed = deterministic_parse(text, resolver_artifact)
    assert len(parsed["substance_mentions"]) == 727
    assert len(parsed["statement_conflicts"]) == 64
    assert parsed["statement_conflict_limit_reached"]
    assert parsed["requires_statement_clarification"]
    assert validate_parser_output(parsed, text) == []
