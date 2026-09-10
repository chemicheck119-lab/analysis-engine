"""4,000자 합성 반복 신고문의 실제 artifact 응답 크기 경계 검사."""

import argparse
import json
import time
from pathlib import Path

from chemiguard119.incident import deterministic_parse, validate_parser_output
from chemiguard119.parser_uncertainty_evaluation import RESOLVER_SHA256
from chemiguard119.resolver import load_resolver
from chemiguard119.utils import sha256_file, write_json


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--resolver-model", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    if args.output.exists():
        raise FileExistsError("기존 보고서는 덮어쓰지 않습니다.")
    if sha256_file(args.resolver_model) != RESOLVER_SHA256:
        raise ValueError("승인된 Resolver SHA 불일치")
    model = load_resolver(args.resolver_model)
    text = ("염산 없음. 염산. " * 400)[:4000]
    started = time.perf_counter()
    parsed = deterministic_parse(text, model)
    elapsed = (time.perf_counter() - started) * 1000
    size = len(json.dumps(parsed, ensure_ascii=False).encode())
    checks = {
        "all_mentions_preserved": len(parsed["substance_mentions"]) == 727,
        "conflict_details_bounded": len(parsed["statement_conflicts"]) == 64,
        "clarification_preserved": parsed["requires_statement_clarification"],
        "limit_disclosed": parsed["statement_conflict_limit_reached"],
        "valid_spans": not validate_parser_output(parsed, text),
        "under_2mib": size <= 2 * 1024 * 1024,
    }
    report = {
        "schema_version": "parser-resource-boundary-v1",
        "checks": checks,
        "passed": all(checks.values()),
        "input_codepoints": len(text),
        "mentions": len(parsed["substance_mentions"]),
        "conflict_pairs": len(parsed["statement_conflicts"]),
        "parser_elapsed_ms": elapsed,
        "parser_serialized_bytes": size,
        "resolver_sha256": RESOLVER_SHA256,
        "evaluator_sha256": sha256_file(Path(__file__)),
        "scope": "실제 artifact·합성 반복 입력 한 건. 네트워크·부하·일반 응답 크기 보장 아님",
    }
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
