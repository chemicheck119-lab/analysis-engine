from __future__ import annotations

from pathlib import Path

import pytest

from chemiguard119.utils import (
    is_lfs_pointer,
    require_materialized_files,
    valid_cas_checksum,
)


@pytest.mark.parametrize(
    "cas_number",
    [
        "64-17-5",
        "7647-01-0",
        " 7681–52–9 ",
    ],
)
def test_valid_cas_checksum_accepts_valid_numbers_and_normalizes_dashes(
    cas_number: str,
) -> None:
    assert valid_cas_checksum(cas_number) is True


@pytest.mark.parametrize(
    "cas_number",
    [
        "64-17-6",  # checksum mismatch
        "7647-1-0",  # malformed middle group
        "not-a-cas",
        "",
        None,
    ],
)
def test_valid_cas_checksum_rejects_bad_checksum_or_format(
    cas_number: str | None,
) -> None:
    assert valid_cas_checksum(cas_number) is False


def test_require_materialized_files_rejects_git_lfs_pointer(tmp_path: Path) -> None:
    pointer = tmp_path / "dataset.csv"
    pointer.write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:0123456789abcdef\n"
        "size 123456\n",
        encoding="utf-8",
    )

    assert is_lfs_pointer(pointer) is True
    with pytest.raises(FileNotFoundError) as exc_info:
        require_materialized_files([pointer])

    message = str(exc_info.value)
    assert "Git LFS 포인터만 존재" in message
    assert str(pointer) in message
    assert "data/raw/" in message


def test_require_materialized_files_accepts_real_file(tmp_path: Path) -> None:
    materialized = tmp_path / "dataset.csv"
    materialized.write_text("cas_number,name\n64-17-5,에탄올\n", encoding="utf-8")

    require_materialized_files([materialized])
    assert is_lfs_pointer(materialized) is False
