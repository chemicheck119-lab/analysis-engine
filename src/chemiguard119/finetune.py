"""구조화 parser SFT 데이터의 안전 게이트와 MLX-LM export.

학습 자체를 자동 실행하지 않는다. 데이터 게이트를 통과한 뒤 명령만 생성한다.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from chemiguard119.utils import normalize_text, write_json


MIN_SPLIT_COUNTS = {"train": 500, "valid": 100, "locked_test": 100}
REQUIRED_HARD_CASES = {
    "NEGATION",
    "UNCERTAINTY",
    "MULTI_SUBSTANCE",
    "ALIAS_FORMULA_CAS",
    "MISSING_FIELD",
    "ASR_TYPO",
    "INCIDENT_VS_FACILITY",
}
FORBIDDEN_TARGET_KEYS = {
    "risk_level",
    "severity",
    "rule_id",
    "hazard_decision",
    "recommended_response",
    "response_command",
    "final_decision",
}


def _jsonl_paths(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.glob("*.jsonl"))
    raise FileNotFoundError(path)


def _normalize_split(value: str | None, filename: str) -> str | None:
    raw = normalize_text(value or "")
    if not raw:
        raw = normalize_text(Path(filename).stem)
    if raw in {"train", "training"}:
        return "train"
    if raw in {"valid", "validation", "dev"}:
        return "valid"
    if raw in {"test", "locked test", "locked_test", "holdout"}:
        return "locked_test"
    return None


def _message_content(messages: list[dict[str, Any]], role: str) -> str:
    return "\n".join(
        str(item.get("content") or "") for item in messages if item.get("role") == role
    )


def _forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_TARGET_KEYS:
                found.add(str(key))
            found.update(_forbidden_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_forbidden_keys(child))
    return found


def _assistant_forbidden(messages: list[dict[str, Any]]) -> set[str]:
    content = _message_content(messages, "assistant")
    if not content:
        return {"MISSING_ASSISTANT_TARGET"}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # 자연어 브리핑 튜닝이 아니라 좁은 JSON parser 튜닝만 허용한다.
        return {"ASSISTANT_TARGET_NOT_JSON"}
    return _forbidden_keys(parsed)


def inspect_finetune_dataset(dataset_path: Path) -> dict[str, Any]:
    paths = _jsonl_paths(dataset_path)
    records: list[dict[str, Any]] = []
    invalid_rows: list[dict[str, Any]] = []
    for path in paths:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as error:
                    invalid_rows.append(
                        {"file": str(path), "line": line_number, "error": str(error)}
                    )
                    continue
                messages = payload.get("messages")
                metadata = payload.get("metadata") or {}
                if not isinstance(messages, list) or not messages:
                    invalid_rows.append(
                        {
                            "file": str(path),
                            "line": line_number,
                            "error": "messages 누락",
                        }
                    )
                    continue
                split = _normalize_split(metadata.get("split"), path.name)
                if split is None:
                    invalid_rows.append(
                        {
                            "file": str(path),
                            "line": line_number,
                            "error": "split 누락/비허용",
                        }
                    )
                    continue
                user_text = _message_content(messages, "user")
                target_text = _message_content(messages, "assistant")
                records.append(
                    {
                        "payload": payload,
                        "file": str(path),
                        "line": line_number,
                        "split": split,
                        "review_status": str(metadata.get("review_status") or ""),
                        "template_group": str(metadata.get("template_group") or ""),
                        "hard_cases": {
                            str(item).upper() for item in metadata.get("hard_cases", [])
                        },
                        "user_text": user_text,
                        "user_hash": hashlib.sha256(
                            normalize_text(user_text).encode("utf-8")
                        ).hexdigest(),
                        "target_hash": hashlib.sha256(
                            normalize_text(target_text).encode("utf-8")
                        ).hexdigest(),
                        "forbidden_target_keys": sorted(_assistant_forbidden(messages)),
                    }
                )

    approved = [row for row in records if row["review_status"] == "APPROVED"]
    counts = Counter(row["split"] for row in approved)
    hard_case_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in approved:
        for hard_case in row["hard_cases"]:
            hard_case_counts[row["split"]][hard_case] += 1

    failures: list[str] = []
    for split, minimum in MIN_SPLIT_COUNTS.items():
        if counts[split] < minimum:
            failures.append(f"{split} 승인 데이터 {counts[split]}건 < 최소 {minimum}건")
    forbidden_rows = [row for row in approved if row["forbidden_target_keys"]]
    if forbidden_rows:
        failures.append(f"금지된 위험판정/비JSON target 포함 {len(forbidden_rows)}건")

    hash_splits: dict[str, set[str]] = defaultdict(set)
    for row in approved:
        hash_splits[row["user_hash"]].add(row["split"])
    cross_split_duplicates = [
        digest for digest, splits in hash_splits.items() if len(splits) > 1
    ]
    if cross_split_duplicates:
        failures.append(
            f"동일 사용자 입력의 split 간 중복 {len(cross_split_duplicates)}건"
        )

    template_splits: dict[str, set[str]] = defaultdict(set)
    for row in approved:
        if row["template_group"]:
            template_splits[row["template_group"]].add(row["split"])
    template_leakage = [
        group
        for group, splits in template_splits.items()
        if "train" in splits and "locked_test" in splits
    ]
    if template_leakage:
        failures.append(f"train–locked_test 템플릿 그룹 누수 {len(template_leakage)}개")

    missing_hard_cases: dict[str, list[str]] = {}
    for split, minimum_per_case in (("train", 10), ("locked_test", 5)):
        missing = sorted(
            case
            for case in REQUIRED_HARD_CASES
            if hard_case_counts[split][case] < minimum_per_case
        )
        if missing:
            missing_hard_cases[split] = missing
            failures.append(f"{split} hard-case 범위 부족: {', '.join(missing)}")

    return {
        "dataset_path": str(dataset_path),
        "files": [str(path) for path in paths],
        "status": "READY_FOR_RESEARCH_SMOKE" if not failures else "NOT_READY",
        "training_executed": False,
        "approved_counts": dict(counts),
        "unapproved_count": len(records) - len(approved),
        "invalid_row_count": len(invalid_rows),
        "invalid_rows": invalid_rows[:50],
        "forbidden_target_row_count": len(forbidden_rows),
        "cross_split_duplicate_count": len(cross_split_duplicates),
        "template_leakage_groups": template_leakage[:50],
        "hard_case_counts": {
            split: dict(counter) for split, counter in hard_case_counts.items()
        },
        "missing_hard_cases": missing_hard_cases,
        "failures": failures,
        "safety_scope": "신고문 JSON 구조화 adapter 연구용. 화학 위험·대응 판정 학습 금지.",
    }


def run_finetune_check(
    dataset_path: Path, report_path: Path | None = None
) -> dict[str, Any]:
    report = inspect_finetune_dataset(dataset_path)
    if report_path:
        write_json(report_path, report)
    return report


def export_mlx_dataset(dataset_path: Path, output_dir: Path) -> dict[str, Any]:
    report = inspect_finetune_dataset(dataset_path)
    if report["status"] != "READY_FOR_RESEARCH_SMOKE":
        raise RuntimeError(
            "파인튜닝 데이터 게이트 실패:\n- " + "\n- ".join(report["failures"])
        )

    approved_by_split: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in _jsonl_paths(dataset_path):
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                metadata = payload.get("metadata") or {}
                if metadata.get("review_status") != "APPROVED":
                    continue
                split = _normalize_split(metadata.get("split"), path.name)
                if split:
                    approved_by_split[split].append({"messages": payload["messages"]})

    output_dir.mkdir(parents=True, exist_ok=True)
    name_map = {
        "train": "train.jsonl",
        "valid": "valid.jsonl",
        "locked_test": "test.jsonl",
    }
    for split, filename in name_map.items():
        text = "".join(
            json.dumps(row, ensure_ascii=False) + "\n"
            for row in approved_by_split[split]
        )
        (output_dir / filename).write_text(text, encoding="utf-8")
    manifest = {
        "source": str(dataset_path),
        "counts": {split: len(rows) for split, rows in approved_by_split.items()},
        "output_dir": str(output_dir),
        "training_executed": False,
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def mlx_qlora_smoke_command(
    model_path: str, data_dir: Path, adapter_dir: Path
) -> list[str]:
    """사용자가 검토해 실행할 보수적 MLX-LM QLoRA 스모크 명령."""

    return [
        "python",
        "-m",
        "mlx_lm.lora",
        "--model",
        model_path,
        "--train",
        "--data",
        str(data_dir),
        "--batch-size",
        "1",
        "--grad-accumulation-steps",
        "8",
        "--num-layers",
        "4",
        "--grad-checkpoint",
        "--mask-prompt",
        "--iters",
        "50",
        "--adapter-path",
        str(adapter_dir),
    ]
