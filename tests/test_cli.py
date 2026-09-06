from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chemiguard119 import cli


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["doctor"], "doctor"),
        (["coverage"], "coverage"),
        (["audit"], "audit"),
        (["prepare"], "prepare"),
        (["train"], "train"),
        (["evaluate"], "evaluate"),
        (["evaluate-e2e"], "evaluate-e2e"),
        (["evaluate-agent-trajectories"], "evaluate-agent-trajectories"),
        (
            ["evaluate-official-incidents", "--split", "development"],
            "evaluate-official-incidents",
        ),
        (["resolve", "염산"], "resolve"),
        (["discover", "무색 휘발성 액체"], "discover"),
        (["search", "염산 누출"], "search"),
        (["parse", "염산 탱크 누출"], "parse"),
        (["incident", "염산 탱크 누출"], "incident"),
        (["review", "7681-52-9", "7647-01-0"], "review"),
        (["finetune-check"], "finetune-check"),
        (["finetune-resolver"], "finetune-resolver"),
        (["pipeline"], "pipeline"),
        (["release-manifest"], "release-manifest"),
        (["interactive"], "interactive"),
    ],
)
def test_all_commands_have_callable_handlers(argv: list[str], command: str) -> None:
    args = cli.build_parser().parse_args(argv)

    assert args.command == command
    assert callable(args.handler)


def test_pipeline_accepts_incident_source_adaptation_inputs(tmp_path: Path) -> None:
    source = tmp_path / "incidents.csv"
    manifest = tmp_path / "incidents.manifest.json"
    args = cli.build_parser().parse_args(
        [
            "pipeline",
            "--incident-adaptation-csv",
            str(source),
            "--incident-adaptation-manifest",
            str(manifest),
        ]
    )

    assert args.incident_adaptation_csv == source.resolve()
    assert args.incident_adaptation_manifest == manifest.resolve()


def test_pipeline_requires_incident_csv_and_manifest_together(tmp_path: Path) -> None:
    source = tmp_path / "incidents.csv"
    args = cli.build_parser().parse_args(
        ["pipeline", "--incident-adaptation-csv", str(source)]
    )

    with pytest.raises(ValueError, match="함께 지정"):
        cli._pipeline(args)


@pytest.mark.parametrize("argv", [["--json", "doctor"], ["doctor", "--json"]])
def test_json_option_works_before_or_after_command(argv: list[str]) -> None:
    args = cli.build_parser().parse_args(argv)

    assert args.json is True


def test_doctor_json_does_not_require_full_dataset(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(
        [
            "doctor",
            "--data-dir",
            str(tmp_path / "empty-data"),
            "--db",
            str(tmp_path / "missing.sqlite"),
            "--resolver-model",
            str(tmp_path / "missing-resolver.joblib"),
            "--retriever-model",
            str(tmp_path / "missing-retriever.joblib"),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "NEEDS_SETUP"
    assert payload["final_csv_count"] == 0
    assert payload["required_csv_count"] == 8
    assert "현장 지휘관" in payload["safety_notice"]


def test_coverage_command_reports_historical_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "coverage.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE facility_candidate(
                facility_name TEXT,
                address TEXT,
                province TEXT,
                cas_number TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO facility_candidate VALUES (?, ?, ?, ?)",
            ("예시 사업장", "서울", "서울특별시", "7647-01-0"),
        )

    code = cli.main(["coverage", "--db", str(db_path), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["scope"] == "PARTIAL_KOREA_HISTORICAL_CANDIDATES"
    assert payload["candidate_row_count"] == 1
    assert payload["current_inventory_confirmed"] is False


def test_deterministic_parse_command_can_run_with_stubbed_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chemiguard119 import incident, resolver

    monkeypatch.setattr(resolver, "load_resolver", lambda _path: {"rows": []})
    monkeypatch.setattr(
        incident,
        "deterministic_parse",
        lambda text, _artifact: {
            "backend": "DETERMINISTIC_BASELINE",
            "source_text": text,
            "incident_types": ["LEAK"],
            "fire_status": "FALSE",
            "substance_mentions": [],
            "planned_actions": [],
            "needs_substance_confirmation": True,
            "missing_fields": ["substance"],
        },
    )

    code = cli.main(
        [
            "parse",
            "탱크에서 액체가 누출 중",
            "--resolver-model",
            str(tmp_path / "stub.joblib"),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["backend"] == "DETERMINISTIC_BASELINE"
    assert payload["incident_types"] == ["LEAK"]
    assert payload["needs_substance_confirmation"] is True


def test_lmstudio_parse_command_uses_injected_client_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chemiguard119 import lmstudio

    calls: dict[str, object] = {}

    def fake_parse(text: str, model: str, base_url: str, timeout: int):
        calls.update(text=text, model=model, base_url=base_url, timeout=timeout)
        return {
            "backend": "LM_STUDIO_STRUCTURED_OUTPUT",
            "incident_types": ["UNKNOWN"],
            "fire_status": "UNKNOWN",
            "substance_mentions": [],
            "planned_actions": [],
            "needs_substance_confirmation": True,
            "missing_fields": ["substance"],
        }

    monkeypatch.setattr(lmstudio, "parse_with_lmstudio", fake_parse)

    code = cli.main(
        [
            "parse",
            "미상 물질 냄새 신고",
            "--backend",
            "lmstudio",
            "--model",
            "local-test-model",
            "--base-url",
            "http://127.0.0.1:9999/v1",
            "--timeout",
            "7",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["backend"] == "LM_STUDIO_STRUCTURED_OUTPUT"
    assert calls == {
        "text": "미상 물질 냄새 신고",
        "model": "local-test-model",
        "base_url": "http://127.0.0.1:9999/v1",
        "timeout": 7,
    }


def test_lmstudio_parse_requires_model_without_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = cli.main(["parse", "누출 신고", "--backend", "lmstudio", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "ERROR"
    assert "--model" in payload["error"]


def test_incident_command_runs_full_pipeline_with_public_source_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chemiguard119 import pipeline, resolver, retrieval

    calls: dict[str, object] = {}
    monkeypatch.setattr(resolver, "load_resolver", lambda _path: {"kind": "resolver"})
    monkeypatch.setattr(
        retrieval, "load_retriever", lambda _path: {"kind": "retriever"}
    )

    def fake_analyze(text: str, **kwargs):
        calls.update(text=text, **kwargs)
        return {
            "status": "NEEDS_SUBSTANCE_CONFIRMATION",
            "parsed_report": {"incident_types": ["LEAK"]},
            "substance_candidates": [],
            "rule_review": {
                "executed": False,
                "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
            },
            "output_validation": {"status": "PASSED", "errors": []},
        }

    monkeypatch.setattr(pipeline, "analyze_incident", fake_analyze)
    code = cli.main(
        [
            "incident",
            "미상 물질 탱크 누출",
            "--db",
            str(tmp_path / "db.sqlite"),
            "--resolver-model",
            str(tmp_path / "resolver.joblib"),
            "--retriever-model",
            str(tmp_path / "retriever.joblib"),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "NEEDS_SUBSTANCE_CONFIRMATION"
    assert calls["text"] == "미상 물질 탱크 누출"
    assert calls["allow_demo_rules"] is False
    assert calls["policy_mode"] == "PUBLIC_SOURCE_PILOT_V1"
    assert calls["confirmed_incident_cas"] is None
    assert calls["confirmed_facility_cas"] is None


def test_prepare_reports_missing_optional_module_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_import = cli.importlib.import_module

    def fake_import(name: str):
        if name == "chemiguard119.preprocessing":
            error = ModuleNotFoundError("test-only missing module")
            error.name = name
            raise error
        return original_import(name)

    monkeypatch.setattr(cli.importlib, "import_module", fake_import)

    code = cli.main(
        [
            "prepare",
            "--data-dir",
            str(tmp_path / "data"),
            "--db",
            str(tmp_path / "test.sqlite"),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "ERROR"
    assert "preprocessing.py가 없습니다" in payload["error"]
    assert "prepare_database" in payload["error"]


def test_prepare_connects_current_preprocessing_contract_without_full_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from chemiguard119 import preprocessing

    calls: dict[str, Path] = {}

    def fake_prepare_dataset(data_dir, config_dir, artifact_dir, db_path):
        calls.update(
            data_dir=Path(data_dir),
            config_dir=Path(config_dir),
            artifact_dir=Path(artifact_dir),
            db_path=Path(db_path),
        )
        return {"schema_version": "test", "counts": {"substance": 9}}

    monkeypatch.setattr(preprocessing, "prepare_dataset", fake_prepare_dataset)
    database = tmp_path / "artifacts" / "test.sqlite"

    code = cli.main(
        [
            "prepare",
            "--data-dir",
            str(tmp_path / "data"),
            "--db",
            str(database),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["schema_version"] == "test"
    assert calls["data_dir"] == (tmp_path / "data").resolve()
    assert calls["artifact_dir"] == database.parent.resolve()
    assert calls["db_path"] == database.resolve()


def test_finetune_check_runs_as_dry_gate_without_training(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset_dir = tmp_path / "finetune"
    dataset_dir.mkdir()
    report_path = tmp_path / "finetune-report.json"

    code = cli.main(
        [
            "finetune-check",
            "--dataset-path",
            str(dataset_dir),
            "--report",
            str(report_path),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "NOT_READY"
    assert payload["training_executed"] is False
    assert report_path.is_file()
    assert "위험" in payload["safety_scope"]


def test_interactive_quit_does_not_touch_models_or_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = iter(["quit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    code = cli.main(["interactive"])

    output = capsys.readouterr().out
    assert code == 0
    assert "대화형 CLI" in output
    assert "CLOSED" in output
