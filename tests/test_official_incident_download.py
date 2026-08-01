from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "data"
        / "download_csi_official_incidents.py"
    )
    spec = importlib.util.spec_from_file_location(
        "download_csi_official_incidents", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = {"Content-Length": str(len(payload))}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


def test_official_download_is_atomic_and_checksum_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    payload = "연번,사고일자\n1,2025-01-01\n".encode("cp949")
    expected = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )
    output = tmp_path / "official.csv"

    result = module.download(output, expected_sha256=expected)

    assert output.read_bytes() == payload
    assert result["sha256"] == expected
    assert not list(tmp_path.glob(".official.csv.*"))


def test_official_download_rejects_checksum_mismatch_without_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    payload = b"changed"
    monkeypatch.setattr(
        module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload),
    )
    output = tmp_path / "official.csv"

    with pytest.raises(RuntimeError, match="checksum"):
        module.download(output, expected_sha256="0" * 64)

    assert not output.exists()


def test_official_download_rejects_invalid_checksum_contract(tmp_path: Path) -> None:
    module = _load_script()

    with pytest.raises(ValueError, match="64자리"):
        module.download(tmp_path / "official.csv", expected_sha256="invalid")
