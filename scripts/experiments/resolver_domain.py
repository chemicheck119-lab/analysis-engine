#!/usr/bin/env python3
"""공식 별칭 감사 → Top-20 재정렬 → 검색 metric learning. 원문은 private 전용."""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import signal
import time
from pathlib import Path

import numpy as np

from chemiguard119.incident_replay import _private_bytes, _private_json
from chemiguard119.resolver import _load_alias_rows, load_resolver
from chemiguard119.resolver_dense_experiment import (
    BGE_M3_HASHES,
    _normalize_embeddings,
    build_dense_alias_corpus,
    make_transformer_cls_encoder,
    verify_bge_m3_snapshot,
)
from chemiguard119.resolver_domain_experiment import (
    DOWNLOAD_URL,
    PUBLIC_TYPES,
    apply_projection,
    batch_rank,
    candidate_safety,
    enrich_corpus,
    make_benchmark,
    rank_metrics,
    rerank_pool,
    source_aliases,
    train_projection,
)
from chemiguard119.ulsan_resolver_comparison import MODEL_HASH
from chemiguard119.utils import compact_text, sha256_file

RERANK_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
RERANK_HASHES = {
    "config.json": "13dcd6c31d9fec9d1d8e158702072f62d7fa7d312a64b9fe057bec9a08cfe41a",
    "model.safetensors": "d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286",
    "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
    "special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    "tokenizer.json": "69564b696052886ed0ac63fa393e928384e0f8caada38c1f4864a9bfbf379c15",
    "tokenizer_config.json": "7e4c1cc848840aeccdd763458c18dd525eb0f795c992e00ebe9c28554e7db2d4",
}
ROOT = Path(__file__).resolve().parents[2]
CODE_PATHS = {
    "script_sha256": Path(__file__).resolve(),
    "module_sha256": ROOT / "src/chemiguard119/resolver_domain_experiment.py",
    "encoder_module_sha256": ROOT / "src/chemiguard119/resolver_dense_experiment.py",
}
INPUT_HASHES = {
    "rows.private.json": "892f035f4313fef50e47878257ca2db4e5ad92953231ec2313c09b75ac638bea",
    "corpus-vectors.float32": "cbfe18d59e24c39393983d7956d8d53a7e013dc59aee68a34f73a69dd27211bc",
    "query-vectors.float32": "5a7871ebcf9ecfec77928d0db62689427255df7447624c6fdcda82903affd3aa",
}


def read_json(path):
    return json.loads(path.read_text())


def assert_hash(path, value):
    if sha256_file(path) != value:
        raise ValueError(f"고정 입력 hash 불일치: {path.name}")


def preparation_code_hashes():
    return {key: sha256_file(path) for key, path in CODE_PATHS.items()}


def verify_reranker_snapshot(path):
    if path.name != RERANK_REVISION:
        raise ValueError("Reranker 고정 revision 불일치")
    for name, expected in RERANK_HASHES.items():
        assert_hash(path / name, expected)
    if any(
        p.is_file()
        and p.name
        not in set(RERANK_HASHES) | {"README.md", "LICENSE", ".gitattributes"}
        for p in path.iterdir()
    ):
        raise ValueError("미검증 Reranker snapshot 파일")


def vectors(path, count):
    result = np.fromfile(path, dtype="<f4").reshape(count, 1024)
    if not np.isfinite(result).all() or not np.allclose(
        np.linalg.norm(result, axis=1), 1, atol=1e-5
    ):
        raise ValueError("고정 embedding 유한성/정규화 위반")
    return result


def write_vectors(path, value):
    _private_bytes(path, np.asarray(value, dtype="<f4").tobytes())


def load_inputs(args):
    assert_hash(args.model, MODEL_HASH)
    assert_hash(
        args.db, "ed81fe9aac45a38880920d967ce8f3954acabfedf7b2f6ea59464552e7958b91"
    )
    assert_hash(args.previous / "rows.private.json", INPUT_HASHES["rows.private.json"])
    assert_hash(
        args.previous / "corpus-vectors.float32", INPUT_HASHES["corpus-vectors.float32"]
    )
    assert_hash(args.query_vectors, INPUT_HASHES["query-vectors.float32"])
    artifact = load_resolver(args.model)
    corpus = build_dense_alias_corpus(artifact)
    rows = read_json(args.previous / "rows.private.json")["rows"]
    if len(rows) != 440:
        raise ValueError("고정 사례 분모 불일치")
    return (
        artifact,
        corpus,
        rows,
        vectors(args.previous / "corpus-vectors.float32", len(corpus)),
        vectors(args.query_vectors, len(rows)),
    )


def merge_gate(rows, rankings):
    return [
        row["systems"]["A_SPARSE"]["candidates"] if row["preserved_gate"] else ranking
        for row, ranking in zip(rows, rankings, strict=True)
    ]


def aggregate(rows, rankings):
    output = {}
    for group, indices in {
        "all_419": [i for i, r in enumerate(rows) if r["cohort"] == "official_2020"],
        "unseen_60": [
            i
            for i, r in enumerate(rows)
            if r["cohort"] == "official_2020" and r["unseen_surface"]
        ],
        "internal_21": [
            i for i, r in enumerate(rows) if r["cohort"] != "official_2020"
        ],
    }.items():
        output[group] = rank_metrics(
            [rankings[i] for i in indices], [rows[i]["expected_cas"] for i in indices]
        )
    return output


def paired(rows, base, candidate):
    result = {}
    for group, indices in {
        "all_419": [i for i, r in enumerate(rows) if r["cohort"] == "official_2020"],
        "unseen_60": [
            i
            for i, r in enumerate(rows)
            if r["cohort"] == "official_2020" and r["unseen_surface"]
        ],
    }.items():
        result[group] = {}
        for k in (1, 3, 20):
            gain = loss = 0
            for i in indices:
                expected = rows[i]["expected_cas"]
                a = expected in [r["cas_number"] for r in base[i][:k]]
                b = expected in [r["cas_number"] for r in candidate[i][:k]]
                gain += b and not a
                loss += a and not b
            result[group][f"top{k}"] = {"gained": gain, "lost": loss}
    return result


def safety(rows, rankings, allowed):
    fuzzy = [
        ranking
        for row, ranking in zip(rows, rankings, strict=True)
        if not row["preserved_gate"]
    ]
    return {
        **candidate_safety(fuzzy, allowed),
        "preserved_gate_change_count": sum(
            ranking != row["systems"]["A_SPARSE"]["candidates"]
            for row, ranking in zip(rows, rankings, strict=True)
            if row["preserved_gate"]
        ),
        "candidate_auto_confirmation_implemented": False,
        "actual_rule_engine_execution_evaluated": False,
    }


def prepare(args):
    # 다운로드·출력 생성·캐시 혼합 전에 같은 벡터 공간의 모델인지 검증한다.
    embedding_hashes = verify_bge_m3_snapshot(args.embedding_model)
    code_hashes = preparation_code_hashes()
    import requests

    artifact, base, rows, base_vectors, query_vectors = load_inputs(args)
    output = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.perf_counter()
    with requests.get(DOWNLOAD_URL, stream=True, timeout=60) as response:
        response.raise_for_status()
        chunks = []
        size = 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > 10 * 1024 * 1024 or time.perf_counter() - started > 60:
                raise ValueError("공식 파일 다운로드 크기/시간 상한 초과")
            chunks.append(chunk)
    payload = b"".join(chunks)
    _private_bytes(output / "official-material.csv", payload)
    official, source_audit = source_aliases(payload)
    enriched, enrichment = enrich_corpus(base, official, artifact)
    public = [r for r in _load_alias_rows(args.db) if r["alias_type"] in PUBLIC_TYPES]
    allowed = {r["cas_number"] for r in base}
    benchmark = make_benchmark(
        public + [r for r in official if r["cas_number"] in allowed]
    )
    if any(
        benchmark["audit"]["eligible_group_counts"][key] < minimum
        for key, minimum in (("train", 30), ("dev", 10), ("test", 10))
    ):
        raise ValueError("도메인 학습 CAS 분할 최소 수 미달")
    # 학습이나 결과를 보기 전에 split·corpus를 private hash로 고정한다.
    _private_json(
        output / "datasets.private.json",
        {"official": official, "enriched": enriched, "benchmark": benchmark},
    )
    _private_json(
        output / "intake.json",
        {
            "source": source_audit,
            "enrichment": enrichment,
            "benchmark": benchmark["audit"],
        },
    )
    print(
        json.dumps(
            {
                "source": source_audit,
                "enrichment": enrichment,
                "benchmark": benchmark["audit"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    cache = {r["alias_text"]: v for r, v in zip(base, base_vectors, strict=True)}
    cache.update({r["query"]: v for r, v in zip(rows, query_vectors, strict=True)})
    texts = {
        r["alias_text"] for r in enriched + benchmark["corpus"] + benchmark["training"]
    }
    for split in ("dev", "test"):
        texts.update(r["query"] for r in benchmark["queries"][split])
    missing = sorted(texts - cache.keys())
    embed, metadata = make_transformer_cls_encoder(
        args.embedding_model, batch_size=64, max_length=32, device_name="mps"
    )
    embedding_started = time.perf_counter()
    added = _normalize_embeddings(embed(missing), expected_rows=len(missing))
    cache.update(zip(missing, added, strict=True))
    arrays = {
        "enriched": np.stack([cache[r["alias_text"]] for r in enriched]),
        "benchmark-corpus": np.stack(
            [cache[r["alias_text"]] for r in benchmark["corpus"]]
        ),
        "training": np.stack([cache[r["alias_text"]] for r in benchmark["training"]]),
    }
    arrays.update(
        {
            split: np.stack([cache[r["query"]] for r in benchmark["queries"][split]])
            for split in ("dev", "test")
        }
    )
    for name, values in arrays.items():
        write_vectors(output / f"{name}.float32", values)
    embedding_seconds = time.perf_counter() - embedding_started
    del embed
    gc.collect()
    baseline = merge_gate(rows, batch_rank(query_vectors, base_vectors, base))
    for row, ranking in zip(rows, baseline, strict=True):
        if [r["cas_number"] for r in ranking[:3]] != [
            r["cas_number"] for r in row["systems"]["B_DENSE_FALLBACK"]["candidates"]
        ]:
            raise ValueError("기존 Dense Top-3 재현 불일치")
    augmented = merge_gate(
        rows, batch_rank(query_vectors, arrays["enriched"], enriched)
    )
    _private_json(
        output / "rankings.private.json", {"baseline": baseline, "enriched": augmented}
    )
    additions = {
        (r["cas_number"], compact_text(r["alias_text"])) for r in enriched[len(base) :]
    }
    overlap = {
        group: sum(
            (r["expected_cas"], compact_text(r["query"])) in additions
            for r in rows
            if r["cohort"] == "official_2020"
            and (group == "all_419" or r["unseen_surface"])
        )
        for group in ("all_419", "unseen_60")
    }
    report = {
        "fact_status": "부분 구현 또는 개발용 데모",
        "source_audit": source_audit,
        "enrichment": enrichment,
        "benchmark_split_audit": benchmark["audit"],
        "baseline": aggregate(rows, baseline),
        "enriched": aggregate(rows, augmented),
        "paired": paired(rows, baseline, augmented),
        "exact_query_overlap_with_additions": overlap,
        "safety": safety(rows, augmented, allowed),
        "embedding": metadata,
        "embedding_seconds": embedding_seconds,
        "new_embedding_text_count": len(missing),
        "decision": "SOURCE_ENRICHMENT_FEASIBLE_OFFLINE_ONLY_NOT_GENERALIZATION_EVIDENCE",
        "runtime_changed": False,
        "server_cost_krw": 0,
    }
    _private_json(output / "enrichment-report.json", report)
    manifest = {
        "input_hashes": {
            **INPUT_HASHES,
            "resolver": MODEL_HASH,
            "db": sha256_file(args.db),
        },
        "files": {
            p.name: sha256_file(p) for p in sorted(output.iterdir()) if p.is_file()
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "numpy": np.__version__,
        },
        **code_hashes,
        "embedding_snapshot_sha256": embedding_hashes,
    }
    if code_hashes != preparation_code_hashes():
        raise ValueError("준비 도중 실행 코드 변경; 새 출력 경로에서 다시 시작하세요")
    _private_json(output / "prepared-manifest.json", manifest)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def verify_prepared(args):
    manifest = read_json(args.output / "prepared-manifest.json")
    for name, actual in preparation_code_hashes().items():
        if manifest.get(name) != actual:
            raise ValueError(
                f"준비 단계 실행 코드 hash 불일치: {name}; 새 prepare가 필요합니다"
            )
    if manifest.get("embedding_snapshot_sha256") != BGE_M3_HASHES:
        raise ValueError("준비 단계 BGE-M3 snapshot hash 불일치")
    for name, expected in manifest["files"].items():
        assert_hash(args.output / name, expected)
    return read_json(args.output / "datasets.private.json")


def rerank(args):
    verify_prepared(args)
    verify_reranker_snapshot(args.reranker_model)
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    _, corpus, rows, _, _ = load_inputs(args)
    baseline = read_json(args.output / "rankings.private.json")["baseline"]
    tokenizer = AutoTokenizer.from_pretrained(
        args.reranker_model, local_files_only=True
    )
    model = (
        AutoModelForSequenceClassification.from_pretrained(
            args.reranker_model, local_files_only=True
        )
        .eval()
        .float()
        .to("mps")
    )
    counters = {"pair_count": 0, "truncated_pair_count": 0}
    started = time.perf_counter()

    def score(pairs):
        values = []
        lengths = tokenizer(pairs, truncation=False)["input_ids"]
        counters["pair_count"] += len(pairs)
        counters["truncated_pair_count"] += sum(len(x) > 128 for x in lengths)
        with torch.inference_mode():
            for start in range(0, len(pairs), 16):
                inputs = tokenizer(
                    pairs[start : start + 16],
                    padding=True,
                    truncation=True,
                    max_length=128,
                    return_tensors="pt",
                ).to("mps")
                values.extend(model(**inputs).logits.view(-1).float().cpu().tolist())
        return values

    ranked = []
    for i, (row, pool) in enumerate(zip(rows, baseline, strict=True)):
        ranked.append(
            pool if row["preserved_gate"] else rerank_pool(row["query"], pool, score)
        )
        if (i + 1) % 50 == 0:
            print(f"Top-20 재정렬 진행: {i + 1}/{len(rows)}", flush=True)
    seconds = time.perf_counter() - started
    original = aggregate(rows, baseline)
    metrics = aggregate(rows, ranked)
    invariant = safety(rows, ranked, {r["cas_number"] for r in corpus})
    improved = metrics["unseen_60"]["top3_hits"] > original["unseen_60"]["top3_hits"]
    nonregression = all(
        metrics[g][k] >= original[g][k]
        for g in original
        for k in ("top1_hits", "top3_hits")
    )
    passed = improved and nonregression and not any(invariant.values())
    _private_json(args.output / "reranker-rows.private.json", {"rankings": ranked})
    report = {
        "fact_status": "부분 구현 또는 개발용 데모",
        "baseline": original,
        "reranked": metrics,
        "paired": paired(rows, baseline, ranked),
        "safety": invariant,
        "decision": "CONDITIONAL_FURTHER_VALIDATION_ONLY"
        if passed
        else "REJECT_KEEP_RUNTIME_SPARSE",
        "model_revision": RERANK_REVISION,
        "model_hashes": {
            p.name: sha256_file(p)
            for p in sorted(args.reranker_model.iterdir())
            if p.is_file()
        },
        "device": "mps",
        "dtype": "float32",
        "batch_size": 16,
        "max_length": 128,
        "seconds_excluding_load": seconds,
        **counters,
        "runtime_changed": False,
        "server_cost_krw": 0,
        "predictions_sha256": sha256_file(args.output / "reranker-rows.private.json"),
    }
    _private_json(args.output / "reranker-report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def train(args):
    dataset = verify_prepared(args)
    _, base, rows, base_vectors, query_vectors = load_inputs(args)
    benchmark = dataset["benchmark"]
    corpus = benchmark["corpus"]
    corpus_vectors = vectors(args.output / "benchmark-corpus.float32", len(corpus))
    training = vectors(args.output / "training.float32", len(benchmark["training"]))
    dev = vectors(args.output / "dev.float32", len(benchmark["queries"]["dev"]))
    expected_dev = [q["expected_cas"] for q in benchmark["queries"]["dev"]]
    a, b, learning = train_projection(
        training,
        [r["cas_number"] for r in benchmark["training"]],
        dev,
        corpus_vectors,
        corpus,
        expected_dev,
        args.output,
    )
    # test 결과는 checkpoint 선택이 끝난 뒤에만 읽는다.
    test = vectors(args.output / "test.float32", len(benchmark["queries"]["test"]))
    expected_test = [q["expected_cas"] for q in benchmark["queries"]["test"]]
    baseline_test = batch_rank(test, corpus_vectors, corpus)
    candidate_test = batch_rank(
        apply_projection(test, a, b), apply_projection(corpus_vectors, a, b), corpus
    )
    base_metrics, candidate_metrics = (
        rank_metrics(baseline_test, expected_test),
        rank_metrics(candidate_test, expected_test),
    )
    baseline = read_json(args.output / "rankings.private.json")["baseline"]
    adapted = merge_gate(
        rows,
        batch_rank(
            apply_projection(query_vectors, a, b),
            apply_projection(base_vectors, a, b),
            base,
        ),
    )
    original, measured = aggregate(rows, baseline), aggregate(rows, adapted)
    invariant = safety(rows, adapted, {r["cas_number"] for r in base})
    approved = (
        learning["selected_epoch"] > 0
        and candidate_metrics["top20_hits"] > base_metrics["top20_hits"]
        and all(
            candidate_metrics[k] >= base_metrics[k] for k in ("top1_hits", "top3_hits")
        )
        and all(
            measured[g][k] >= original[g][k]
            for g in original
            for k in ("top1_hits", "top3_hits")
        )
        and not any(invariant.values())
    )
    _private_json(
        args.output / "projection-rows.private.json",
        {
            "benchmark_frozen": baseline_test,
            "benchmark_selected": candidate_test,
            "official_adapted": adapted,
        },
    )
    report = {
        "fact_status": "부분 구현 또는 개발용 데모",
        "learning": learning,
        "benchmark_audit": benchmark["audit"],
        "test_frozen": base_metrics,
        "test_selected": candidate_metrics,
        "official_baseline": original,
        "official_selected": measured,
        "official_paired": paired(rows, baseline, adapted),
        "safety": invariant,
        "decision": "CONDITIONAL_FURTHER_VALIDATION_ONLY"
        if approved
        else "REJECT_KEEP_FROZEN_ENCODER_AND_RUNTIME_SPARSE",
        "runtime_changed": False,
        "server_cost_krw": 0,
        "weights_sha256": {
            p.name: sha256_file(p)
            for p in sorted(args.output.glob("projection-epoch-*.npz"))
        },
        "predictions_sha256": sha256_file(args.output / "projection-rows.private.json"),
    }
    _private_json(args.output / "projection-report.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "rerank", "train"))
    for name in (
        "model",
        "db",
        "previous",
        "query-vectors",
        "embedding-model",
        "reranker-model",
        "output",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    args.output = args.output.resolve()
    if args.output.is_relative_to(root) or "private-data" not in args.output.parts:
        raise ValueError("출력은 Git 밖 private-data 하위여야 합니다")
    os.umask(0o077)
    signal.alarm(1800)
    globals()[args.stage](args)
    signal.alarm(0)


if __name__ == "__main__":
    main()
