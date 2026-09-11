"""실행 중인 로컬 API의 JSON/SSE를 HTTP로 측정한다. 실제 음성은 사용하지 않는다."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from copy import deepcopy
from pathlib import Path

import httpx2 as httpx

from chemiguard119.action_evaluation import distribution
from chemiguard119.action_examples import BOTH_CONFIRMED, CANCELLED, UNCONFIRMED
from chemiguard119.action_models import BriefResponse
from chemiguard119.brief_consumer import BriefConsumer
from chemiguard119.utils import write_json


def main() -> None:
    parser = argparse.ArgumentParser(description="로컬 HTTP 순차 출력 평가")
    parser.add_argument("--url", default="http://127.0.0.1:8011")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, choices=range(1, 21), default=10)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("기존 보고서를 덮어쓰지 않습니다.")
    rows = []
    with httpx.Client(
        base_url=args.url,
        headers={"X-API-Key": os.environ["CHEMIGUARD119_API_KEY"]},
        timeout=35,
    ) as client:
        assert client.get("/health/ready").status_code == 200
        spec_response = client.get("/openapi.json")
        schema = spec_response.json()
        assert schema["paths"]["/api/v1/agents/incidents/brief"]["post"][
            "security"
        ] == [{"APIKeyHeader": []}]
        assert client.get("/docs").status_code == 200
        for index in range(args.repeats):
            for template in (UNCONFIRMED, BOTH_CONFIRMED, CANCELLED):
                payload = deepcopy(template)
                payload["analysis"]["request_id"] = f"REQ-{uuid.uuid4().hex}"
                consumer = BriefConsumer()
                consumer.activate(
                    payload["analysis"]["incident_id"],
                    payload["revision"],
                    payload["analysis"]["request_id"],
                )
                events = []
                started = time.perf_counter()
                with client.stream(
                    "POST", "/api/v1/agents/incidents/brief/stream", json=payload
                ) as response:
                    assert response.status_code == 200
                    for line in response.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        value = json.loads(line[6:])
                        assert consumer.accept(value)
                        BriefResponse.model_validate(value)
                        events.append(
                            {
                                "phase": value["phase"],
                                "status": value["status"],
                                "elapsed_ms": (time.perf_counter() - started) * 1000,
                            }
                        )
                assert [item["phase"] for item in events] == ["initial", "final"]
                assert consumer.snapshot is not None
                rows.append(
                    {
                        "iteration": index,
                        "revision": payload["revision"],
                        "input_characters": len(payload["analysis"]["input"]["text"]),
                        "first_card_ms": round(events[0]["elapsed_ms"], 3),
                        "final_ms": round(events[1]["elapsed_ms"], 3),
                        "status": events[1]["status"],
                        "passed": events[0]["elapsed_ms"] <= events[1]["elapsed_ms"],
                    }
                )
    report = {
        "schema_version": "action-brief-live-http-v1",
        "scope": "LOCALHOST_SYNTHETIC_ACTUAL_ARTIFACT_SERVER",
        "openapi_sha256": hashlib.sha256(spec_response.content).hexdigest(),
        "sample_count": len(rows),
        "first_card": distribution([row["first_card_ms"] for row in rows]),
        "final": distribution([row["final_ms"] for row in rows]),
        "passed": all(row["passed"] for row in rows),
        "runs": rows,
        "stt_included": False,
        "cold_start_included": False,
        "limitations": [
            "기동된 로컬 서버에서 측정. WAN/Cloud/현장/음성 전사 시간 아님",
            "최초 initial 안내와 완성 final은 별도 snapshot",
        ],
    }
    write_json(args.output, report)
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "runs"},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
