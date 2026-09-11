"""로컬 전용 Dense 실험 도구. 운영 의존성이나 Resolver 기본값을 변경하지 않는다.

기존 experiment/hybrid-resolver@1715e1c의 corpus·CLS·RRF 계산을 재사용한다.
전체 공식 데이터 비교의 분할·공통 Gate·판정은 ulsan_resolver_comparison에서 담당한다.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from chemiguard119.utils import normalize_text, sha256_file, valid_cas_checksum

EmbeddingFunction = Callable[[list[str]], np.ndarray]
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"
BGE_M3_HASHES = {
    "pytorch_model.bin": "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38",
    "config.json": "26159e7ad065073448460117eb24b7a4572f6f4e78eadff65dc0a11c052449fa",
    "tokenizer.json": "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08",
    "tokenizer_config.json": "a62b2b6784f990259fddef5f16388693a8043be4f69179e6a5257eeb3f9abac4",
    "special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
    "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
}


def verify_bge_m3_snapshot(model_path: Path) -> dict[str, str]:
    """고정 실험의 모델 bytes를 적재 전에 검증한다. 폴더명만 신뢰하지 않는다."""
    if model_path.name != BGE_M3_REVISION:
        raise ValueError("사전 고정 BGE-M3 revision 경로가 아닙니다.")
    for name, expected in BGE_M3_HASHES.items():
        path = model_path / name
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"사전 고정 BGE-M3 파일 hash 불일치: {name}")
    # Transformers가 검증하지 않은 safetensors/adapter/추가 vocabulary를
    # 우선 로드하지 못하게 한다. SentenceTransformer 전용 메타데이터는 미사용.
    allowed = set(BGE_M3_HASHES) | {
        "README.md",
        "LICENSE",
        ".gitattributes",
        "modules.json",
        "sentence_bert_config.json",
        "config_sentence_transformers.json",
    }
    if any(
        path.is_file() and path.name not in allowed for path in model_path.iterdir()
    ):
        raise ValueError("사전 고정 BGE-M3 snapshot에 미검증 파일이 있습니다.")
    return dict(BGE_M3_HASHES)


def build_dense_alias_corpus(artifact: dict[str, Any]) -> list[dict[str, str]]:
    """운영 안전 경계를 유지한 Dense 후보용 별칭 목록을 만든다.

    source-only CAS는 기존 정책대로 정확 일치에만 노출하며 Dense 유사도 후보에서
    제외한다. 숫자 CAS 자체도 의미 임베딩 corpus에는 넣지 않는다.
    """

    seen: set[tuple[str, str]] = set()
    corpus: list[dict[str, str]] = []
    for row in artifact.get("rows", []):
        cas_number = str(row.get("cas_number") or "")
        alias_text = str(row.get("alias_text") or "").strip()
        alias_type = str(row.get("alias_type") or "").strip().lower()
        if (
            not valid_cas_checksum(cas_number)
            or not alias_text
            or alias_type == "cas"
            or row.get("fuzzy_search_eligible") is False
        ):
            continue
        key = (cas_number, normalize_text(alias_text))
        if key in seen:
            continue
        seen.add(key)
        corpus.append(
            {
                "cas_number": cas_number,
                "alias_text": alias_text,
                "alias_type": alias_type,
            }
        )
    if not corpus:
        raise RuntimeError("Dense 후보로 사용할 검증 별칭이 없습니다.")
    return sorted(
        corpus,
        key=lambda row: (row["cas_number"], row["alias_type"], row["alias_text"]),
    )


def _normalize_embeddings(values: np.ndarray, *, expected_rows: int) -> np.ndarray:
    matrix = np.asarray(values, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] != expected_rows:
        raise ValueError(
            "embedding 결과 shape가 잘못되었습니다: "
            f"expected_rows={expected_rows}, actual={matrix.shape}"
        )
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(~np.isfinite(matrix)) or np.any(norms <= 0):
        raise ValueError("embedding 결과에 비유한 값 또는 영벡터가 있습니다.")
    return matrix / norms


def _rank_dense(
    query_vector: np.ndarray,
    corpus_vectors: np.ndarray,
    corpus: list[dict[str, str]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    scores = corpus_vectors @ query_vector
    best_by_cas: dict[str, tuple[float, int]] = {}
    for index, score in enumerate(scores.tolist()):
        cas_number = corpus[index]["cas_number"]
        current = best_by_cas.get(cas_number)
        if current is None or score > current[0]:
            best_by_cas[cas_number] = (float(score), index)
    ranked = sorted(best_by_cas.items(), key=lambda item: (-item[1][0], item[0]))
    return [
        {
            "cas_number": cas_number,
            "score": round(score, 6),
            "matched_alias": corpus[index]["alias_text"],
            "match_type": "BGE_M3_DENSE_CANDIDATE",
            "rule_eligible": False,
        }
        for cas_number, (score, index) in ranked[:top_k]
    ]


def _rrf_fuse(
    sparse: list[dict[str, Any]],
    dense: list[dict[str, Any]],
    *,
    top_k: int,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    scores: dict[str, float] = defaultdict(float)
    sources: dict[str, set[str]] = defaultdict(set)
    for source, ranking in (("SPARSE", sparse), ("DENSE", dense)):
        for rank, candidate in enumerate(ranking, 1):
            cas_number = str(candidate["cas_number"])
            scores[cas_number] += 1.0 / (rrf_k + rank)
            sources[cas_number].add(source)
    ranked = sorted(scores, key=lambda cas: (-scores[cas], cas))
    return [
        {
            "cas_number": cas_number,
            "score": round(scores[cas_number], 8),
            "sources": sorted(sources[cas_number]),
            "match_type": "SPARSE_DENSE_RRF_CANDIDATE",
            "rule_eligible": False,
        }
        for cas_number in ranked[:top_k]
    ]


def _candidate_rank(candidates: Iterable[dict[str, Any]], expected: str) -> int | None:
    ranking = [str(item["cas_number"]) for item in candidates]
    return ranking.index(expected) + 1 if expected in ranking else None


def make_transformer_cls_encoder(
    model_path: Path,
    *,
    batch_size: int = 64,
    max_length: int = 32,
    device_name: str = "auto",
) -> tuple[EmbeddingFunction, dict[str, Any]]:
    """로컬 Hugging Face 가중치에서 BGE 계열 CLS encoder를 만든다."""

    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as error:  # pragma: no cover - optional experiment dependency
        raise RuntimeError(
            "Dense probe에는 torch와 transformers가 필요합니다. "
            "운영 dependency에는 포함하지 마세요."
        ) from error

    if device_name == "auto":
        if torch.backends.mps.is_available():
            device_name = "mps"
        elif torch.cuda.is_available():
            device_name = "cuda"
        else:
            device_name = "cpu"
    device = torch.device(device_name)
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
    model = AutoModel.from_pretrained(model_path, local_files_only=True)
    model.eval()
    model.float()
    model.to(device)

    def embed(texts: list[str]) -> np.ndarray:
        batches: list[np.ndarray] = []
        lengths = tokenizer(texts, truncation=False, padding=False)["input_ids"]
        metadata["encoded_text_count"] += len(texts)
        metadata["truncated_text_count"] += sum(
            len(ids) > max_length for ids in lengths
        )
        with torch.inference_mode():
            for start in range(0, len(texts), batch_size):
                encoded = tokenizer(
                    texts[start : start + batch_size],
                    padding=True,
                    truncation=True,
                    max_length=max_length,
                    return_tensors="pt",
                ).to(device)
                vectors = model(**encoded).last_hidden_state[:, 0]
                vectors = torch.nn.functional.normalize(vectors, p=2, dim=1)
                batches.append(vectors.float().cpu().numpy())
                print(
                    f"임베딩 진행: {min(start + batch_size, len(texts))}/{len(texts)}",
                    flush=True,
                )
        return np.concatenate(batches, axis=0)

    weight_candidates = sorted(model_path.glob("*.safetensors")) + sorted(
        model_path.glob("pytorch_model*.bin")
    )
    if not weight_candidates:
        raise RuntimeError(f"로컬 모델 가중치 파일을 찾을 수 없습니다: {model_path}")
    config_path = model_path / "config.json"
    tokenizer_path = model_path / "tokenizer.json"
    if not config_path.is_file() or not tokenizer_path.is_file():
        raise RuntimeError("로컬 모델 config.json 또는 tokenizer.json이 없습니다.")
    metadata = {
        "model_reference": model_path.name,
        "weight_files": [path.name for path in weight_candidates],
        "weight_sha256": {path.name: sha256_file(path) for path in weight_candidates},
        "config_sha256": sha256_file(config_path),
        "tokenizer_sha256": sha256_file(tokenizer_path),
        "local_files_only": True,
        "pooling": "CLS",
        "normalized": True,
        "max_length": max_length,
        "batch_size": batch_size,
        "device": device_name,
        "candidate_score_is_probability": False,
        "dtype": "float32",
        "encoded_text_count": 0,
        "truncated_text_count": 0,
        "torch_version": torch.__version__,
        "numpy_version": np.__version__,
    }
    return embed, metadata
