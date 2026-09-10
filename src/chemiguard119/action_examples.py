"""공개 가능한 합성 입력만 포함한다. 실제 신고·사람 검수 기록이 아니다."""

from copy import deepcopy

UNCONFIRMED = {
    "revision": 1,
    "analysis": {
        "request_id": "REQ-DEMO-1",
        "incident_id": "INC-SYNTHETIC-1",
        "input": {
            "type": "VOICE_TRANSCRIPT",
            "text": "차아염소산나트륨 탱크에서 누출이 있고 옆 저장고에는 염산이 있습니다.",
        },
        "evidence_top_k": 5,
    },
}


def confirmation(role: str, cas: str) -> dict:
    return {
        "confirmation_id": f"CNF-DEMO-{role}",
        "cas_number": cas,
        "role": role,
        "presence_status": "CONFIRMED_PRESENT",
        "confirmation_basis": "CONTAINER_LABEL",
        "observed_at": "2026-09-01T00:00:00+09:00",
    }


ONE_CONFIRMED = deepcopy(UNCONFIRMED)
ONE_CONFIRMED["revision"] = 2
ONE_CONFIRMED["analysis"]["confirmed_incident_substance"] = confirmation(
    "INCIDENT", "7681-52-9"
)
BOTH_CONFIRMED = deepcopy(ONE_CONFIRMED)
BOTH_CONFIRMED["revision"] = 3
BOTH_CONFIRMED["analysis"]["confirmed_facility_substance"] = confirmation(
    "FACILITY", "7647-01-0"
)
CANCELLED = deepcopy(BOTH_CONFIRMED)
CANCELLED["revision"] = 4
CANCELLED["invalidated_confirmation_ids"] = ["CNF-DEMO-INCIDENT"]
CONFLICT = deepcopy(BOTH_CONFIRMED)
CONFLICT["revision"] = 4
CONFLICT["reported_evidence_conflict"] = True
NO_EVIDENCE = deepcopy(UNCONFIRMED)
NO_EVIDENCE["analysis"]["input"]["text"] = "미상 제품의 정보 확인 요청"
NO_EVIDENCE["analysis"]["confirmed_incident_substance"] = confirmation(
    "INCIDENT", "7732-18-5"
)
INVALID_CAS = deepcopy(ONE_CONFIRMED)
INVALID_CAS["analysis"]["confirmed_incident_substance"]["cas_number"] = "7681-52-0"
EXAMPLES = {
    key: {"summary": title, "value": value}
    for key, title, value in [
        ("unconfirmed", "합성 예시: 미확인", UNCONFIRMED),
        ("one_confirmed", "합성 예시: 사고물질만 확인", ONE_CONFIRMED),
        ("both_confirmed", "합성 예시: 두 역할 확인", BOTH_CONFIRMED),
        ("cancelled", "합성 예시: 확인 취소 후 재분석", CANCELLED),
        ("conflict", "합성 예시: 새 근거 상충 보고", CONFLICT),
        (
            "no_evidence",
            "합성 예시: 인덱스의 해당 CAS 근거가 없을 수 있음",
            NO_EVIDENCE,
        ),
        ("invalid_cas", "오류 예시: CAS checksum 오류(422)", INVALID_CAS),
    ]
}
