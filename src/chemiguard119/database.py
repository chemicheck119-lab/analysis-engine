"""운영 조회 경로에서 SQLite를 명시적으로 읽기 전용으로 연다."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def connect_readonly(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """읽기 전용 연결을 열고 성공·예외 여부와 관계없이 즉시 닫는다."""

    path = Path(db_path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


__all__ = ["connect_readonly"]
