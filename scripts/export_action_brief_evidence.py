"""잠금 hash가 일치하는 과거 보고서의 비민감 계측 열만 공개한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from chemiguard119.action_evaluation import distribution

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "data/evaluation/action_brief_v1_summary.json"
DESTINATION = ROOT / "data/evaluation/action_brief_v1_runs.json"
ROW_KEYS = (
    "scenario_id",
    "mode",
    "iteration",
    "http",
    "status",
    "rule_calls",
    "latency_ms",
    "checks",
    "passed",
    "input_characters",
    "failure_code",
    "reference_card_count",
)
HTTP_KEYS = (
    "iteration",
    "revision",
    "status",
    "passed",
    "input_characters",
    "first_card_ms",
    "final_ms",
)


def safe_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        return value
    raise ValueError("비공개 텍스트 또는 알 수 없는 계측 형식: 공개 중단")


def project_row(row, keys):
    result = {}
    for key in keys:
        value = row[key]
        if key == "checks":
            if not isinstance(value, dict) or not all(
                type(v) is bool for v in value.values()
            ):
                raise ValueError("검사 결과는 bool이어야 합니다")
            result[key] = {safe_scalar(k): v for k, v in value.items()}
        else:
            result[key] = safe_scalar(value)
    return result


def recompute(report):
    if "first_pass" not in report:
        rows = report["runs"]
        return {
            "sample_count": len(rows),
            "passed": all(row["passed"] for row in rows),
            "first_card": distribution([row["first_card_ms"] for row in rows]),
            "final": distribution([row["final_ms"] for row in rows]),
        }
    rows = report["first_pass"] + report["runs"]
    return {
        "execution_count": len(rows),
        "http_200": sum(row["http"] == 200 for row in rows),
        "http_422": sum(row["http"] == 422 for row in rows),
        "rule_calls": sum(row["rule_calls"] for row in rows),
        "failure_count": sum(not row["passed"] for row in rows),
        "semantic_checks": {
            "compared": sum("same_semantic_result" in row["checks"] for row in rows),
            "failed": sum(
                row["checks"].get("same_semantic_result") is False for row in rows
            ),
        },
        "warm_json_latency": {
            mode: distribution(
                [
                    row["latency_ms"]
                    for row in report["runs"]
                    if row["mode"] == mode and row["http"] == 200
                ]
            )
            for mode in ("sequential", "parallel")
        },
    }


def export(private_root: Path, summary: dict) -> dict:
    reports = {}
    for name, expected in summary["report_sha256"].items():
        raw = (private_root / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("잠금 보고서 hash 불일치")
        source = json.loads(raw)
        public = {"private_report_sha256": expected}
        if "first_pass" in source:
            for key in (
                "git_head",
                "inputs_sha256",
                "catalog",
                "catalog_sha256",
                "policy",
            ):
                public[key] = safe_scalar(source[key])
            for key in ("source_sha256", "config_sha256", "artifact_sha256"):
                values = source[key]
                if not all(re.fullmatch(r"[a-f0-9]{64}", v) for v in values.values()):
                    raise ValueError("잘못된 provenance hash")
                if not all(
                    re.fullmatch(r"[A-Za-z0-9_./-]+", k)
                    and not k.startswith("/")
                    and ".." not in k
                    for k in values
                ):
                    raise ValueError("공개할 수 없는 provenance 경로")
                public[key] = values
            public["first_pass"] = [
                project_row(row, ROW_KEYS) for row in source["first_pass"]
            ]
            public["runs"] = [project_row(row, ROW_KEYS) for row in source["runs"]]
        else:
            public["runs"] = [project_row(row, HTTP_KEYS) for row in source["runs"]]
        public["recomputed"] = recompute(public)
        for key in (
            "failure_count",
            "warm_json_latency",
            "sample_count",
            "passed",
            "first_card",
            "final",
        ):
            if key in source and source[key] != public["recomputed"][key]:
                raise ValueError("원 보고서와 공개 계측 재계산 불일치")
        reports[name] = public
    return {
        "schema_version": "action-brief-public-measurements-v1",
        "scope": "HISTORICAL_INTERNAL_SYNTHETIC_MEASUREMENTS_NOT_FIELD_VALIDATION",
        "limitations": [
            "원문 입력·전체 모델 응답·문서 본문·가중치는 공개하지 않음",
            "semantic 검사는 당시 기록된 bool의 집계이며 독립적인 응답 내용 재검수 아님",
            "source_sha256은 미커밋 변경을 포함한 당시 실행 bytes 식별자; git_head만으로 실행 버전을 대체하지 않음",
        ],
        "reports": reports,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        value = json.loads(DESTINATION.read_text())
        for report in value["reports"].values():
            if recompute(report) != report["recomputed"]:
                raise ValueError("공개 계측 집계 불일치")
        print(f"공개 보고서 {len(value['reports'])}개 집계 재계산 통과")
    else:
        if args.private_root is None:
            parser.error("--private-root 또는 --verify가 필요합니다")
        value = export(args.private_root, json.loads(SUMMARY.read_text()))
        DESTINATION.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
