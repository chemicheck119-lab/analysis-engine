from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chemiguard119 import api, observability
from chemiguard119.api import ModelRuntime, create_app
from chemiguard119.paths import CONFIG_DIR


@pytest.fixture()
def runtime(tmp_path: Path) -> ModelRuntime:
    db_path = tmp_path / "chemiguard119.sqlite"
    resolver_path = tmp_path / "resolver.joblib"
    retriever_path = tmp_path / "retriever.joblib"
    for path in (db_path, resolver_path, retriever_path):
        path.touch()
    return ModelRuntime(
        db_path=db_path,
        resolver_model_path=resolver_path,
        retriever_model_path=retriever_path,
        config_dir=CONFIG_DIR,
        resolver_artifact={"schema_version": "resolver-test-v1", "rows": []},
        retriever_artifact={"schema_version": "retriever-test-v1", "rows": []},
        loaded_at_utc="2026-07-28T00:00:00+00:00",
    )


def test_json_event_is_single_line_and_escapes_untrusted_text(
    monkeypatch,
) -> None:
    records: list[tuple[int, str]] = []
    monkeypatch.setattr(
        observability.LOGGER,
        "log",
        lambda level, message: records.append((level, message)),
    )

    payload = observability.emit_json_event(
        "test_event",
        request_id="REQ-TEST-LOG",
        http_route="/api/example\nforged-log-line",
    )

    assert len(records) == 1
    level, message = records[0]
    assert level == logging.INFO
    assert "\n" not in message
    assert json.loads(message) == payload
    assert payload["schema_version"] == "chemicheck119-log-v1"
    assert payload["timestamp"].endswith("+00:00")


def test_request_logs_are_correlated_without_secrets_or_payload(
    runtime: ModelRuntime,
    monkeypatch,
) -> None:
    events: list[dict[str, Any]] = []

    def capture(event: str, **fields: Any) -> dict[str, Any]:
        record = {"event": event, **fields}
        events.append(record)
        return record

    monkeypatch.setattr(api, "emit_json_event", capture)
    configured_key = "configured-secret-key-value-123456789"
    supplied_key = "wrong-secret-key-value"
    application = create_app(runtime=runtime, api_key=configured_key)

    with TestClient(application) as client:
        live = client.get(
            "/health/live?token=query-secret",
            headers={"X-Request-Id": "REQ-LIVE-LOG-001"},
        )
        unauthorized = client.post(
            "/api/v1/substances/resolve?debug=query-secret",
            headers={
                "X-Request-Id": "REQ-AUTH-LOG-001",
                "X-API-Key": supplied_key,
            },
            json={"query": "body-secret-substance"},
        )
        not_found = client.get(
            "/private-secret-in-path",
            headers={"X-Request-Id": "REQ-NOT-FOUND-LOG-001"},
        )

    assert live.status_code == 200
    assert unauthorized.status_code == 401
    assert not_found.status_code == 404
    assert len(events) == 3
    assert events[0] == {
        "event": "http_request_completed",
        "level": logging.INFO,
        "request_id": "REQ-LIVE-LOG-001",
        "service_name": "chemicheck119-model-api",
        "service_version": "0.4.0",
        "deployment_environment": "development",
        "authentication_mode": "API_KEY",
        "http_request_method": "GET",
        "http_route": "/health/live",
        "http_response_status_code": 200,
        "duration_ms": events[0]["duration_ms"],
        "outcome": "SUCCESS",
    }
    assert events[1] == {
        "event": "http_request_completed",
        "level": logging.WARNING,
        "request_id": "REQ-AUTH-LOG-001",
        "service_name": "chemicheck119-model-api",
        "service_version": "0.4.0",
        "deployment_environment": "development",
        "authentication_mode": "API_KEY",
        "http_request_method": "POST",
        "http_route": "/api/v1/substances/resolve",
        "http_response_status_code": 401,
        "duration_ms": events[1]["duration_ms"],
        "outcome": "CLIENT_ERROR",
    }
    assert events[0]["duration_ms"] >= 0
    assert events[1]["duration_ms"] >= 0
    assert events[2] == {
        "event": "http_request_completed",
        "level": logging.WARNING,
        "request_id": "REQ-NOT-FOUND-LOG-001",
        "service_name": "chemicheck119-model-api",
        "service_version": "0.4.0",
        "deployment_environment": "development",
        "authentication_mode": "API_KEY",
        "http_request_method": "GET",
        "http_route": "<unmatched>",
        "http_response_status_code": 404,
        "duration_ms": events[2]["duration_ms"],
        "outcome": "CLIENT_ERROR",
    }
    assert events[2]["duration_ms"] >= 0
    serialized = json.dumps(events, ensure_ascii=False)
    for secret in (
        configured_key,
        supplied_key,
        "query-secret",
        "body-secret-substance",
        "private-secret-in-path",
    ):
        assert secret not in serialized


def test_console_server_disables_uvicorn_raw_access_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    fake_uvicorn = SimpleNamespace(
        run=lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setenv("CHEMIGUARD119_API_HOST", "127.0.0.1")
    monkeypatch.setenv("CHEMIGUARD119_API_PORT", "8000")
    monkeypatch.delenv("CHEMIGUARD119_API_KEY", raising=False)
    monkeypatch.delenv("CHEMIGUARD119_ALLOW_ANONYMOUS", raising=False)

    api.run()

    assert calls == [
        (
            ("chemiguard119.api:app",),
            {
                "host": "127.0.0.1",
                "port": 8000,
                "workers": 1,
                "access_log": False,
            },
        )
    ]
