"""운영 조회 경로에서 SQLite를 명시적으로 읽기 전용으로 연다."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """변경되지 않는 배포 artifact를 read-only/immutable URI로 연결한다."""

    path = Path(db_path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


__all__ = ["connect_readonly"]
