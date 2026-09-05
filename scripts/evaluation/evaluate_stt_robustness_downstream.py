#!/usr/bin/env python3
"""비공개 radio-sim-v1 STT 결과의 후단 실버 평가를 실행한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.stt_downstream_evaluation import (
    MAX_WORKERS,
    ModelApiClient,
    required_secret_from_env,
)
from chemiguard119.stt_robustness_downstream_evaluation import (
    build_robustness_report,
    evaluate_robustness_conditions,
    load_priority_terms,
    load_robustness_private_records,
    load_robustness_summary,
    sha256_file,
    write_robustness_outputs,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-private", type=Path, required=True)
    parser.add_argument("--speech-summary", type=Path, required=True)
    parser.add_argument("--priority-terms", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-api-base-url", required=True)
    parser.add_argument("--api-key-env", default="CHEMIGUARD119_API_KEY")
    parser.add_argument("--bearer-token-env", default="CHEMICHECK119_IDENTITY_TOKEN")
    parser.add_argument(
        "--workers", type=int, default=4, choices=range(1, MAX_WORKERS + 1)
    )
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--max-retries", type=int, default=2, choices=range(0, 6))
    parser.add_argument("--evaluator-git-commit", required=True)
    parser.add_argument("--speech-image-digest", required=True)
    parser.add_argument("--service-revision", required=True)
    parser.add_argument("--service-git-commit", required=True)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    args = parser.parse_args()

    rows_by_condition = load_robustness_private_records(args.records_private)
    priority_terms = load_priority_terms(args.priority_terms)
    speech_summary = load_robustness_summary(
        args.speech_summary,
        rows_by_condition=rows_by_condition,
        priority_terms=priority_terms,
        priority_terms_sha256=sha256_file(args.priority_terms),
    )
    client = ModelApiClient(
        base_url=args.model_api_base_url,
        api_key=required_secret_from_env(args.api_key_env),
        bearer_token=required_secret_from_env(args.bearer_token_env),
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
    )

    last_percent_by_condition: dict[str, int] = {}

    def progress(condition: str, completed: int, total: int) -> None:
        percent = completed * 100 // max(1, total)
        previous = last_percent_by_condition.get(condition, -1)
        if percent >= previous + 10 or completed == total:
            print(
                json.dumps(
                    {
                        "condition": condition,
                        "completed_api_calls": completed,
                        "total_api_calls": total,
                    }
                )
            )
            last_percent_by_condition[condition] = percent

    metrics, private_rows = evaluate_robustness_conditions(
        rows_by_condition,
        client.analyze,
        priority_terms=priority_terms,
        workers=args.workers,
        progress=progress,
    )
    report = build_robustness_report(
        speech_summary=speech_summary,
        metrics=metrics,
        records_sha256=sha256_file(args.records_private),
        speech_summary_sha256=sha256_file(args.speech_summary),
        speech_image_digest=args.speech_image_digest,
        evaluator_git_commit=args.evaluator_git_commit,
        service_revision=args.service_revision,
        service_git_commit=args.service_git_commit,
        runtime_manifest_sha256=args.runtime_manifest_sha256,
    )
    report_path, private_path = write_robustness_outputs(
        args.output_dir, report, private_rows
    )
    print(
        json.dumps(
            {
                "status": "completed",
                "condition_count": metrics["condition_count"],
                "record_count_per_condition": metrics["record_count_per_condition"],
                "report": str(report_path),
                "report_sha256": sha256_file(report_path),
                "private_records": str(private_path),
                "private_records_sha256": sha256_file(private_path),
                "lora_decision": report["whisper_lora_gate"]["decision"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
