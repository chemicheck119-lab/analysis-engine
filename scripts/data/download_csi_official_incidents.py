#!/usr/bin/env python3
"""화학물질안전원 전국 화학사고 CSV를 고정 checksum으로 다운로드한다."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from chemiguard119.official_incident_evaluation import (
    PINNED_SOURCE_SHA256,
    SOURCE_DOWNLOAD_URL,
)


MAX_DOWNLOAD_BYTES = 10_000_000


def download(output: Path, *, expected_sha256: str = PINNED_SOURCE_SHA256) -> dict:
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError("expected_sha256은 소문자 64자리 SHA-256이어야 합니다.")
    parsed = urlparse(SOURCE_DOWNLOAD_URL)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("공식 원천 URL은 HTTPS여야 합니다.")
    request = urllib.request.Request(
        SOURCE_DOWNLOAD_URL,
        headers={"User-Agent": "chemicheck119-official-evaluation/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("공식 원천 파일이 10MB 제한을 초과했습니다.")
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    if not payload or len(payload) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("공식 원천 파일 크기가 허용 범위를 벗어났습니다.")
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "공식 원천 checksum이 고정값과 다릅니다. 데이터 갱신 여부를 검토하세요."
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return {
        "status": "DOWNLOADED",
        "output": str(output),
        "bytes": len(payload),
        "sha256": actual_sha256,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/raw/09_CSI_전국_화학사고정보_20250430.csv"),
    )
    parser.add_argument("--expected-sha256", default=PINNED_SOURCE_SHA256)
    args = parser.parse_args()
    result = download(args.output.resolve(), expected_sha256=args.expected_sha256)
    print(f"{result['status']}: {result['bytes']} bytes, sha256={result['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
