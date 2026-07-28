"""평가 데이터의 검수 범위와 provenance를 검증하는 공통 계약.

내부 회귀 데이터는 개발 중 버그 재발을 찾는 데 사용할 수 있지만, 검수 완료
성능으로 승격할 수 없다. 이 모듈은 평가 알고리즘과 독립적으로 데이터 파일의
검수 상태, provenance, 중복과 split 누수를 검사한다.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from chemiguard119.utils import sha256_file


EVALUATION_CONTRACT_SCHEMA_VERSION = "chemicheck119-evaluation-contract-v1"
INTERNAL_CLAIM_SCOPE = "INTERNAL_REGRESSION_ONLY"

REQUIRED_PROVENANCE_FIELDS = (
    "case_id",
    "review_status",
    "source_type",
    "source_reference",
    "labeler_id",
    "reviewer_id",
    "split",
    "duplicate_group",
)
REVIEWED_STATUSES = frozenset(
    {
        "APPROVED",
        "DOUBLE_REVIEWED_NON_EXPERT",
        "EXPERT_REVIEWED",
    }
)
EXPERT_REVIEWED_STATUS = "EXPERT_REVIEWED"
LOCKED_TEST_SPLIT = "locked_test"


class EvaluationProfile(str, Enum):
    """평가 결과로 주장할 수 있는 검수 범위."""

    INTERNAL_REGRESSION = "INTERNAL_REGRESSION"
    COMPETITION_REVIEWED = "COMPETITION_REVIEWED"
    PILOT_REVIEWED = "PILOT_REVIEWED"


class EvaluationContractError(ValueError):
    """평가 데이터 계약 또는 reviewed gate가 실패한 경우."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = dict(report or {})


def _coerce_profile(profile: EvaluationProfile | str) -> EvaluationProfile:
    if isinstance(profile, EvaluationProfile):
        return profile
    normalized = str(profile).strip().upper().replace("-", "_")
    try:
        return EvaluationProfile(normalized)
    except ValueError as error:
        allowed = ", ".join(item.value for item in EvaluationProfile)
        raise EvaluationContractError(
            f"지원하지 않는 평가 profile={profile!r}; 허용값: {allowed}"
        ) from error


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as error:
                raise EvaluationContractError(
                    f"{path}:{line_number} JSON을 읽을 수 없습니다."
                ) from error
            if not isinstance(payload, dict):
                raise EvaluationContractError(
                    f"{path}:{line_number} JSON 객체가 필요합니다."
                )
            rows.append(payload)
    return rows


def load_evaluation_rows(dataset_path: Path) -> list[dict[str, Any]]:
    """CSV·JSONL·JSON 평가 파일을 원본 순서로 읽는다."""

    path = Path(dataset_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if suffix == ".jsonl":
        return _read_jsonl(path)
    if suffix == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise EvaluationContractError(f"{path} JSON을 읽을 수 없습니다.") from error
        if isinstance(payload, dict):
            payload = payload.get("rows")
        if not isinstance(payload, list) or any(
            not isinstance(row, dict) for row in payload
        ):
            raise EvaluationContractError(
                f"{path}에는 JSON 객체 배열 또는 rows 배열이 필요합니다."
            )
        return [dict(row) for row in payload]
    raise EvaluationContractError(
        f"지원하지 않는 평가 파일 형식입니다: {path.suffix or '<없음>'}"
    )


def _field(row: Mapping[str, Any], name: str) -> Any:
    """최상위 필드를 우선하고 JSONL의 metadata도 지원한다."""

    if name in row:
        return row.get(name)
    metadata = row.get("metadata")
    if isinstance(metadata, Mapping):
        return metadata.get(name)
    return None


def _text(row: Mapping[str, Any], name: str) -> str:
    value = _field(row, name)
    return str(value).strip() if value is not None else ""


def _explicit_expert_reviewed(row: Mapping[str, Any]) -> bool:
    if _text(row, "review_status").upper() == EXPERT_REVIEWED_STATUS:
        return True
    value = _field(row, "expert_reviewed")
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _issue(
    code: str,
    message: str,
    *,
    case_ids: list[str] | None = None,
    groups: list[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if case_ids:
        result["case_ids"] = sorted(set(case_ids))
    if groups:
        result["groups"] = sorted(set(groups))
    return result


def evaluate_dataset_contract(
    rows: list[Mapping[str, Any]],
    profile: EvaluationProfile | str,
    dataset_path: Path,
) -> dict[str, Any]:
    """이미 읽은 평가행의 reviewed 사용 가능 범위와 데이터 누수를 감사한다.

    ``INTERNAL_REGRESSION``은 DRAFT와 provenance 누락을 경고로 남기되 개발
    회귀 실행을 허용한다. 다른 두 profile은 검수 완료 상태, 필수 provenance,
    서로 다른 라벨러·검수자와 locked-test split을 모두 요구한다.
    """

    path = Path(dataset_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    selected_profile = _coerce_profile(profile)
    rows = [dict(row) for row in rows]
    reviewed_profile = selected_profile is not EvaluationProfile.INTERNAL_REGRESSION

    case_ids: list[str] = []
    row_labels: list[str] = []
    missing_provenance: dict[str, list[str]] = {}
    draft_case_ids: list[str] = []
    unreviewed_case_ids: list[str] = []
    same_reviewer_case_ids: list[str] = []
    non_locked_case_ids: list[str] = []
    expert_flags: list[bool] = []
    duplicate_group_splits: dict[str, set[str]] = defaultdict(set)

    for index, row in enumerate(rows, 1):
        case_id = _text(row, "case_id")
        row_label = case_id or f"<row:{index}>"
        case_ids.append(case_id)
        row_labels.append(row_label)

        missing = [
            field for field in REQUIRED_PROVENANCE_FIELDS if not _text(row, field)
        ]
        if missing:
            missing_provenance[row_label] = missing

        review_status = _text(row, "review_status").upper()
        if review_status.startswith("DRAFT") or not review_status:
            draft_case_ids.append(row_label)
        if review_status not in REVIEWED_STATUSES:
            unreviewed_case_ids.append(row_label)

        labeler_id = _text(row, "labeler_id")
        reviewer_id = _text(row, "reviewer_id")
        if labeler_id and reviewer_id and labeler_id == reviewer_id:
            same_reviewer_case_ids.append(row_label)

        split = _text(row, "split").lower()
        if split != LOCKED_TEST_SPLIT:
            non_locked_case_ids.append(row_label)
        duplicate_group = _text(row, "duplicate_group")
        if duplicate_group and split:
            duplicate_group_splits[duplicate_group].add(split)
        expert_flags.append(_explicit_expert_reviewed(row))

    duplicate_case_ids = sorted(
        case_id for case_id, count in Counter(case_ids).items() if case_id and count > 1
    )
    split_leakage_groups = sorted(
        group for group, splits in duplicate_group_splits.items() if len(splits) > 1
    )
    review_status_counts = Counter(
        _text(row, "review_status").upper() or "<MISSING>" for row in rows
    )
    split_counts = Counter(_text(row, "split").lower() or "<MISSING>" for row in rows)

    blockers: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if not rows:
        blockers.append(_issue("EMPTY_DATASET", "평가 데이터에 유효한 행이 없습니다."))
    if duplicate_case_ids:
        blockers.append(
            _issue(
                "DUPLICATE_CASE_ID",
                "중복된 case_id가 있습니다.",
                case_ids=duplicate_case_ids,
            )
        )
    if split_leakage_groups:
        blockers.append(
            _issue(
                "DUPLICATE_GROUP_SPLIT_LEAKAGE",
                "같은 duplicate_group이 둘 이상의 split에 있습니다.",
                groups=split_leakage_groups,
            )
        )

    provenance_issue = _issue(
        "MISSING_PROVENANCE",
        "필수 provenance가 누락된 평가행이 있습니다.",
        case_ids=list(missing_provenance),
    )
    draft_issue = _issue(
        "DRAFT_ROWS_NOT_ALLOWED",
        "DRAFT 또는 미검수 평가행은 reviewed profile에 사용할 수 없습니다.",
        case_ids=draft_case_ids,
    )
    unreviewed_issue = _issue(
        "UNREVIEWED_STATUS",
        "reviewed profile에서 허용하지 않는 review_status가 있습니다.",
        case_ids=unreviewed_case_ids,
    )
    same_reviewer_issue = _issue(
        "LABELER_REVIEWER_NOT_INDEPENDENT",
        "labeler_id와 reviewer_id는 서로 달라야 합니다.",
        case_ids=same_reviewer_case_ids,
    )
    non_locked_issue = _issue(
        "NON_LOCKED_TEST_ROWS",
        "reviewed 평가에는 locked_test 행만 사용할 수 있습니다.",
        case_ids=non_locked_case_ids,
    )

    conditional_issues = [
        (bool(missing_provenance), provenance_issue),
        (bool(draft_case_ids), draft_issue),
        (bool(unreviewed_case_ids), unreviewed_issue),
        (bool(same_reviewer_case_ids), same_reviewer_issue),
        (bool(non_locked_case_ids), non_locked_issue),
    ]
    for present, issue in conditional_issues:
        if not present:
            continue
        (blockers if reviewed_profile else warnings).append(issue)

    eligible_case_count = 0
    for row in rows:
        review_status = _text(row, "review_status").upper()
        provenance_complete = all(
            _text(row, field) for field in REQUIRED_PROVENANCE_FIELDS
        )
        independent_review = bool(
            _text(row, "labeler_id")
            and _text(row, "reviewer_id")
            and _text(row, "labeler_id") != _text(row, "reviewer_id")
        )
        locked = _text(row, "split").lower() == LOCKED_TEST_SPLIT
        if selected_profile is EvaluationProfile.INTERNAL_REGRESSION or (
            review_status in REVIEWED_STATUSES
            and provenance_complete
            and independent_review
            and locked
        ):
            eligible_case_count += 1

    passed = not blockers
    effective_claim_scope = (
        selected_profile.value if passed and reviewed_profile else INTERNAL_CLAIM_SCOPE
    )
    return {
        "schema_version": EVALUATION_CONTRACT_SCHEMA_VERSION,
        "dataset": str(path),
        "dataset_sha256": sha256_file(path),
        "profile": selected_profile.value,
        "passed": passed,
        "claim_scope": effective_claim_scope,
        "case_count": len(rows),
        "eligible_case_count": eligible_case_count,
        "expert_reviewed": (
            bool(rows)
            and all(expert_flags)
            and all(
                _text(row, "review_status").upper() in REVIEWED_STATUSES for row in rows
            )
        ),
        "review_status_counts": dict(sorted(review_status_counts.items())),
        "split_counts": dict(sorted(split_counts.items())),
        "missing_provenance_count": len(missing_provenance),
        "missing_provenance": {
            key: sorted(value) for key, value in sorted(missing_provenance.items())
        },
        "duplicate_case_ids": duplicate_case_ids,
        "split_leakage_groups": split_leakage_groups,
        "blockers": blockers,
        "warnings": warnings,
    }


def audit_evaluation_dataset(
    dataset_path: Path,
    profile: EvaluationProfile | str = EvaluationProfile.INTERNAL_REGRESSION,
) -> dict[str, Any]:
    """평가 파일을 읽고 공통 데이터 계약을 감사한다."""

    path = Path(dataset_path)
    return evaluate_dataset_contract(
        load_evaluation_rows(path),
        profile,
        path,
    )


def require_evaluation_dataset(
    dataset_path: Path,
    profile: EvaluationProfile | str,
) -> dict[str, Any]:
    """계약을 통과한 보고서를 반환하고, 실패하면 구조화 보고서와 함께 중단한다."""

    report = audit_evaluation_dataset(dataset_path, profile)
    if not report["passed"]:
        codes = ", ".join(item["code"] for item in report["blockers"])
        raise EvaluationContractError(
            f"평가 데이터 gate 실패: {codes}",
            report=report,
        )
    return report


__all__ = [
    "EVALUATION_CONTRACT_SCHEMA_VERSION",
    "INTERNAL_CLAIM_SCOPE",
    "LOCKED_TEST_SPLIT",
    "REQUIRED_PROVENANCE_FIELDS",
    "REVIEWED_STATUSES",
    "EvaluationContractError",
    "EvaluationProfile",
    "audit_evaluation_dataset",
    "evaluate_dataset_contract",
    "load_evaluation_rows",
    "require_evaluation_dataset",
]
