"""잠금 원본 확보 전 Hybrid Resolver 후보를 거르는 오프라인 진단.

이 모듈은 운영 Resolver를 바꾸지 않는다. 저장소에 이미 공개된 실패 예시와
내부 회귀 질의에서 Sparse, Dense, RRF 결합 후보의 순위만 비교한다. 전체 울산
원본을 다시 확보해 419건·미관측 60건을 재평가하기 전에는 어떤 결과도 runtime
채택 근거로 사용할 수 없다.
"""

from __future__ import annotations

import csv
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from chemiguard119.resolver import (
    evaluate_resolver_hint_safety,
    load_resolver,
    resolve_substance,
)
from chemiguard119.utils import (
    normalize_text,
    sha256_file,
    valid_cas_checksum,
    write_json,
)


METRICS_VERSION = "hybrid-resolver-observed-probe-v1"
CLAIM_SCOPE = "OBSERVED_FAILURE_SAMPLE_AND_INTERNAL_REGRESSION_DIAGNOSTIC_ONLY"
RUNTIME_ADOPTION_DECISION = "NOT_ELIGIBLE_MISSING_FULL_TEMPORAL_SOURCE"

EmbeddingFunction = Callable[[list[str]], np.ndarray]


def _load_observed_failure_cases(snapshot_path: Path) -> list[dict[str, str]]:
    import json

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    cohorts = {
        "DEVELOPMENT_FAILURE_SAMPLE": payload["validation"]["adapted"][
            "failure_examples"
        ],
        "LOCKED_RESULT_EXPOSED_FAILURE_SAMPLE": payload["locked_test"]["adapted"][
            "failure_examples"
        ],
    }
    cases: list[dict[str, str]] = []
    for cohort, rows in cohorts.items():
        for index, row in enumerate(rows, 1):
            cases.append(
                {
                    "case_id": f"{cohort}-{index:03d}",
                    "cohort": cohort,
                    "query": str(row["query"]),
                    "expected_cas": str(row["expected_cas"]),
                    "review_status": "PREVIOUSLY_OBSERVED_FAILURE_EXAMPLE",
                }
            )
    return cases


def _load_internal_regression_cases(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        {
            "case_id": f"INTERNAL_REGRESSION-{index:03d}",
            "cohort": "INTERNAL_REGRESSION",
            "query": str(row["query"]),
            "expected_cas": str(row["expected_cas"]),
            "review_status": "DRAFT_INTERNAL_REGRESSION",
        }
        for index, row in enumerate(rows, 1)
    ]


def load_probe_cases(
    temporal_snapshot_path: Path,
    regression_path: Path,
) -> list[dict[str, str]]:
    """공개된 실패 예시와 내부 회귀를 섞지 않고 한 입력 목록으로 읽는다."""

    cases = _load_observed_failure_cases(temporal_snapshot_path)
    cases.extend(_load_internal_regression_cases(regression_path))
    invalid = sorted(
        {
            row["expected_cas"]
            for row in cases
            if not valid_cas_checksum(row["expected_cas"])
        }
    )
    if invalid:
        raise ValueError(f"유효하지 않은 기대 CAS가 있습니다: {invalid}")
    case_ids = [row["case_id"] for row in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("probe case_id가 중복되었습니다.")
    return cases


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


def _metrics(rows: list[dict[str, Any]], system: str) -> dict[str, Any]:
    ranks = [row["ranks"][system] for row in rows]
    count = len(ranks)
    return {
        "case_count": count,
        "top1_accuracy": round(sum(rank == 1 for rank in ranks) / count, 6),
        "top3_recall": round(
            sum(rank is not None and rank <= 3 for rank in ranks) / count,
            6,
        ),
        "mrr": round(
            sum(1.0 / rank if rank else 0.0 for rank in ranks) / count,
            6,
        ),
    }


def evaluate_hybrid_probe(
    *,
    resolver_model_path: Path,
    temporal_snapshot_path: Path,
    regression_path: Path,
    safety_evaluation_path: Path,
    embed: EmbeddingFunction,
    embedding_model: dict[str, Any],
    report_path: Path | None = None,
    retrieval_depth: int = 100,
    top_k: int = 3,
) -> dict[str, Any]:
    """Sparse·Dense·RRF를 관측된 작은 probe에서 비교한다."""

    if retrieval_depth < top_k:
        raise ValueError("retrieval_depth는 top_k 이상이어야 합니다.")
    artifact = load_resolver(resolver_model_path)
    cases = load_probe_cases(temporal_snapshot_path, regression_path)
    corpus = build_dense_alias_corpus(artifact)

    started = time.perf_counter()
    corpus_vectors = _normalize_embeddings(
        embed([row["alias_text"] for row in corpus]),
        expected_rows=len(corpus),
    )
    query_vectors = _normalize_embeddings(
        embed([row["query"] for row in cases]),
        expected_rows=len(cases),
    )
    embedding_seconds = time.perf_counter() - started

    rows: list[dict[str, Any]] = []
    for case, query_vector in zip(cases, query_vectors, strict=True):
        sparse_result = resolve_substance(
            case["query"],
            artifact,
            top_k=retrieval_depth,
            minimum_score=0.0,
        )
        sparse = list(sparse_result.get("candidates") or [])
        dense = _rank_dense(
            query_vector,
            corpus_vectors,
            corpus,
            top_k=retrieval_depth,
        )
        # 기존 exact·ambiguous 계약은 Dense가 덮어쓰지 않는다. Hybrid는 fuzzy 또는
        # unresolved 입력에만 후보 순위를 보조한다.
        if sparse_result.get("status") in {
            "EXACT_IDENTIFIER_MATCH",
            "EXACT_ALIAS_CANDIDATE",
            "AMBIGUOUS_ALIAS",
        }:
            hybrid = sparse[:top_k]
            hybrid_policy = "PRESERVE_SPARSE_EXACT_OR_AMBIGUOUS_RESULT"
        else:
            hybrid = _rrf_fuse(sparse, dense, top_k=top_k)
            hybrid_policy = "RRF_FOR_FUZZY_OR_UNRESOLVED_ONLY"
        expected = case["expected_cas"]
        rows.append(
            {
                **case,
                "sparse_status": sparse_result.get("status"),
                "hybrid_policy": hybrid_policy,
                "ranks": {
                    "A_SPARSE": _candidate_rank(sparse[:top_k], expected),
                    "B_DENSE": _candidate_rank(dense[:top_k], expected),
                    "C_SPARSE_DENSE_RRF": _candidate_rank(hybrid, expected),
                },
                "top_candidates": {
                    "A_SPARSE": [item["cas_number"] for item in sparse[:top_k]],
                    "B_DENSE": [item["cas_number"] for item in dense[:top_k]],
                    "C_SPARSE_DENSE_RRF": [item["cas_number"] for item in hybrid],
                },
                "dense_top1_score": dense[0]["score"] if dense else None,
                "dense_top1_margin": (
                    round(float(dense[0]["score"]) - float(dense[1]["score"]), 6)
                    if len(dense) > 1
                    else None
                ),
            }
        )

    by_cohort: dict[str, Any] = {}
    for cohort in sorted({row["cohort"] for row in rows}):
        selected = [row for row in rows if row["cohort"] == cohort]
        by_cohort[cohort] = {
            system: _metrics(selected, system)
            for system in (
                "A_SPARSE",
                "B_DENSE",
                "C_SPARSE_DENSE_RRF",
            )
        }

    safety = evaluate_resolver_hint_safety(
        resolver_model_path,
        safety_evaluation_path,
    )
    development = by_cohort["DEVELOPMENT_FAILURE_SAMPLE"]
    locked_exposed = by_cohort["LOCKED_RESULT_EXPOSED_FAILURE_SAMPLE"]
    regression = by_cohort["INTERNAL_REGRESSION"]
    proceed_to_full_ablation = bool(
        development["C_SPARSE_DENSE_RRF"]["top3_recall"]
        > development["A_SPARSE"]["top3_recall"]
        and locked_exposed["C_SPARSE_DENSE_RRF"]["top3_recall"]
        > locked_exposed["A_SPARSE"]["top3_recall"]
        and regression["C_SPARSE_DENSE_RRF"]["top3_recall"]
        >= regression["A_SPARSE"]["top3_recall"]
        and safety["deployment_gate"]["passed"] is True
    )
    payload = {
        "metrics_version": METRICS_VERSION,
        "task": "substance_candidate_ranking_diagnostic_only",
        "claim_scope": CLAIM_SCOPE,
        "fact_status": "부분 구현 또는 개발용 데모",
        "inputs": {
            "resolver_model": resolver_model_path.name,
            "resolver_model_sha256": sha256_file(resolver_model_path),
            "temporal_snapshot": temporal_snapshot_path.name,
            "temporal_snapshot_sha256": sha256_file(temporal_snapshot_path),
            "regression_dataset": regression_path.name,
            "regression_dataset_sha256": sha256_file(regression_path),
            "safety_dataset": safety_evaluation_path.name,
            "safety_dataset_sha256": sha256_file(safety_evaluation_path),
            "embedding_model": embedding_model,
            "evaluator_source_sha256": sha256_file(Path(__file__)),
            "dense_alias_count": len(corpus),
            "case_count": len(cases),
        },
        "split_notice": {
            "development_failure_sample": "2019 결과에서 이미 공개된 실패 예시",
            "locked_result_exposed_failure_sample": (
                "2020 잠금 결과에서 이미 노출된 실패 예시이며 새 잠금 테스트가 아님"
            ),
            "internal_regression": "작은 DRAFT 내부 회귀셋",
        },
        "systems": {
            "A_SPARSE": "기존 exact + 문자 n-gram TF-IDF",
            "B_DENSE": "사전학습 BGE-M3 dense 후보",
            "C_SPARSE_DENSE_RRF": (
                "exact·ambiguity는 Sparse 보존, fuzzy·unresolved만 RRF 결합"
            ),
        },
        "metrics_by_cohort": by_cohort,
        "safety": {
            "existing_hint_gate_passed": safety["deployment_gate"]["passed"],
            "unsafe_auto_hint_count": safety["unsafe_auto_hint_count"],
            "wrong_cas_auto_hint_count": safety["wrong_cas_auto_hint_count"],
            "resolver_rule_eligibility_violation_count": safety[
                "resolver_rule_eligibility_violation_count"
            ],
            "runtime_default_changed": False,
            "dense_source_only_cas_excluded": True,
            "dense_candidate_rule_eligible": False,
        },
        "timing": {
            "embedding_seconds": round(embedding_seconds, 6),
            "latency_is_service_slo": False,
        },
        "gate": {
            "proceed_to_full_temporal_ablation": proceed_to_full_ablation,
            "runtime_adoption_decision": RUNTIME_ADOPTION_DECISION,
            "requires_official_full_source": True,
            "requires_full_419_and_unseen_60_reproduction": True,
            "requires_reranker_and_selective_abstention_ablation": True,
        },
        "rows": rows,
        "limitations": [
            "실패 예시만 모은 선택 편향 표본이며 전체 정확도를 계산할 수 없습니다.",
            "2020 실패 예시는 이미 관찰된 결과이므로 새 잠금 테스트가 아닙니다.",
            "BGE-M3는 한국어 화학물질 Entity Linking용으로 학습된 모델이 아닙니다.",
            "Dense 점수는 확률이 아니며 CAS 확정이나 Rule 입력으로 사용할 수 없습니다.",
            "전체 울산 원본을 다시 확보하기 전에는 419건·미관측 60건을 재현할 수 없습니다.",
        ],
    }
    if report_path:
        write_json(report_path, payload)
    return payload


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
    model.to(device)

    def embed(texts: list[str]) -> np.ndarray:
        batches: list[np.ndarray] = []
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
    }
    return embed, metadata


__all__ = [
    "CLAIM_SCOPE",
    "METRICS_VERSION",
    "RUNTIME_ADOPTION_DECISION",
    "build_dense_alias_corpus",
    "evaluate_hybrid_probe",
    "load_probe_cases",
    "make_transformer_cls_encoder",
]
