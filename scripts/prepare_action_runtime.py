"""기존 잠금 artifact를 별도 로컬 평가 묶음으로 복사한다. 원본/운영 manifest를 수정하지 않는다."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from chemiguard119.release import create_runtime_manifest
from chemiguard119.utils import write_json

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "chemiguard119.sqlite": "ed81fe9aac45a38880920d967ce8f3954acabfedf7b2f6ea59464552e7958b91",
    "resolver.joblib": "2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff",
    "retriever.joblib": "2e93e066648890400b26f56b532d6ed608b828203f8668e4af3cf63de1bc544b",
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="신뢰된 기존 artifact의 로컬 평가 사본 준비"
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError(
            "새 출력 디렉터리가 필요합니다. 기존 묶음은 덮어쓰지 않습니다."
        )
    for name, digest in EXPECTED.items():
        if hashlib.sha256((args.source / name).read_bytes()).hexdigest() != digest:
            raise ValueError(
                f"잠금 artifact hash 불일치: {name}. 임의로 승인하거나 로드하지 않습니다."
            )
    args.output.mkdir(parents=True)
    for name in EXPECTED:
        shutil.copy2(args.source / name, args.output / name)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    result = create_runtime_manifest(
        db_path=args.output / "chemiguard119.sqlite",
        resolver_model_path=args.output / "resolver.joblib",
        retriever_model_path=args.output / "retriever.joblib",
        config_dir=ROOT / "config",
        git_commit=commit,
    )
    write_json(
        args.output / "local_evaluation_provenance.json",
        {
            "scope": "LOCAL_DEVELOPMENT_ONLY_NOT_DEPLOYMENT_APPROVAL",
            "source_artifact_sha256": EXPECTED,
            "git_head": commit,
            "working_tree_modified": bool(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=ROOT, text=True
                ).strip()
            ),
            "manifest_sha256": result["manifest_sha256"],
            "expert_reviewed": False,
            "note": "기존 모델/DB bytes 유지. 현재 정책의 미검수·배포 부적격 판정도 manifest에 보존. 실제 코드 hash는 평가 보고서 참조.",
        },
    )
    print(
        json.dumps(
            {
                "artifact_dir": str(args.output),
                "manifest_sha256": result["manifest_sha256"],
                "scope": "LOCAL_DEVELOPMENT_ONLY",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
