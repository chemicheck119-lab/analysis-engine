from __future__ import annotations

import json
from pathlib import Path

import pytest

from chemiguard119.finetune import (
    export_mlx_dataset,
    inspect_finetune_dataset,
    mlx_qlora_smoke_command,
)


def _write(path: Path, payloads: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in payloads),
        encoding="utf-8",
    )


def _row(review_status: str = "DRAFT", assistant: dict | None = None) -> dict:
    return {
        "messages": [
            {"role": "user", "content": "염산이 아니다. 아세톤이 누출됐다."},
            {
                "role": "assistant",
                "content": json.dumps(
                    assistant
                    or {
                        "incident_types": ["LEAK"],
                        "substance_mentions": [
                            {
                                "surface_text": "염산",
                                "role": "NEGATED",
                                "assertion": "NEGATED",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "metadata": {
            "review_status": review_status,
            "split": "train",
            "template_group": "negation-a",
            "hard_cases": ["NEGATION"],
        },
    }


def test_draft_seed_is_not_ready_and_export_is_blocked(tmp_path: Path) -> None:
    dataset = tmp_path / "seed.jsonl"
    _write(dataset, [_row()])
    report = inspect_finetune_dataset(dataset)
    assert report["status"] == "NOT_READY"
    assert report["approved_counts"] == {}
    with pytest.raises(RuntimeError, match="데이터 게이트 실패"):
        export_mlx_dataset(dataset, tmp_path / "export")


def test_approved_target_with_risk_decision_is_rejected(tmp_path: Path) -> None:
    dataset = tmp_path / "train.jsonl"
    _write(
        dataset, [_row("APPROVED", {"incident_types": ["LEAK"], "risk_level": "HIGH"})]
    )
    report = inspect_finetune_dataset(dataset)
    assert report["forbidden_target_row_count"] == 1
    assert any("금지된" in reason for reason in report["failures"])


def test_mlx_command_is_only_a_command_description(tmp_path: Path) -> None:
    command = mlx_qlora_smoke_command("model", tmp_path / "data", tmp_path / "adapter")
    assert command[:3] == ["python", "-m", "mlx_lm.lora"]
    assert "--iters" in command
    assert command[command.index("--iters") + 1] == "50"
