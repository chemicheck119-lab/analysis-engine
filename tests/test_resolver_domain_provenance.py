"""실험 단계 사이 모델·코드 bytes 혼합을 막는 계약 테스트."""

import json
from types import SimpleNamespace

import pytest

import scripts.experiments.resolver_domain as domain
from chemiguard119 import resolver_dense_experiment as dense
from chemiguard119.utils import sha256_file


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    code_paths = {}
    for name in domain.CODE_PATHS:
        path = tmp_path / (name + ".py")
        path.write_text(name)
        code_paths[name] = path
    monkeypatch.setattr(domain, "CODE_PATHS", code_paths)
    output = tmp_path / "output"
    output.mkdir()
    (output / "datasets.private.json").write_text('{"fixture": true}')
    manifest = {
        **domain.preparation_code_hashes(),
        "embedding_snapshot_sha256": domain.BGE_M3_HASHES,
        "files": {
            "datasets.private.json": sha256_file(output / "datasets.private.json")
        },
    }
    (output / "prepared-manifest.json").write_text(json.dumps(manifest))
    return SimpleNamespace(output=output)


def test_matching_preparation_verifies(prepared):
    assert domain.verify_prepared(prepared) == {"fixture": True}


@pytest.mark.parametrize("name", list(domain.CODE_PATHS))
def test_changed_code_rejected_even_when_all_data_hashes_match(prepared, name):
    domain.CODE_PATHS[name].write_text("changed-code")
    with pytest.raises(ValueError, match="실행 코드 hash"):
        domain.verify_prepared(prepared)


def test_missing_historical_code_hash_cannot_silently_continue(prepared):
    path = prepared.output / "prepared-manifest.json"
    manifest = json.loads(path.read_text())
    del manifest["module_sha256"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="실행 코드 hash"):
        domain.verify_prepared(prepared)


def test_changed_data_still_rejected(prepared):
    (prepared.output / "datasets.private.json").write_text("{}")
    with pytest.raises(ValueError, match="고정 입력 hash"):
        domain.verify_prepared(prepared)


def test_changed_snapshot_provenance_rejected(prepared):
    path = prepared.output / "prepared-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["embedding_snapshot_sha256"] = {}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="snapshot hash"):
        domain.verify_prepared(prepared)


def test_bad_embedding_bytes_fail_before_inputs_download_or_output(
    tmp_path, monkeypatch
):
    model = tmp_path / dense.BGE_M3_REVISION
    model.mkdir()
    (model / "pytorch_model.bin").write_bytes(b"wrong-model")

    def forbidden(*args, **kwargs):
        pytest.fail("모델 검증 전에 캐시/데이터를 읽으면 안 됩니다")

    monkeypatch.setattr(domain, "load_inputs", forbidden)
    args = SimpleNamespace(embedding_model=model, output=tmp_path / "never-created")
    with pytest.raises(ValueError, match="BGE-M3.*hash"):
        domain.prepare(args)
    assert not args.output.exists()


@pytest.mark.parametrize("stage", ["rerank", "train"])
def test_stale_code_stops_both_stages_before_models_or_inputs(
    prepared, monkeypatch, stage
):
    domain.CODE_PATHS["script_sha256"].write_text("changed-script")

    def forbidden(*args, **kwargs):
        pytest.fail("코드 검증 전에 후속 작업을 실행하면 안 됩니다")

    monkeypatch.setattr(domain, "load_inputs", forbidden)
    with pytest.raises(ValueError, match="실행 코드 hash"):
        getattr(domain, stage)(prepared)


def test_reranker_bytes_are_pinned_before_optional_model_import(prepared, tmp_path):
    model = tmp_path / domain.RERANK_REVISION
    model.mkdir()
    (model / "config.json").write_bytes(b"wrong")
    prepared.reranker_model = model
    with pytest.raises(ValueError, match="고정 입력 hash"):
        domain.rerank(prepared)
