"""기존 승인 STT 전사 표본의 후단 보존 감사. 정답 CAS 평가가 아니다."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import types
from collections import Counter
from pathlib import Path

from chemiguard119.incident import deterministic_parse
from chemiguard119.parser_uncertainty_evaluation import RESOLVER_SHA256
from chemiguard119.resolver import load_resolver
from chemiguard119.stt_downstream_evaluation import load_private_records
from chemiguard119.utils import sha256_file, write_json

BASELINE_COMMIT = "7eda3a8dec7649df5d54eb9d541500e87bed091c"
ROOT = Path(__file__).resolve().parents[2]


def signatures(parsed: dict) -> tuple[set, set]:
    mentions = parsed["substance_mentions"]
    return (
        {m["surface_text"] for m in mentions},
        {
            c["cas_number"]
            for m in mentions
            for c in m["resolver"].get("candidates", [])[:3]
        },
    )


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--records", type=Path, required=True)
    cli.add_argument("--summary", type=Path, required=True)
    cli.add_argument("--resolver-model", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--region", required=True)
    args = cli.parse_args()
    if args.output.exists():
        raise FileExistsError("기존 감사 보고서는 덮어쓰지 않습니다.")
    if sha256_file(args.resolver_model) != RESOLVER_SHA256:
        raise ValueError("Resolver SHA 불일치")
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE_COMMIT}:src/chemiguard119/incident.py"],
        cwd=ROOT,
        text=True,
    )
    baseline = types.ModuleType("frozen_parser_baseline")
    exec(compile(source, "frozen_parser_baseline", "exec"), baseline.__dict__)
    model = load_resolver(args.resolver_model)
    rows = load_private_records(args.records)
    summary = json.loads(args.summary.read_text())
    if summary.get("dataset", {}).get("record_count") != len(rows):
        raise ValueError("STT summary의 표본 수 불일치")
    runtime = summary["runtime"]
    if runtime.get("model") != "small" or runtime.get("compute_type") != "int8":
        raise ValueError("예정된 STT 기준선이 아님")
    # 출력 관찰 전에 고정한 표본 선정: transcript/label 내용과 무관한 hash 순서 최대 50건.
    selected = sorted(
        rows,
        key=lambda r: hashlib.sha256(
            ("parser-stt-v1:" + r["record_key"]).encode()
        ).hexdigest(),
    )[:50]
    totals = Counter()
    for row in selected:
        ref = signatures(baseline.deterministic_parse(row["reference"], model))
        old = signatures(baseline.deterministic_parse(row["hypothesis"], model))
        new_parsed = deterministic_parse(row["hypothesis"], model)
        new = signatures(new_parsed)
        for i, name in enumerate(("surface", "candidate_top3")):
            totals[f"fixed_reference_{name}"] += len(ref[i])
            totals[f"baseline_retained_{name}"] += len(ref[i] & old[i])
            totals[f"candidate_retained_{name}"] += len(ref[i] & new[i])
            totals[f"candidate_additions_unverified_{name}"] += len(new[i] - old[i])
            totals[f"candidate_removals_unverified_{name}"] += len(old[i] - new[i])
        totals["candidate_rule_input_eligible"] += sum(
            m["resolver"]["rule_input_eligible"]
            for m in new_parsed["substance_mentions"]
        )
    report = {
        "schema_version": "parser-stt-fixed-silver-sample-v1",
        "region": args.region,
        "source_records": len(rows),
        "sample_records": len(selected),
        "counts": dict(totals),
        "selection": "sha256(parser-stt-v1:record_key) ascending first 50",
        "selection_sha256": hashlib.sha256(
            json.dumps([r["record_key"] for r in selected]).encode()
        ).hexdigest(),
        "records_sha256": sha256_file(args.records),
        "summary_sha256": sha256_file(args.summary),
        "resolver_sha256": RESOLVER_SHA256,
        "baseline_commit": BASELINE_COMMIT,
        "baseline_parser_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "candidate_source_sha256": {
            str(p.relative_to(ROOT)): sha256_file(p)
            for p in (ROOT / "src/chemiguard119").glob("*.py")
        },
        "evaluator_sha256": sha256_file(Path(__file__)),
        "claim_scope": "관측된 신고전화 STT의 고정 기준선 참조 후보 보존 표본 감사. 사람 CAS/NER 정답·지역 전체·현장 무전 정확도 아님",
        "human_reviewed": False,
        "stt_rerun": False,
        "raw_text_in_report": False,
    }
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "region": args.region,
                "sample_records": len(selected),
                "counts": dict(totals),
                "report_sha256": sha256_file(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
