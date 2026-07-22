"""데이터 파이프라인과 CLI가 공유하는 작은 안전 유틸리티."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable


CAS_PATTERN = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")
LFS_HEADER = b"version https://git-lfs.github.com/spec/v1"


def normalize_text(value: str | None) -> str:
    """검색용 텍스트를 NFKC·소문자·공백 정리 형태로 바꾼다."""

    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    text = re.sub(r"[\s\-_/,()\[\]{}]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_text(value: str | None) -> str:
    """별칭 exact 비교를 위해 문자·숫자만 남긴다."""

    return re.sub(r"[^0-9a-z가-힣]+", "", normalize_text(value))


def normalize_cas(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip()
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", "", text)


def valid_cas_checksum(value: str | None) -> bool:
    """CAS 형식과 공식 체크디지트 계산만 검사한다."""

    cas = normalize_cas(value)
    match = CAS_PATTERN.fullmatch(cas)
    if not match:
        return False
    body = match.group(1) + match.group(2)
    checksum = (
        sum(int(digit) * weight for weight, digit in enumerate(reversed(body), 1)) % 10
    )
    return checksum == int(match.group(3))


def is_lfs_pointer(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        return handle.read(len(LFS_HEADER)) == LFS_HEADER


def require_materialized_files(paths: Iterable[Path]) -> None:
    missing = [str(path) for path in paths if not path.exists()]
    pointers = [str(path) for path in paths if path.exists() and is_lfs_pointer(path)]
    messages: list[str] = []
    if missing:
        messages.append("누락 파일: " + ", ".join(missing))
    if pointers:
        messages.append(
            "Git LFS 포인터만 존재: "
            + ", ".join(pointers)
            + ". 검증된 데이터 bundle을 다시 받아 `data/raw/`에 복원하세요."
        )
    if messages:
        raise FileNotFoundError("\n".join(messages))


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
