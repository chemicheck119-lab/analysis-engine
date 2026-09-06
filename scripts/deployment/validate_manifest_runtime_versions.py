#!/usr/bin/env python3
"""Cloud Build 전에 runtime manifest와 고정 serving 의존성을 비교한다."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


RUNTIME_PACKAGE_FIELDS = {
    "numpy": "numpy",
    "scikit-learn": "scikit_learn",
    "joblib": "joblib",
}


def _normalize_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip().lower())


def _pinned_versions(requirements_path: Path) -> dict[str, str]:
    pinned: dict[str, str] = {}
    pattern = re.compile(r"^\s*([A-Za-z0-9_.-]+)==([^\s;]+)")
    for line in requirements_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            pinned[_normalize_package_name(match.group(1))] = match.group(2)
    return pinned


def _docker_python_version(dockerfile_path: Path) -> str | None:
    pattern = re.compile(r"^\s*FROM\s+python:([0-9]+(?:\.[0-9]+){1,2})(?:-|\s|@)")
    for line in dockerfile_path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def validate_runtime_versions(
    manifest_path: Path,
    requirements_path: Path,
    dockerfile_path: Path,
    expected_git_commit: str,
) -> dict[str, object]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime_versions = manifest.get("runtime_versions")
    if not isinstance(runtime_versions, dict):
        raise ValueError("runtime manifest에 runtime_versions가 없습니다.")

    pinned = _pinned_versions(requirements_path)
    mismatches: dict[str, dict[str, str | None]] = {}
    manifest_git_commit = manifest.get("git_commit")
    if manifest_git_commit != expected_git_commit:
        mismatches["git_commit"] = {
            "manifest": (
                str(manifest_git_commit) if manifest_git_commit is not None else None
            ),
            "serving": expected_git_commit,
        }
    for package_name, manifest_field in RUNTIME_PACKAGE_FIELDS.items():
        expected = pinned.get(_normalize_package_name(package_name))
        actual = runtime_versions.get(manifest_field)
        if expected is None or actual != expected:
            mismatches[manifest_field] = {
                "manifest": str(actual) if actual is not None else None,
                "serving": expected,
            }

    serving_python = _docker_python_version(dockerfile_path)
    manifest_python = runtime_versions.get("python")
    if serving_python is None or manifest_python != serving_python:
        mismatches["python"] = {
            "manifest": str(manifest_python) if manifest_python is not None else None,
            "serving": serving_python,
        }

    return {
        "status": "VERIFIED" if not mismatches else "MISMATCH",
        "manifest": str(manifest_path),
        "requirements": str(requirements_path),
        "dockerfile": str(dockerfile_path),
        "mismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--requirements", type=Path, required=True)
    parser.add_argument("--dockerfile", type=Path, required=True)
    parser.add_argument(
        "--expected-git-commit",
        required=True,
    )
    args = parser.parse_args()

    if re.fullmatch(r"[0-9a-f]{40}", args.expected_git_commit) is None:
        parser.error("--expected-git-commit은 40자리 소문자 Git SHA여야 합니다.")

    result = validate_runtime_versions(
        args.manifest,
        args.requirements,
        args.dockerfile,
        args.expected_git_commit,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
