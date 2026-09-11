"""공식 ZIP을 private 최소 열로 투영하고 학습 없이 고정 Resolver를 재평가한다."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from chemiguard119.incident_adaptation import (
    _cases_for_year,
    _evaluate_cases,
    load_incident_alias_records,
)
from chemiguard119.incident_source_intake import prepare_ulsan_resolver_source
from chemiguard119.resolver import load_resolver
from chemiguard119.utils import sha256_file


REPLAY_SCHEMA_VERSION = "ulsan-fixed-resolver-source-replay-v1"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_CSV_BYTES = 10 * 1024 * 1024
METRIC_GROUPS = ("all", "unseen_surface", "unseen_cas")
AUDIT_FIELDS = (
    "source_row_count",
    "valid_alias_record_count",
    "pre_ambiguity_filter_record_count",
    "invalid_year_row_count",
    "invalid_or_composite_cas_row_count",
    "ambiguous_surface_count",
    "year_counts",
)


def _private_bytes(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


def _private_json(path: Path, data: dict[str, Any]) -> None:
    _private_bytes(
        path,
        (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_official_zip(
    archive_path: Path, expected_sha256: str
) -> tuple[bytes, bytes, dict[str, Any]]:
    """멤버 경로를 파일 시스템에 풀지 않고, 크기가 제한된 단일 CSV만 읽는다."""
    with Path(archive_path).open("rb") as handle:
        archive_bytes = handle.read(MAX_ARCHIVE_BYTES + 1)
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise ValueError("ZIP 크기 제한을 초과했습니다.")
    archive_hash = hashlib.sha256(archive_bytes).hexdigest()
    if archive_hash != expected_sha256:
        raise ValueError("원본 ZIP SHA-256이 사전 고정값과 다릅니다.")
    with zipfile.ZipFile(
        io.BytesIO(archive_bytes), metadata_encoding="cp949"
    ) as archive:
        files = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(files) != 1:
            raise ValueError("ZIP에는 공식 CSV 파일이 정확히 한 개 있어야 합니다.")
        entry = files[0]
        member = PurePosixPath(entry.filename)
        if (
            member.is_absolute()
            or ".." in member.parts
            or "\\" in entry.filename
            or ":" in entry.filename
            or member.suffix.lower() != ".csv"
            or stat.S_ISLNK(entry.external_attr >> 16)
            or entry.flag_bits & 1
        ):
            raise ValueError("안전한 일반 CSV 멤버만 허용합니다.")
        if not 0 < entry.file_size <= MAX_CSV_BYTES:
            raise ValueError("CSV 크기 제한을 초과했거나 빈 파일입니다.")
        with archive.open(entry) as handle:
            source_bytes = handle.read(MAX_CSV_BYTES + 1)
        if len(source_bytes) != entry.file_size:
            raise ValueError("CSV 실제 크기와 ZIP metadata가 다릅니다.")
    return (
        archive_bytes,
        source_bytes,
        {
            "archive_sha256": archive_hash,
            "archive_bytes": len(archive_bytes),
            "member_name": entry.filename,
            "member_compressed_bytes": entry.compress_size,
            "source_csv_bytes": len(source_bytes),
            "source_csv_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "member_path_used_for_extraction": False,
            "source_is_user_provided_official_download": True,
            "redistribution_permission_verified": False,
        },
    )


def _compare_reference(
    actual: dict[str, Any], reference: dict[str, Any]
) -> list[dict[str, Any]]:
    differences = []
    for field in sorted(set(actual) | set(reference)):
        # JSON roundtrip으로 연도 dict의 정수/문자열 key를 같은 표현으로 비교한다.
        observed = json.loads(json.dumps(actual.get(field)))
        expected = reference.get(field)
        if observed != expected:
            differences.append(
                {"field": field, "observed": observed, "reference": expected}
            )
    return differences


def replay_official_source(
    archive_path: Path,
    model_path: Path,
    reference_report_path: Path,
    output_dir: Path,
    *,
    expected_archive_sha256: str,
    expected_model_sha256: str,
) -> dict[str, Any]:
    """원본 계보·시간 분할·집계만 공개 가능하게 반환한다. 정답/실패 원문은 private이다."""
    archive_bytes, source_bytes, archive_manifest = read_official_zip(
        archive_path, expected_archive_sha256
    )
    model_hash = sha256_file(model_path)
    if model_hash != expected_model_sha256:
        raise ValueError("Resolver SHA-256이 사전 고정값과 다릅니다.")
    model = load_resolver(model_path)
    training = model.get("training_metadata") or {}
    if (
        type(training.get("training_year_max")) is not int
        or training["training_year_max"] != 2019
    ):
        raise ValueError("2019년까지 학습한 고정 artifact만 이 재평가에 허용합니다.")
    reference = json.loads(reference_report_path.read_text(encoding="utf-8"))
    if reference.get("split_policy", {}).get("locked_test_year") != 2020:
        raise ValueError("과거 보고서의 잠금 연도가 2020년이 아닙니다.")
    output_dir = Path(output_dir).resolve()
    if any((parent / ".git").exists() for parent in (output_dir, *output_dir.parents)):
        raise ValueError(
            "원본·파생·상세 결과는 Git worktree 밖의 private 경로에 저장해야 합니다."
        )
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    _private_bytes(output_dir / "source.zip", archive_bytes)
    _private_json(output_dir / "archive_manifest.json", archive_manifest)
    derived = output_dir / "resolver-source.csv"
    manifest_path = output_dir / "resolver-source.manifest.json"
    # 원본의 주소·대응 열을 별도 영구 CSV로 복제하지 않는다. ZIP 사본은 보존한다.
    with tempfile.TemporaryDirectory(prefix=".intake-", dir=output_dir) as temporary:
        source_path = Path(temporary) / "official-source.csv"
        _private_bytes(source_path, source_bytes)
        intake = prepare_ulsan_resolver_source(source_path, derived, manifest_path)
    source = load_incident_alias_records(derived, manifest_path)
    records = source["records"]
    if any(not 2015 <= row["year"] <= 2020 for row in records):
        raise ValueError("사전 고정한 2015~2020 범위 밖의 유효 표현이 있습니다.")
    cases = _cases_for_year(records, 2020)
    past_records = [row for row in records if row["year"] <= 2019]
    if not cases or not past_records:
        raise ValueError("과거 이력과 2020 평가 사례가 모두 필요합니다.")
    past_pairs = {(row["normalized_text"], row["cas_number"]) for row in past_records}
    unseen = [
        case
        for case in cases
        if (case["normalized_query"], case["expected_cas"]) not in past_pairs
    ]
    evaluation = _evaluate_cases(model, cases, training_records=past_records)
    _private_json(output_dir / "locked_evaluation_private.json", evaluation)
    source_audit = {field: source["audit"][field] for field in AUDIT_FIELDS}
    reference_audit = {
        field: reference["source_audit"].get(field) for field in AUDIT_FIELDS
    }
    source_differences = _compare_reference(source_audit, reference_audit)
    metric_differences = {
        group: _compare_reference(
            evaluation[group], reference["locked_test"]["adapted"][group]
        )
        for group in METRIC_GROUPS
    }
    data_and_metrics_match = not source_differences and not any(
        metric_differences.values()
    )
    unsafe_count = round(evaluation["all"]["wrong_unique_resolution_rate"] * len(cases))
    result = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "REFERENCE_AGGREGATES_MATCH"
        if data_and_metrics_match
        else "REFERENCE_DIFFERENCE_REQUIRES_REVIEW",
        "implementation_state": "부분 구현 또는 개발용 데모",
        "task": "FIXED_MODEL_REPLAY_NO_TRAINING",
        "environment": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "provenance": {
            "archive_sha256": archive_manifest["archive_sha256"],
            "source_csv_sha256": archive_manifest["source_csv_sha256"],
            "derived_csv_sha256": intake["derived"]["sha256"],
            "intake_manifest_sha256": sha256_file(manifest_path),
            "archive_manifest_sha256": sha256_file(
                output_dir / "archive_manifest.json"
            ),
            "model_sha256": model_hash,
            "model_schema_version": model.get("schema_version"),
            "model_training_year_max": training["training_year_max"],
            "model_recorded_training_source_sha256": training.get(
                "source_audit", {}
            ).get("source_sha256"),
            "reference_report_sha256": sha256_file(reference_report_path),
            "reference_model_sha256": reference["artifacts"]["final"][
                "artifact_sha256"
            ],
            "same_model_bytes_as_reference": model_hash
            == reference["artifacts"]["final"]["artifact_sha256"],
            "reference_processed_source_sha256": reference["source_audit"][
                "source_sha256"
            ],
            "canonical_test_cases_sha256": _fingerprint(cases),
            "canonical_unseen_cases_sha256": _fingerprint(unseen),
            "canonical_past_records_sha256": _fingerprint(past_records),
        },
        "source_audit": source_audit,
        "split_audit": {
            "past_year_max": 2019,
            "test_year": 2020,
            "past_record_count": len(past_records),
            "test_case_count": len(cases),
            "unseen_surface_case_count": len(unseen),
            "repeated_past_pair_case_count": len(cases) - len(unseen),
            "duplicate_test_pair_count": len(cases)
            - len({(case["normalized_query"], case["expected_cas"]) for case in cases}),
            "is_new_secret_test": False,
            "training_or_tuning_performed": False,
            "source_record_recurrence_allowed": True,
            "ambiguity_filter": "기존 보고서 재현을 위해 전체 기간의 다중 CAS 공유 표현을 평가에서 제외; 학습 없음",
        },
        "metrics": {group: evaluation[group] for group in METRIC_GROUPS},
        "wrong_unique_exact_candidate_count": unsafe_count,
        "reference_comparison": {
            "source_differences": source_differences,
            "metric_differences": metric_differences,
        },
        "timing_ms": evaluation["latency_ms"],
        "timing_scope": "고정 artifact 메모리 로드 이후 후보 검색, STT/HTTP 제외, 속도 개선 비교 아님",
        "adoption": {
            "new_model_adopted": False,
            "runtime_change_allowed": False,
            "confirmation_or_rule_execution_performed": False,
        },
        "source_permission": {
            "internal_user_provided_data": True,
            "redistribution_permission_verified": False,
        },
        "claim_limit": "공식 사고표의 유효 단일 CAS·비모호 표현에 대한 고정 후보 검색 평가다. 현장 안전성·무전 정확도·독립 qrel 검증·새 모델 성능 개선이 아니다.",
    }
    _private_json(output_dir / "summary.json", result)
    return result
