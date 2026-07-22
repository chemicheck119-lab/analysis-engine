#!/usr/bin/env python3
"""LM Studio OpenAI 호환 API에서 케미가드119용 구조화 모델을 비교한다.

새 모델을 내려받거나 로드하지 않는다. LM Studio 서버에 이미 로드된 모델 식별자를
인자로 받아, 동일한 한국어 신고문과 JSON Schema를 사용해 정합성·지연시간을 측정한다.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "outputs" / "lmstudio_benchmark"


SUBSTANCE_REGISTRY = """
검증된 물질 사전:
- 염산 | 염화수소 | HCl | Hydrochloric acid = 염화수소, CAS 7647-01-0
- 아세톤 | Acetone = 아세톤, CAS 67-64-1
- 차아염소산나트륨 | Sodium hypochlorite = 차아염소산나트륨, CAS 7681-52-9
- 금속 나트륨 | Sodium metal = 나트륨, CAS 7440-23-5

검증되지 않은 제품명·현장 속칭·냄새 표현은 특정 CAS로 확정하지 않고
UNRESOLVED로 남긴다.

예정 대응 코드:
- 물 뿌리기 | 주수 = WATER_APPLICATION
- 물안개 | 분무주수 = WATER_FOG
- 환기 = VENTILATION
- 포 소화 = FOAM
- 배수로 차단 = DRAIN_BLOCK
""".strip()


SYSTEM_PROMPT = f"""
너는 119 화학사고 신고문 구조화기다. 화학적 위험을 판단하거나 대응을 지시하지 않는다.
원문에 있는 사실만 구조화하고, 아래 사전에 없는 물질·CAS·대응코드를 새로 만들지 않는다.
부정 범위를 정확히 지킨다. '염산이 아니다'는 염산만 NEGATED이며 뒤에 나오는 다른 물질을
부정하지 않는다. '같다', '의심', '일 수도'는 확정이 아니라 CANDIDATE다. 모호하거나 물질을
모르면 UNRESOLVED로 남기고 needs_substance_confirmation을 true로 설정한다.

역할:
- INCIDENT: 사고 원인 또는 누출·화재 물질로 명시
- FACILITY: 주변·창고·시설에 있다고 언급된 물질
- NEGATED: 원문이 명시적으로 아니라고 한 물질
- UNKNOWN: 역할도 확정할 수 없음

{SUBSTANCE_REGISTRY}
""".strip()


JSON_SCHEMA = {
    "name": "chemguard_incident_structure",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "incident_types": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["LEAK", "FIRE", "EXPLOSION", "UNKNOWN"],
                },
            },
            "substances": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "raw_text": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": ["INCIDENT", "FACILITY", "NEGATED", "UNKNOWN"],
                        },
                        "canonical_name": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                        "cas_number": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                        "resolution_status": {
                            "type": "string",
                            "enum": ["EXACT", "CANDIDATE", "UNRESOLVED", "NEGATED"],
                        },
                    },
                    "required": [
                        "raw_text",
                        "role",
                        "canonical_name",
                        "cas_number",
                        "resolution_status",
                    ],
                },
            },
            "planned_actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "raw_text": {"type": "string"},
                        "action_code": {
                            "anyOf": [{"type": "string"}, {"type": "null"}]
                        },
                        "resolution_status": {
                            "type": "string",
                            "enum": ["EXACT", "CANDIDATE", "UNRESOLVED"],
                        },
                    },
                    "required": ["raw_text", "action_code", "resolution_status"],
                },
            },
            "fire_status": {"type": "string", "enum": ["TRUE", "FALSE", "UNKNOWN"]},
            "needs_substance_confirmation": {"type": "boolean"},
            "missing_fields": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "incident_types",
            "substances",
            "planned_actions",
            "fire_status",
            "needs_substance_confirmation",
            "missing_fields",
        ],
    },
}


CASES: list[dict[str, Any]] = [
    {
        "id": "simple_hcl_leak",
        "text": "염산 탱크에서 새고 있어. 불은 안 났고 물 뿌리는 걸 검토 중이야.",
        "incident_type": "LEAK",
        "fire_status": "FALSE",
        "needs_confirmation": False,
        "required_substances": [("7647-01-0", "INCIDENT", "EXACT")],
        "required_actions": ["WATER_APPLICATION"],
    },
    {
        "id": "negation_scope",
        "text": "염산 아니다. 아세톤 드럼에서 새고 있고 화재는 없어.",
        "incident_type": "LEAK",
        "fire_status": "FALSE",
        "needs_confirmation": False,
        "required_substances": [
            ("7647-01-0", "NEGATED", "NEGATED"),
            ("67-64-1", "INCIDENT", "EXACT"),
        ],
        "required_actions": [],
    },
    {
        "id": "ambiguous_bleach",
        "text": "자극성 냄새가 나는 액체가 새는데 제품명은 모르겠어.",
        "incident_type": "LEAK",
        "fire_status": "UNKNOWN",
        "needs_confirmation": True,
        "required_substances": [(None, "INCIDENT", "UNRESOLVED")],
        "required_actions": [],
    },
    {
        "id": "unknown_substance",
        "text": "흰 연기가 보이는데 정확한 물질명도 모르고 누출인지도 모르겠어요.",
        "incident_type": "UNKNOWN",
        "fire_status": "UNKNOWN",
        "needs_confirmation": True,
        "required_substances": [(None, "UNKNOWN", "UNRESOLVED")],
        "required_actions": [],
    },
    {
        "id": "incident_and_facility",
        "text": "차아염소산나트륨이 누출됐고 옆 저장고에는 염산이 있어. 물안개 분사를 검토 중이야.",
        "incident_type": "LEAK",
        "fire_status": "UNKNOWN",
        "needs_confirmation": False,
        "required_substances": [
            ("7681-52-9", "INCIDENT", "EXACT"),
            ("7647-01-0", "FACILITY", "EXACT"),
        ],
        "required_actions": ["WATER_FOG"],
    },
    {
        "id": "english_cas_ventilation",
        "text": "HCl 7647-01-0 leak, no fire. 지금 환기 중입니다.",
        "incident_type": "LEAK",
        "fire_status": "FALSE",
        "needs_confirmation": False,
        "required_substances": [("7647-01-0", "INCIDENT", "EXACT")],
        "required_actions": ["VENTILATION"],
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models", nargs="+", required=True, help="LM Studio에 로드된 모델 식별자"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--case-ids",
        nargs="+",
        choices=[case["id"] for case in CASES],
        help="지정한 핵심 사례만 실행한다. 생략하면 전체 사례를 실행한다.",
    )
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="별도 워밍업 요청을 생략해 짧은 점검을 빠르게 끝낸다.",
    )
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def request_model(
    base_url: str, model: str, text: str, timeout: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "max_tokens": 700,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "response_format": {"type": "json_schema", "json_schema": JSON_SCHEMA},
    }
    started = time.perf_counter()
    response = post_json(f"{base_url.rstrip('/')}/chat/completions", payload, timeout)
    latency = time.perf_counter() - started
    content = response["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    meta = {
        "latency_seconds": latency,
        "prompt_tokens": response.get("usage", {}).get("prompt_tokens"),
        "completion_tokens": response.get("usage", {}).get("completion_tokens"),
        "finish_reason": response.get("choices", [{}])[0].get("finish_reason"),
    }
    return parsed, meta


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return ordered[index]


def find_substance(
    output: dict[str, Any], expected: tuple[str | None, str, str]
) -> bool:
    expected_cas, expected_role, expected_status = expected
    for item in output.get("substances", []):
        cas_match = item.get("cas_number") == expected_cas
        role_match = item.get("role") == expected_role
        status_match = item.get("resolution_status") == expected_status
        if cas_match and role_match and status_match:
            return True
    return False


def score_output(case: dict[str, Any], output: dict[str, Any]) -> dict[str, bool]:
    action_codes = {
        item.get("action_code") for item in output.get("planned_actions", [])
    }
    checks: dict[str, bool] = {
        "incident_type": case["incident_type"] in output.get("incident_types", []),
        "fire_status": output.get("fire_status") == case["fire_status"],
        "confirmation": output.get("needs_substance_confirmation")
        is case["needs_confirmation"],
        "actions": all(code in action_codes for code in case["required_actions"]),
    }
    for index, expected in enumerate(case["required_substances"], 1):
        checks[f"substance_{index}"] = find_substance(output, expected)
    return checks


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    selected_cases = [
        case for case in CASES if not args.case_ids or case["id"] in args.case_ids
    ]
    partial_path = (
        args.output_dir / f"lmstudio_incident_parser_{timestamp}_partial.json"
    )
    results: list[dict[str, Any]] = []

    for model in args.models:
        if not args.skip_warmup:
            try:
                request_model(
                    args.base_url, model, selected_cases[0]["text"], args.timeout
                )
            except Exception:
                # 워밍업 실패는 본 시험에서 상세 오류로 기록한다.
                pass
        for repeat in range(1, args.repeats + 1):
            for case in selected_cases:
                row: dict[str, Any] = {
                    "model": model,
                    "case_id": case["id"],
                    "repeat": repeat,
                    "input": case["text"],
                }
                try:
                    output, meta = request_model(
                        args.base_url, model, case["text"], args.timeout
                    )
                    checks = score_output(case, output)
                    row.update(
                        {
                            "schema_valid": True,
                            "checks": checks,
                            "passed_checks": sum(checks.values()),
                            "total_checks": len(checks),
                            "pass_rate": sum(checks.values()) / len(checks),
                            "output": output,
                            "error": None,
                            **meta,
                        }
                    )
                except (
                    HTTPError,
                    URLError,
                    TimeoutError,
                    KeyError,
                    ValueError,
                    json.JSONDecodeError,
                ) as error:
                    row.update(
                        {
                            "schema_valid": False,
                            "checks": {},
                            "passed_checks": 0,
                            "total_checks": 0,
                            "pass_rate": 0.0,
                            "output": None,
                            "error": f"{type(error).__name__}: {error}",
                            "latency_seconds": None,
                            "prompt_tokens": None,
                            "completion_tokens": None,
                            "finish_reason": None,
                        }
                    )
                results.append(row)
                partial_path.write_text(
                    json.dumps(
                        {
                            "created_at_utc": datetime.now(timezone.utc).isoformat(),
                            "models": args.models,
                            "selected_case_ids": [
                                case["id"] for case in selected_cases
                            ],
                            "results": results,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(
                    f"{model} | {case['id']} | repeat={repeat} | "
                    f"schema={row['schema_valid']} | score={row['passed_checks']}/{row['total_checks']} | "
                    f"latency={row['latency_seconds']}",
                    flush=True,
                )

    summary: list[dict[str, Any]] = []
    for model in args.models:
        model_rows = [row for row in results if row["model"] == model]
        latencies = [
            float(row["latency_seconds"])
            for row in model_rows
            if row["latency_seconds"] is not None
        ]
        passed = sum(int(row["passed_checks"]) for row in model_rows)
        total = sum(int(row["total_checks"]) for row in model_rows)
        summary.append(
            {
                "model": model,
                "runs": len(model_rows),
                "schema_success_rate": sum(
                    bool(row["schema_valid"]) for row in model_rows
                )
                / len(model_rows),
                "semantic_check_pass_rate": passed / total if total else 0.0,
                "average_latency_seconds": statistics.mean(latencies)
                if latencies
                else None,
                "p95_latency_seconds": percentile(latencies, 0.95),
                "errors": sum(row["error"] is not None for row in model_rows),
            }
        )

    artifact = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "models": args.models,
        "repeats": args.repeats,
        "case_count": len(selected_cases),
        "selected_case_ids": [case["id"] for case in selected_cases],
        "note": "안전 인증 결과가 아니라 설치 모델의 1차 선별 벤치마크",
        "summary": summary,
        "results": results,
    }
    json_path = args.output_dir / f"lmstudio_incident_parser_{timestamp}.json"
    csv_path = args.output_dir / f"lmstudio_incident_parser_{timestamp}_summary.csv"
    json_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary[0].keys()))
        writer.writeheader()
        writer.writerows(summary)
    partial_path.unlink(missing_ok=True)

    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(json_path, flush=True)
    print(csv_path, flush=True)


if __name__ == "__main__":
    main()
