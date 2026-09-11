from __future__ import annotations

import csv
import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

import chemiguard119.incident_replay as replay_module
from chemiguard119.incident_replay import read_official_zip, replay_official_source
from chemiguard119.incident_source_intake import OFFICIAL_SOURCE_COLUMNS, OUTPUT_COLUMNS
from chemiguard119.resolver import fit_resolver_rows
from chemiguard119.utils import sha256_file


def _zip(path: Path, entries: dict[str, bytes]) -> str:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return sha256_file(path)


def _fixture(tmp_path: Path, *, encoding: str = "utf-8-sig", cutoff: int = 2019):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=OFFICIAL_SOURCE_COLUMNS)
    writer.writeheader()
    for year, cas, name in [
        (2018, "64-17-5", "에탄올"),
        (2019, "67-56-1", "메탄올"),
        (2020, "64-17-5", "에탄올"),
        (2020, "67-56-1", "미등록실험명"),
    ]:
        writer.writerow(
            {
                "OCRN_YR": year,
                "CAS_NO": cas,
                "CHEM_SBSTN_KORN_NM": name,
                "ROAD_NM": "절대출력금지도로명",
            }
        )
    raw = buffer.getvalue().encode(encoding)
    archive = tmp_path / "source.zip"
    _zip(archive, {"유해물질판단/유해물질판단.csv": raw})
    model = tmp_path / "model.joblib"
    fit_resolver_rows(
        [
            {
                "cas_number": "64-17-5",
                "alias_text": "에탄올",
                "normalized_text": "에탄올",
            },
            {
                "cas_number": "67-56-1",
                "alias_text": "메탄올",
                "normalized_text": "메탄올",
            },
        ],
        model,
        training_metadata={"training_year_max": cutoff},
    )
    reference = tmp_path / "reference.json"
    reference.write_text(
        json.dumps(
            {
                "split_policy": {"locked_test_year": 2020},
                "source_audit": {"source_sha256": "0" * 64},
                "locked_test": {
                    "adapted": {group: {} for group in replay_module.METRIC_GROUPS}
                },
                "artifacts": {"final": {"artifact_sha256": "1" * 64}},
            }
        ),
        encoding="utf-8",
    )
    return archive, model, reference, raw


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp949"])
def test_replay_preserves_archive_and_projects_private_source_without_training(
    tmp_path: Path, encoding: str
) -> None:
    archive, model, reference, raw = _fixture(tmp_path, encoding=encoding)
    output = tmp_path / "private-replay"
    model_hash = sha256_file(model)
    result = replay_official_source(
        archive,
        model,
        reference,
        output,
        expected_archive_sha256=sha256_file(archive),
        expected_model_sha256=model_hash,
    )
    summary = json.dumps(result, ensure_ascii=False)
    assert "절대출력금지도로명" not in summary
    assert "미등록실험명" not in summary
    assert "절대출력금지도로명" not in (output / "resolver-source.csv").read_text(
        encoding="utf-8-sig"
    )
    assert (output / "source.zip").read_bytes() == archive.read_bytes()
    assert not list(output.glob(".intake-*"))
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir())
    with (output / "resolver-source.csv").open(encoding="utf-8-sig") as handle:
        assert next(csv.reader(handle)) == list(OUTPUT_COLUMNS)
    assert result["provenance"]["source_csv_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["provenance"]["same_model_bytes_as_reference"] is False
    assert result["split_audit"]["test_case_count"] == 2
    assert result["split_audit"]["unseen_surface_case_count"] == 1
    assert result["split_audit"]["repeated_past_pair_case_count"] == 1
    assert result["split_audit"]["training_or_tuning_performed"] is False
    assert result["adoption"]["runtime_change_allowed"] is False
    assert result["status"] == "REFERENCE_DIFFERENCE_REQUIRES_REVIEW"
    assert sha256_file(model) == model_hash
    with pytest.raises(FileExistsError):
        replay_official_source(
            archive,
            model,
            reference,
            output,
            expected_archive_sha256=sha256_file(archive),
            expected_model_sha256=model_hash,
        )
    assert (output / "source.zip").read_bytes() == archive.read_bytes()


def test_wrong_archive_hash_is_rejected_before_output(tmp_path: Path) -> None:
    archive, model, reference, _raw = _fixture(tmp_path)
    output = tmp_path / "private-replay"
    with pytest.raises(ValueError, match="ZIP SHA-256"):
        replay_official_source(
            archive,
            model,
            reference,
            output,
            expected_archive_sha256="0" * 64,
            expected_model_sha256=sha256_file(model),
        )
    assert not output.exists()


def test_wrong_model_hash_is_rejected_before_output(tmp_path: Path) -> None:
    archive, model, reference, _raw = _fixture(tmp_path)
    output = tmp_path / "private-replay"
    with pytest.raises(ValueError, match="Resolver SHA-256"):
        replay_official_source(
            archive,
            model,
            reference,
            output,
            expected_archive_sha256=sha256_file(archive),
            expected_model_sha256="0" * 64,
        )
    assert not output.exists()


def test_test_year_trained_model_cannot_enter_replay(tmp_path: Path) -> None:
    archive, model, reference, _raw = _fixture(tmp_path, cutoff=2020)
    output = tmp_path / "private-replay"
    with pytest.raises(ValueError, match="2019년까지"):
        replay_official_source(
            archive,
            model,
            reference,
            output,
            expected_archive_sha256=sha256_file(archive),
            expected_model_sha256=sha256_file(model),
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "name",
    [
        "../source.csv",
        "/source.csv",
        "C:/source.csv",
        "folder\\source.csv",
        "source.txt",
    ],
)
def test_zip_rejects_unsafe_or_non_csv_member(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "source.zip"
    digest = _zip(archive, {name: b"data"})
    with pytest.raises(ValueError, match="안전한 일반 CSV"):
        read_official_zip(archive, digest)


def test_zip_rejects_multiple_csv_members(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    digest = _zip(archive, {"a.csv": b"a", "b.csv": b"b"})
    with pytest.raises(ValueError, match="정확히 한 개"):
        read_official_zip(archive, digest)


def test_zip_rejects_symlink(tmp_path: Path) -> None:
    archive = tmp_path / "source.zip"
    info = zipfile.ZipInfo("source.csv")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(info, "target.csv")
    with pytest.raises(ValueError, match="안전한 일반 CSV"):
        read_official_zip(archive, sha256_file(archive))


def test_zip_rejects_oversized_member_before_decompression(
    tmp_path: Path, monkeypatch
) -> None:
    archive = tmp_path / "source.zip"
    digest = _zip(archive, {"source.csv": b"a" * 100})
    monkeypatch.setattr(replay_module, "MAX_CSV_BYTES", 10)
    with pytest.raises(ValueError, match="CSV 크기 제한"):
        read_official_zip(archive, digest)


def test_replay_rejects_git_output(tmp_path: Path) -> None:
    archive, model, reference, _raw = _fixture(tmp_path)
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="Git worktree 밖"):
        replay_official_source(
            archive,
            model,
            reference,
            tmp_path / "public-output",
            expected_archive_sha256=sha256_file(archive),
            expected_model_sha256=sha256_file(model),
        )
    assert not (tmp_path / "public-output").exists()


def test_reference_comparison_preserves_mismatch_and_normalizes_year_keys() -> None:
    assert (
        replay_module._compare_reference({"years": {2020: 2}}, {"years": {"2020": 2}})
        == []
    )
    assert replay_module._compare_reference({"case_count": 2}, {"case_count": 419}) == [
        {"field": "case_count", "observed": 2, "reference": 419}
    ]
