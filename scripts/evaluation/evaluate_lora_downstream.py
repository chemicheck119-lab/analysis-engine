#!/usr/bin/env python3
"""LoRA B/C wind 전사를 동일 Model API로 평가하고 비식별 집계를 생성한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.stt_downstream_evaluation import (
    MAX_WORKERS,
    ModelApiClient,
    required_secret_from_env,
    sha256_file,
)
from chemiguard119.stt_lora_downstream_evaluation import (
    ARMS,
    evaluate_lora_downstream,
    write_outputs,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speech-wind-report", type=Path, required=True)
    parser.add_argument("--b-summary", type=Path, required=True)
    parser.add_argument("--b-records", type=Path, required=True)
    parser.add_argument("--c-summary", type=Path, required=True)
    parser.add_argument("--c-records", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-api-base-url", required=True)
    parser.add_argument("--api-key-env", default="CHEMIGUARD119_API_KEY")
    parser.add_argument("--bearer-token-env", default="CHEMICHECK119_IDENTITY_TOKEN")
    parser.add_argument(
        "--workers", type=int, default=4, choices=range(1, MAX_WORKERS + 1)
    )
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--max-retries", type=int, default=2, choices=range(0, 6))
    parser.add_argument("--service-revision", required=True)
    parser.add_argument("--service-git-commit", required=True)
    parser.add_argument("--runtime-manifest-sha256", required=True)
    parser.add_argument("--evaluator-git-commit", required=True)
    args = parser.parse_args()

    client = ModelApiClient(
        base_url=args.model_api_base_url,
        api_key=required_secret_from_env(args.api_key_env),
        bearer_token=required_secret_from_env(args.bearer_token_env),
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
    )
    last_percent = {arm: -1 for arm in ARMS}

    def progress(arm: str, completed: int, total: int) -> None:
        percent = completed * 100 // max(1, total)
        if percent >= last_percent[arm] + 5 or completed == total:
            print(
                json.dumps(
                    {
                        "arm": arm,
                        "completed_api_calls": completed,
                        "total_api_calls": total,
                    }
                )
            )
            last_percent[arm] = percent

    summaries = dict(zip(ARMS, (args.b_summary, args.c_summary)))
    records = dict(zip(ARMS, (args.b_records, args.c_records)))
    report, private_rows = evaluate_lora_downstream(
        wind_report_path=args.speech_wind_report,
        summaries=summaries,
        records=records,
        analyze=client.analyze,
        workers=args.workers,
        service_revision=args.service_revision,
        service_git_commit=args.service_git_commit,
        runtime_manifest_sha256=args.runtime_manifest_sha256,
        evaluator_git_commit=args.evaluator_git_commit,
        progress=progress,
    )
    report_path, private_paths = write_outputs(args.output_dir, report, private_rows)
    print(
        json.dumps(
            {
                "status": "completed",
                "fact_status": report["fact_status"],
                "decision": report["decision"],
                "automatic_adoption_allowed": False,
                "report": str(report_path),
                "report_sha256": sha256_file(report_path),
                "private_record_sha256": {
                    arm: sha256_file(path) for arm, path in private_paths.items()
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
