"""고정 합성 회귀의 실제 artifact 평가. 개인정보·원문을 콘솔로 보내지 않는다."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path

import numpy as np

from chemiguard119.incident import INCIDENT_PARSER_POLICY_VERSION, deterministic_parse
from chemiguard119.parser_uncertainty_cases import cases
from chemiguard119.resolver import load_resolver
from chemiguard119.utils import sha256_file, write_json

RESOLVER_SHA256 = "2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff"
ROOT = Path(__file__).resolve().parents[2]


def score_case(case: dict, parsed: dict) -> dict:
    expected = {(m["start"], m["end"]): m for m in case["expected"]}
    predicted = {}
    seen = {}
    outside = promotions = 0
    for m in parsed["substance_mentions"]:
        surface = m["surface_text"]
        # v2에는 offset이 없다. 원문 등장 순서로 정렬하는 평가 adapter이며 모델 수정이 아니다.
        start = m.get("start", case["text"].find(surface, seen.get(surface, 0)))
        end = m.get("end", start + len(surface))
        seen[surface] = end
        outside += int(start < 0 or case["text"][start:end] != surface)
        promotions += int(m["resolver"].get("rule_input_eligible") is True)
        predicted[(start, end)] = m
    matching = expected.keys() & predicted.keys()
    confusion = Counter()
    role_confusion = Counter()
    for span, target in expected.items():
        found = predicted.get(span, {})
        confusion[f"{target['assertion']}->{found.get('assertion', 'MISSING')}"] += 1
        role_confusion[f"{target['role']}->{found.get('role', 'MISSING')}"] += 1
    assertion_correct = sum(
        expected[s]["assertion"] == predicted[s]["assertion"] for s in matching
    )
    role_correct = sum(expected[s]["role"] == predicted[s]["role"] for s in matching)
    return {
        "id": case["id"],
        "family": case["family"],
        "expected": len(expected),
        "tp": len(matching),
        "fp": len(predicted.keys() - expected.keys()),
        "fn": len(expected.keys() - predicted.keys()),
        "assertion_correct": assertion_correct,
        "role_correct": role_correct,
        "assertion_confusion": dict(confusion),
        "role_confusion": dict(role_confusion),
        "outside_source": outside,
        "candidate_promotions": promotions,
        "candidate_provided": bool(predicted),
        "candidate_needed": bool(expected),
        "all_correct": len(expected)
        == len(predicted)
        == len(matching)
        == assertion_correct
        == role_correct,
        "predicted": [
            {k: m.get(k) for k in ("surface_text", "start", "end", "assertion", "role")}
            for m in parsed["substance_mentions"]
        ],
    }


def aggregate(rows: list[dict]) -> dict:
    totals = {
        key: sum(row[key] for row in rows)
        for key in (
            "expected",
            "tp",
            "fp",
            "fn",
            "assertion_correct",
            "role_correct",
            "outside_source",
            "candidate_promotions",
        )
    }
    tp, fp, fn = (totals[key] for key in ("tp", "fp", "fn"))
    return {
        **totals,
        "precision": tp / (tp + fp) if tp + fp else 0,
        "recall": tp / (tp + fn) if tp + fn else 0,
        "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0,
        "assertion_joint_accuracy": totals["assertion_correct"] / totals["expected"],
        "role_joint_accuracy": totals["role_correct"] / totals["expected"],
        "case_exact_accuracy": sum(r["all_correct"] for r in rows) / len(rows),
        "candidate_coverage": sum(r["candidate_provided"] for r in rows) / len(rows),
        "unnecessary_abstention_count": sum(
            r["candidate_needed"] and not r["candidate_provided"] for r in rows
        ),
        "negative_control_false_positives": sum(
            r["fp"] for r in rows if not r["candidate_needed"]
        ),
    }


def compare(baseline: dict, candidate: dict) -> dict:
    if (
        baseline["input_sha256"] != candidate["input_sha256"]
        or baseline["resolver_sha256"] != candidate["resolver_sha256"]
    ):
        raise ValueError("비교 입력 또는 모델 hash 불일치")
    a, b = baseline["metrics"], candidate["metrics"]
    limits = {
        "mention_f1_plus_5pp": b["f1"] - a["f1"] >= 0.05,
        "precision_nonregression": b["precision"] >= a["precision"],
        "assertion_improved": b["assertion_joint_accuracy"]
        > a["assertion_joint_accuracy"],
        "role_nonregression": b["role_joint_accuracy"] >= a["role_joint_accuracy"],
        "no_negative_control_fp": b["negative_control_false_positives"] == 0,
        "source_and_confirmation": b["outside_source"]
        == b["candidate_promotions"]
        == 0,
        "latency_budget": candidate["latency_ms"]["p95"]
        <= max(baseline["latency_ms"]["p95"] * 1.5, baseline["latency_ms"]["p95"] + 20),
    }
    required = {
        "same_cas_sentences",
        "same_cas_clause",
        "same_cas_roles",
        "possible_vs_known",
    }
    limits["required_cases"] = all(
        r["all_correct"]
        for r in candidate["rows"]
        if r["id"] in required or r["family"].startswith("unconfirmed")
    )
    families = sorted({r["family"] for r in candidate["rows"]})
    rng = np.random.default_rng(119)
    deltas = []
    for _ in range(2000):
        sampled = rng.choice(families, size=len(families), replace=True)
        arms = []
        for report in (baseline, candidate):
            selected = [
                r for family in sampled for r in report["rows"] if r["family"] == family
            ]
            arms.append(aggregate(selected)["f1"])
        deltas.append(arms[1] - arms[0])
    return {
        "gates": limits,
        "synthetic_gates_passed": all(limits.values()),
        "f1_delta": b["f1"] - a["f1"],
        "f1_delta_family_bootstrap_95ci": np.quantile(deltas, [0.025, 0.975]).tolist(),
        "bootstrap_seed": 119,
        "bootstrap_repeats": 2000,
        "decision": "AWAIT_OFFICIAL_AND_API_GATES"
        if all(limits.values())
        else "REJECT_OR_ANALYZE_DEVELOPMENT_FAILURE",
        "claim_scope": "AI DRAFT 합성 설계 회귀; 사람 정답/현장 일반화/안전성 증명 아님",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolver-model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("기존 평가 보고서는 덮어쓰지 않습니다.")
    actual = sha256_file(args.resolver_model)
    if actual != RESOLVER_SHA256:
        raise ValueError("승인된 평가 Resolver SHA-256 불일치; 로드 중단")
    model = load_resolver(args.resolver_model)
    inputs = cases()
    rows, latency = [], []
    deterministic_parse("기동 확인용 합성 문장", model)
    for case in inputs:
        started = time.perf_counter()
        parsed = deterministic_parse(case["text"], model)
        latency.append((time.perf_counter() - started) * 1000)
        rows.append(score_case(case, parsed))
    report = {
        "schema_version": "parser-uncertainty-evaluation-v1",
        "arm": args.arm,
        "policy_version": INCIDENT_PARSER_POLICY_VERSION,
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "input_sha256": hashlib.sha256(
            json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "resolver_sha256": actual,
        "source_sha256": {
            str(p.relative_to(ROOT)): sha256_file(p)
            for p in (ROOT / "src/chemiguard119").glob("*.py")
        },
        "plan_sha256": sha256_file(ROOT / "docs/PARSER_UNCERTAINTY_PLAN.md"),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "max_rss_platform_units": resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss,
        },
        "case_count": len(rows),
        "family_count": len({r["family"] for r in rows}),
        "label_status": "AI_DRAFT_DESIGN_REGRESSION",
        "human_reviewed": False,
        "metrics": aggregate(rows),
        "rows": rows,
        "latency_ms": {
            "n": len(latency),
            "p50": statistics.median(latency),
            "p95": float(np.quantile(latency, 0.95)),
            "cold_start_included": False,
            "stt_included": False,
        },
    }
    if args.baseline:
        report["comparison"] = compare(json.loads(args.baseline.read_text()), report)
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "arm": args.arm,
                "case_count": len(rows),
                "metrics": report["metrics"],
                "comparison": report.get("comparison"),
                "report_sha256": sha256_file(args.output),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
