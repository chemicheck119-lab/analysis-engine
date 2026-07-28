#!/usr/bin/env python3
"""동일 artifact·장비에서 온라인 검색 경로의 상대 지연시간을 측정한다."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chemiguard119.resolver import load_resolver, resolve_substance
from chemiguard119.retrieval import load_retriever, search_evidence


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _measure(
    function: Callable[[], Any],
    *,
    iterations: int,
    warmups: int,
) -> dict[str, float]:
    for _ in range(warmups):
        function()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        function()
        samples.append((time.perf_counter() - started) * 1_000)
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, int(len(ordered) * 0.95))
    return {
        "mean_ms": round(statistics.fmean(samples), 3),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    resolver_path = args.resolver_model.resolve()
    retriever_path = args.retriever_model.resolve()
    db_path = args.db.resolve()
    resolver = load_resolver(resolver_path)
    retriever = load_retriever(retriever_path)

    exact_iterations = max(args.iterations, 1)
    model_iterations = max(args.model_iterations, 1)
    warmups = max(args.warmups, 0)
    measurements = {
        "resolver_exact_alias": _measure(
            lambda: resolve_substance("염산", resolver),
            iterations=exact_iterations,
            warmups=warmups,
        ),
        "resolver_exact_cas": _measure(
            lambda: resolve_substance("7647-01-0", resolver),
            iterations=exact_iterations,
            warmups=warmups,
        ),
        "resolver_fuzzy": _measure(
            lambda: resolve_substance("차아염소산나트륨 누출", resolver),
            iterations=model_iterations,
            warmups=warmups,
        ),
        "retriever_same_cas": _measure(
            lambda: search_evidence(
                "염산 누출 대응",
                db_path,
                retriever,
                cas_hint="7647-01-0",
                top_k=5,
            ),
            iterations=model_iterations,
            warmups=warmups,
        ),
        "retriever_text": _measure(
            lambda: search_evidence(
                "염산 누출 대응",
                db_path,
                retriever,
                top_k=5,
            ),
            iterations=model_iterations,
            warmups=warmups,
        ),
    }
    return {
        "schema_version": "runtime-benchmark-v1",
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "label": args.label,
        "interpretation": (
            "동일 장비·동일 artifact의 상대 비교용이며 운영 SLO나 현장 정확도 수치가 아님"
        ),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "iterations": {
            "exact": exact_iterations,
            "model": model_iterations,
            "warmups": warmups,
        },
        "artifacts": {
            "database_sha256": _sha256(db_path),
            "resolver_sha256": _sha256(resolver_path),
            "retriever_sha256": _sha256(retriever_path),
            "resolver_alias_count": len(resolver["rows"]),
            "retriever_document_count": len(retriever["rows"]),
        },
        "measurements": measurements,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("artifacts/chemiguard119.sqlite"),
    )
    parser.add_argument(
        "--resolver-model",
        type=Path,
        default=Path("artifacts/resolver.joblib"),
    )
    parser.add_argument(
        "--retriever-model",
        type=Path,
        default=Path("artifacts/retriever.joblib"),
    )
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--model-iterations", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--label", default="local")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = benchmark(args)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
