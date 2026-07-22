#!/usr/bin/env python3
"""설치된 LM Studio 임베딩 모델의 케미가드119용 최소 검색 점검."""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = ROOT / "outputs" / "lmstudio_benchmark"

DOCUMENTS = [
    ("hcl", "염산, 염화수소, HCl, Hydrochloric acid. CAS 7647-01-0. 산성 부식성 물질."),
    (
        "hypochlorite",
        "차아염소산나트륨, Sodium hypochlorite. CAS 7681-52-9. 산과 접촉하면 염소가스 위험.",
    ),
    ("acetone", "아세톤, Acetone. CAS 67-64-1. 인화성이 높은 유기용제."),
    (
        "sodium",
        "금속 나트륨, Sodium metal. CAS 7440-23-5. 물과 접촉하면 수소가 발생할 수 있음.",
    ),
    ("ammonia", "암모니아, Ammonia. CAS 7664-41-7. 자극성 독성 가스 누출 위험."),
    ("sulfuric_acid", "황산, Sulfuric acid. CAS 7664-93-9. 강산성 부식성 액체."),
]

QUERIES = [
    ("염산 누출 물질을 찾아줘", "hcl", "korean_name"),
    (
        "차아염소산나트륨이 산과 섞여 염소가스가 날 수 있음",
        "hypochlorite",
        "korean_hazard",
    ),
    ("7647-01-0", "hcl", "cas_exact"),
    ("HCl leak", "hcl", "english_alias"),
    ("물에 닿으면 수소가 생기는 금속", "sodium", "korean_hazard"),
    ("아세톤 화재", "acetone", "korean_name"),
    ("암모니아 누출", "ammonia", "korean_name"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--timeout", type=int, default=60)
    return parser.parse_args()


def embed(
    base_url: str, model: str, texts: list[str], timeout: int
) -> list[list[float]]:
    request = Request(
        f"{base_url.rstrip('/')}/embeddings",
        data=json.dumps({"model": model, "input": texts}, ensure_ascii=False).encode(
            "utf-8"
        ),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    return [
        item["embedding"]
        for item in sorted(payload["data"], key=lambda item: item["index"])
    ]


def cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    return numerator / (left_norm * right_norm)


def main() -> None:
    args = parse_args()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    document_vectors = embed(
        args.base_url,
        args.model,
        [f"search_document: {text}" for _, text in DOCUMENTS],
        args.timeout,
    )
    query_vectors = embed(
        args.base_url,
        args.model,
        [f"search_query: {text}" for text, _, _ in QUERIES],
        args.timeout,
    )

    rows = []
    reciprocal_ranks = []
    for (query, expected, category), query_vector in zip(QUERIES, query_vectors):
        ranked = sorted(
            [
                {"document_id": doc_id, "score": cosine(query_vector, vector)}
                for (doc_id, _), vector in zip(DOCUMENTS, document_vectors)
            ],
            key=lambda item: item["score"],
            reverse=True,
        )
        rank = next(
            index
            for index, item in enumerate(ranked, 1)
            if item["document_id"] == expected
        )
        reciprocal_ranks.append(1 / rank)
        row = {
            "query": query,
            "category": category,
            "expected": expected,
            "rank": rank,
            "top1": ranked[0]["document_id"],
            "top1_score": ranked[0]["score"],
            "top3": [item["document_id"] for item in ranked[:3]],
        }
        rows.append(row)
        print(
            f"{category} | rank={rank} | expected={expected} | top1={row['top1']}",
            flush=True,
        )

    summary = {
        "model": args.model,
        "query_count": len(rows),
        "top1_accuracy": sum(row["rank"] == 1 for row in rows) / len(rows),
        "top3_recall": sum(row["rank"] <= 3 for row in rows) / len(rows),
        "mrr": sum(reciprocal_ranks) / len(reciprocal_ranks),
        "elapsed_seconds": time.perf_counter() - started,
        "warning": "6개 합성 문서의 smoke test이며 운영 성능 인증이 아님",
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUTPUT_DIR / f"lmstudio_embedding_{timestamp}.json"
    csv_path = OUTPUT_DIR / f"lmstudio_embedding_{timestamp}.csv"
    json_path.write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "query",
                "category",
                "expected",
                "rank",
                "top1",
                "top1_score",
                "top3",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(json_path, flush=True)
    print(csv_path, flush=True)


if __name__ == "__main__":
    main()
