"""민감정보를 남기지 않는 운영용 JSON 이벤트 로그."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, TextIO


LOG_SCHEMA_VERSION = "chemicheck119-log-v1"
LOG_LEVEL_ENV_VAR = "CHEMIGUARD119_LOG_LEVEL"
LOGGER_NAME = "chemiguard119.telemetry"
LOGGER = logging.getLogger(LOGGER_NAME)
_HANDLER_MARKER = "_chemicheck119_json_handler"
_SUPPORTED_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _configured_log_level(value: str | None = None) -> int:
    """환경변수의 로그 수준을 안전한 기본값으로 정규화한다."""

    normalized = (value or os.getenv(LOG_LEVEL_ENV_VAR) or "INFO").strip().upper()
    return _SUPPORTED_LOG_LEVELS.get(normalized, logging.INFO)


def configure_json_logging(
    *,
    level: str | None = None,
    stream: TextIO | None = None,
) -> logging.Logger:
    """전용 telemetry logger가 JSON 한 줄만 stdout에 기록하도록 구성한다."""

    LOGGER.setLevel(_configured_log_level(level))
    LOGGER.propagate = False
    if not any(getattr(handler, _HANDLER_MARKER, False) for handler in LOGGER.handlers):
        handler = logging.StreamHandler(stream or sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, _HANDLER_MARKER, True)
        LOGGER.addHandler(handler)
    return LOGGER


def emit_json_event(
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> dict[str, Any]:
    """허용된 구조화 필드만 호출자가 전달하도록 하고 JSON 한 줄을 기록한다."""

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "schema_version": LOG_SCHEMA_VERSION,
        "level": logging.getLevelName(level),
        "event": event,
        **fields,
    }
    LOGGER.log(
        level,
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    return payload


__all__ = [
    "LOG_LEVEL_ENV_VAR",
    "LOG_SCHEMA_VERSION",
    "LOGGER",
    "configure_json_logging",
    "emit_json_event",
]
