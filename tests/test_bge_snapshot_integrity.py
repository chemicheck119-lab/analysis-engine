"""작은 합성 파일로 모델 사전 검증을 검사한다. 실제 가중치를 CI에 넣지 않는다."""

import hashlib
from pathlib import Path

import pytest

from chemiguard119 import resolver_dense_experiment as dense
from chemiguard119 import ulsan_resolver_comparison as comparison


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    path = tmp_path / dense.BGE_M3_REVISION
    path.mkdir()
    expected = {}
    for name in dense.BGE_M3_HASHES:
        value = name.encode()
        (path / name).write_bytes(value)
        expected[name] = hashlib.sha256(value).hexdigest()
    monkeypatch.setattr(dense, "BGE_M3_HASHES", expected)
    return path


def test_complete_snapshot_passes(snapshot):
    assert dense.verify_bge_m3_snapshot(snapshot) == dense.BGE_M3_HASHES


@pytest.mark.parametrize("name", list(dense.BGE_M3_HASHES))
def test_expected_directory_with_changed_bytes_fails(snapshot, name):
    (snapshot / name).write_bytes(b"changed")
    with pytest.raises(ValueError, match="BGE-M3.*hash"):
        dense.verify_bge_m3_snapshot(snapshot)


def test_missing_required_file_fails(snapshot):
    (snapshot / "tokenizer.json").unlink()
    with pytest.raises(ValueError, match="hash"):
        dense.verify_bge_m3_snapshot(snapshot)


@pytest.mark.parametrize(
    "name", ["model.safetensors", "adapter_config.json", "added_tokens.json"]
)
def test_alternative_unverified_loader_input_fails(snapshot, name):
    (snapshot / name).write_bytes(b"alternate")
    with pytest.raises(ValueError, match="미검증"):
        dense.verify_bge_m3_snapshot(snapshot)


def test_wrong_revision_fails_before_file_access(tmp_path):
    with pytest.raises(ValueError, match="revision"):
        dense.verify_bge_m3_snapshot(tmp_path / "wrong")


def test_comparison_rejects_corruption_before_encoder_or_resolver_load(
    snapshot, monkeypatch, tmp_path
):
    (snapshot / "config.json").write_bytes(b"changed")
    monkeypatch.setattr(
        comparison,
        "sha256_file",
        lambda p: comparison.LOCKED_HASHES.get(p.name, comparison.MODEL_HASH),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("무결성 검증 실패 전에 artifact/encoder를 적재하면 안 됩니다")

    monkeypatch.setattr(comparison, "load_resolver", forbidden)
    monkeypatch.setattr(comparison, "make_transformer_cls_encoder", forbidden)
    with pytest.raises(ValueError, match="BGE-M3.*hash"):
        comparison.run_comparison(
            source_dir=tmp_path,
            model_path=Path("resolver.joblib"),
            embedding_path=snapshot,
            regression_path=Path("unused"),
            safety_path=Path("unused"),
            output_dir=tmp_path / "output",
        )
    assert not (tmp_path / "output").exists()
