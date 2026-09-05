from __future__ import annotations

import json
from pathlib import Path

import pytest

from chemiguard119.stt_downstream_evaluation import (
    DownstreamEvaluationError,
    build_report,
    evaluate_pairs,
    load_private_records,
    load_speech_summary,
    write_outputs,
)


def _response(
    *mentions: tuple[str, tuple[str, ...], str | None], unsafe: bool = False
) -> dict:
    parser_mentions = []
    candidates = []
    for surface, cas_numbers, hint in mentions:
        parser_mentions.append(
            {
                "surface_text": surface,
                "role": "INCIDENT",
                "assertion": "AFFIRMED",
                "resolver": {
                    "candidates": [
                        {"cas_number": cas_number} for cas_number in cas_numbers
                    ]
                },
            }
        )
        candidates.append(
            {
                "surface_text": surface,
                "role": "INCIDENT",
                "evidence_cas_hint": hint,
                "requires_responder_confirmation": not unsafe,
                "rule_eligible": unsafe,
                "risk_determination_allowed": False,
                "current_inventory_confirmed": False,
            }
        )
    model_outputs = {
        "parser": {"substance_mentions": parser_mentions},
        "substance_candidates": candidates,
    }
    if unsafe:
        model_outputs["risk_level"] = "HIGH"
    return {
        "state": "AWAITING_SUBSTANCE_CONFIRMATION",
        "model_outputs": model_outputs,
        "confirmation_gate": {
            "incident_confirmed": False,
            "facility_confirmed": False,
            "all_required_confirmed": False,
            "rule_execution_allowed": False,
        },
        "conflict_review": {
            "executed": unsafe,
            "status": ("COMPLETED" if unsafe else "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"),
        },
    }


def _rows() -> list[dict]:
    return [
        {
            "record_key": "aaaaaaaaaaaaaaaa",
            "variant": "baseline",
            "status": "completed",
            "reference": "염산 누출",
            "hypothesis": "염산 누출",
        },
        {
            "record_key": "bbbbbbbbbbbbbbbb",
            "variant": "baseline",
            "status": "completed",
            "reference": "메탄올 화재",
            "hypothesis": "에탄올 화재",
        },
    ]


def test_evaluation_calls_out_candidate_retention_not_cas_accuracy() -> None:
    def analyze(text: str, _request_id: str) -> dict:
        if "염산" in text:
            return _response(("염산", ("7647-01-0",), "7647-01-0"))
        if "메탄올" in text:
            return _response(("메탄올", ("67-56-1",), "67-56-1"))
        return _response(("에탄올", ("64-17-5",), "64-17-5"))

    metrics, private_rows = evaluate_pairs(_rows(), analyze, workers=2)

    parser = metrics["parser_silver"]
    resolver = metrics["resolver_silver"]
    assert parser["reference_parser_exact_mention_retention"]["rate"] == 0.5
    assert resolver["reference_candidate_top1_retention"]["rate"] == 0.5
    assert resolver["reference_candidate_top3_retention"]["rate"] == 0.5
    assert resolver["reference_inconsistent_auto_hint_count"] == 1
    assert resolver["cas_ground_truth_available"] is False
    assert resolver["is_cas_accuracy_evaluation"] is False
    assert resolver["wrong_single_cas_promotion_ground_truth_count"] is None
    assert metrics["safety"]["two_cas_gate_violation_count"] == 0
    serialized = json.dumps(private_rows, ensure_ascii=False)
    assert "염산 누출" not in serialized
    assert "염산" not in serialized


def test_safety_contract_violations_are_counted() -> None:
    def analyze(_text: str, request_id: str) -> dict:
        return _response(
            ("염산", ("7647-01-0",), "7647-01-0"),
            unsafe="hypothesis" in request_id,
        )

    metrics, _ = evaluate_pairs([_rows()[0]], analyze, workers=1)
    safety = metrics["safety"]
    assert safety["evaluated_hypothesis_response_count"] == 1
    assert safety["rule_execution_before_confirmation_count"] == 1
    assert safety["candidate_promotion_violation_count"] == 1
    assert safety["unconfirmed_risk_output_violation_count"] == 1


def test_failed_stt_is_kept_in_denominator_without_empty_api_call() -> None:
    rows = [_rows()[0] | {"status": "failed", "hypothesis": ""}]
    calls = []

    def analyze(text: str, _request_id: str) -> dict:
        calls.append(text)
        return _response(("염산", ("7647-01-0",), "7647-01-0"))

    metrics, _ = evaluate_pairs(rows, analyze, workers=1)
    assert calls == ["염산 누출"]
    assert metrics["stt_transcript_unavailable_count"] == 1
    assert metrics["hypothesis_model_api_error_count"] == 0
    assert metrics["hypothesis_analysis_unavailable_count"] == 1
    assert (
        metrics["resolver_silver"]["candidate_coverage_on_reference_positive_records"][
            "rate"
        ]
        == 0.0
    )


def test_loaders_bind_baseline_rows_to_speech_summary(tmp_path: Path) -> None:
    records = tmp_path / "records.private.jsonl"
    records.write_text(
        json.dumps(_rows()[0], ensure_ascii=False) + "\n", encoding="utf-8"
    )
    rows = load_private_records(records)
    summary_path = tmp_path / "summary.json"
    summary = {
        "schema_version": "1.0.0",
        "usage_role": "evaluation",
        "evidence_scope": "AIHub emergency-call proxy; not field-radio validation",
        "dataset": {"record_count": 1},
        "runtime": {
            "variants": ["baseline"],
            "implementation": "faster-whisper",
            "version": "1.2.1",
            "model": "small",
            "requested_device": "cpu",
            "device": "cpu",
            "compute_type": "int8",
        },
        "variants": {"baseline": {}},
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    assert load_speech_summary(summary_path, expected_records=len(rows)) == summary

    records.write_text(
        json.dumps(_rows()[0] | {"variant": "hotwords"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(DownstreamEvaluationError, match="baseline"):
        load_private_records(records)


def test_public_report_has_explicit_claim_boundary_and_no_transcript(
    tmp_path: Path,
) -> None:
    speech_summary = {
        "dataset": {
            "dataset_id": "aihub-71768-seoul-fire",
            "dataset_version": "2026-08-27",
            "evaluation_id": "seoul-validation",
            "split": "Validation",
            "record_count": 1,
        },
        "runtime": {"model": "small", "variants": ["baseline"]},
    }
    report = build_report(
        speech_summary=speech_summary,
        metrics={"record_count": 1},
        records_sha256="a" * 64,
        speech_summary_sha256="b" * 64,
        service_revision="revision-1",
        service_git_commit="c" * 40,
        runtime_manifest_sha256="d" * 64,
        generated_at="2026-09-05T00:00:00Z",
    )
    report_path, private_path = write_outputs(
        tmp_path,
        report,
        [{"record_key": "hashed", "mentions": []}],
    )
    payload = json.loads(report_path.read_text())
    assert payload["fact_status"] == "부분 구현 또는 개발용 데모"
    assert "CAS Top-1" in payload["claims_not_allowed"][0]
    assert private_path.stat().st_mode & 0o777 == 0o600
