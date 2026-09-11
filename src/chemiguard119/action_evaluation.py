"""실제 artifact + 사전 정의 합성 입력 평가. 현장 정답 라벨 평가가 아니다."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import statistics
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from chemiguard119 import pipeline
from chemiguard119.action_catalog import (
    CATALOG_SHA256,
    CATALOG_VERSION,
    POLICY_VERSION,
    stable_hash,
)
from chemiguard119.action_examples import (
    BOTH_CONFIRMED,
    CANCELLED,
    CONFLICT,
    INVALID_CAS,
    ONE_CONFIRMED,
    UNCONFIRMED,
    confirmation,
)
from chemiguard119.action_models import BriefResponse
from chemiguard119.api import ModelRuntime, create_app
from chemiguard119.brief_orchestrator import BriefOrchestrator
from chemiguard119.utils import write_json

ROOT = Path(__file__).resolve().parents[2]


def scenarios() -> list[dict]:
    cases = []

    def add(
        name: str,
        payload: dict,
        *,
        calls: int = 0,
        status: str | None = None,
        http: int = 200,
        text: str | None = None,
    ) -> dict:
        value = deepcopy(payload)
        value["analysis"]["request_id"] = f"REQ-EVAL-{name}"
        if text:
            value["analysis"]["input"]["text"] = text
        item = {
            "id": name,
            "input": value,
            "expected": {"rule_calls": calls, "http": http, "status": status},
        }
        cases.append(item)
        return value

    add("normal_unconfirmed", UNCONFIRMED, status="NEEDS_CONFIRMATION")
    add(
        "asr_wrong_material",
        UNCONFIRMED,
        text="나트륨 탱크에서 누출이 있습니다.",
        status="NEEDS_CONFIRMATION",
    )
    add(
        "ambiguous_expression",
        UNCONFIRMED,
        text="산성 세척제인지 모르겠습니다.",
        status="NEEDS_CONFIRMATION",
    )
    add(
        "unknown_product",
        UNCONFIRMED,
        text="시험용미등록제품XYZ 통에서 액체가 새고 있습니다.",
        status="NEEDS_CONFIRMATION",
    )
    add(
        "negation",
        UNCONFIRMED,
        text="염산은 아닙니다. 미상 물질입니다.",
        status="NEEDS_CONFIRMATION",
    )
    add(
        "uncertainty",
        UNCONFIRMED,
        text="염산인 것 같습니다. 아직 확인하지 못했습니다.",
        status="NEEDS_CONFIRMATION",
    )
    absent = add("no_facility_history", UNCONFIRMED, status="NEEDS_CONFIRMATION")
    absent["analysis"]["location"] = {"facility_name": "합성평가시설ZZQ999"}
    add("one_confirmed", ONE_CONFIRMED, status="NEEDS_CONFIRMATION")
    add("both_confirmed", BOTH_CONFIRMED, calls=1)
    absent = add(
        "missing_cas_evidence",
        ONE_CONFIRMED,
        text="미상 물질의 자료를 확인합니다.",
        status="HELD",
    )
    # 등록 물질이라고 주장하지 않는 checksum-valid 합성 식별자. 인덱스 부재 경계만 검사.
    absent["analysis"]["confirmed_incident_substance"] = confirmation(
        "INCIDENT", "9999999-99-5"
    )
    unsupported = add(
        "unsupported_pair",
        BOTH_CONFIRMED,
        text="미상 물질 두 종류의 자료를 확인합니다.",
        status="HELD",
        calls=1,
    )
    unsupported["analysis"]["confirmed_incident_substance"] = confirmation(
        "INCIDENT", "9999999-99-5"
    )
    add("invalid_checksum", INVALID_CAS, http=422)
    add("cancelled", CANCELLED, status="NEEDS_CONFIRMATION")
    add("reported_conflict", CONFLICT, status="NEEDS_CONFIRMATION")
    add(
        "changed_identity",
        BOTH_CONFIRMED,
        text="나트륨 탱크에서 누출이 있고 옆 저장고에는 염산이 있습니다.",
        status="HELD",
    )
    return cases


def semantic_response(value: dict) -> dict:
    # 시간·실행 계획만 제외한다. 사용자에게 보이는 카드·CAS·출처·확인·규칙은 전부 비교.
    return {
        key: item
        for key, item in value.items()
        if key not in {"processing", "request_id"}
    }


def safety_checks(
    value: dict, actual_rule_calls: int, expected: dict
) -> dict[str, bool]:
    parsed = BriefResponse.model_validate(value)
    pair = all(parsed.confirmation_state.values())
    sources = {source.source_id: source for source in parsed.sources}
    refs = [card for card in parsed.cards if card.category == "대응 참고"]
    return {
        "expected_rule_calls": actual_rule_calls == expected["rule_calls"],
        "no_rule_before_two_confirmations": pair or actual_rule_calls == 0,
        "no_risk_before_confirmation": pair or not parsed.rule_review.get("risk_level"),
        "candidates_never_confirmed": all(
            item.get("requires_responder_confirmation") is True
            for item in parsed.substance_candidates
        ),
        "reference_cas_role_matches": all(
            any(
                sources[key].cas_number == card.cas_number
                and sources[key].role == card.role
                for key in card.source_ids
            )
            for card in refs
        ),
        "reference_has_no_missing_conditions": all(
            not card.unmet_conditions for card in refs
        ),
        "no_unreviewed_tactical_authorization": all(
            not card.tactical_authorization
            and card.review_status == "DRAFT_NOT_EXPERT_REVIEWED"
            for card in parsed.cards
        ),
        "expected_status": expected["status"] is None
        or parsed.status == expected["status"],
    }


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[max(0, int(0.95 * len(ordered) + 0.999) - 1)], 3),
        "max_ms": round(max(ordered), 3),
    }


def evaluate(
    runtime: ModelRuntime, output: Path, repeats: int = 3, reverse_order: bool = False
) -> dict:
    if output.exists():
        raise ValueError(
            "기존 평가 디렉터리를 덮어쓰지 않습니다. 새 경로를 사용하세요."
        )
    output.mkdir(parents=True)
    cases = scenarios()
    write_json(output / "inputs.synthetic.json", cases)
    runs = []
    semantic = {}
    first_runs = []
    original_review = pipeline.review_pair
    # 계측용 wrapping만 사용한다. 실제 Rule·Parser·검색 반환값을 mock으로 대체하지 않는다.
    with patch.object(pipeline, "review_pair", wraps=original_review) as counted:
        for parallel in (True, False) if reverse_order else (False, True):
            app = create_app(runtime=runtime, api_key="synthetic-local-evaluation")
            app.state.brief_orchestrator.close()
            app.state.brief_orchestrator = BriefOrchestrator(parallel=parallel)
            mode = "parallel" if parallel else "sequential"
            with TestClient(app) as client:
                for iteration in range(repeats + 1):
                    for case in cases:
                        counted.reset_mock()
                        started = time.perf_counter()
                        response = client.post(
                            "/api/v1/agents/incidents/brief",
                            json=case["input"],
                            headers={"X-API-Key": "synthetic-local-evaluation"},
                        )
                        elapsed = (time.perf_counter() - started) * 1000
                        value = response.json()
                        checks = {
                            "expected_http": response.status_code
                            == case["expected"]["http"]
                        }
                        if response.status_code == 200:
                            checks.update(
                                safety_checks(
                                    value, counted.call_count, case["expected"]
                                )
                            )
                            digest = stable_hash(semantic_response(value))
                            key = case["id"]
                            if key in semantic:
                                checks["same_semantic_result"] = digest == semantic[key]
                            else:
                                semantic[key] = digest
                        else:
                            checks["no_rule_on_schema_error"] = counted.call_count == 0
                        row = {
                            "scenario_id": case["id"],
                            "mode": mode,
                            "iteration": iteration,
                            "http": response.status_code,
                            "status": value.get("status"),
                            "rule_calls": counted.call_count,
                            "latency_ms": round(elapsed, 3),
                            "checks": checks,
                            "passed": all(checks.values()),
                            "input_characters": len(
                                case["input"]["analysis"]["input"]["text"]
                            ),
                            "failure_code": value.get("processing", {}).get(
                                "failure_code"
                            ),
                            "reference_card_count": sum(
                                card["category"] == "대응 참고"
                                for card in value.get("cards", [])
                            ),
                        }
                        (first_runs if iteration == 0 else runs).append(row)
                        if iteration == 0:
                            write_json(output / f"{mode}-{case['id']}.json", value)
    warm = {
        mode: distribution(
            [
                row["latency_ms"]
                for row in runs
                if row["mode"] == mode and row["http"] == 200
            ]
        )
        for mode in ("sequential", "parallel")
    }
    failures = [row for row in first_runs + runs if not row["passed"]]

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    tracked_code = sorted((ROOT / "src" / "chemiguard119").glob("*.py"))
    report = {
        "schema_version": "action-brief-artifact-evaluation-v1",
        "data_status": "INTERNAL_SYNTHETIC_NO_EXPERT_LABELS",
        "artifact_execution": "ACTUAL_LOCAL_ARTIFACTS_NO_MODEL_RESPONSE_MOCKS",
        "mock_fault_tests": "tests/test_action_brief.py (별도 실행)",
        "scenario_count": len(cases),
        "warm_repeats_per_mode": repeats,
        "mode_order": ["parallel", "sequential"]
        if reverse_order
        else ["sequential", "parallel"],
        "first_pass": first_runs,
        "runs": runs,
        "failure_count": len(failures),
        "failures": failures,
        "warm_json_latency": warm,
        "cold_scope": "첫 pass는 같은 로드된 runtime의 첫 요청 집합. OS cold cache/독립 cold start 비교 아님.",
        "first_sse_card_latency": "별도 실제 HTTP SSE 측정 필요(TestClient buffering 수치 사용 금지)",
        "stt_included": False,
        "environment": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "processor": platform.processor(),
        },
        "git_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "inputs_sha256": sha(output / "inputs.synthetic.json"),
        "catalog": CATALOG_VERSION,
        "catalog_sha256": CATALOG_SHA256,
        "policy": POLICY_VERSION,
        "artifact_sha256": {
            "sqlite": sha(runtime.db_path),
            "resolver": sha(runtime.resolver_model_path),
            "retriever": sha(runtime.retriever_model_path),
        },
        "source_sha256": {
            str(path.relative_to(ROOT)): sha(path) for path in tracked_code
        },
        "config_sha256": {
            str(path.relative_to(ROOT)): sha(path)
            for path in sorted(runtime.config_dir.glob("*"))
            if path.is_file() and path.suffix in {".json", ".csv"}
        },
        "decision": "CONDITIONAL_DEVELOPMENT_ADOPTION"
        if not failures
        else "REJECT_PENDING_FIX",
        "parallel_speed_claim": "SUPPORTED_IN_THIS_LOCAL_SAMPLE"
        if not failures
        and warm["parallel"]["p50_ms"] < warm["sequential"]["p50_ms"]
        and warm["parallel"]["p95_ms"] <= warm["sequential"]["p95_ms"]
        else "NOT_SUPPORTED_BY_THIS_SAMPLE",
        "additional_server_cost_krw": 0,
        "cumulative_account_cost": "NOT_VERIFIED",
        "limitations": [
            "현장 안전·사용자 시간 절감·STT 정확도를 검증한 평가가 아님",
            "문구 전문 검수 0건, 기관 SOP 승인 없음",
            "부정·추정은 기존 Parser 범위만 지원",
            "semantic_response 비교는 검색 품질 정답 판정이 아님",
        ],
    }
    write_json(output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="행동 카드 실제 artifact 평가: 내부 합성 입력"
    )
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=3)
    parser.add_argument(
        "--reverse-order",
        action="store_true",
        help="실행 순서 편향을 확인하기 위해 병렬→순차로 반복합니다.",
    )
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    runtime_started = time.perf_counter()
    runtime = ModelRuntime.load(
        db_path=args.artifact_dir / "chemiguard119.sqlite",
        resolver_model_path=args.artifact_dir / "resolver.joblib",
        retriever_model_path=args.artifact_dir / "retriever.joblib",
        config_dir=ROOT / "config",
        environment="development",
    )
    load_ms = (time.perf_counter() - runtime_started) * 1000
    report = evaluate(runtime, args.output, args.repeats, args.reverse_order)
    report["runtime_load_ms"] = round(load_ms, 3)
    write_json(args.output / "report.json", report)
    print(
        json.dumps(
            {
                "report": str(args.output / "report.json"),
                "scenarios": report["scenario_count"],
                "failure_count": report["failure_count"],
                "decision": report["decision"],
                "warm_json_latency": report["warm_json_latency"],
                "parallel_speed_claim": report["parallel_speed_claim"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if report["failure_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
