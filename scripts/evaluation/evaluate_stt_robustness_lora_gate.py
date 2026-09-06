#!/usr/bin/env python3
"""서울·인천 모의 왜곡 후단 보고서로 LoRA 진입 Gate를 생성한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.stt_robustness_downstream_evaluation import load_priority_terms
from chemiguard119.stt_robustness_lora_gate import build_from_paths, sha256_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incheon-report", type=Path, required=True)
    parser.add_argument("--seoul-report", type=Path, required=True)
    parser.add_argument("--runtime-provenance", type=Path, required=True)
    parser.add_argument("--priority-terms", type=Path, required=True)
    parser.add_argument("--evaluator-git-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-at")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"기존 결과를 덮어쓰지 않습니다: {args.output}")

    priority_terms = load_priority_terms(args.priority_terms)
    report = build_from_paths(
        incheon_path=args.incheon_report,
        seoul_path=args.seoul_report,
        runtime_provenance_path=args.runtime_provenance,
        priority_terms=priority_terms,
        priority_terms_sha256=sha256_file(args.priority_terms),
        evaluator_git_commit=args.evaluator_git_commit,
        generated_at=args.generated_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "output": str(args.output),
                "sha256": sha256_file(args.output),
                "decision": report["whisper_lora_gate"]["decision"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
