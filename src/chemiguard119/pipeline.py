"""신고문 구조화·근거 검색·Rule 검토를 연결하는 사고 분석 오케스트레이터.

이 모듈은 HTTP 프레임워크와 무관한 순수 Python 경계다. API 서버는 모델
artifact를 시작 시 한 번 로드한 뒤 :func:`analyze_incident`에 전달할 수 있다.

안전상 가장 중요한 불변조건은 다음과 같다.

* 신고문에서 찾은 이름과 Resolver 결과는 항상 ``후보``다.
* ``confirmed_incident_cas``와 ``confirmed_facility_cas``가 모두 명시적으로
  전달된 경우에만 Rule Engine을 실행한다.
* 검색 순위와 Parser 출력은 위험등급 또는 현장 명령으로 승격하지 않는다.
* 최종 결정권자는 항상 현장 지휘관이다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from chemiguard119.incident import deterministic_parse, validate_parser_output
from chemiguard119.paths import CONFIG_DIR
from chemiguard119.resolver import select_evidence_cas_hint
from chemiguard119.retrieval import (
    CAS_EVIDENCE_NOT_LOADED_STATUS,
    INVALID_CAS_HINT_STATUS,
    search_evidence,
)
from chemiguard119.rules import (
    APPROVED_ONLY_POLICY,
    PUBLIC_SOURCE_PILOT_POLICY,
    SUPPORTED_POLICY_MODES,
    review_pair,
    validate_review_output,
)
from chemiguard119.utils import normalize_cas, valid_cas_checksum


PIPELINE_SCHEMA_VERSION = "incident-analysis-v1"
RULE_GATE = "BOTH_CAS_RESPONDER_CONFIRMED"
FINAL_DECISION_AUTHORITY = "현장 지휘관 판단"


def _safety_fields() -> dict[str, Any]:
    return {
        "decision_support_only": True,
        "human_confirmation_required": True,
        "final_decision_authority": FINAL_DECISION_AUTHORITY,
        "rule_execution_gate": RULE_GATE,
        "name_candidates_do_not_trigger_rules": True,
        "retrieval_scores_are_not_risk_scores": True,
        "planned_actions_are_not_automatically_approved": True,
        "notice": (
            "이 결과는 케미체크119의 의사결정 보조 정보이며 현장 명령이 아닙니다. "
            "물질과 시설 상태를 대원이 확인하고 최종 결정은 현장 지휘관이 수행합니다."
        ),
    }


def _normalize_actions(
    planned_actions: Sequence[str] | None,
) -> tuple[list[str], list[str]]:
    if planned_actions is None:
        return [], []
    if isinstance(planned_actions, (str, bytes)):
        return [], ["planned_actions는 문자열 배열이어야 합니다."]

    normalized: list[str] = []
    errors: list[str] = []
    for index, action in enumerate(planned_actions):
        if not isinstance(action, str):
            errors.append(f"planned_actions[{index}]는 문자열이어야 합니다.")
            continue
        value = action.strip()
        if not value:
            errors.append(f"planned_actions[{index}]는 빈 문자열일 수 없습니다.")
            continue
        normalized.append(value)
    return normalized, errors


def _normalize_confirmed_cas(
    value: str | None, field_name: str
) -> tuple[str | None, list[str]]:
    if value is None:
        return None, []
    if not isinstance(value, str) or not value.strip():
        return None, [f"{field_name}는 유효한 CAS 문자열이어야 합니다."]
    normalized = normalize_cas(value)
    if not valid_cas_checksum(normalized):
        return None, [
            f"{field_name}의 CAS 형식 또는 체크섬이 유효하지 않습니다: {value}"
        ]
    return normalized, []


def _confirmation(cas_number: str | None) -> dict[str, str] | None:
    if cas_number is None:
        return None
    return {
        "cas_number": cas_number,
        "confirmation": "RESPONDER_CONFIRMED",
    }


def _candidate_mentions(
    parsed: dict[str, Any], confirmed: dict[str, str | None]
) -> list[dict[str, Any]]:
    """Parser의 물질 표현을 역할별 Resolver 후보로 평탄화한다."""

    candidates: list[dict[str, Any]] = []
    for mention in parsed.get("substance_mentions", []):
        resolver = mention.get("resolver") or {}
        role = str(mention.get("role") or "UNKNOWN")
        role_key = (
            "incident"
            if role == "INCIDENT"
            else "facility"
            if role == "FACILITY"
            else None
        )
        confirmed_cas = confirmed.get(role_key) if role_key else None
        candidates.append(
            {
                "surface_text": mention.get("surface_text"),
                "role": role,
                "assertion": mention.get("assertion"),
                "resolver_status": resolver.get("status"),
                "resolver_input_class": resolver.get("input_class"),
                "evidence_cas_hint": select_evidence_cas_hint(resolver),
                "requires_responder_confirmation": True,
                "matches_confirmed_cas": bool(
                    confirmed_cas
                    and any(
                        item.get("cas_number") == confirmed_cas
                        for item in resolver.get("candidates", [])
                    )
                ),
                "candidates": resolver.get("candidates", []),
            }
        )
    return candidates


def _first_candidate_for_role(
    candidates: list[dict[str, Any]],
    role: str,
) -> tuple[str, str] | None:
    for mention in candidates:
        if mention.get("role") != role or mention.get("assertion") == "NEGATED":
            continue
        cas_hint = mention.get("evidence_cas_hint")
        if cas_hint:
            return str(mention.get("surface_text") or ""), str(cas_hint)
    return None


def _evidence_targets(
    raw_text: str,
    candidates: list[dict[str, Any]],
    confirmed_incident_cas: str | None,
    confirmed_facility_cas: str | None,
) -> list[dict[str, Any]]:
    """역할별 최대 한 개의 검색 대상을 만들고 CAS의 근거 수준을 표시한다."""

    targets: list[dict[str, Any]] = []
    for role, confirmed_cas in (
        ("INCIDENT", confirmed_incident_cas),
        ("FACILITY", confirmed_facility_cas),
    ):
        if confirmed_cas:
            targets.append(
                {
                    "role": role,
                    "query": raw_text,
                    "cas_hint": confirmed_cas,
                    "cas_basis": "RESPONDER_CONFIRMED",
                    "requires_responder_confirmation": False,
                }
            )
            continue

        candidate = _first_candidate_for_role(candidates, role)
        if candidate:
            surface, cas_number = candidate
            targets.append(
                {
                    "role": role,
                    "query": f"{surface} {raw_text}".strip(),
                    "cas_hint": cas_number,
                    "cas_basis": "PARSER_CANDIDATE",
                    "requires_responder_confirmation": True,
                }
            )

    # 역할을 판별하지 못했더라도 신고문 자체에 대한 보조 검색은 남긴다.
    if not targets:
        targets.append(
            {
                "role": "UNKNOWN",
                "query": raw_text,
                "cas_hint": None,
                "cas_basis": "NO_CAS_HINT",
                "requires_responder_confirmation": True,
            }
        )
    return targets


def _top_level_status(
    confirmed_incident_cas: str | None,
    confirmed_facility_cas: str | None,
    review: dict[str, Any] | None,
) -> str:
    if not confirmed_incident_cas and not confirmed_facility_cas:
        return "NEEDS_SUBSTANCE_CONFIRMATION"
    if not confirmed_incident_cas:
        return "NEEDS_INCIDENT_SUBSTANCE_CONFIRMATION"
    if not confirmed_facility_cas:
        return "NEEDS_FACILITY_SUBSTANCE_CONFIRMATION"
    if not review:
        return "OUTPUT_VALIDATION_FAILED"
    if review.get("status") in {"COMPLETED", "COMPLETED_DEMO"}:
        return "COMPLETED_WITH_WARNINGS"
    if review.get("status") == "SCREENING_COMPLETED":
        return "SCREENING_COMPLETED"
    return str(review.get("status") or "UNCLASSIFIED")


def _contains_safe_severity(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("severity") == "SAFE":
            return True
        return any(_contains_safe_severity(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_safe_severity(item) for item in value)
    return False


def validate_pipeline_output(
    payload: dict[str, Any], source_text: str | None = None
) -> list[str]:
    """API로 내보내기 전 오케스트레이터의 안전 불변조건을 검사한다."""

    errors: list[str] = []
    if payload.get("schema_version") != PIPELINE_SCHEMA_VERSION:
        errors.append("지원하지 않거나 누락된 pipeline schema_version")
    policy_mode = payload.get("conflict_policy_mode")
    if policy_mode not in SUPPORTED_POLICY_MODES:
        errors.append("지원하지 않거나 누락된 conflict_policy_mode")

    input_payload = payload.get("input") or {}
    expected_source = (
        source_text if source_text is not None else input_payload.get("raw_text")
    )
    if input_payload.get("raw_text") != expected_source:
        errors.append("출력의 raw_text가 입력 원문과 다릅니다.")

    safety = payload.get("safety") or {}
    if safety.get("decision_support_only") is not True:
        errors.append("의사결정 보조 전용 표시 누락")
    if safety.get("human_confirmation_required") is not True:
        errors.append("사람 확인 필수 표시 누락")
    if safety.get("final_decision_authority") != FINAL_DECISION_AUTHORITY:
        errors.append("최종 결정권자 문구 변경")
    if safety.get("rule_execution_gate") != RULE_GATE:
        errors.append("Rule 실행 게이트 문구 변경")
    if safety.get("name_candidates_do_not_trigger_rules") is not True:
        errors.append("이름 후보의 Rule 실행 금지 표시 누락")

    parsed = payload.get("parsed_report")
    if parsed is not None and expected_source is not None:
        errors.extend(validate_parser_output(parsed, str(expected_source)))

    confirmations = payload.get("confirmed_substances") or {}
    confirmed_values: dict[str, str | None] = {"incident": None, "facility": None}
    for role in ("incident", "facility"):
        item = confirmations.get(role)
        if item is None:
            continue
        if item.get("confirmation") != "RESPONDER_CONFIRMED":
            errors.append(f"{role} CAS의 RESPONDER_CONFIRMED 표시 누락")
        cas_number = normalize_cas(str(item.get("cas_number") or ""))
        if not valid_cas_checksum(cas_number):
            errors.append(f"{role} confirmed CAS가 유효하지 않습니다.")
        else:
            confirmed_values[role] = cas_number

    rule_wrapper = payload.get("rule_review") or {}
    rule_executed = rule_wrapper.get("executed") is True
    both_confirmed = bool(confirmed_values["incident"] and confirmed_values["facility"])
    if rule_executed and not both_confirmed:
        errors.append("두 CAS의 대원 확인 없이 Rule Engine이 실행되었습니다.")
    if not rule_executed:
        forbidden = {"severity", "rule_id", "brief_text", "required_checks"}
        if forbidden.intersection(rule_wrapper):
            errors.append("실행되지 않은 Rule 검토에 위험판정 필드가 포함되었습니다.")
        if both_confirmed and payload.get("status") != "OUTPUT_VALIDATION_FAILED":
            errors.append("두 CAS가 확인됐지만 Rule Engine 실행 기록이 없습니다.")
    else:
        result = rule_wrapper.get("result") or {}
        errors.extend(validate_review_output(result))
        if (
            result.get("status") == "SCREENING_COMPLETED"
            and policy_mode != PUBLIC_SOURCE_PILOT_POLICY
        ):
            errors.append("공개근거 screening 결과와 pipeline policy_mode가 다릅니다.")
        if (
            result.get("incident_cas")
            and result.get("incident_cas") != confirmed_values["incident"]
        ):
            errors.append("Rule incident CAS와 대원 확인 CAS가 다릅니다.")
        if (
            result.get("facility_cas")
            and result.get("facility_cas") != confirmed_values["facility"]
        ):
            errors.append("Rule facility CAS와 대원 확인 CAS가 다릅니다.")

    for item in payload.get("evidence", []):
        basis = item.get("cas_basis")
        if (
            basis == "PARSER_CANDIDATE"
            and item.get("requires_responder_confirmation") is not True
        ):
            errors.append("Parser 후보 기반 근거 검색의 대원 확인 표시 누락")
        if basis == "RESPONDER_CONFIRMED":
            role_key = "incident" if item.get("role") == "INCIDENT" else "facility"
            if item.get("cas_hint") != confirmed_values.get(role_key):
                errors.append("근거 검색의 확인 CAS가 confirmed_substances와 다릅니다.")

        cas_hint = item.get("cas_hint")
        retrieval = item.get("retrieval") or {}
        retrieval_results = retrieval.get("results") or []
        if cas_hint:
            normalized_hint = normalize_cas(str(cas_hint))
            if retrieval.get("cas_hint") != normalized_hint:
                errors.append("근거 검색 응답 CAS가 요청 CAS와 다릅니다.")
            if any(
                normalize_cas(str(result.get("cas_number") or "")) != normalized_hint
                for result in retrieval_results
            ):
                errors.append("CAS 제한 근거 검색에 다른 CAS 문서가 포함되었습니다.")
            if (
                not retrieval_results
                and retrieval.get("status") != CAS_EVIDENCE_NOT_LOADED_STATUS
            ):
                errors.append(
                    "CAS 제한 검색의 빈 결과에 상세 근거 미적재 상태가 없습니다."
                )

    if _contains_safe_severity(payload):
        errors.append("SAFE severity 사용 금지")
    return errors


def analyze_incident(
    raw_text: str,
    *,
    db_path: Path,
    resolver_artifact: dict[str, Any],
    retriever_artifact: dict[str, Any],
    confirmed_incident_cas: str | None = None,
    confirmed_facility_cas: str | None = None,
    planned_actions: Sequence[str] | None = None,
    allow_demo_rules: bool = False,
    policy_mode: str = APPROVED_ONLY_POLICY,
    config_dir: Path = CONFIG_DIR,
    evidence_top_k: int = 5,
) -> dict[str, Any]:
    """사고 신고 한 건을 구조화하고 검증 가능한 분석 JSON으로 변환한다.

    ``confirmed_*_cas`` 인자는 API가 인증된 대원 확인 이벤트를 받은 뒤에만
    채워야 한다. 신고문에 CAS가 직접 적혀 있어도 이 인자 두 개가 없으면
    Rule Engine은 실행되지 않는다.
    """

    raw_value = raw_text if isinstance(raw_text, str) else ""
    actions, action_errors = _normalize_actions(planned_actions)
    incident_cas, incident_errors = _normalize_confirmed_cas(
        confirmed_incident_cas, "confirmed_incident_cas"
    )
    facility_cas, facility_errors = _normalize_confirmed_cas(
        confirmed_facility_cas, "confirmed_facility_cas"
    )
    input_errors = action_errors + incident_errors + facility_errors
    if not raw_value.strip():
        input_errors.insert(0, "raw_text는 비어 있을 수 없습니다.")
    if evidence_top_k < 1:
        input_errors.append("evidence_top_k는 1 이상이어야 합니다.")

    confirmations = {
        "incident": _confirmation(incident_cas),
        "facility": _confirmation(facility_cas),
    }
    base: dict[str, Any] = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "input": {
            "raw_text": raw_value,
            "planned_actions": actions,
        },
        "confirmed_substances": confirmations,
        "conflict_policy_mode": policy_mode,
        "safety": _safety_fields(),
        "trace": [],
    }
    if input_errors:
        base.update(
            {
                "status": "INVALID_INPUT",
                "errors": input_errors,
                "parsed_report": None,
                "substance_candidates": [],
                "evidence": [],
                "rule_review": {
                    "executed": False,
                    "status": "NOT_RUN_INVALID_INPUT",
                    "gate": RULE_GATE,
                },
            }
        )
        base["trace"].append(
            {"stage": "INPUT_VALIDATION", "status": "FAILED", "errors": input_errors}
        )
        validation_errors = validate_pipeline_output(base, raw_value)
        base["output_validation"] = {
            "status": "PASSED" if not validation_errors else "FAILED",
            "errors": validation_errors,
        }
        return base

    base["trace"].append({"stage": "INPUT_VALIDATION", "status": "PASSED"})
    parsed = deterministic_parse(raw_value, resolver_artifact)
    parser_errors = validate_parser_output(parsed, raw_value)
    base["trace"].append(
        {
            "stage": "REPORT_PARSING",
            "status": "COMPLETED" if not parser_errors else "FAILED",
            "backend": parsed.get("backend"),
        }
    )
    if parser_errors:
        base.update(
            {
                "status": "OUTPUT_VALIDATION_FAILED",
                "errors": parser_errors,
                "parsed_report": None,
                "substance_candidates": [],
                "evidence": [],
                "rule_review": {
                    "executed": False,
                    "status": "NOT_RUN_PARSER_OUTPUT_INVALID",
                    "gate": RULE_GATE,
                },
            }
        )
        base["output_validation"] = {"status": "FAILED", "errors": parser_errors}
        return base

    candidates = _candidate_mentions(
        parsed,
        {"incident": incident_cas, "facility": facility_cas},
    )
    base["parsed_report"] = parsed
    base["substance_candidates"] = candidates

    evidence: list[dict[str, Any]] = []
    for target in _evidence_targets(raw_value, candidates, incident_cas, facility_cas):
        retrieval = search_evidence(
            target["query"],
            db_path,
            retriever_artifact,
            cas_hint=target["cas_hint"],
            top_k=evidence_top_k,
        )
        evidence.append({**target, "retrieval": retrieval})
    base["evidence"] = evidence
    retrieval_statuses = [
        str(item["retrieval"].get("status") or "UNKNOWN") for item in evidence
    ]
    missing_cas_evidence_count = sum(
        status == CAS_EVIDENCE_NOT_LOADED_STATUS for status in retrieval_statuses
    )
    retrieval_warning_count = sum(
        status in {CAS_EVIDENCE_NOT_LOADED_STATUS, INVALID_CAS_HINT_STATUS}
        for status in retrieval_statuses
    )
    base["trace"].append(
        {
            "stage": "EVIDENCE_RETRIEVAL",
            "status": (
                "COMPLETED_WITH_WARNINGS" if retrieval_warning_count else "COMPLETED"
            ),
            "query_count": len(evidence),
            "result_count": sum(
                len(item["retrieval"].get("results", [])) for item in evidence
            ),
            "missing_cas_evidence_count": missing_cas_evidence_count,
        }
    )

    review: dict[str, Any] | None = None
    if incident_cas and facility_cas:
        review = review_pair(
            incident_cas,
            facility_cas,
            db_path,
            planned_actions=actions,
            allow_demo_rules=allow_demo_rules,
            policy_mode=policy_mode,
            config_dir=config_dir,
        )
        base["rule_review"] = {
            "executed": True,
            "status": review.get("status"),
            "gate": RULE_GATE,
            "policy_mode": policy_mode,
            "result": review,
        }
        base["trace"].append(
            {
                "stage": "RULE_REVIEW",
                "status": "EXECUTED",
                "gate": RULE_GATE,
                "review_status": review.get("status"),
            }
        )
    else:
        missing = []
        if not incident_cas:
            missing.append("incident_cas")
        if not facility_cas:
            missing.append("facility_cas")
        base["rule_review"] = {
            "executed": False,
            "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
            "gate": RULE_GATE,
            "missing_confirmations": missing,
            "reason": "이름·Resolver 후보만으로는 Rule Engine을 실행하지 않습니다.",
        }
        base["trace"].append(
            {
                "stage": "RULE_REVIEW",
                "status": "SKIPPED",
                "gate": RULE_GATE,
                "reason_code": "TWO_CONFIRMED_CAS_REQUIRED",
                "missing_confirmations": missing,
            }
        )

    base["status"] = _top_level_status(incident_cas, facility_cas, review)
    validation_errors = validate_pipeline_output(base, raw_value)
    if validation_errors:
        base["status"] = "OUTPUT_VALIDATION_FAILED"
        base["errors"] = validation_errors
        base["output_validation"] = {"status": "FAILED", "errors": validation_errors}
    else:
        base["output_validation"] = {"status": "PASSED", "errors": []}
    base["trace"].append(
        {
            "stage": "OUTPUT_VALIDATION",
            "status": base["output_validation"]["status"],
            "error_count": len(base["output_validation"]["errors"]),
        }
    )
    return base


__all__ = [
    "FINAL_DECISION_AUTHORITY",
    "PIPELINE_SCHEMA_VERSION",
    "RULE_GATE",
    "analyze_incident",
    "validate_pipeline_output",
]
