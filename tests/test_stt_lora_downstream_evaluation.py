from __future__ import annotations

import json
from pathlib import Path

import pytest

from chemiguard119.stt_downstream_evaluation import DownstreamEvaluationError
from chemiguard119.stt_lora_downstream_evaluation import (
    ARMS,
    EXPECTED_CONDITION,
    EXPECTED_EVIDENCE_SCOPE,
    EXPECTED_EVALUATION_ID,
    EXPECTED_RECORDS,
    EXPECTED_WIND_CHECKS,
    evaluate_lora_downstream,
    load_bound_inputs,
    write_outputs,
)
from chemiguard119.stt_downstream_evaluation import sha256_file


def _response(unsafe: bool = False, cas_number: str = "7647-01-0") -> dict:
    candidates = [
        {
            "surface_text": "염산",
            "role": "INCIDENT",
            "evidence_cas_hint": cas_number,
            "requires_responder_confirmation": not unsafe,
            "rule_eligible": unsafe,
            "risk_determination_allowed": False,
            "current_inventory_confirmed": False,
        }
    ]
    outputs = {
        "parser": {
            "substance_mentions": [
                {
                    "surface_text": "염산",
                    "role": "INCIDENT",
                    "assertion": "AFFIRMED",
                    "resolver": {"candidates": [{"cas_number": cas_number}]},
                }
            ]
        },
        "substance_candidates": candidates,
    }
    if unsafe:
        outputs["risk_level"] = "HIGH"
    return {
        "state": "AWAITING_SUBSTANCE_CONFIRMATION",
        "model_outputs": outputs,
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


def _write_private_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def _fixtures(
    root: Path, *, candidate_hypothesis: str = "염산 누출"
) -> tuple[Path, dict[str, Path], dict[str, Path]]:
    conversion_sha256 = "a" * 64
    summaries: dict[str, Path] = {}
    records: dict[str, Path] = {}
    for arm, hypothesis in zip(ARMS, ("염산 누출", candidate_hypothesis)):
        arm_dir = root / arm
        arm_dir.mkdir()
        rows = [
            {
                "record_key": f"{index:016x}",
                "variant": "baseline",
                "status": "completed",
                "reference": "염산 누출",
                "hypothesis": hypothesis,
                "audio_seconds": 10.0,
            }
            for index in range(EXPECTED_RECORDS)
        ]
        record_path = arm_dir / "records.private.jsonl"
        record_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
        record_path.chmod(0o600)
        summary = {
            "schema_version": "1.0.0",
            "protocol_id": "whisper-small-lora-wind-dev-arm-v1",
            "experiment_id": EXPECTED_EVALUATION_ID,
            "usage_role": "development",
            "evidence_scope": EXPECTED_EVIDENCE_SCOPE,
            "fact_status": "부분 구현 또는 개발용 데모",
            "model_arm": arm,
            "automatic_adoption_allowed": False,
            "dataset": {
                "dataset_id": "aihub_71768_gwangju_fire_lora_dev_wind_snr0",
                "dataset_version": (
                    "dataset-71768_downloaded-2026-09-05"
                    "+whisper-lora-clean-wind-snr0-v1"
                ),
                "evaluation_id": EXPECTED_EVALUATION_ID,
                "record_count": EXPECTED_RECORDS,
                "expected_record_count": EXPECTED_RECORDS,
                "manifest_sha256": "1" * 64,
                "archive_sha256": {"audio": "2" * 64, "labels": "3" * 64},
                "split": "Training internal dev",
                "condition": EXPECTED_CONDITION,
                "used_for_tuning": True,
            },
            "runtime": {
                "implementation": "faster-whisper",
                "version": "1.2.1",
                "model": str(arm_dir / "model"),
                "requested_device": "cpu",
                "device": "cpu",
                "compute_type": "int8",
                "initialization_fallback": None,
                "language": "ko (configured, not detected)",
                "beam_size": 5,
                "temperature": 0.0,
                "vad_filter": True,
                "condition_on_previous_text": False,
                "variants": ["baseline"],
            },
            "input_bindings": {
                "conversion_report_sha256": conversion_sha256,
                "data_preflight": {
                    "execution_config": "4" * 64,
                    "experiment_config": "5" * 64,
                    "run_summary": "6" * 64,
                },
            },
        }
        summary_path = arm_dir / "summary.json"
        _write_private_json(summary_path, summary)
        summaries[arm] = summary_path
        records[arm] = record_path

    wind = {
        "schema_version": "1.0.0",
        "protocol_id": "whisper-small-lora-wind-dev-evaluation-v1",
        "status": "evaluated",
        "fact_status": "부분 구현 또는 개발용 데모",
        "evidence_scope": EXPECTED_EVIDENCE_SCOPE,
        "dataset": {
            "dataset_id": "aihub_71768_gwangju_fire_lora_dev_wind_snr0",
            "dataset_version": (
                "dataset-71768_downloaded-2026-09-05+whisper-lora-clean-wind-snr0-v1"
            ),
            "evaluation_id": EXPECTED_EVALUATION_ID,
            "record_count": EXPECTED_RECORDS,
            "manifest_sha256": "1" * 64,
            "audio_sha256": "2" * 64,
            "labels_sha256": "3" * 64,
            "membership_role": "development_used_for_tuning",
            "condition": EXPECTED_CONDITION,
            "data_preflight": {
                "execution_config": "4" * 64,
                "experiment_config": "5" * 64,
                "run_summary": "6" * 64,
            },
        },
        "provenance": {
            "evaluator_revision": "7" * 40,
            "conversion_report_sha256": conversion_sha256,
            "clean_report_sha256": "8" * 64,
            "experiment_config_sha256": "9" * 64,
            "priority_terms_sha256": "b" * 64,
            "inputs": {
                arm: {
                    "summary_sha256": sha256_file(summaries[arm]),
                    "records_sha256": sha256_file(records[arm]),
                }
                for arm in ARMS
            },
        },
        "checks": {name: True for name in EXPECTED_WIND_CHECKS},
        "decision": "continue_downstream_safety_gate",
        "automatic_adoption_allowed": False,
    }
    wind_path = root / "wind-report.json"
    _write_private_json(wind_path, wind)
    return wind_path, summaries, records


def _evaluate(
    root: Path,
    *,
    candidate_hypothesis: str = "염산 누출",
    unsafe: bool = False,
    candidate_cas: str = "7647-01-0",
) -> tuple[dict, dict]:
    wind, summaries, records = _fixtures(
        root, candidate_hypothesis=candidate_hypothesis
    )

    def analyze(text: str, _request_id: str) -> dict:
        is_candidate = (
            text == candidate_hypothesis and candidate_hypothesis != "염산 누출"
        )
        return _response(
            unsafe=unsafe and is_candidate,
            cas_number=candidate_cas if is_candidate else "7647-01-0",
        )

    return evaluate_lora_downstream(
        wind_report_path=wind,
        summaries=summaries,
        records=records,
        analyze=analyze,
        workers=4,
        service_revision="model-api-r1",
        service_git_commit="8" * 40,
        runtime_manifest_sha256="9" * 64,
        evaluator_git_commit="b" * 40,
        generated_at="2026-09-07T00:00:00Z",
    )


def test_passes_proxy_gate_without_claiming_cas_accuracy(tmp_path: Path) -> None:
    report, private_rows = _evaluate(tmp_path)

    assert report["decision"] == "pass_proxy_downstream_keep_adoption_blocked"
    assert all(report["checks"].values())
    assert report["automatic_adoption_allowed"] is False
    assert report["comparison"]["cas_ground_truth_available"] is False
    assert report["comparison"]["wrong_single_cas_promotion_ground_truth_count"] is None

    report_path, private_paths = write_outputs(
        tmp_path / "output", report, private_rows
    )
    serialized = report_path.read_text(encoding="utf-8") + "".join(
        path.read_text(encoding="utf-8") for path in private_paths.values()
    )
    assert "염산 누출" not in serialized
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in private_paths.values())
    with pytest.raises(FileExistsError):
        write_outputs(tmp_path / "output", report, private_rows)


def test_rejects_candidate_on_preconfirmation_safety_violation(
    tmp_path: Path,
) -> None:
    report, _ = _evaluate(tmp_path, candidate_hypothesis="염산 후보", unsafe=True)

    assert report["checks"]["all_preconfirmation_safety_contracts_passed"] is False
    assert report["decision"] == "reject_candidate_keep_operational_baseline"


def test_rejects_candidate_on_resolver_silver_top3_regression(tmp_path: Path) -> None:
    report, _ = _evaluate(
        tmp_path,
        candidate_hypothesis="염산 후보",
        candidate_cas="7664-93-9",
    )

    assert report["checks"]["candidate_resolver_top3_retention_nonregression"] is False
    assert report["decision"] == "reject_candidate_keep_operational_baseline"


def test_rejects_speech_artifact_hash_drift(tmp_path: Path) -> None:
    wind, summaries, records = _fixtures(tmp_path)
    summaries[ARMS[1]].write_text("{}", encoding="utf-8")
    summaries[ARMS[1]].chmod(0o600)

    with pytest.raises(DownstreamEvaluationError, match="artifact hash"):
        load_bound_inputs(
            wind_report_path=wind,
            summaries=summaries,
            records=records,
        )
