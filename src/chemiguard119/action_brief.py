"""검증된 분석 결과를 짧은 카드로 투영한다. 생성형 전술 문장은 만들지 않는다."""

from __future__ import annotations

import hashlib
from typing import Any

from chemiguard119.action_catalog import (
    CATALOG,
    CATALOG_SHA256,
    CATALOG_VERSION,
    POLICY_VERSION,
    stable_hash,
)
from chemiguard119.action_models import (
    ActionCard,
    BriefRequest,
    BriefResponse,
    BriefSource,
)
from chemiguard119.action_policy import usable_source
from chemiguard119.api_models import AnalysisResponse
from chemiguard119.rag import _valid_official_url

LIMITATIONS = [
    "개발용 의사결정 지원 API입니다. 현장 적용·전술 문구 전문 검수는 완료되지 않았습니다.",
    "확인 상태는 Backend가 전달한 기록입니다. 모델 API는 실제 대원 확인이나 최신 revision을 인증하지 않습니다.",
    "주소 비식별 부분 복원·ASR 오인식 정답 복구·근거의 의미 상충 완전 탐지는 지원하지 않습니다.",
    "공식 자료의 존재와 CAS 일치는 제품·농도·상태·기관 SOP의 현장 적용 승인이 아닙니다.",
]


def _card(
    response: BriefResponse,
    phrase_id: str,
    *,
    role: str = "UNKNOWN",
    cas: str | None = None,
    sources: list[str] | None = None,
    unmet: list[str] | None = None,
) -> ActionCard:
    phrase = CATALOG[phrase_id]
    return ActionCard(
        card_id=f"{phrase_id}:{role}",
        phrase_id=phrase_id,
        phrase_version=CATALOG_VERSION,
        category=phrase["category"],
        priority=phrase["priority"],
        title=phrase["title"],
        message=phrase["message"],
        reason=phrase["reason"],
        role=role,
        target_label={
            "INCIDENT": "사고물질",
            "FACILITY": "시설물질",
            "UNKNOWN": "전체 안내",
        }[role],
        cas_number=cas,
        confirmation_status=(
            "CONFIRMED_INPUT"
            if response.confirmation_state.get(role)
            else "UNCONFIRMED"
            if role != "UNKNOWN"
            else "NOT_APPLICABLE"
        ),
        required_conditions=phrase["conditions"],
        unmet_conditions=unmet if unmet is not None else phrase["conditions"],
        source_ids=sources or ["POLICY"],
        state_fingerprint=response.state_fingerprint,
    )


def initial_brief(
    payload: BriefRequest,
    request_id: str,
    runtime_fingerprint: str,
    versions: dict[str, Any],
) -> BriefResponse:
    effective = payload.effective_analysis()
    fingerprint = stable_hash(
        {
            "input": payload.model_dump(
                mode="json", exclude={"analysis": {"request_id"}}
            ),
            "runtime": runtime_fingerprint,
            "catalog": CATALOG_SHA256,
            "policy": POLICY_VERSION,
        }
    )
    confirmed = {
        "INCIDENT": bool(effective.confirmed_incident_substance),
        "FACILITY": bool(effective.confirmed_facility_substance),
    }
    result = BriefResponse(
        request_id=request_id,
        incident_id=str(effective.incident_id),
        revision=payload.revision,
        state_fingerprint=fingerprint,
        phase="initial",
        status="PENDING",
        summary=f"확인 기록 {sum(confirmed.values())}/2개를 받았습니다. 현재 정보의 확인·보류 안내이며 분석은 진행 중입니다.",
        cards=[],
        missing_information=[],
        confirmation_state=confirmed,
        sources=[
            BriefSource(
                source_id="POLICY",
                document_id="docs/ACTION_BRIEF.md",
                source_type="INTERNAL_POLICY",
                url="docs/ACTION_BRIEF.md",
                section="확인 Gate와 카드 정책",
                document_version=POLICY_VERSION,
                content_sha256=CATALOG_SHA256,
                license_status="PROJECT_AUTHORED",
                freshness_status="VERSIONED_POLICY",
            )
        ],
        versions={
            **versions,
            "catalog": CATALOG_VERSION,
            "catalog_sha256": CATALOG_SHA256,
            "policy": POLICY_VERSION,
            "planning_mode": "DETERMINISTIC_POLICY_PLANNER",
            "phrase_review": "DRAFT_NOT_EXPERT_REVIEWED",
            "llm": "NOT_USED",
        },
        limitations=LIMITATIONS,
        input_provenance={
            "type": effective.input.type.value,
            "text_sha256": hashlib.sha256(effective.input.text.encode()).hexdigest(),
            "characters": len(effective.input.text),
            "transcript_rewritten": False,
            "stt_included": False,
        },
    )
    for role, is_confirmed in confirmed.items():
        if not is_confirmed:
            result.cards.append(_card(result, "VERIFY_MATERIAL", role=role))
            result.missing_information.append(
                f"{'사고' if role == 'INCIDENT' else '시설'}물질의 현장 확인 기록"
            )
    if not all(confirmed.values()):
        result.cards.append(
            _card(
                result,
                "HOLD_PAIR",
                unmet=[
                    f"{'사고물질' if role == 'INCIDENT' else '시설물질'} 확인"
                    for role, value in confirmed.items()
                    if not value
                ],
            )
        )
    else:
        result.cards.append(_card(result, "ANALYSIS_PENDING"))
    if not effective.location or not (
        effective.location.address
        or (
            effective.location.latitude is not None
            and effective.location.longitude is not None
        )
    ):
        result.cards.append(_card(result, "VERIFY_LOCATION"))
        result.missing_information.append("접수 시스템에서 확인한 위치")
    if payload.reported_evidence_conflict or payload.invalidated_confirmation_ids:
        result.cards.append(_card(result, "HOLD_CONFLICT"))
    result.handoff = {
        "received_confirmation_records": [
            {
                "role": item.role,
                "cas_number": item.cas_number,
                "confirmation_id": item.confirmation_id,
                "confirmation_basis": item.confirmation_basis.value,
                "model_verified_human_confirmation": False,
            }
            for item in (
                effective.confirmed_incident_substance,
                effective.confirmed_facility_substance,
            )
            if item
        ],
        "analysis_complete": False,
        "unconfirmed_items": list(result.missing_information),
    }
    return BriefResponse.model_validate(result.model_dump())


def failed_brief(
    initial: BriefResponse, code: str, processing: dict[str, Any]
) -> BriefResponse:
    result = initial.model_copy(deep=True)
    result.phase = "final"
    result.status = "TIMEOUT" if code == "DEADLINE_EXCEEDED" else "HELD"
    result.cards = [
        card for card in result.cards if card.phrase_id != "ANALYSIS_PENDING"
    ]
    result.summary = (
        "현재 상태의 분석을 완료하지 못했습니다. 확인·보류 안내만 사용할 수 있습니다."
    )
    result.cards.append(
        _card(
            result,
            "HOLD_CONFLICT"
            if code in {"CONFIRMATION_CONFLICT", "DOCUMENT_CONFLICT"}
            else "HOLD_FAILURE",
        )
    )
    result.processing = {**processing, "failure_code": code}
    result.missing_information.append("현재 상태로 재분석한 검증 결과")
    # 실패에서는 동적으로 생성한 대응 문구나 이전 사고 결과를 재사용하지 않는다.
    result.cards = list({card.card_id: card for card in result.cards}.values())
    return BriefResponse.model_validate(result.model_dump())


def final_brief(
    initial: BriefResponse, analysis: AnalysisResponse, processing: dict[str, Any]
) -> BriefResponse:
    result = initial.model_copy(deep=True)
    result.phase = "final"
    result.status = (
        "NEEDS_CONFIRMATION"
        if not all(result.confirmation_state.values())
        else "COMPLETED"
    )
    result.cards = [
        card for card in result.cards if card.phrase_id != "ANALYSIS_PENDING"
    ]
    result.facts = analysis.model_outputs.get("parser", {})
    result.substance_candidates = analysis.model_outputs.get("substance_candidates", [])
    result.processing = processing
    for role in ("INCIDENT", "FACILITY"):
        if not result.confirmation_state[role]:
            continue
        found = []
        target_cas = None
        for group in analysis.evidence:
            if (
                group.get("role") != role
                or group.get("cas_basis") != "RESPONDER_CONFIRMED"
            ):
                continue
            target_cas = group.get("cas_hint")
            for row in group.get("retrieval", {}).get("results", []):
                if not usable_source(row, target_cas):
                    continue
                source_id = f"{role}:{row['evidence_id']}"
                if source_id in found:
                    continue
                found.append(source_id)
                result.sources.append(
                    BriefSource(
                        source_id=source_id,
                        document_id=str(
                            row.get("source_record_id") or row["evidence_id"]
                        ),
                        source_type=row["source"],
                        url=row["source_url"],
                        section=str(row.get("title") or "인덱스 항목"),
                        cas_number=target_cas,
                        role=role,
                        document_version=str(row["document_version"]),
                        selection_method=str(
                            row.get("brief_selection") or "BASELINE_RETRIEVAL"
                        ),
                        content_sha256=hashlib.sha256(
                            str(row.get("body_preview") or "").encode()
                        ).hexdigest(),
                        license_status="LINK_ONLY_TERMS_REVIEW_REQUIRED",
                        freshness_status="INDEX_VERSION_ONLY",
                    )
                )
        result.cards.append(
            _card(
                result,
                "REFERENCE_AVAILABLE" if found else "NO_EVIDENCE",
                role=role,
                cas=target_cas,
                sources=found or None,
                unmet=[] if found else None,
            )
        )
        if not found:
            result.missing_information.append(
                f"{'사고물질' if role == 'INCIDENT' else '시설물질'}의 사용 가능한 공식 근거"
            )
            result.status = "HELD"
    if analysis.model_outputs.get("facility_history_candidates") is not None:
        result.cards.append(_card(result, "HISTORY", role="FACILITY"))
        history = analysis.model_outputs["facility_history_candidates"]
        result.processing["facility_history"] = {
            "status": history.get("status"),
            "current_inventory": False,
        }
    review = analysis.conflict_review.model_dump(mode="json")
    result.rule_review = {
        "executed": review.get("executed", False),
        "status": review.get("status"),
        "is_probability": False,
        "tactical_authorization": False,
    }
    if review.get("executed"):
        rule_result = review.get("result") or {}
        links = [
            url
            for url in rule_result.get("evidence_urls", [])
            if _valid_official_url(url, "CAMEO")
        ]
        if review.get("status") in {"COMPLETED", "SCREENING_COMPLETED"} and links:
            result.rule_review.update(
                {
                    key: rule_result.get(key)
                    for key in (
                        "severity",
                        "risk_level",
                        "risk_level_ko",
                        "hazard_codes",
                        "gas_products",
                    )
                }
            )
            result.rule_review["source_urls"] = links
            result.rule_review["scope"] = (
                "LIMITED_PUBLIC_RULE_ORDINAL_NOT_FIELD_PROBABILITY"
            )
            result.rule_review["chemical_form_concentration_and_sop_verified"] = False
        else:
            result.status = "HELD"
            result.missing_information.append("적용 가능한 조합 규칙과 출처")
        result.cards.append(_card(result, "RULE_REFERENCE"))
    result.cards.append(_card(result, "HANDOFF", unmet=[]))
    result.cards.sort(key=lambda card: (card.priority, card.card_id))
    count = sum(result.confirmation_state.values())
    labels = {
        "LEAK": "누출",
        "FIRE": "화재",
        "EXPLOSION": "폭발",
        "UNKNOWN": "유형 미상",
    }
    reported = "·".join(
        labels.get(value, "유형 미상")
        for value in result.facts.get("incident_types", ["UNKNOWN"])
    )
    result.summary = f"신고 표현: {reported}. 확인 기록 {count}/2개 · 미확인 항목 {len(result.missing_information)}개. 후보와 확인 기록을 구분해 인계하세요. 현장 명령이 아닌 검수 전 보조 안내입니다."
    result.handoff.update(
        {
            "analysis_complete": True,
            "reported_incident_types": result.facts.get("incident_types", []),
            "reported_mentions_not_confirmed_facts": [
                {key: mention.get(key) for key in ("surface_text", "role", "assertion")}
                for mention in result.substance_candidates
            ],
            "unconfirmed_items": list(result.missing_information),
            "source_ids": [
                source.source_id
                for source in result.sources
                if source.source_type != "INTERNAL_POLICY"
            ],
            "rule_status": result.rule_review.get("status"),
        }
    )
    return BriefResponse.model_validate(result.model_dump())
