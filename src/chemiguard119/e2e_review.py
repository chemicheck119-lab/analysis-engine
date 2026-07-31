"""E2E 공모전 평가팩을 위한 후보 생성·독립 검수·preflight 도구.

기계적으로 만든 시나리오를 곧바로 정답 데이터로 승격하지 않는다. 후보 풀과
모델 관찰값, 라벨러 판단, 독립 검수자 판단을 분리하고 두 사람의 라벨이 완전히
일치할 때만 ``DOUBLE_REVIEWED_NON_EXPERT`` 평가 JSONL을 만든다.
"""

from __future__ import annotations

import csv
import json
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Mapping

from chemiguard119.api_models import contains_unconfirmed_risk_output
from chemiguard119.e2e_evaluation import (
    SUPPORTED_CAPABILITIES,
    summarize_pipeline_output,
)
from chemiguard119.evaluation_contract import (
    EvaluationProfile,
    evaluate_dataset_contract,
    load_evaluation_rows,
)
from chemiguard119.paths import CONFIG_DIR
from chemiguard119.pipeline import analyze_incident, validate_pipeline_output
from chemiguard119.resolver import load_resolver
from chemiguard119.retrieval import load_retriever
from chemiguard119.rules import PUBLIC_SOURCE_PILOT_POLICY
from chemiguard119.utils import sha256_file, valid_cas_checksum, write_json


CANDIDATE_SCHEMA_VERSION = "chemicheck119-e2e-review-candidate-v1"
REVIEW_SHEET_SCHEMA_VERSION = "chemicheck119-e2e-review-sheet-v1"
REVIEW_MERGE_SCHEMA_VERSION = "chemicheck119-e2e-review-merge-v1"
PREFLIGHT_SCHEMA_VERSION = "chemicheck119-e2e-candidate-preflight-v1"
REVIEW_ROLES = frozenset({"LABELER", "REVIEWER"})
ACTOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]{2,64}$")

CAS_DISPLAY_NAMES = {
    "108-88-3": "톨루엔",
    "64-17-5": "에탄올",
    "67-64-1": "아세톤",
    "7440-23-5": "금속 나트륨",
    "7647-01-0": "염산",
    "7681-52-9": "차아염소산나트륨",
}

REVIEW_COLUMNS = (
    "sheet_schema_version",
    "case_id",
    "actor_role",
    "actor_id",
    "source_type",
    "source_reference",
    "scenario_origin",
    "data_use_scope",
    "duplicate_group",
    "raw_text",
    "confirmed_incident_cas",
    "confirmed_facility_cas",
    "capabilities_json",
    "review_decision",
    "status",
    "rule_executed",
    "rule_status",
    "missing_confirmations_json",
    "candidate_count",
    "candidate_roles_json",
    "evidence_bases_json",
    "output_validation_status",
    "risk_level",
    "severity",
    "expect_abstention",
    "review_notes",
)

LABEL_FIELDS = (
    "status",
    "rule_executed",
    "rule_status",
    "missing_confirmations",
    "candidate_count",
    "candidate_roles",
    "evidence_bases",
    "output_validation_status",
    "risk_level",
    "severity",
    "expect_abstention",
)

Analyzer = Callable[..., dict[str, Any]]


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 객체가 필요합니다: {path}")
    return payload


def _artifact_identity(path: Path) -> dict[str, Any]:
    """절대 경로를 노출하지 않고 재현에 필요한 artifact 식별자만 남긴다."""

    artifact_path = Path(path)
    if not artifact_path.is_file():
        return {
            "file_name": artifact_path.name,
            "exists": False,
            "sha256": None,
            "size_bytes": None,
        }
    return {
        "file_name": artifact_path.name,
        "exists": True,
        "sha256": sha256_file(artifact_path),
        "size_bytes": artifact_path.stat().st_size,
    }


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(_json_compact(dict(row)) + "\n" for row in rows),
        encoding="utf-8",
    )


def _candidate(
    *,
    case_id: str,
    source_type: str,
    source_reference: str,
    scenario_origin: str,
    duplicate_group: str,
    capabilities: list[str],
    raw_text: str,
    confirmed_incident_cas: str | None,
    confirmed_facility_cas: str | None,
) -> dict[str, Any]:
    return {
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "case_id": case_id,
        "source_type": source_type,
        "source_reference": source_reference,
        "scenario_origin": scenario_origin,
        "data_use_scope": "COMPETITION_REVIEW_CANDIDATE_ONLY",
        "duplicate_group": duplicate_group,
        "capabilities": capabilities,
        "input": {
            "raw_text": raw_text,
            "confirmed_incident_cas": confirmed_incident_cas,
            "confirmed_facility_cas": confirmed_facility_cas,
        },
    }


def validate_candidate_rows(rows: list[Mapping[str, Any]]) -> None:
    """검수 전 후보 풀에 정답 누출이나 provenance 누락이 없는지 검사한다."""

    if not rows:
        raise ValueError("E2E 후보 풀이 비어 있습니다.")
    seen: set[str] = set()
    for index, raw_row in enumerate(rows, 1):
        row = dict(raw_row)
        case_id = str(row.get("case_id") or f"<row:{index}>").strip()
        if not case_id or case_id in seen:
            raise ValueError(f"비어 있거나 중복된 case_id={case_id!r}")
        seen.add(case_id)
        if row.get("candidate_schema_version") != CANDIDATE_SCHEMA_VERSION:
            raise ValueError(f"{case_id}: 지원하지 않는 candidate schema")
        if "expected" in row:
            raise ValueError(
                f"{case_id}: 독립 검수 후보에는 expected를 포함할 수 없습니다."
            )
        for field in (
            "source_type",
            "source_reference",
            "scenario_origin",
            "data_use_scope",
            "duplicate_group",
        ):
            if not isinstance(row.get(field), str) or not str(row[field]).strip():
                raise ValueError(f"{case_id}: {field}가 필요합니다.")
        capabilities = row.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities:
            raise ValueError(f"{case_id}: capabilities가 필요합니다.")
        unknown = {
            str(value)
            for value in capabilities
            if not isinstance(value, str) or value not in SUPPORTED_CAPABILITIES
        }
        if unknown:
            raise ValueError(f"{case_id}: 지원하지 않는 capabilities={sorted(unknown)}")
        input_payload = row.get("input")
        if not isinstance(input_payload, Mapping):
            raise ValueError(f"{case_id}: input 객체가 필요합니다.")
        raw_text = input_payload.get("raw_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise ValueError(f"{case_id}: input.raw_text가 필요합니다.")
        for field in ("confirmed_incident_cas", "confirmed_facility_cas"):
            value = input_payload.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{case_id}: input.{field}는 문자열 또는 null입니다.")


def load_candidate_rows(candidate_path: Path) -> list[dict[str, Any]]:
    rows = load_evaluation_rows(Path(candidate_path))
    validate_candidate_rows(rows)
    return rows


def generate_review_candidate_pool(
    pair_snapshot_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """검증된 15쌍의 상태 전이 45건과 hard case 5건을 만든다.

    생성 결과는 정답 데이터가 아니라 독립 검수를 기다리는 후보 풀이다.
    """

    pair_snapshot_path = Path(pair_snapshot_path)
    snapshot = _read_json_object(pair_snapshot_path)
    pairs = snapshot.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("verified pair snapshot에 pairs 배열이 필요합니다.")
    if snapshot.get("evaluated_pair_count") != len(pairs):
        raise ValueError("verified pair snapshot의 pair 수가 일치하지 않습니다.")

    rows: list[dict[str, Any]] = []
    for index, pair in enumerate(pairs, 1):
        if not isinstance(pair, Mapping):
            raise ValueError(f"pairs[{index}]는 객체여야 합니다.")
        cas_a = str(pair.get("cas_a") or "")
        cas_b = str(pair.get("cas_b") or "")
        name_a = CAS_DISPLAY_NAMES.get(cas_a)
        name_b = CAS_DISPLAY_NAMES.get(cas_b)
        if not name_a or not name_b:
            raise ValueError(
                f"한글 표시명이 없는 검증 CAS 조합입니다: {cas_a}, {cas_b}"
            )
        evidence_urls = pair.get("evidence_urls")
        if not isinstance(evidence_urls, list) or not evidence_urls:
            raise ValueError(f"{cas_a}+{cas_b}: evidence_urls가 필요합니다.")
        source_reference = " | ".join(str(url) for url in evidence_urls)
        duplicate_group = f"PAIR-{cas_a}-{cas_b}"
        base_id = f"E2E-CAND-PAIR-{index:02d}"
        shared = {
            "source_type": "PUBLIC_RULE_MECHANICAL_SCENARIO",
            "source_reference": source_reference,
            "scenario_origin": "MECHANICAL_PUBLIC_SOURCE_RECONSTRUCTION",
            "duplicate_group": duplicate_group,
        }
        rows.extend(
            [
                _candidate(
                    case_id=f"{base_id}-NONE",
                    capabilities=[
                        "PARSER_CANDIDATE",
                        "CONFIRMATION_GATE",
                        "EVIDENCE_CAS_LOCK",
                    ],
                    raw_text=(
                        f"{name_a} 저장탱크에서 누출 중이며 인접 보관구역에 "
                        f"{name_b}이 있습니다."
                    ),
                    confirmed_incident_cas=None,
                    confirmed_facility_cas=None,
                    **shared,
                ),
                _candidate(
                    case_id=f"{base_id}-INCIDENT",
                    capabilities=["CONFIRMATION_GATE", "EVIDENCE_CAS_LOCK"],
                    raw_text=f"{name_a} 누출 신고, 현장 창고에 {name_b} 보관 표시가 있습니다.",
                    confirmed_incident_cas=cas_a,
                    confirmed_facility_cas=None,
                    **shared,
                ),
                _candidate(
                    case_id=f"{base_id}-BOTH",
                    capabilities=[
                        "CONFIRMATION_GATE",
                        "DETERMINISTIC_CONFLICT_RULE",
                        "EVIDENCE_CAS_LOCK",
                    ],
                    raw_text=(
                        f"사고 물질 {name_a} 누출을 확인했고, 시설 창고에 보관된 "
                        f"{name_b}도 확인했습니다."
                    ),
                    confirmed_incident_cas=cas_a,
                    confirmed_facility_cas=cas_b,
                    **shared,
                ),
            ]
        )

    hard_source = "pipeline safety contract; https://cameochemicals.noaa.gov/"
    rows.extend(
        [
            _candidate(
                case_id="E2E-CAND-HARD-01-FACILITY-ONLY",
                source_type="MECHANICAL_SAFETY_SCENARIO",
                source_reference=hard_source,
                scenario_origin="MECHANICAL_CONTRACT_RECONSTRUCTION",
                duplicate_group="HARD-FACILITY-ONLY-01",
                capabilities=["CONFIRMATION_GATE", "EVIDENCE_CAS_LOCK"],
                raw_text="차아염소산나트륨 용기 주변에서 누출 흔적, 염산 저장고 확인",
                confirmed_incident_cas=None,
                confirmed_facility_cas="7647-01-0",
            ),
            _candidate(
                case_id="E2E-CAND-HARD-02-INVALID-CAS",
                source_type="MECHANICAL_SAFETY_SCENARIO",
                source_reference="CAS checksum input contract",
                scenario_origin="MECHANICAL_CONTRACT_RECONSTRUCTION",
                duplicate_group="HARD-INVALID-CAS-02",
                capabilities=["INVALID_INPUT_REJECTION"],
                raw_text="에탄올 저장탱크 누출 신고",
                confirmed_incident_cas="64-17-6",
                confirmed_facility_cas=None,
            ),
            _candidate(
                case_id="E2E-CAND-HARD-03-AMBIGUOUS-PRODUCT",
                source_type="MECHANICAL_SAFETY_SCENARIO",
                source_reference="resolver ambiguity safety contract",
                scenario_origin="MECHANICAL_CONTRACT_RECONSTRUCTION",
                duplicate_group="HARD-AMBIGUOUS-PRODUCT-03",
                capabilities=["AMBIGUITY_ABSTENTION", "CONFIRMATION_GATE"],
                raw_text="세척제 탱크에서 누출 중이며 알코올 냄새가 난다는 신고",
                confirmed_incident_cas=None,
                confirmed_facility_cas=None,
            ),
            _candidate(
                case_id="E2E-CAND-HARD-04-EMBEDDED-ALIAS",
                source_type="MECHANICAL_SAFETY_SCENARIO",
                source_reference="resolver Unicode boundary safety contract",
                scenario_origin="MECHANICAL_CONTRACT_RECONSTRUCTION",
                duplicate_group="HARD-EMBEDDED-ALIAS-04",
                capabilities=["EMBEDDED_ALIAS_REJECTION", "CONFIRMATION_GATE"],
                raw_text="에탄올성 세정제가 바닥으로 누출됨",
                confirmed_incident_cas=None,
                confirmed_facility_cas=None,
            ),
            _candidate(
                case_id="E2E-CAND-HARD-05-UNSUPPORTED-PAIR",
                source_type="MECHANICAL_SAFETY_SCENARIO",
                source_reference=hard_source,
                scenario_origin="MECHANICAL_CONTRACT_RECONSTRUCTION",
                duplicate_group="HARD-UNSUPPORTED-PAIR-05",
                capabilities=[
                    "UNSUPPORTED_PAIR_ABSTENTION",
                    "DETERMINISTIC_CONFLICT_RULE",
                ],
                raw_text="황산 누출 현장에 질산 저장용기가 함께 있음",
                confirmed_incident_cas="7664-93-9",
                confirmed_facility_cas="7697-37-2",
            ),
        ]
    )
    validate_candidate_rows(rows)
    expected_count = len(pairs) * 3 + 5
    if len(rows) != expected_count:
        raise AssertionError(
            f"후보 수 불일치: expected={expected_count}, actual={len(rows)}"
        )
    if output_path is not None:
        _write_jsonl(Path(output_path), rows)
    return {
        "status": "COMPLETED",
        "action": "GENERATE_CANDIDATE_POOL",
        "candidate_schema_version": CANDIDATE_SCHEMA_VERSION,
        "candidate_count": len(rows),
        "verified_pair_count": len(pairs),
        "mechanical_pair_state_case_count": len(pairs) * 3,
        "hard_case_count": 5,
        "output_path": str(output_path) if output_path is not None else None,
        "source_snapshot": {
            "file_name": pair_snapshot_path.name,
            "sha256": sha256_file(pair_snapshot_path),
        },
        "claim_scope": "REVIEW_CANDIDATE_ONLY",
        "warning": "독립 검수 전 후보이며 정확도·현장 성능 주장에 사용할 수 없습니다.",
    }


def _candidate_context(row: Mapping[str, Any]) -> dict[str, str]:
    input_payload = dict(row["input"])
    return {
        "source_type": str(row["source_type"]),
        "source_reference": str(row["source_reference"]),
        "scenario_origin": str(row["scenario_origin"]),
        "data_use_scope": str(row["data_use_scope"]),
        "duplicate_group": str(row["duplicate_group"]),
        "raw_text": str(input_payload["raw_text"]),
        "confirmed_incident_cas": str(
            input_payload.get("confirmed_incident_cas") or ""
        ),
        "confirmed_facility_cas": str(
            input_payload.get("confirmed_facility_cas") or ""
        ),
        "capabilities_json": _json_compact(row["capabilities"]),
    }


def export_review_sheet(
    candidate_path: Path,
    output_path: Path,
    *,
    actor_role: str,
    actor_id: str,
) -> dict[str, Any]:
    role = str(actor_role).strip().upper()
    normalized_actor = str(actor_id).strip()
    if role not in REVIEW_ROLES:
        raise ValueError(f"actor_role은 {sorted(REVIEW_ROLES)} 중 하나여야 합니다.")
    if not ACTOR_ID_PATTERN.fullmatch(normalized_actor):
        raise ValueError("actor_id는 2~64자의 영문·숫자·_.:@-만 사용할 수 있습니다.")
    rows = load_candidate_rows(Path(candidate_path))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for candidate in rows:
            writer.writerow(
                {
                    "sheet_schema_version": REVIEW_SHEET_SCHEMA_VERSION,
                    "case_id": candidate["case_id"],
                    "actor_role": role,
                    "actor_id": normalized_actor,
                    **_candidate_context(candidate),
                    "review_decision": "",
                    "status": "",
                    "rule_executed": "",
                    "rule_status": "",
                    "missing_confirmations_json": "",
                    "candidate_count": "",
                    "candidate_roles_json": "",
                    "evidence_bases_json": "",
                    "output_validation_status": "",
                    "risk_level": "",
                    "severity": "",
                    "expect_abstention": "",
                    "review_notes": "",
                }
            )
    return {
        "status": "COMPLETED",
        "action": "EXPORT_REVIEW_SHEET",
        "actor_role": role,
        "actor_id": normalized_actor,
        "case_count": len(rows),
        "candidate_sha256": sha256_file(Path(candidate_path)),
        "output_path": str(output_path),
        "instruction": "다른 검수자의 시트를 보지 않고 모든 기대값과 APPROVE 여부를 작성하세요.",
    }


def _read_review_sheet(
    path: Path, expected_role: str
) -> tuple[str, dict[str, dict[str, str]]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing_columns = sorted(set(REVIEW_COLUMNS) - set(reader.fieldnames or []))
        if missing_columns:
            raise ValueError(f"{path}: 누락된 검수 열={missing_columns}")
        rows = [dict(row) for row in reader]
    if not rows:
        raise ValueError(f"{path}: 검수 행이 없습니다.")
    actor_ids = {str(row.get("actor_id") or "").strip() for row in rows}
    roles = {str(row.get("actor_role") or "").strip().upper() for row in rows}
    if len(actor_ids) != 1 or not next(iter(actor_ids)):
        raise ValueError(f"{path}: 하나의 actor_id만 허용합니다.")
    actor_id = next(iter(actor_ids))
    if not ACTOR_ID_PATTERN.fullmatch(actor_id):
        raise ValueError(f"{path}: actor_id 형식이 올바르지 않습니다.")
    if roles != {expected_role}:
        raise ValueError(f"{path}: actor_role은 {expected_role}여야 합니다.")
    by_case: dict[str, dict[str, str]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "").strip()
        if not case_id or case_id in by_case:
            raise ValueError(f"{path}: 비어 있거나 중복된 case_id={case_id!r}")
        if row.get("sheet_schema_version") != REVIEW_SHEET_SCHEMA_VERSION:
            raise ValueError(f"{path}:{case_id}: 지원하지 않는 sheet schema")
        by_case[case_id] = row
    return actor_id, by_case


def _parse_bool(value: str, field: str, case_id: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{case_id}: {field}는 true 또는 false여야 합니다.")


def _parse_json_type(
    value: str,
    expected_type: type,
    field: str,
    case_id: str,
) -> Any:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{case_id}: {field}는 유효한 JSON이어야 합니다.") from error
    if not isinstance(parsed, expected_type):
        raise ValueError(f"{case_id}: {field}의 JSON 타입이 올바르지 않습니다.")
    return parsed


def _parse_label(row: Mapping[str, str], case_id: str) -> dict[str, Any]:
    if str(row.get("review_decision") or "").strip().upper() != "APPROVE":
        raise ValueError(f"{case_id}: review_decision=APPROVE가 필요합니다.")
    status = str(row.get("status") or "").strip()
    rule_status = str(row.get("rule_status") or "").strip()
    output_status = str(row.get("output_validation_status") or "").strip()
    if not status or not rule_status or not output_status:
        raise ValueError(
            f"{case_id}: status·rule_status·output_validation_status가 필요합니다."
        )
    count_text = str(row.get("candidate_count") or "").strip()
    try:
        candidate_count = int(count_text)
    except ValueError as error:
        raise ValueError(
            f"{case_id}: candidate_count는 0 이상의 정수여야 합니다."
        ) from error
    if candidate_count < 0:
        raise ValueError(f"{case_id}: candidate_count는 0 이상의 정수여야 합니다.")
    missing = _parse_json_type(
        str(row.get("missing_confirmations_json") or ""),
        list,
        "missing_confirmations_json",
        case_id,
    )
    roles = _parse_json_type(
        str(row.get("candidate_roles_json") or ""),
        list,
        "candidate_roles_json",
        case_id,
    )
    bases = _parse_json_type(
        str(row.get("evidence_bases_json") or ""), dict, "evidence_bases_json", case_id
    )
    if any(not isinstance(item, str) for item in missing + roles):
        raise ValueError(f"{case_id}: 확인·후보 역할 배열에는 문자열만 허용합니다.")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in bases.items()
    ):
        raise ValueError(f"{case_id}: evidence_bases는 문자열 키·값 객체여야 합니다.")
    return {
        "status": status,
        "rule_executed": _parse_bool(
            str(row.get("rule_executed") or ""), "rule_executed", case_id
        ),
        "rule_status": rule_status,
        "missing_confirmations": missing,
        "candidate_count": candidate_count,
        "candidate_roles": roles,
        "evidence_bases": bases,
        "output_validation_status": output_status,
        "risk_level": str(row.get("risk_level") or "").strip() or None,
        "severity": str(row.get("severity") or "").strip() or None,
        "expect_abstention": _parse_bool(
            str(row.get("expect_abstention") or ""), "expect_abstention", case_id
        ),
    }


def _label_safety_errors(
    candidate: Mapping[str, Any], label: Mapping[str, Any]
) -> list[str]:
    """두 사람이 같은 실수를 승인해도 위험한 상태 모순은 통과시키지 않는다."""

    errors: list[str] = []
    input_payload = dict(candidate["input"])
    incident_cas = input_payload.get("confirmed_incident_cas")
    facility_cas = input_payload.get("confirmed_facility_cas")
    both_present = bool(incident_cas and facility_cas)
    both_valid = bool(
        both_present
        and valid_cas_checksum(str(incident_cas))
        and valid_cas_checksum(str(facility_cas))
    )
    executed = label["rule_executed"] is True
    risk_present = label["risk_level"] is not None or label["severity"] is not None

    if label["output_validation_status"] != "PASSED":
        errors.append("output_validation_status는 PASSED여야 합니다.")
    if executed and not both_valid:
        errors.append("유효한 확인 CAS 두 개 없이 Rule 실행을 승인할 수 없습니다.")
    if both_valid and not executed:
        errors.append(
            "유효한 확인 CAS 두 개가 있으면 Rule 실행 결과를 검수해야 합니다."
        )
    if not executed and risk_present:
        errors.append("Rule 미실행 상태에는 위험등급을 둘 수 없습니다.")
    if (label["risk_level"] is None) != (label["severity"] is None):
        errors.append("risk_level과 severity는 함께 있거나 함께 없어야 합니다.")
    if not executed and label["expect_abstention"] is not True:
        errors.append("Rule 미실행 상태는 expect_abstention=true여야 합니다.")
    if label["candidate_count"] != len(label["candidate_roles"]):
        errors.append("candidate_count와 candidate_roles 길이가 다릅니다.")

    expected_missing: list[str] = []
    if not incident_cas:
        expected_missing.append("incident_cas")
    if not facility_cas:
        expected_missing.append("facility_cas")
    invalid_input = label["status"] == "INVALID_INPUT"
    if invalid_input:
        if label["missing_confirmations"]:
            errors.append("INVALID_INPUT에는 missing_confirmations를 둘 수 없습니다.")
    elif label["missing_confirmations"] != expected_missing:
        errors.append(
            "missing_confirmations가 후보 입력의 확인 상태와 일치하지 않습니다."
        )
    return errors


def merge_review_sheets(
    candidate_path: Path,
    labeler_sheet_path: Path,
    reviewer_sheet_path: Path,
    output_path: Path,
    *,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """독립된 두 시트가 완전히 일치할 때만 reviewed JSONL을 생성한다."""

    candidates = load_candidate_rows(Path(candidate_path))
    candidate_by_case = {str(row["case_id"]): row for row in candidates}
    labeler_id, labeler_rows = _read_review_sheet(Path(labeler_sheet_path), "LABELER")
    reviewer_id, reviewer_rows = _read_review_sheet(
        Path(reviewer_sheet_path), "REVIEWER"
    )
    blockers: list[dict[str, Any]] = []
    if labeler_id == reviewer_id:
        blockers.append({"code": "REVIEWERS_NOT_INDEPENDENT", "actor_id": labeler_id})
    candidate_ids = set(candidate_by_case)
    for role, rows in (("LABELER", labeler_rows), ("REVIEWER", reviewer_rows)):
        missing = sorted(candidate_ids - set(rows))
        unexpected = sorted(set(rows) - candidate_ids)
        if missing or unexpected:
            blockers.append(
                {
                    "code": "REVIEW_CASE_SET_MISMATCH",
                    "role": role,
                    "missing_case_ids": missing,
                    "unexpected_case_ids": unexpected,
                }
            )

    merged_rows: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    if not blockers:
        for case_id, candidate in candidate_by_case.items():
            context = _candidate_context(candidate)
            for role, sheet_row in (
                ("LABELER", labeler_rows[case_id]),
                ("REVIEWER", reviewer_rows[case_id]),
            ):
                changed = [
                    field
                    for field, expected_value in context.items()
                    if str(sheet_row.get(field) or "") != expected_value
                ]
                if changed:
                    blockers.append(
                        {
                            "code": "CANDIDATE_CONTEXT_CHANGED",
                            "case_id": case_id,
                            "role": role,
                            "fields": changed,
                        }
                    )
            try:
                labeler_label = _parse_label(labeler_rows[case_id], case_id)
                reviewer_label = _parse_label(reviewer_rows[case_id], case_id)
            except ValueError as error:
                parse_errors.append({"case_id": case_id, "message": str(error)})
                continue
            for role, label in (
                ("LABELER", labeler_label),
                ("REVIEWER", reviewer_label),
            ):
                safety_errors = _label_safety_errors(candidate, label)
                if safety_errors:
                    parse_errors.append(
                        {
                            "case_id": case_id,
                            "message": f"{role}: {' '.join(safety_errors)}",
                        }
                    )
            if any(error["case_id"] == case_id for error in parse_errors):
                continue
            differing_fields = [
                field
                for field in LABEL_FIELDS
                if labeler_label[field] != reviewer_label[field]
            ]
            if differing_fields:
                disagreements.append({"case_id": case_id, "fields": differing_fields})
                continue
            merged_rows.append(
                {
                    "case_id": case_id,
                    "review_status": "DOUBLE_REVIEWED_NON_EXPERT",
                    "source_type": candidate["source_type"],
                    "source_reference": candidate["source_reference"],
                    "scenario_origin": candidate["scenario_origin"],
                    "data_use_scope": "COMPETITION_REVIEWED_EVALUATION_ONLY",
                    "labeler_id": labeler_id,
                    "reviewer_id": reviewer_id,
                    "expert_reviewed": False,
                    "split": "locked_test",
                    "duplicate_group": candidate["duplicate_group"],
                    "capabilities": candidate["capabilities"],
                    "input": candidate["input"],
                    "expected": labeler_label,
                }
            )

    if parse_errors:
        blockers.append({"code": "INVALID_REVIEW_LABEL", "errors": parse_errors})
    if disagreements:
        blockers.append(
            {
                "code": "INDEPENDENT_REVIEW_DISAGREEMENT",
                "case_count": len(disagreements),
                "cases": disagreements,
            }
        )
    output_path = Path(output_path)
    contract: dict[str, Any] | None = None
    if not blockers and len(merged_rows) == len(candidates):
        temporary_path = output_path.with_name(f".{output_path.name}.tmp")
        _write_jsonl(temporary_path, merged_rows)
        contract = evaluate_dataset_contract(
            merged_rows,
            EvaluationProfile.COMPETITION_REVIEWED,
            temporary_path,
        )
        if not contract["passed"]:
            temporary_path.unlink(missing_ok=True)
            blockers.append(
                {
                    "code": "MERGED_DATASET_CONTRACT_FAILED",
                    "contract_blockers": contract["blockers"],
                }
            )
        else:
            temporary_path.replace(output_path)
            contract["dataset"] = str(output_path)
    report = {
        "schema_version": REVIEW_MERGE_SCHEMA_VERSION,
        "status": "COMPLETED" if not blockers else "BLOCKED_REVIEW_GATE",
        "candidate_count": len(candidates),
        "merged_case_count": len(merged_rows) if not blockers else 0,
        "labeler_id": labeler_id,
        "reviewer_id": reviewer_id,
        "independent_review": labeler_id != reviewer_id,
        "disagreement_count": len(disagreements),
        "blockers": blockers,
        "output_path": str(output_path) if not blockers else None,
        "evaluation_contract": contract,
        "claim_limit": ("이중 비전문가 검수 데이터이며 field_validated가 아닙니다."),
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


def preflight_candidate_pool(
    candidate_path: Path,
    db_path: Path,
    resolver_model_path: Path,
    retriever_model_path: Path,
    *,
    config_dir: Path = CONFIG_DIR,
    report_path: Path | None = None,
    resolver_artifact: dict[str, Any] | None = None,
    retriever_artifact: dict[str, Any] | None = None,
    analyzer: Analyzer | None = None,
) -> dict[str, Any]:
    """정답 없이 현재 모델 상태만 관찰한다. 정확도 계산에는 사용하지 않는다."""

    candidate_path = Path(candidate_path)
    rows = load_candidate_rows(candidate_path)
    resolver = (
        resolver_artifact
        if resolver_artifact is not None
        else load_resolver(Path(resolver_model_path))
    )
    retriever = (
        retriever_artifact
        if retriever_artifact is not None
        else load_retriever(Path(retriever_model_path))
    )
    analyze = analyzer or analyze_incident
    cases: list[dict[str, Any]] = []
    unsafe_execution_count = 0
    unconfirmed_risk_count = 0
    contract_failure_count = 0
    for row in rows:
        input_payload = dict(row["input"])
        started = time.perf_counter()
        output = analyze(
            str(input_payload["raw_text"]),
            db_path=Path(db_path),
            resolver_artifact=resolver,
            retriever_artifact=retriever,
            confirmed_incident_cas=input_payload.get("confirmed_incident_cas"),
            confirmed_facility_cas=input_payload.get("confirmed_facility_cas"),
            planned_actions=[],
            policy_mode=PUBLIC_SOURCE_PILOT_POLICY,
            config_dir=Path(config_dir),
        )
        latency_ms = (time.perf_counter() - started) * 1_000
        actual = summarize_pipeline_output(output)
        both_confirmed = bool(
            input_payload.get("confirmed_incident_cas")
            and input_payload.get("confirmed_facility_cas")
        )
        unsafe_execution = bool(actual["rule_executed"] and not both_confirmed)
        unconfirmed_risk = bool(
            not both_confirmed
            and contains_unconfirmed_risk_output(output.get("rule_review") or {})
        )
        validation_errors = validate_pipeline_output(
            output, str(input_payload["raw_text"])
        )
        unsafe_execution_count += int(unsafe_execution)
        unconfirmed_risk_count += int(unconfirmed_risk)
        contract_failure_count += int(bool(validation_errors))
        cases.append(
            {
                "case_id": row["case_id"],
                "scenario_origin": row["scenario_origin"],
                "actual": actual,
                "unsafe_conflict_execution": unsafe_execution,
                "unconfirmed_risk_exposure": unconfirmed_risk,
                "pipeline_contract_errors": validation_errors,
                "latency_ms": round(latency_ms, 6),
            }
        )
    latencies = [float(row["latency_ms"]) for row in cases]
    status_counts = Counter(str(row["actual"]["status"]) for row in cases)
    report = {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "action": "PREFLIGHT_CANDIDATE_POOL",
        "status": (
            "COMPLETED"
            if not unsafe_execution_count
            and not unconfirmed_risk_count
            and not contract_failure_count
            else "FAILED_SAFETY_PREFLIGHT"
        ),
        "machine_observation_only": True,
        "is_accuracy_evaluation": False,
        "field_validated": False,
        "candidate_count": len(rows),
        "candidate_pool": {
            "file_name": candidate_path.name,
            "sha256": sha256_file(candidate_path),
        },
        "artifacts": {
            "database": _artifact_identity(Path(db_path)),
            "resolver": _artifact_identity(Path(resolver_model_path)),
            "retriever": _artifact_identity(Path(retriever_model_path)),
        },
        "metrics": {
            "status_counts": dict(sorted(status_counts.items())),
            "rule_executed_count": sum(
                bool(row["actual"]["rule_executed"]) for row in cases
            ),
            "unsafe_conflict_execution_count": unsafe_execution_count,
            "unconfirmed_risk_exposure_count": unconfirmed_risk_count,
            "pipeline_contract_failure_count": contract_failure_count,
            "latency_ms": {
                "mean": sum(latencies) / len(latencies) if latencies else None,
                "p95": _percentile(latencies, 0.95),
            },
        },
        "cases": cases,
        "warning": (
            "정답 라벨이 없는 모델 관찰값입니다. 정확도·Recall·현장 성능으로 인용하지 마세요."
        ),
    }
    if report_path is not None:
        write_json(Path(report_path), report)
    return report


__all__ = [
    "ACTOR_ID_PATTERN",
    "CANDIDATE_SCHEMA_VERSION",
    "PREFLIGHT_SCHEMA_VERSION",
    "REVIEW_COLUMNS",
    "REVIEW_MERGE_SCHEMA_VERSION",
    "REVIEW_SHEET_SCHEMA_VERSION",
    "export_review_sheet",
    "generate_review_candidate_pool",
    "load_candidate_rows",
    "merge_review_sheets",
    "preflight_candidate_pool",
    "validate_candidate_rows",
]
