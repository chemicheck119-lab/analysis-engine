"""합성 요청으로 SSE를 재현한다. 키는 환경변수에서만 읽으며 출력하지 않는다."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid

import httpx2 as httpx

from chemiguard119.action_examples import EXAMPLES
from chemiguard119.brief_consumer import BriefConsumer


def main() -> None:
    parser = argparse.ArgumentParser(description="행동 카드 SSE 합성 데모")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--example", choices=EXAMPLES, default="unconfirmed")
    args = parser.parse_args()
    payload = json.loads(json.dumps(EXAMPLES[args.example]["value"]))
    payload["analysis"]["request_id"] = f"REQ-{uuid.uuid4().hex}"
    consumer = BriefConsumer()
    consumer.activate(
        payload["analysis"]["incident_id"],
        payload["revision"],
        payload["analysis"]["request_id"],
    )
    started = time.perf_counter()
    with httpx.stream(
        "POST",
        f"{args.url.rstrip('/')}/api/v1/agents/incidents/brief/stream",
        json=payload,
        headers={"X-API-Key": os.environ["CHEMIGUARD119_API_KEY"]},
        timeout=35,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if consumer.accept(event):
                    print(
                        json.dumps(
                            {
                                "phase": event["phase"],
                                "status": event["status"],
                                "elapsed_ms": round(
                                    (time.perf_counter() - started) * 1000, 2
                                ),
                                "summary": event["summary"],
                                "cards": [
                                    {"title": card["title"], "message": card["message"]}
                                    for card in event["cards"]
                                ],
                            },
                            ensure_ascii=False,
                        )
                    )


if __name__ == "__main__":
    main()
