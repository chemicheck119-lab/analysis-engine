#!/usr/bin/env python3
"""비공개 STT 결과를 Model API에 보내고 비식별 집계만 생성한다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from chemiguard119.stt_downstream_evaluation import (
    MAX_WORKERS,
    ModelApiClient,
    bearer_token_from_env,
    build_report,
    evaluate_pairs,
    load_private_records,
    load_speech_summary,
    required_secret_from_env,
    sha256_file,
    write_outputs,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-private", type=Path, required=True)
    parser.add_argument("--speech-summary", type=Path, required=True)
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
    args = parser.parse_args()

    rows = load_private_records(args.records_private)
    speech_summary = load_speech_summary(
        args.speech_summary, expected_records=len(rows)
    )
    client = ModelApiClient(
        base_url=args.model_api_base_url,
        api_key=required_secret_from_env(args.api_key_env),
        bearer_token=bearer_token_from_env(
            args.bearer_token_env, base_url=args.model_api_base_url
        ),
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
    )

    last_percent = -1

    def progress(completed: int, total: int) -> None:
        nonlocal last_percent
        percent = completed * 100 // max(1, total)
        if percent >= last_percent + 5 or completed == total:
            print(
                json.dumps({"completed_api_calls": completed, "total_api_calls": total})
            )
            last_percent = percent

    metrics, private_rows = evaluate_pairs(
        rows, client.analyze, workers=args.workers, progress=progress
    )
    report = build_report(
        speech_summary=speech_summary,
        metrics=metrics,
        records_sha256=sha256_file(args.records_private),
        speech_summary_sha256=sha256_file(args.speech_summary),
        service_revision=args.service_revision,
        service_git_commit=args.service_git_commit,
        runtime_manifest_sha256=args.runtime_manifest_sha256,
    )
    report_path, private_path = write_outputs(args.output_dir, report, private_rows)
    print(
        json.dumps(
            {
                "status": "completed",
                "report": str(report_path),
                "report_sha256": sha256_file(report_path),
                "private_records": str(private_path),
                "private_records_sha256": sha256_file(private_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
