"""프로젝트 작성 문구: 전문 검수 전. 외부 문서 원문을 전술 지시로 재배포하지 않는다."""

import hashlib
import json

CATALOG_VERSION = "action-catalog-v1"
POLICY_VERSION = "action-policy-v1"
CATALOG = {
    "ANALYSIS_PENDING": {
        "category": "보류",
        "priority": 2,
        "title": "현재 정보의 분석을 기다려 주세요",
        "message": "확인 기록을 받았습니다. 공식 근거와 조합 규칙 검증이 끝나기 전에는 대응 결과를 표시하지 않습니다.",
        "reason": "확인 기록 수신과 근거 검증 완료는 서로 다릅니다.",
        "conditions": ["현재 상태의 분석 완료"],
    },
    "VERIFY_MATERIAL": {
        "category": "확인",
        "priority": 1,
        "title": "물질을 확인해 주세요",
        "message": "용기 라벨이나 제품 SDS에서 물질명과 CAS를 확인해 주세요.",
        "reason": "신고 표현과 검색 후보는 현장 물질 확인을 대신하지 않습니다.",
        "conditions": ["역할별 현장 확인 기록"],
    },
    "VERIFY_LOCATION": {
        "category": "확인",
        "priority": 1,
        "title": "위치를 별도로 확인해 주세요",
        "message": "접수 시스템의 위치 정보를 확인해 주세요. 삐 처리된 주소는 복원하지 않습니다.",
        "reason": "음성에서 가려진 주소를 모델이 추측하지 않습니다.",
        "conditions": ["접수 시스템 또는 대원이 확인한 위치"],
    },
    "HOLD_PAIR": {
        "category": "보류",
        "priority": 2,
        "title": "물질 조합 검토는 아직 보류입니다",
        "message": "사고물질과 시설물질의 CAS를 각각 확인해야 조합 규칙을 조회할 수 있습니다.",
        "reason": "두 CAS 확인은 규칙 실행 조건이며 진입·방수·대피 승인 조건이 아닙니다.",
        "conditions": ["사고물질 확인", "시설물질 확인"],
    },
    "REFERENCE_AVAILABLE": {
        "category": "대응 참고",
        "priority": 2,
        "title": "확인 물질의 근거를 찾았습니다",
        "message": "연결된 공식 자료 항목을 확인해 주세요. 제품·농도·상태와 기관 절차가 맞는지는 별도 확인이 필요합니다.",
        "reason": "이 카드는 자료 조회 안내입니다. 자료 내용의 현장 적용이나 특정 전술을 승인하지 않습니다.",
        "conditions": ["역할별 CAS 확인", "같은 CAS의 공식 출처", "상충 보고 없음"],
    },
    "NO_EVIDENCE": {
        "category": "보류",
        "priority": 2,
        "title": "사용할 근거가 부족합니다",
        "message": "제품 SDS와 해당 기관의 절차를 별도로 확인해 주세요.",
        "reason": "검색 결과 없음·검색 실패·출처 불일치는 생성 답변으로 채우지 않습니다.",
        "conditions": ["같은 CAS의 사용 가능한 공식 근거"],
    },
    "HOLD_CONFLICT": {
        "category": "보류",
        "priority": 1,
        "title": "새 근거를 확인한 뒤 다시 확인해 주세요",
        "message": "기존 확인과 새 정보가 충돌하거나 확인이 취소되었습니다. 기존 카드 대신 재확인이 필요합니다.",
        "reason": "새 증거의 의미를 완전 자동 검증하지 않습니다. Backend에서 확인 기록과 revision을 갱신해야 합니다.",
        "conditions": ["상충 해소", "갱신된 현장 확인 기록"],
    },
    "HOLD_FAILURE": {
        "category": "보류",
        "priority": 1,
        "title": "분석을 마치지 못했습니다",
        "message": "이전 결과를 현재 결과로 사용하지 마세요. 필요한 자료를 확인한 뒤 다시 요청해 주세요.",
        "reason": "제한시간·도구 오류·출력 검증 실패 시 추정한 대응을 표시하지 않습니다.",
        "conditions": ["현재 상태의 검증된 분석 결과"],
    },
    "HISTORY": {
        "category": "확인",
        "priority": 3,
        "title": "시설의 현재 보유 물질을 확인해 주세요",
        "message": "시설 검색은 과거 공개 이력입니다. 결과가 있어도 현재 재고나 현장 존재를 뜻하지 않습니다.",
        "reason": "이력이 없으면 물질이 없다고 결론 내릴 수 없습니다.",
        "conditions": ["현재 시설의 물질 확인 기록"],
    },
    "RULE_REFERENCE": {
        "category": "보류",
        "priority": 3,
        "title": "조합 규칙 조회와 전술 결정은 다릅니다",
        "message": "조합 규칙 상태를 별도 필드에서 확인해 주세요. 미지원 조합은 보류하며 현장 지휘 판단을 대신하지 않습니다.",
        "reason": "공개 CAMEO 규칙의 제한된 서수 결과이며 안전 확률이 아닙니다.",
        "conditions": ["제품·농도·상태·사고 조건", "기관 SOP 적용 확인", "전문 검수"],
    },
    "HANDOFF": {
        "category": "인계",
        "priority": 4,
        "title": "확인 내용과 남은 질문을 함께 인계하세요",
        "message": "확인 상태·물질 후보·근거 링크·미확인 항목을 구분해서 전달해 주세요.",
        "reason": "후보나 확인 대기 정보를 확정 사실로 인계하지 않습니다.",
        "conditions": ["현재 revision의 결과"],
    },
}
for _phrase_id, _entry in CATALOG.items():
    _entry.update(
        {
            "phrase_id": _phrase_id,
            "version": CATALOG_VERSION,
            "author": "프로젝트 모델 API 구현",
            "reviewer": None,
            "review_status": "DRAFT_NOT_EXPERT_REVIEWED",
            "source_document_id": "docs/ACTION_BRIEF.md",
            "section": _phrase_id,
            "source_kind": "PROJECT_AUTHORED_WORKFLOW_NOT_SOP",
            "excluded": ["자동 전술 지시", "전문 검수 또는 현장 적용 승인으로 표시"],
            "usage": "프로젝트 작성 문구; 외부 원문 재배포 없음",
            "freshness": "문구·정책 버전 고정; 공식 자료 최신성은 별도 확인",
        }
    )


def stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


CATALOG_SHA256 = stable_hash(CATALOG)
