"""현재 artifact로 Parser JSON/SSE 전달과 신고 충돌 보류를 검사한다."""

import argparse
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from chemiguard119 import pipeline
from chemiguard119.action_examples import BOTH_CONFIRMED, UNCONFIRMED
from chemiguard119.api import ModelRuntime, create_app
from chemiguard119.parser_uncertainty_cases import cases
from chemiguard119.parser_uncertainty_evaluation import score_case
from chemiguard119.utils import sha256_file, write_json


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--artifact-dir", type=Path, required=True)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    if args.output.exists():
        raise FileExistsError("기존 보고서는 덮어쓰지 않습니다.")
    runtime = ModelRuntime.load(
        db_path=args.artifact_dir / "chemiguard119.sqlite",
        resolver_model_path=args.artifact_dir / "resolver.joblib",
        retriever_model_path=args.artifact_dir / "retriever.joblib",
        environment="development",
    )
    app = create_app(runtime=runtime, api_key="local-evaluation")
    checks, examples = [], {}
    with (
        TestClient(app) as client,
        patch.object(pipeline, "review_pair", wraps=pipeline.review_pair) as rule,
    ):
        client.headers["X-API-Key"] = "local-evaluation"
        for case in cases():
            payload = deepcopy(UNCONFIRMED)
            payload["analysis"]["input"]["text"] = case["text"]
            response = client.post("/api/v1/agents/incidents/brief", json=payload)
            body = response.json()
            good = (
                response.status_code == 200
                and score_case(case, body["facts"])["all_correct"]
                and not body["rule_review"]["executed"]
            )
            checks.append({"id": case["id"], "passed": good})
            if case["id"] in {
                "possible_copula-0",
                "unconfirmed_label-0",
                "same_cas_sentences",
            }:
                examples[case["id"]] = body
        assert rule.call_count == 0
        conflict = deepcopy(BOTH_CONFIRMED)
        conflict["analysis"]["input"]["text"] = "염산은 없습니다. 염산이 누출됩니다."
        body = client.post("/api/v1/agents/incidents/brief", json=conflict).json()
        checks.append(
            {
                "id": "confirmed_statement_conflict",
                "passed": body["status"] == "HELD"
                and not body["rule_review"]["executed"]
                and rule.call_count == 0,
            }
        )
        with client.stream(
            "POST", "/api/v1/agents/incidents/brief/stream", json=payload
        ) as response:
            events = [
                json.loads(line[6:])
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]
        checks.append(
            {
                "id": "sse_parser_delivery",
                "passed": [e["phase"] for e in events] == ["initial", "final"]
                and score_case(case, events[-1]["facts"])["all_correct"],
            }
        )
        checks.append(
            {
                "id": "swagger_contract",
                "passed": client.get("/docs").status_code == 200
                and "/api/v1/agents/incidents/brief"
                in client.get("/openapi.json").json()["paths"],
            }
        )
    report = {
        "schema_version": "parser-api-artifact-regression-v1",
        "checks": checks,
        "passed": all(c["passed"] for c in checks),
        "synthetic_examples": examples,
        "rule_calls": rule.call_count,
        "scope": "실제 artifact + AI DRAFT 합성, TestClient. 현장 안전성·네트워크 속도 평가 아님",
        "evaluator_sha256": sha256_file(Path(__file__)),
    }
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "check_count": len(checks),
                "rule_calls": rule.call_count,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
