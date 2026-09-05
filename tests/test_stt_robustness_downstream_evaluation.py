from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from chemiguard119.stt_downstream_evaluation import DownstreamEvaluationError
from chemiguard119.stt_robustness_downstream_evaluation import (
    CONDITIONS,
    build_robustness_report,
    evaluate_robustness_conditions,
    load_robustness_private_records,
    load_robustness_summary,
    write_robustness_outputs,
)


PRIORITY_TERMS = ["염산"]
PRIORITY_TERMS_SHA256 = "f" * 64


def _row(
    condition: str,
    key: str = "a" * 16,
    *,
    hypothesis: str = "염산 누출",
) -> dict:
    return {
        "record_key": key,
        "variant": "baseline",
        "inference_variant": "baseline",
        "channel_variant": condition,
        "status": "completed",
        "reference": "염산 누출",
        "hypothesis": hypothesis,
    }


def _rows_by_condition(
    record_count: int = 1, *, failing_condition: str | None = None
) -> dict[str, list[dict]]:
    return {
        condition: [
            _row(
                condition,
                f"{index:016x}",
                hypothesis=("누출" if condition == failing_condition else "염산 누출"),
            )
            for index in range(record_count)
        ]
        for condition in CONDITIONS
    }


def _summary(record_count: int = 1, *, failing_condition: str | None = None) -> dict:
    keys = [f"{index:016x}" for index in range(record_count)]
    record_digest = hashlib.sha256("\n".join(keys).encode("ascii")).hexdigest()
    return {
        "schema_version": "1.0.0",
        "usage_role": "evaluation",
        "evidence_scope": (
            "simulated communication distortion on AIHub calls; "
            "not field-radio validation"
        ),
        "simulation_run": {
            "profile_id": "radio-sim-v1",
            "source_manifest_sha256": "e" * 64,
            "priority_terms_sha256": PRIORITY_TERMS_SHA256,
            "variant_count": len(CONDITIONS),
            "selected": {"total": record_count},
        },
        "runtime": {
            "variants": ["baseline"],
            "implementation": "faster-whisper",
            "version": "1.2.1",
            "model": "small",
            "requested_device": "cpu",
            "device": "cpu",
            "compute_type": "int8",
        },
        "record_count": record_count,
        "record_key_set_sha256": record_digest,
        "variants": {
            condition: {
                "record_count": record_count,
                "priority_term_presence": {
                    "true_positive": (
                        0 if condition == failing_condition else record_count
                    ),
                    "false_negative": (
                        record_count if condition == failing_condition else 0
                    ),
                    "false_insertion": 0,
                    "recall": 0.0 if condition == failing_condition else 1.0,
                },
            }
            for condition in CONDITIONS
        },
    }


def _safe_response() -> dict:
    return {
        "state": "AWAITING_SUBSTANCE_CONFIRMATION",
        "model_outputs": {
            "parser": {
                "substance_mentions": [
                    {
                        "surface_text": "염산",
                        "role": "INCIDENT",
                        "assertion": "AFFIRMED",
                        "resolver": {"candidates": [{"cas_number": "7647-01-0"}]},
                    }
                ]
            },
            "substance_candidates": [
                {
                    "surface_text": "염산",
                    "role": "INCIDENT",
                    "evidence_cas_hint": "7647-01-0",
                    "requires_responder_confirmation": True,
                    "rule_eligible": False,
                    "risk_determination_allowed": False,
                    "current_inventory_confirmed": False,
                }
            ],
        },
        "confirmation_gate": {
            "incident_confirmed": False,
            "facility_confirmed": False,
            "all_required_confirmed": False,
            "rule_execution_allowed": False,
        },
        "conflict_review": {
            "executed": False,
            "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
        },
    }


def test_loaders_require_complete_paired_radio_sim_contract(tmp_path: Path) -> None:
    records_path = tmp_path / "records.private.jsonl"
    records_path.write_text(
        "".join(
            json.dumps(_row(condition, "0000000000000000"), ensure_ascii=False) + "\n"
            for condition in sorted(CONDITIONS)
        ),
        encoding="utf-8",
    )
    grouped = load_robustness_private_records(records_path)
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(_summary(), ensure_ascii=False), encoding="utf-8"
    )

    assert (
        load_robustness_summary(
            summary_path,
            rows_by_condition=grouped,
            priority_terms=PRIORITY_TERMS,
            priority_terms_sha256=PRIORITY_TERMS_SHA256,
        )
        == _summary()
    )

    malformed_summary = _summary()
    malformed_summary["variants"]["clean"]["priority_term_presence"].pop(
        "true_positive"
    )
    summary_path.write_text(
        json.dumps(malformed_summary, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(DownstreamEvaluationError, match="평가 범위"):
        load_robustness_summary(
            summary_path,
            rows_by_condition=grouped,
            priority_terms=PRIORITY_TERMS,
            priority_terms_sha256=PRIORITY_TERMS_SHA256,
        )

    records_path.write_text(
        "".join(
            json.dumps(_row(condition), ensure_ascii=False) + "\n"
            for condition in sorted(CONDITIONS - {"wind_snr0"})
        ),
        encoding="utf-8",
    )
    with pytest.raises(DownstreamEvaluationError, match="모두"):
        load_robustness_private_records(records_path)


def test_condition_evaluation_separates_request_ids_and_keeps_gate_closed() -> None:
    request_ids: list[str] = []

    def analyze(_text: str, request_id: str) -> dict:
        request_ids.append(request_id)
        return _safe_response()

    metrics, private_rows = evaluate_robustness_conditions(
        _rows_by_condition(), analyze, priority_terms=PRIORITY_TERMS, workers=2
    )

    assert metrics["condition_count"] == 18
    assert metrics["condition_record_count"] == 18
    assert metrics["evaluation_integrity_gate"]["passed"] is True
    assert metrics["analysis_coverage_gate"]["passed"] is True
    assert metrics["safety_contract_gate"]["passed"] is True
    assert metrics["downstream_evaluation_gate"]["passed"] is True
    assert metrics["cas_ground_truth_available"] is False
    assert len(request_ids) == 36
    assert len(set(request_ids)) == 36
    assert len(private_rows) == 18
    assert {row["channel_variant"] for row in private_rows} == CONDITIONS


def test_missing_stt_hypothesis_fails_coverage_but_not_evaluable_safety() -> None:
    rows = _rows_by_condition()
    rows["wind_snr0"][0] |= {"status": "failed", "hypothesis": ""}

    metrics, _ = evaluate_robustness_conditions(
        rows,
        lambda _text, _request_id: _safe_response(),
        priority_terms=PRIORITY_TERMS,
        workers=1,
    )

    assert metrics["analysis_coverage_gate"]["passed"] is False
    assert metrics["safety_contract_gate"]["passed"] is True
    assert metrics["downstream_evaluation_gate"]["passed"] is False
    assert metrics["by_condition"]["wind_snr0"]["stt_transcript_unavailable_count"] == 1


def test_report_preserves_claim_boundary_and_evaluator_revision(tmp_path: Path) -> None:
    metrics, private_rows = evaluate_robustness_conditions(
        _rows_by_condition(),
        lambda _text, _request_id: _safe_response(),
        priority_terms=PRIORITY_TERMS,
        workers=1,
    )
    report = build_robustness_report(
        speech_summary=_summary(),
        metrics=metrics,
        records_sha256="a" * 64,
        speech_summary_sha256="b" * 64,
        speech_image_digest="sha256:" + "1" * 64,
        evaluator_git_commit="c" * 40,
        service_revision="staging-revision",
        service_git_commit="d" * 40,
        runtime_manifest_sha256="e" * 64,
        generated_at="2026-09-05T00:00:00Z",
    )
    report_path, private_path = write_robustness_outputs(
        tmp_path / "out", report, private_rows
    )
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert payload["evaluation_runtime"]["git_commit"] == "c" * 40
    assert payload["whisper_lora_gate"]["decision"] == ("NOT_DECIDABLE_FROM_ONE_REGION")
    assert "CAS Top-1" in payload["claims_not_allowed"][0]
    assert "염산 누출" not in report_path.read_text(encoding="utf-8")
    assert private_path.stat().st_mode & 0o777 == 0o600


def test_lora_signal_requires_denominator_twenty() -> None:
    metrics, _ = evaluate_robustness_conditions(
        _rows_by_condition(20, failing_condition="wind_snr0"),
        lambda _text, _request_id: _safe_response(),
        priority_terms=PRIORITY_TERMS,
        workers=8,
    )
    summary = _summary(20, failing_condition="wind_snr0")
    report = build_robustness_report(
        speech_summary=summary,
        metrics=metrics,
        records_sha256="a" * 64,
        speech_summary_sha256="b" * 64,
        speech_image_digest="sha256:" + "1" * 64,
        evaluator_git_commit="c" * 40,
        service_revision="staging-revision",
        service_git_commit="d" * 40,
        runtime_manifest_sha256="e" * 64,
    )

    assert report["whisper_lora_gate"]["signals"] == [
        {
            "condition": "wind_snr0",
            "reason": "SPECIFIC_PRIORITY_TERM_RECALL_BELOW_0_80",
            "public_term": "염산",
            "priority_term_aggregate_denominator": 20,
            "priority_term_aggregate_recall": 0.0,
            "term_denominator": 20,
            "term_recall": 0.0,
            "term_false_insertion": 0,
        }
    ]
