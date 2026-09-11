"""공식 별칭·CAS-disjoint metric learning의 로컬 실험 도구. API와 분리한다."""

from __future__ import annotations

import csv
import hashlib
import io
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

import numpy as np

from chemiguard119.incident_replay import _fingerprint
from chemiguard119.resolver_dense_experiment import _rank_dense
from chemiguard119.utils import compact_text, normalize_cas, valid_cas_checksum

SEED = 20260911
SOURCE_URL = "https://www.data.go.kr/data/15081005/fileData.do"
DOWNLOAD_URL = (
    "https://www.data.go.kr/cmm/cmm/fileDownload.do?"
    "atchFileId=FILE_000000002667763&fileDetailSn=1&insertDataPrcus=N"
)
SOURCE_COLUMNS = ("cas_등록번호", "화학물질 한글명", "화학물질 영문명")
PUBLIC_TYPES = {
    "icis_primary_name",
    "icis_reported_alias",
    "kosha_name",
    "search_name",
    "ulsan_name_ko",
    "ulsan_name_en",
    "official_ulsan_ko",
    "official_ulsan_en",
}


def keyed_hash(value: str) -> str:
    return hashlib.sha256(f"{SEED}:{value}".encode()).hexdigest()


def source_aliases(payload: bytes) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """공식 물질 표의 세 컬럼만 사용. 복합 CAS와 공유 표현은 격리한다."""
    if not 0 < len(payload) <= 10 * 1024 * 1024:
        raise ValueError("공식 물질 CSV 크기 제한 위반")
    encoding = "utf-8-sig"
    try:
        text = payload.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp949"
        text = payload.decode(encoding)
    reader = csv.DictReader(io.StringIO(text), strict=True)
    if not set(SOURCE_COLUMNS).issubset(reader.fieldnames or []):
        raise ValueError("공식 물질 CSV 필수 컬럼 불일치")
    records = []
    counts = Counter()
    for row in reader:
        counts["source_rows"] += 1
        cas = normalize_cas(row[SOURCE_COLUMNS[0]])
        if not valid_cas_checksum(cas):
            counts["invalid_or_composite_cas_rows"] += 1
            continue
        for column, kind in zip(SOURCE_COLUMNS[1:], ("ko", "en"), strict=True):
            for name in str(row[column] or "").split(";"):
                name = name.strip()
                normalized = compact_text(name)
                if len(normalized) < 2 or normalized in {"na", "null", "자료없음"}:
                    counts["empty_names"] += 1
                    continue
                records.append(
                    {
                        "cas_number": cas,
                        "alias_text": name,
                        "alias_type": f"official_ulsan_{kind}",
                    }
                )
    clean, audit = unambiguous_aliases(records)
    return clean, {
        **dict(counts),
        **audit,
        "encoding": encoding,
        "source_url": SOURCE_URL,
        "document_version": "2021-01-15 기준",
        "license_observed": "이용허락범위 제한 없음 · 무료",
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "chemical_expert_reviewed": False,
    }


def unambiguous_aliases(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], dict]:
    targets: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        targets[compact_text(row["alias_text"])].add(row["cas_number"])
    ambiguous = {text for text, targets_ in targets.items() if len(targets_) > 1}
    unique = {}
    for row in sorted(
        rows, key=lambda r: (r["cas_number"], r["alias_text"], r["alias_type"])
    ):
        key = (row["cas_number"], compact_text(row["alias_text"]))
        if key[1] and key[1] not in ambiguous:
            unique.setdefault(
                key, {k: row[k] for k in ("cas_number", "alias_text", "alias_type")}
            )
    return list(unique.values()), {
        "ambiguous_surface_count": len(ambiguous),
        "unique_alias_count": len(unique),
        "unique_cas_count": len({k[0] for k in unique}),
    }


def enrich_corpus(
    base: list[dict], official: list[dict], artifact: dict
) -> tuple[list[dict], dict]:
    """기존 fuzzy 경계 안의 이름만 추가. 기존 Gate에는 추가하지 않는다."""
    eligible = {row["cas_number"] for row in base}
    catalog = {row["cas_number"] for row in artifact["rows"]}
    all_rows = base + official
    targets: dict[str, set[str]] = defaultdict(set)
    for row in all_rows:
        targets[compact_text(row["alias_text"])].add(row["cas_number"])
    existing = {(r["cas_number"], compact_text(r["alias_text"])) for r in base}
    additions = []
    counts = Counter()
    new_cas = set()
    for row in official:
        cas, normalized = row["cas_number"], compact_text(row["alias_text"])
        if cas not in catalog:
            new_cas.add(cas)
            counts["new_cas_aliases_quarantined"] += 1
        elif cas not in eligible:
            counts["exact_only_aliases_quarantined"] += 1
        elif len(targets[normalized]) > 1:
            counts["conflicting_aliases_quarantined"] += 1
        elif (cas, normalized) in existing:
            counts["duplicate_aliases"] += 1
        else:
            additions.append(row)
            existing.add((cas, normalized))
    return base + additions, {
        **dict(counts),
        "base_alias_count": len(base),
        "added_alias_count": len(additions),
        "new_cas_count_quarantined": len(new_cas),
        "exact_gate_modified": False,
        "source_only_fuzzy_leak_count": 0,
    }


def make_benchmark(public_rows: list[dict]) -> dict:
    """CAS group 분할과 dev/test 질의의 전역 corpus 제거를 모델 실행 전에 고정."""
    clean, audit = unambiguous_aliases(public_rows)
    by_cas = defaultdict(list)
    for row in clean:
        by_cas[row["cas_number"]].append(row)
    groups = {name: [] for name in ("train", "dev", "test")}
    partition = {}
    for cas, rows in sorted(by_cas.items()):
        bucket = int(keyed_hash(cas), 16) % 100
        split = "train" if bucket < 70 else "dev" if bucket < 85 else "test"
        partition[cas] = split
        if len(rows) >= 2:
            groups[split].append(cas)
    heldout = set()
    queries = {"dev": [], "test": []}
    for split in queries:
        for cas in groups[split]:
            row = min(
                by_cas[cas], key=lambda r: keyed_hash(compact_text(r["alias_text"]))
            )
            heldout.add(compact_text(row["alias_text"]))
            queries[split].append({"query": row["alias_text"], "expected_cas": cas})
    corpus = [r for r in clean if compact_text(r["alias_text"]) not in heldout]
    training = [r for r in corpus if partition[r["cas_number"]] == "train"]
    train_cas = {r["cas_number"] for r in training}
    dev_cas = {r["expected_cas"] for r in queries["dev"]}
    test_cas = {r["expected_cas"] for r in queries["test"]}
    if train_cas & (dev_cas | test_cas) or dev_cas & test_cas:
        raise ValueError("CAS split 누수")
    if heldout & {compact_text(r["alias_text"]) for r in corpus}:
        raise ValueError("질의 표현 corpus 누수")
    if not all(
        {q["expected_cas"] for q in queries[s]} <= {r["cas_number"] for r in corpus}
        for s in queries
    ):
        raise ValueError("holdout 정답 CAS가 corpus에 없음")
    return {
        "corpus": corpus,
        "training": training,
        "queries": queries,
        "audit": {
            **audit,
            "eligible_group_counts": {k: len(v) for k, v in groups.items()},
            "training_alias_count": len(training),
            "corpus_alias_count": len(corpus),
            "cas_split_overlap_count": 0,
            "heldout_surface_in_corpus_count": 0,
            "split_fingerprint": _fingerprint(
                {"partition": partition, "queries": queries}
            ),
            "uses_incident_records_for_training": False,
        },
    }


def rank_metrics(rankings: list[list[dict]], expected: list[str]) -> dict:
    if len(rankings) != len(expected) or not rankings:
        raise ValueError("평가 분모 불일치 또는 빈 평가")
    ranks = []
    for ranking, target in zip(rankings, expected, strict=True):
        cas = [r["cas_number"] for r in ranking]
        if len(cas) != len(set(cas)):
            raise ValueError("CAS 후보 중복")
        ranks.append(cas.index(target) + 1 if target in cas else None)
    return {
        "count": len(ranks),
        **{
            f"top{k}_hits": sum(r is not None and r <= k for r in ranks)
            for k in (1, 3, 20)
        },
        **{
            f"top{k}_rate": round(
                sum(r is not None and r <= k for r in ranks) / len(ranks), 6
            )
            for k in (1, 3, 20)
        },
        "mrr_at_20": round(sum(1 / r if r else 0 for r in ranks) / len(ranks), 6),
    }


def rerank_pool(query: str, pool: list[dict], score_pairs: Callable) -> list[dict]:
    if not pool:
        return []
    values = np.asarray(
        score_pairs([(query, r["matched_alias"]) for r in pool]), dtype=np.float32
    )
    if values.shape != (len(pool),) or not np.isfinite(values).all():
        raise ValueError("Reranker 점수 shape/유한성 위반")
    order = sorted(range(len(pool)), key=lambda i: (-float(values[i]), i))
    return [
        {
            **pool[i],
            "score": float(values[i]),
            "match_type": "CROSS_ENCODER_CANDIDATE",
            "rule_eligible": False,
            "current_inventory_confirmed": False,
        }
        for i in order
    ]


def candidate_safety(rankings: list[list[dict]], allowed_cas: set[str]) -> dict:
    return {
        "rule_eligibility_violation_count": sum(
            bool(r.get("rule_eligible")) for items in rankings for r in items
        ),
        "fuzzy_catalog_escape_count": sum(
            r["cas_number"] not in allowed_cas for items in rankings for r in items
        ),
        "current_inventory_violation_count": sum(
            bool(r.get("current_inventory_confirmed"))
            for items in rankings
            for r in items
        ),
    }


def batch_rank(
    queries: np.ndarray, vectors: np.ndarray, corpus: list[dict]
) -> list[list[dict]]:
    return [_rank_dense(q, vectors, corpus, top_k=20) for q in queries]


def choose_epoch(metrics: dict[int, dict]) -> int:
    return max(
        metrics, key=lambda e: (metrics[e]["top20_hits"], metrics[e]["top3_hits"], -e)
    )


def train_projection(
    train_vectors: np.ndarray,
    train_cas: list[str],
    dev_vectors: np.ndarray,
    corpus_vectors: np.ndarray,
    corpus: list[dict],
    dev_expected: list[str],
    output_dir: Path,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Frozen embedding 위의 rank-32 residual metric 학습. test 인자를 받지 않는다."""
    import torch

    torch.manual_seed(SEED)
    rng = np.random.default_rng(SEED)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    x = torch.tensor(train_vectors, device=device)
    dim = x.shape[1]
    a = torch.nn.Parameter(torch.randn(dim, 32, device=device) * 0.01)
    b = torch.nn.Parameter(torch.zeros(32, dim, device=device))
    optimizer = torch.optim.AdamW([a, b], lr=0.001, weight_decay=0.01)
    groups = defaultdict(list)
    for i, cas in enumerate(train_cas):
        groups[cas].append(i)
    pairs = [
        (i, members[(j + 1) % len(members)])
        for members in groups.values()
        if len(members) > 1
        for j, i in enumerate(members)
    ]
    if len(groups) < 30 or not pairs:
        raise ValueError("학습 synonym CAS 부족")
    # hard negative mining은 train CAS와 frozen 벡터만 사용한다.
    cas_ids = np.array([sorted(groups).index(cas) for cas in train_cas])
    negatives = []
    for anchor, _ in pairs:
        scores = train_vectors @ train_vectors[anchor]
        scores[cas_ids == cas_ids[anchor]] = -np.inf
        negatives.append(int(np.argmax(scores)))
    triples = np.array([(p[0], p[1], n) for p, n in zip(pairs, negatives, strict=True)])
    label = torch.tensor(cas_ids, device=device)

    def project(t):
        return torch.nn.functional.normalize(t + (t @ a) @ b, dim=1)

    def export():
        return a.detach().cpu().numpy(), b.detach().cpu().numpy()

    def evaluate(aa, bb):
        return rank_metrics(
            batch_rank(
                apply_projection(dev_vectors, aa, bb),
                apply_projection(corpus_vectors, aa, bb),
                corpus,
            ),
            dev_expected,
        )

    initial_a, initial_b = export()
    checkpoints = {0: (initial_a.copy(), initial_b.copy())}
    evaluations = {0: evaluate(initial_a, initial_b)}
    losses = []
    started = time.monotonic()
    for epoch in range(1, 21):
        order = rng.permutation(len(triples))
        total = 0.0
        batches = 0
        for start in range(0, len(order), 64):
            if time.monotonic() - started > 600:
                raise TimeoutError("학습 10분 상한 초과; 채택 중단")
            qids, pids, nids = triples[order[start : start + 64]].T
            q, p, n = project(x[qids]), project(x[pids]), project(x[nids])
            candidates = torch.cat([p, n])
            scores = q @ candidates.T / 0.07
            candidate_ids = torch.cat([label[pids], label[nids]])
            same = label[qids, None] == candidate_ids[None, :]
            same[
                torch.arange(len(q), device=device), torch.arange(len(q), device=device)
            ] = False
            scores = scores.masked_fill(same, -1e9)
            loss = torch.nn.functional.cross_entropy(
                scores, torch.arange(len(q), device=device)
            )
            loss = loss + 0.01 * (((x[qids] @ a) @ b) ** 2).mean()
            if not bool(torch.isfinite(loss)):
                raise ValueError("학습 손실 비유한 값")
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += float(loss.detach().cpu())
            batches += 1
        losses.append({"epoch": epoch, "loss": total / batches})
        print(f"도메인 projection 학습: {epoch}/20 epoch", flush=True)
        if epoch in (5, 10, 20):
            aa, bb = export()
            checkpoints[epoch] = (aa.copy(), bb.copy())
            evaluations[epoch] = evaluate(aa, bb)
    selected = choose_epoch(evaluations)
    aa, bb = checkpoints[selected]
    # 가중치는 private 출력 경로만 허용하는 CLI가 지정한다.
    from chemiguard119.incident_replay import _private_bytes

    for epoch, (ca, cb) in checkpoints.items():
        buffer = io.BytesIO()
        np.savez(buffer, a=ca, b=cb)
        _private_bytes(output_dir / f"projection-epoch-{epoch}.npz", buffer.getvalue())
    return (
        aa,
        bb,
        {
            "device": device,
            "selected_epoch": selected,
            "dev_checkpoints": evaluations,
            "training_losses": losses,
            "training_pair_count": len(pairs),
            "training_seconds": time.monotonic() - started,
            "encoder_frozen": True,
            "projection_rank": 32,
            "trainable_parameter_count": dim * 32 * 2,
            "test_used_for_selection": False,
            "seed": SEED,
        },
    )


def apply_projection(values: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    updated = values + (values @ a) @ b
    norms = np.linalg.norm(updated, axis=1, keepdims=True)
    if not np.isfinite(updated).all() or np.any(norms == 0):
        raise ValueError("projection 벡터 유한성/영벡터 위반")
    return updated / norms
