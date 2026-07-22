"""LM Studio OpenAI 호환 API의 선택적 신고문 구조화 클라이언트."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen

from chemiguard119.incident import validate_parser_output


PARSER_SCHEMA = {
    "name": "chemiguard119_incident_parser",
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
            "fire_status": {"type": "string", "enum": ["TRUE", "FALSE", "UNKNOWN"]},
            "substance_mentions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "surface_text": {"type": "string"},
                        "role": {
                            "type": "string",
                            "enum": ["INCIDENT", "FACILITY", "NEGATED", "UNKNOWN"],
                        },
                        "assertion": {
                            "type": "string",
                            "enum": ["AFFIRMED", "POSSIBLE", "NEGATED", "UNKNOWN"],
                        },
                    },
                    "required": ["surface_text", "role", "assertion"],
                },
            },
            "planned_actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "surface_text": {"type": "string"},
                        "action_code": {
                            "type": ["string", "null"],
                            "enum": [
                                "WATER_APPLICATION",
                                "WATER_FOG",
                                "VENTILATION",
                                "FOAM",
                                "DRAIN_BLOCK",
                                None,
                            ],
                        },
                    },
                    "required": ["surface_text", "action_code"],
                },
            },
            "needs_substance_confirmation": {"type": "boolean"},
            "missing_fields": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "incident_types",
            "fire_status",
            "substance_mentions",
            "planned_actions",
            "needs_substance_confirmation",
            "missing_fields",
        ],
    },
}


SYSTEM_PROMPT = """너는 119 화학사고 신고문 구조화기다.
원문에 명시된 표현만 구조화한다. 화학물질 CAS, 위험등급, 반응, 대응 권고를 추측하지 않는다.
'아니다', '없다'의 부정 범위를 지킨다. '같다', '의심', '일 수도'는 POSSIBLE이다.
사고물질과 시설·주변 물질의 역할을 구분할 수 없으면 UNKNOWN으로 둔다.
원문에 없는 물질이나 대응은 절대 추가하지 않는다."""


def _request_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def list_models(
    base_url: str = "http://127.0.0.1:1234/v1", timeout: int = 3
) -> list[dict[str, Any]]:
    with urlopen(f"{base_url.rstrip('/')}/models", timeout=timeout) as response:
        payload = json.load(response)
    return payload.get("data", [])


def parse_with_lmstudio(
    text: str,
    model: str,
    base_url: str = "http://127.0.0.1:1234/v1",
    timeout: int = 120,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0,
        "seed": 42,
        "max_tokens": 500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "response_format": {"type": "json_schema", "json_schema": PARSER_SCHEMA},
    }
    response = _request_json(
        f"{base_url.rstrip('/')}/chat/completions", payload, timeout
    )
    parsed = json.loads(response["choices"][0]["message"]["content"])
    errors = validate_parser_output(parsed, text)
    if errors:
        return {
            "backend": "LM_STUDIO_BLOCKED",
            "status": "OUTPUT_VALIDATION_FAILED",
            "errors": errors,
            "source_text": text,
        }
    parsed["backend"] = "LM_STUDIO_STRUCTURED_OUTPUT"
    parsed["model"] = model
    parsed["source_text"] = text
    parsed["warning"] = (
        "JSON Schema는 형식만 보장합니다. 물질 후보는 결정적 resolver로 다시 확인해야 합니다."
    )
    return parsed
