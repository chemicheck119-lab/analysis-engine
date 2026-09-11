"""공개 OpenAPI의 합성 예시로 재현 가능한 Postman collection을 만든다.

기본 출력은 stdout이다. 모델·Secret·네트워크를 사용하거나 API를 호출하지 않는다.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts/generated/model-api-v1.openapi.json"
OUTPUT = ROOT / "examples/api/chemicheck119-model-api.postman_collection.json"
BRIEF = "/api/v1/agents/incidents/brief"


def build_collection() -> dict:
    raw = CONTRACT.read_bytes()
    spec = json.loads(raw)
    examples = spec["paths"][BRIEF]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    base = deepcopy(examples["unconfirmed"]["value"]["analysis"])
    both = examples["both_confirmed"]["value"]["analysis"]
    payloads = {
        "/api/v1/incidents/analyze": base,
        "/api/v1/agents/incidents/step": {"analysis": base, "max_actions": 6},
        "/api/v1/substances/resolve": {"query": "염산", "top_k": 3},
        "/api/v1/substances/discover": {
            "query": "염산",
            "top_k": 3,
            "evidence_top_k": 3,
        },
        "/api/v1/evidence/search": {
            "query": "염산의 누출 대응 근거",
            "cas_hint": "7647-01-0",
            "cas_hint_status": "RESOLVER_CANDIDATE",
            "top_k": 5,
        },
        "/api/v1/facilities/candidates": {
            "query": "합성 예시 사업장",
            "province": "울산광역시",
            "top_k": 10,
        },
        "/api/v1/conflicts/review": {
            "incident": both["confirmed_incident_substance"],
            "facility": both["confirmed_facility_substance"],
        },
    }
    groups = {"01 상태 확인": [], "02 행동 카드": [], "03 개별 기능": []}
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post"}:
                continue
            variants = (
                [
                    (key, examples[key]["value"])
                    for key in (
                        "unconfirmed",
                        "one_confirmed",
                        "both_confirmed",
                        "cancelled",
                        "conflict",
                        "no_evidence",
                        "invalid_cas",
                    )
                ]
                if path == BRIEF
                else [("unconfirmed", examples["unconfirmed"]["value"])]
                if path == BRIEF + "/stream"
                else [("default", payloads.get(path))]
            )
            for key, payload in variants:
                title = (
                    examples[key]["summary"]
                    if key != "default"
                    else operation["summary"]
                )
                request = {
                    "method": method.upper(),
                    "url": "{{base_url}}" + path,
                    "header": [
                        {
                            "key": "Authorization",
                            "value": "Bearer {{id_token}}",
                            "disabled": True,
                            "description": "기본 프록시에서는 끔. Cloud Run 직접 호출 시에만 유효한 ID token으로 활성화.",
                        }
                    ],
                    "description": (
                        "합성 테스트 전용. 실제 확인 기록·현장 검증 결과가 아닙니다. "
                        "기본 base_url은 IAM 인증 프록시이고 api_key는 개인 환경에서만 설정하세요. "
                        "no_evidence와 시설 예시의 검색 결과는 artifact에 따라 달라집니다. "
                        "SSE는 initial/final 전체 snapshot을 교체하며 자동 재연결하지 않습니다."
                    ),
                }
                if method == "get":
                    request["auth"] = {"type": "noauth"}
                if payload is not None:
                    request["header"].append(
                        {"key": "Content-Type", "value": "application/json"}
                    )
                    request["body"] = {
                        "mode": "raw",
                        "raw": json.dumps(payload, ensure_ascii=False, indent=2),
                        "options": {"raw": {"language": "json"}},
                    }
                expected = 422 if key == "invalid_cas" else 200
                checks = [
                    f'pm.test("HTTP {expected}", function () {{ pm.response.to.have.status({expected}); }});'
                ]
                if path == BRIEF and key != "invalid_cas":
                    checks += [
                        "const body = pm.response.json();",
                        'pm.test("행동 카드 계약", function () { pm.expect(body.schema_version).to.eql("action-brief-v1"); pm.expect(body.phase).to.eql("final"); });',
                        'pm.test("전술 승인 없음", function () { body.cards.forEach(card => pm.expect(card.tactical_authorization).to.eql(false)); });',
                    ]
                    if key in {"unconfirmed", "one_confirmed", "cancelled", "conflict"}:
                        checks.append(
                            'pm.test("확인 Gate 잠금", function () { pm.expect(body.rule_review.executed).to.eql(false); });'
                        )
                item = {
                    "name": f"{method.upper()} {path} · {title}",
                    "request": request,
                    "event": [
                        {
                            "listen": "test",
                            "script": {"type": "text/javascript", "exec": checks},
                        }
                    ],
                    "response": [],
                }
                group = (
                    "01 상태 확인"
                    if method == "get"
                    else "02 행동 카드"
                    if path.startswith(BRIEF)
                    else "03 개별 기능"
                )
                groups[group].append(item)
    return {
        "info": {
            "name": "케미체크119 모델 API · 합성 연동 테스트",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "description": (
                "docs/API.md가 공식 명세서입니다. 공개 Swagger 서버는 실행 대상이 아닙니다. "
                "모든 요청은 공개 합성 입력이며 API 키·ID token은 포함하지 않습니다. "
                "개별 Send로 검증하세요. 예상 결과는 계약 검사이지 현장 정확도 증명이 아닙니다. "
                "OpenAPI SHA-256: " + hashlib.sha256(raw).hexdigest()
            ),
        },
        "auth": {
            "type": "apikey",
            "apikey": [
                {"key": "key", "value": "X-API-Key", "type": "string"},
                {"key": "value", "value": "{{api_key}}", "type": "string"},
                {"key": "in", "value": "header", "type": "string"},
            ],
        },
        "variable": [
            {"key": "base_url", "value": "http://127.0.0.1:8087", "type": "string"},
            {"key": "api_key", "value": "", "type": "string"},
            {"key": "id_token", "value": "", "type": "string"},
        ],
        "item": [{"name": name, "item": items} for name, items in groups.items()],
    }


def serialize() -> str:
    return json.dumps(build_collection(), ensure_ascii=False, indent=2) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = serialize()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != expected:
            raise SystemExit("Postman collection이 공개 OpenAPI·생성 규칙과 다릅니다.")
        print("Postman collection 일치")
    else:
        print(expected, end="")


if __name__ == "__main__":
    main()
