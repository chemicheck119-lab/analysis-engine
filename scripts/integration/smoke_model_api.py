#!/usr/bin/env python3
"""배포된 모델 API의 생존·준비·계약·인증·통합 분석을 확인한다."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


API_SCHEMA_VERSION = "chemiguard119-api-v1"
SERVICE_ID = "chemicheck119-model-api"


def _request_json(
    base_url: str,
    path: str,
    *,
    timeout: float,
    api_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if api_key:
        headers["X-API-Key"] = api_key
    if payload is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            response_headers = {
                name.lower(): value for name, value in response.headers.items()
            }
            return body, response_headers
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {path}: HTTP {error.code}, body={body}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"{method} {path}: 연결 실패 ({error})") from error
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{method} {path}: JSON 응답이 아닙니다.") from error


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(args: argparse.Namespace) -> dict[str, Any]:
    parsed_url = urlparse(args.base_url)
    _require(
        parsed_url.scheme in {"http", "https"}
        and bool(parsed_url.netloc)
        and parsed_url.username is None
        and parsed_url.password is None,
        "base URL은 인증정보를 포함하지 않은 http(s) 주소여야 합니다.",
    )
    api_key = os.getenv(args.api_key_env, "").strip()
    if not args.allow_anonymous:
        _require(
            len(api_key) >= 32,
            f"{args.api_key_env} 환경변수에 32자 이상 API Key가 필요합니다.",
        )
    else:
        api_key = None

    live, _ = _request_json(
        args.base_url,
        "/health/live",
        timeout=args.timeout,
    )
    _require(live.get("status") == "UP", "liveness가 UP이 아닙니다.")
    _require(live.get("service") == SERVICE_ID, "service ID가 다릅니다.")

    ready, _ = _request_json(
        args.base_url,
        "/health/ready",
        timeout=args.timeout,
    )
    _require(ready.get("status") == "READY", "readiness가 READY가 아닙니다.")
    _require(ready.get("ready") is True, "runtime ready가 true가 아닙니다.")
    material_capability = ready.get("material_discovery_capability") or {}
    _require(
        material_capability.get("ready") is True,
        "관찰 기반 물질 탐색 인덱스가 준비되지 않았습니다.",
    )
    _require(
        int(material_capability.get("profile_count") or 0)
        >= int(material_capability.get("minimum_profile_count") or 700),
        "관찰 기반 물질 프로필이 운영 최소 건수보다 적습니다.",
    )

    metadata, _ = _request_json(
        args.base_url,
        "/api/v1/meta",
        timeout=args.timeout,
    )
    _require(
        metadata.get("api_schema_version") == API_SCHEMA_VERSION,
        "metadata API schema version이 다릅니다.",
    )

    payload = json.loads(args.request_file.read_text(encoding="utf-8"))
    analysis, headers = _request_json(
        args.base_url,
        "/api/v1/incidents/analyze",
        timeout=args.timeout,
        api_key=api_key,
        payload=payload,
    )
    required_fields = {
        "schema_version",
        "analysis_id",
        "request_id",
        "state",
        "model_outputs",
        "evidence",
        "conflict_review",
        "confirmation_gate",
        "required_next_steps",
        "provenance",
        "safety_notice",
    }
    missing_fields = sorted(required_fields - analysis.keys())
    _require(not missing_fields, f"통합 응답 필드가 없습니다: {missing_fields}")
    _require(
        analysis["schema_version"] == API_SCHEMA_VERSION,
        "통합 응답 schema version이 다릅니다.",
    )
    _require(
        headers.get("x-api-schema-version") == API_SCHEMA_VERSION,
        "X-API-Schema-Version 헤더가 다릅니다.",
    )
    _require(
        headers.get("x-service-id") == SERVICE_ID,
        "X-Service-Id 헤더가 다릅니다.",
    )
    _require(bool(headers.get("x-request-id")), "X-Request-Id 헤더가 없습니다.")
    gate = analysis.get("confirmation_gate") or {}
    _require(
        gate.get("all_required_confirmed") is False,
        "미확인 smoke 요청이 확인 완료로 처리됐습니다.",
    )
    _require(
        (analysis.get("conflict_review") or {}).get("executed") is False,
        "미확인 smoke 요청에서 충돌 검토가 실행됐습니다.",
    )
    discovery, _ = _request_json(
        args.base_url,
        "/api/v1/substances/discover",
        timeout=args.timeout,
        api_key=api_key,
        payload={
            "query": "무색 투명하고 박하 냄새가 나는 휘발성 액체",
            "top_k": 3,
            "evidence_top_k": 3,
        },
    )
    _require(
        discovery.get("status") == "CANDIDATES_FOUND",
        "관찰 기반 물질 탐색 smoke 후보가 없습니다.",
    )
    _require(
        bool(discovery.get("candidates")),
        "관찰 기반 물질 탐색 candidates가 비어 있습니다.",
    )
    _require(
        discovery.get("requires_responder_confirmation") is True
        and discovery.get("rule_eligible") is False
        and discovery.get("risk_determination_allowed") is False,
        "관찰 기반 물질 후보 안전 계약이 깨졌습니다.",
    )
    return {
        "status": "PASSED",
        "service": SERVICE_ID,
        "api_schema_version": API_SCHEMA_VERSION,
        "runtime_integrity": (ready.get("integrity") or {}).get("status"),
        "analysis_state": analysis.get("state"),
        "confirmation_gate_closed": True,
        "material_discovery_profile_count": material_capability.get("profile_count"),
        "material_discovery_candidate_count": len(discovery["candidates"]),
        "material_discovery_gate_closed": True,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--api-key-env",
        default="CHEMICHECK119_MODEL_API_KEY",
        help="API Key 값을 직접 받지 않고 읽을 환경변수 이름",
    )
    parser.add_argument(
        "--request-file",
        type=Path,
        default=Path("examples/api/incident_unconfirmed_request.json"),
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--allow-anonymous",
        action="store_true",
        help="localhost 익명 개발 서버에서만 사용",
    )
    return parser.parse_args()


def main() -> int:
    try:
        result = run(parse_args())
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        sys.stderr.write(f"smoke test 실패: {error}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
