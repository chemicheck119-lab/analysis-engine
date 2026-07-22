"""프로젝트 경로를 한 곳에서 관리한다."""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    """환경변수 경로를 확장하되 import 시 파일 생성을 시도하지 않는다."""

    value = os.getenv(name)
    return Path(value).expanduser() if value else default


PROJECT_ROOT = _env_path(
    "CHEMIGUARD119_PROJECT_ROOT", Path(__file__).resolve().parents[2]
)
FINAL_DATA_DIR = _env_path("CHEMIGUARD119_DATA_DIR", PROJECT_ROOT / "data" / "raw")
CONFIG_DIR = _env_path("CHEMIGUARD119_CONFIG_DIR", PROJECT_ROOT / "config")
EVALUATION_DIR = _env_path(
    "CHEMIGUARD119_EVALUATION_DIR", PROJECT_ROOT / "data" / "evaluation"
)
DEFAULT_ARTIFACT_DIR = _env_path(
    "CHEMIGUARD119_ARTIFACT_DIR", PROJECT_ROOT / "artifacts"
)
DEFAULT_DB_PATH = _env_path(
    "CHEMIGUARD119_DB_PATH", DEFAULT_ARTIFACT_DIR / "chemiguard119.sqlite"
)
DEFAULT_RESOLVER_MODEL = _env_path(
    "CHEMIGUARD119_RESOLVER_MODEL", DEFAULT_ARTIFACT_DIR / "resolver.joblib"
)
DEFAULT_RETRIEVER_MODEL = _env_path(
    "CHEMIGUARD119_RETRIEVER_MODEL", DEFAULT_ARTIFACT_DIR / "retriever.joblib"
)
DEFAULT_REPORT_DIR = _env_path(
    "CHEMIGUARD119_REPORT_DIR", PROJECT_ROOT / "outputs" / "modeling"
)
