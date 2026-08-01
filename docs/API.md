# 케미체크119 모델 API 계약

## 1. 기본 정보

| 항목 | 값 |
|---|---|
| 로컬 기본 주소 | `http://127.0.0.1:8000` |
| API schema | `chemiguard119-api-v1` |
| 서비스 ID | `chemicheck119-model-api` |
| 서비스명 | 케미체크119 |
| Swagger UI | `/docs` |
| OpenAPI JSON | `/openapi.json` |
| 인증 헤더 | `X-API-Key` |
| 요청 추적 헤더 | `X-Request-Id` |

모델 API는 데이터 저장용 CRUD 서버가 아닙니다. 사고 기록, 사용자 인증, 현장 확인 원본은 서비스
백엔드가 관리하고 모델 API에는 분석에 필요한 값만 전달합니다.

## 2. 인증

### 2.1 로컬 개발

로컬호스트에서만 익명 모드를 명시적으로 켤 수 있습니다.

```bash
CHEMIGUARD119_ALLOW_ANONYMOUS=true chemiguard119-api
```

### 2.2 운영

staging·production에서는 32바이트 난수의 64자리 hex 또는 43자리 base64url API Key를
배포 Secret으로 주입하고 모든 분석 POST 요청에 헤더를 포함합니다. hex 키는
`openssl rand -hex 32`로 생성할 수 있습니다.

```text
X-API-Key: 실제-배포-Secret
```

staging·production에서 익명 접근을 켜거나 올바른 API Key가 없으면 서비스는 fail-closed
상태가 되고 readiness가 실패합니다. 브라우저·태블릿 앱에 모델 API Key를 저장하지 말고
서비스 백엔드에서만 호출하세요.

## 3. 공통 응답 헤더

모든 응답에는 다음 헤더가 포함됩니다.

| 헤더 | 의미 |
|---|---|
| `X-Request-Id` | 요청 추적 ID |
| `X-API-Schema-Version` | API schema version |
| `X-Service-Id` | 모델 API 식별자 |
| `X-Content-Type-Options: nosniff` | MIME sniffing 차단 |
| `Cache-Control: no-store` | `/api/*` 응답 캐시 금지 |

클라이언트가 `X-Request-Id`를 전달할 수 있습니다. 허용 문자는 영문자, 숫자, `_ . : -`이며
최대 128자입니다. 이 값은 추적용이며 중복 요청을 막는 idempotency key가 아닙니다.

## 4. 엔드포인트 요약

| 메서드 | 경로 | 인증 | 설명 |
|---|---|---|---|
| `GET` | `/health/live` | 없음 | 프로세스 생존 여부 |
| `GET` | `/health/ready` | 없음 | runtime·인증·충돌 정책 준비 여부 |
| `GET` | `/api/v1/meta` | 없음 | 버전·정책·인증 방식·OpenAPI 위치 |
| `POST` | `/api/v1/incidents/analyze` | 필요 | 전체 사고 분석 |
| `POST` | `/api/v1/substances/discover` | 필요 | 관찰 정보 기반 확인 전 물질 후보·출처 검색 |
| `POST` | `/api/v1/substances/resolve` | 필요 | 물질 후보 검색 |
| `POST` | `/api/v1/evidence/search` | 필요 | KOSHA·CAMEO 근거 검색 |
| `POST` | `/api/v1/facilities/candidates` | 필요 | 시설 과거 취급 이력 후보 검색 |
| `POST` | `/api/v1/conflicts/review` | 필요 | 현장 확인된 두 물질 충돌 검토 |

로컬 익명 모드에서는 표의 “필요” 엔드포인트도 API Key 없이 호출할 수 있습니다.

## 5. Health와 메타데이터

### 5.1 `GET /health/live`

프로세스가 요청을 받을 수 있는지만 확인합니다. 모델 artifact가 준비되었다는 뜻은 아닙니다.

```json
{
  "status": "UP",
  "service": "chemicheck119-model-api",
  "service_name": "케미체크119",
  "version": "0.4.0"
}
```

### 5.2 `GET /health/ready`

다음을 함께 검사합니다.

- SQLite, Resolver, Retriever, config 존재
- 관찰 검색용 `substance_profile` 인덱스 존재와 프로필 수
- runtime manifest 무결성
- API 인증 구성
- `PUBLIC_SOURCE_PILOT_V1` 충돌 정책과 공개 검증 crosswalk 준비 상태

준비되면 HTTP `200`, 아니면 HTTP `503`입니다. 배포 플랫폼의 readiness probe는 이 경로를
사용해야 합니다.

정책 관련 공통 필드는 다음과 같습니다.

```json
{
  "rule_policy": "PUBLIC_SOURCE_PILOT_V1",
  "rule_policy_ready": true,
  "rule_policy_error": null,
  "expert_reviewed": false,
  "decision_support_only": true,
  "responder_confirmation_required": true,
  "material_discovery_capability": {
    "ready": true,
    "profile_count": 749,
    "minimum_profile_count": 700,
    "reason": null
  },
  "conflict_review_capability": {
    "policy_mode": "PUBLIC_SOURCE_PILOT_V1",
    "public_source_verified_crosswalk_count": 2,
    "eligible_public_source_cas_count": 2,
    "approved_crosswalk_count": 0,
    "approved_direct_rule_count": 0,
    "public_source_screening_ready": true,
    "expert_approved_decision_ready": false,
    "conflict_review_ready": true,
    "expert_reviewed": false,
    "direct_rules_enabled": false,
    "configuration_valid": true
  }
}
```

`conflict_review_ready=true`는 선택한 공개 근거 정책이 실행 가능하다는 뜻입니다.
`expert_approved_decision_ready=false`와 모순되지 않습니다.

### 5.3 `GET /api/v1/meta`

클라이언트가 API schema, pipeline schema, 인증 방식, confirmation gate, 충돌 정책을 확인하는
경로입니다. 프론트는 파일럿 라벨을 하드코딩하기보다 이 응답의 정책 정보를 함께 기록하는 것이
좋습니다.

`rule_policy`, `rule_policy_ready`, `rule_policy_error`, `expert_reviewed`,
`decision_support_only`, `responder_confirmation_required`,
`conflict_review_capability`를 `/health/ready`와 같은 의미로 제공합니다.

## 6. 전체 분석 API

### 6.1 `POST /api/v1/incidents/analyze`

백엔드의 기본 연동점입니다. 한 번의 요청 안에서 파서, Resolver, Retriever, 시설 이력 검색과
조건부 Rule Engine을 실행합니다.

### 6.2 1차 요청: 현장 확인 전

예시 파일: [`incident_unconfirmed_request.json`](../examples/api/incident_unconfirmed_request.json)

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents/analyze \
  -H "Content-Type: application/json" \
  --data @examples/api/incident_unconfirmed_request.json
```

주요 요청 필드는 다음과 같습니다.

| 필드 | 필수 | 설명 |
|---|---|---|
| `request_id` | 아니요 | 클라이언트 추적 ID |
| `incident_id` | 아니요 | 서비스 백엔드의 사고 ID |
| `input.type` | 아니요 | 기본 `MANUAL_TEXT` |
| `input.text` | 예 | 1~4,000자 신고 원문 |
| `input.occurred_at` | 아니요 | 시간대가 포함된 ISO 8601 권장 |
| `location` | 아니요 | 주소·시도·좌표·시설명 |
| `operations_context` | 아니요 | 출동 상태·현재 위치·BE가 조회한 도로 경로 |
| `planned_actions` | 아니요 | 검토 중인 대응, 최대 20개 |
| `evidence_top_k` | 아니요 | 1~10, 기본 5 |

`latitude`와 `longitude`는 함께 보내거나 모두 생략해야 합니다.
경로와 ETA는 모델이 추측하지 않습니다. `operations_context.route`는 BE가 서버 측 길찾기
사업자에서 받은 값만 전달하며, 없으면 `agent.map_context.route.status`가
`ROUTE_UNAVAILABLE`입니다.

현장 확인 전 응답의 핵심 형태는 다음과 같습니다. 아래는 구조를 설명하기 위해 일부 필드만
표시한 예입니다.

```json
{
  "schema_version": "chemiguard119-api-v1",
  "state": "AWAITING_SUBSTANCE_CONFIRMATION",
  "model_outputs": {
    "substance_candidates": [],
    "candidate_score_notice": "후보 점수는 위험확률이 아닙니다."
  },
  "grounded_rag": {
    "status": "NOT_RUN_REQUIRES_CONFIRMED_PAIR",
    "statements": [],
    "citations": []
  },
  "agent": {
    "phase": "INCIDENT_INTAKE",
    "current_objective": "신고 내용과 위치를 구조화해 출동 준비 정보를 만듭니다.",
    "next_actions": [],
    "workflow": [],
    "tool_executions": [],
    "map_context": {
      "coverage_scope": "NATIONWIDE_KOREA",
      "route": {
        "status": "RESPONDER_POSITION_REQUIRED",
        "eta_seconds": null,
        "progress_ratio_is_probability": false
      },
      "hazard_overlay_status": "NOT_COMPUTED_NO_VALIDATED_DISPERSION_MODEL"
    },
    "autonomous_risk_decision_allowed": false,
    "final_decision_authority": "현장 지휘관"
  },
  "conflict_review": {
    "executed": false,
    "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS"
  },
  "confirmation_gate": {
    "policy": "TWO_AUTHENTICATED_ON_SITE_CONFIRMATIONS_REQUIRED",
    "incident_confirmed": false,
    "facility_confirmed": false,
    "all_required_confirmed": false,
    "rule_execution_allowed": false
  },
  "required_next_steps": [],
  "safety_notice": "..."
}
```

후보가 한 개여도 API가 물질 존재를 확정한 것은 아닙니다. 서비스 백엔드는 대원의 확인 행위를
별도 레코드로 보관해야 합니다.

`agent.workflow`는 숨겨진 추론 과정이 아니라 실제 파서·검색·확인 게이트·CAMEO·RAG의
실행 상태입니다. 전국 지도와 이동 갱신의 전체 설명은
[전국 현장대응 에이전트와 지도 연동](OPERATIONS_AGENT_AND_MAP.md)을 참고합니다.

### 6.3 현장 확인 입력

두 확인 객체는 다음 계약을 만족해야 합니다.

| 필드 | 조건 |
|---|---|
| `confirmation_id` | 1~128자, 영문·숫자·`_ . : -`, 두 역할이 서로 달라야 함 |
| `cas_number` | 형식과 체크디지트가 유효한 CAS |
| `display_name` | 선택, 최대 160자 |
| `role` | `INCIDENT` 또는 `FACILITY` |
| `presence_status` | `CONFIRMED_PRESENT` 고정 |
| `confirmation_basis` | 아래 허용값 중 하나 |
| `observed_at` | 시간대 포함 ISO 8601, 서버 시각보다 5분을 초과해 미래일 수 없음 |

허용되는 `confirmation_basis`는 다음과 같습니다.

```text
CONTAINER_LABEL
SITE_MSDS
SHIPPING_DOCUMENT
INSTRUMENT_READING
RESPONDER_OBSERVATION
OTHER_VERIFIED_SOURCE
```

모델 API는 `confirmation_id`의 실제 사용자 권한을 조회하지 않습니다. 서비스 백엔드가 인증된
사용자의 확인 이벤트를 저장한 뒤 그 레코드 ID를 전달해야 합니다.

### 6.4 2차 요청: 두 물질 확인 후

예시 파일: [`incident_confirmed_request.json`](../examples/api/incident_confirmed_request.json)

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents/analyze \
  -H "Content-Type: application/json" \
  --data @examples/api/incident_confirmed_request.json
```

공개 검증 매핑이 있는 물질쌍의 충돌 결과에는 다음 계약이 유지됩니다.

```json
{
  "status": "SCREENING_COMPLETED",
  "scope": "PUBLIC_SOURCE_CAMEO_SCREENING",
  "policy_mode": "PUBLIC_SOURCE_PILOT_V1",
  "expert_reviewed": false,
  "risk_scale": {
    "type": "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
    "raw_class_id": 2,
    "is_probability": false,
    "probability_percent": null
  },
  "mapping_provenance": [
    {
      "role": "INCIDENT",
      "cas_number": "7681-52-9",
      "cameo_chemical_id": "4503",
      "selected_form": "SODIUM HYPOCHLORITE",
      "verification_status": "PUBLIC_SOURCE_VERIFIED",
      "verification_method": "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET",
      "evidence_url": "https://cameochemicals.noaa.gov/chemical/4503",
      "source_product": "NOAA/EPA CAMEO Chemicals",
      "source_version": "3.1.0 rev 1",
      "checked_at_utc": "2026-07-22T00:00:00+00:00"
    },
    {
      "role": "FACILITY",
      "cas_number": "7647-01-0",
      "cameo_chemical_id": "3598",
      "selected_form": "HYDROCHLORIC ACID, SOLUTION",
      "verification_status": "PUBLIC_SOURCE_VERIFIED",
      "verification_method": "EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET",
      "evidence_url": "https://cameochemicals.noaa.gov/chemical/3598",
      "source_product": "NOAA/EPA CAMEO Chemicals",
      "source_version": "3.1.0 rev 1",
      "checked_at_utc": "2026-07-22T00:00:00+00:00"
    }
  ],
  "evidence_provenance": {
    "basis": "PUBLIC_OFFICIAL_SOURCE",
    "source_product": "NOAA/EPA CAMEO Chemicals",
    "source_versions": ["3.1.0 rev 1"],
    "mapping_evidence_urls": [
      "https://cameochemicals.noaa.gov/chemical/4503",
      "https://cameochemicals.noaa.gov/chemical/3598"
    ],
    "compatibility_evidence_urls": [
      "https://cameochemicals.noaa.gov/reactivity"
    ]
  },
  "human_confirmation_required": true,
  "final_decision": "현장 지휘관 판단"
}
```

`raw_class_id`는 `0`, `1`, `2` 중 하나인 CAMEO 서수 class입니다. 화면에서 백분율로
변환하면 안 됩니다. `mapping_provenance`는 사고물질·시설물질 두 매핑의 CAS, CAMEO ID,
선택한 물질 형태, 검증 상태·방법, URL, 출처 버전과 확인 시각을 제공합니다.

공개 검증 매핑이 없으면 `VERIFY_REQUIRED`, 그룹 결과가 없거나 미매핑이면
`UNCLASSIFIED`가 될 수 있습니다. 이 경우 등급을 임의 생성하지 않습니다.

### 6.5 근거 제한형 RAG 응답

두 CAS가 현장에서 확인되고 Rule 결과가 완료된 경우에만 `grounded_rag`가 설명을
반환합니다. UI는 `statements[].text`와 해당 `source_ids`의 `citations[].source_urls`만
연결해 간단한 “대응 근거” 카드로 표시하면 됩니다.

```json
{
  "grounded_rag": {
    "schema_version": "chemicheck119-grounded-rag-v1",
    "status": "FALLBACK_EXTRACTIVE",
    "mode": "extractive",
    "used_llm": false,
    "statements": [
      {
        "text": "공개 CAMEO 근거에서 높은 충돌 위험이 확인됐습니다.",
        "source_ids": ["RULE_RESULT"]
      }
    ],
    "citations": [
      {
        "source_id": "RULE_RESULT",
        "source_type": "CAMEO_RULE_ENGINE",
        "title": "확인된 두 물질의 CAMEO 충돌 스크리닝",
        "source_urls": ["https://cameochemicals.noaa.gov/reactivity"]
      }
    ],
    "risk_decision_source": "DETERMINISTIC_CAMEO_RULE_ENGINE",
    "semantic_grounding_verified": false,
    "fallback_reason": "EXTRACTIVE_MODE"
  }
}
```

| `status` | 의미 |
|---|---|
| `COMPLETED` | 선택 LLM이 만든 요약이 인용 ID·위험등급 검사를 통과 |
| `FALLBACK_EXTRACTIVE` | LLM 미사용·실패·검증 실패로 공식 근거를 그대로 조립 |
| `DISABLED` | RAG 기능을 명시적으로 끔 |
| `NO_GROUNDED_EVIDENCE` | 표시할 공식 근거가 없음 |
| `NOT_RUN_REQUIRES_CONFIRMED_PAIR` | 두 물질의 현장 확인 전이라 미실행 |
| `NOT_RUN_RULE_NOT_COMPLETED` | Rule이 미분류·추가 확인 상태라 미실행 |

`semantic_grounding_verified=false`는 인용 ID가 존재함을 검사했지만 문장 의미 전체를 자동으로
과학 검증했다고 주장하지 않는다는 뜻입니다. RAG의 문장을 위험 판정으로 사용하면 안 되며
`conflict_review`가 유일한 위험등급 원본입니다.

전체 분석 응답에서는 같은 정책 정보가 `provenance.rule_policy`,
`provenance.expert_reviewed`, `provenance.decision_support_only`,
`provenance.responder_confirmation_required`, `provenance.conflict_review_capability`에
기록됩니다.

## 7. 관찰 정보 기반 물질 탐색

### `POST /api/v1/substances/discover`

정확한 물질명·CAS뿐 아니라 상태·색상·냄새·용도 같은 관찰 표현에서 공개자료 기반 후보를
찾습니다. 성상 검색은 일반어를 제외하고 서로 다른 물성 영역이 최소 두 개 일치할 때만 후보를
반환합니다.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/substances/discover \
  -H "Content-Type: application/json" \
  --data @examples/api/material_discovery_request.json
```

요청:

```json
{
  "query": "무색 투명하고 박하 냄새가 나는 휘발성 액체",
  "top_k": 3,
  "evidence_top_k": 3
}
```

| 필드 | 범위 | 의미 |
|---|---:|---|
| `query` | 2~500자 | 물질명·CAS 또는 구별되는 관찰 정보 |
| `top_k` | 1~5 | 후보 최대 수 |
| `evidence_top_k` | 1~5 | 후보별 공식 근거 카드 최대 수 |

주요 응답 필드:

| 필드 | 의미 |
|---|---|
| `status` | 후보 발견, 신뢰할 후보 없음, 프로필 인덱스 미준비 |
| `search_mode` | 명칭 검색, 성상 검색 또는 결합 검색 |
| `matched_properties` | 질의와 일치한 상태·색상·냄새·용도 |
| `property_profile` | 소방청 공개자료의 성상과 출처 |
| `evidence` | 같은 CAS로 제한한 KOSHA·CAMEO 공식 문서 카드 |
| `evidence_status` | 상세 근거 검색 상태 |
| `evidence_warning` | 검색 순위·미적재 상태에 대한 안전 경고 |
| `evidence_notice` | 외부 원문 확인 등 후속 조치 안내 |
| `cas_link_warning` | CAMEO–CAS 연결 검증 상태 경고 |
| `evidence[].cas_link_status` | 개별 근거의 CAS 연결 검증 상태 |
| `requires_responder_confirmation` | 항상 `true` |
| `rule_eligible` | 항상 `false` |
| `risk_determination_allowed` | 항상 `false` |
| `candidate_score_is_probability` | 항상 `false` |

`CAS_EVIDENCE_NOT_LOADED`는 후보가 안전하다는 뜻이 아니라 해당 CAS의 상세 KOSHA·CAMEO
근거가 현재 artifact에 없다는 뜻입니다. 클라이언트는 `evidence_warning`,
`evidence_notice`, `cas_link_warning`을 숨기지 않아야 합니다. 후보 순위만으로 현장 물질을
확정하지 않습니다.

`NO_RELIABLE_CANDIDATE`도 물질이 없거나 안전하다는 뜻이 아닙니다. 현재 749개 프로필 밖의
물질이거나 관찰 표현이 부족할 수 있으므로, 화면은 “관찰 정보 보강 또는 외부 공식 MSDS
확인”을 안내해야 하며 위험 부재로 표시하면 안 됩니다.

전체 예시:
[`material_discovery_response.json`](../examples/api/material_discovery_response.json)

## 8. 물질명·CAS 후보 검색

### `POST /api/v1/substances/resolve`

요청:

```json
{
  "query": "아세톤",
  "top_k": 3
}
```

- `query`: 1~200자
- `top_k`: 1~10

응답에서 확인할 필드는 다음과 같습니다.

| 필드 | 의미 |
|---|---|
| `status` | 정확 일치, 모호 후보, 퍼지 후보, 미해결 등 검색 상태 |
| `input_class` | 식별자·공식명·일반명 등 입력 분류 |
| `candidates` | CAS별 후보 목록 |
| `score` | 후보 정렬값, 확률이 아님 |
| `requires_responder_confirmation` | 항상 `true` |
| `rule_eligible` | API 후보 결과에서는 `false` |
| `risk_determination_allowed` | `false` |

등록되지 않은 제품명이나 현장 속칭은 임의 CAS로 확정하지 않으며 `UNRESOLVED`가 될 수
있습니다.

긴 신고문에서 별칭을 찾을 때는 독립된 원문 경계를 요구합니다. `염산 누출`과
`염산이 누출됨`은 염산 후보가 될 수 있지만, `염산염`·`염산성` 안의 부분 문자열은
염산 CAS 자동 힌트가 되지 않습니다. 이 경우 후보 누락보다 잘못된 동일-CAS 근거 제한을
피하는 것을 우선합니다.

## 9. 공식 근거 검색

### `POST /api/v1/evidence/search`

CAS를 대원이 확인한 경우:

```json
{
  "query": "산과 접촉할 때 반응",
  "cas_hint": "7681-52-9",
  "cas_hint_status": "RESPONDER_CONFIRMED",
  "top_k": 5
}
```

Resolver 후보를 검색 힌트로만 사용하는 경우:

```json
{
  "query": "아세톤 화재 위험",
  "cas_hint": "67-64-1",
  "cas_hint_status": "RESOLVER_CANDIDATE",
  "top_k": 5
}
```

`cas_hint`와 `cas_hint_status`는 함께 보내거나 모두 생략해야 합니다. 후보 기반 검색 결과는
Rule 입력이 아니며 `risk_determination_allowed=false`입니다. 해당 CAS의 상세 근거가 로드되지
않았다면 `CAS_EVIDENCE_NOT_LOADED`를 반환하고 다른 CAS 문서로 대체하지 않습니다.

## 10. 시설 이력 후보 검색

### `POST /api/v1/facilities/candidates`

```json
{
  "query": "예시 사업장",
  "province": "경기도",
  "top_k": 10
}
```

- `query`: 2~300자
- `province`: 선택, 최대 80자
- `top_k`: 1~50

결과의 `evidence_class`는 `REPORTED_HANDLING_HISTORY`이며 다음 값이 고정됩니다.

```json
{
  "current_inventory_confirmed": false,
  "rule_eligible": false,
  "requires_on_site_confirmation": true
}
```

과거 취급 이력을 현재 재고·보유량·저장 위치로 표시해서는 안 됩니다.

## 11. 충돌 검토 단독 호출

### `POST /api/v1/conflicts/review`

이미 현장 확인 레코드 두 개가 있고 전체 파서·검색 흐름이 필요하지 않을 때 사용합니다.

```json
{
  "incident": {
    "confirmation_id": "CFM-INC-0001",
    "cas_number": "7681-52-9",
    "display_name": "차아염소산나트륨",
    "role": "INCIDENT",
    "presence_status": "CONFIRMED_PRESENT",
    "confirmation_basis": "CONTAINER_LABEL",
    "observed_at": "2026-01-15T14:25:00+09:00"
  },
  "facility": {
    "confirmation_id": "CFM-FAC-0001",
    "cas_number": "7647-01-0",
    "display_name": "염산",
    "role": "FACILITY",
    "presence_status": "CONFIRMED_PRESENT",
    "confirmation_basis": "SITE_MSDS",
    "observed_at": "2026-01-15T14:27:00+09:00"
  },
  "planned_actions": [
    {
      "raw_text": "배수로 유입 차단 검토"
    }
  ]
}
```

API는 두 현장 확인 게이트를 통과한 뒤 `PUBLIC_SOURCE_PILOT_V1`로 조회합니다. 전문가 검토는
실행 조건이 아니지만 응답에는 `expert_reviewed=false`가 명시됩니다.

완료 결과의 `reference_assurance`는 공식근거 증빙 범위를 별도로 제공합니다.

```json
{
  "status": "REFERENCE_TRIANGULATED",
  "reference_count": 5,
  "independent_authority_count": 4,
  "expert_reviewed": false,
  "human_expert_substitute": false,
  "claim_checks": [
    {"claim": "PAIR_REACTIVITY_SCREENING", "status": "PASSED"},
    {"claim": "CURRENT_SITE_INVENTORY", "status": "NOT_PROVEN"},
    {"claim": "ACTUAL_MIXING_AND_FIELD_CONDITIONS", "status": "NOT_PROVEN"}
  ]
}
```

현재 차아염소산나트륨–염산과 금속 나트륨–염산 2개 조합이
`REFERENCE_TRIANGULATED`이며 다른 13개는
`PRIMARY_AUTHORITY_ONLY`입니다. registry 누락·변조 또는 생성물 불일치는 완료 결과가 아니라
Rule `VERIFY_REQUIRED`로 반환됩니다.

최상위 응답에도 `rule_policy`, `expert_reviewed`, `decision_support_only`,
`responder_confirmation_required`, `conflict_review_capability`가 포함되고, 실제 스크리닝
내용은 `result`에 들어갑니다.

## 12. 오류 형식

모든 표준 오류는 같은 envelope를 사용합니다.

```json
{
  "schema_version": "chemiguard119-api-v1",
  "service_name": "케미체크119",
  "error": {
    "code": "INVALID_SCHEMA",
    "message": "요청 JSON이 API 스키마를 만족하지 않습니다.",
    "retryable": false,
    "fields": [
      "body.input.text"
    ]
  },
  "request_id": "REQ-...",
  "occurred_at_utc": "2026-01-15T05:30:00+00:00"
}
```

| HTTP | 대표 상황 | 재시도 판단 |
|---|---|---|
| `401` | API Key 없음·불일치 | 키 수정 전 재시도 금지 |
| `422` | 요청 schema·CAS·역할 오류 | 요청 수정 후 재시도 |
| `500` | 출력 안전 검증 또는 내부 오류 | 같은 `request_id`로 운영자 확인 |
| `503` | artifact·manifest·인증 구성 미준비 | readiness 복구 후 재시도 |

`AWAITING_SUBSTANCE_CONFIRMATION`, `VERIFY_REQUIRED`, `UNCLASSIFIED`는 정상적인 업무 상태일
수 있으며 HTTP 오류와 구분해야 합니다.

`UNCONFIRMED_RISK_OUTPUT_BLOCKED`는 현장 확인 두 건이 없는데 내부 출력에 위험등급·반응·
완료 상태가 섞였거나, 누락된 확인 역할과 상태가 모순될 때 반환하는 fail-closed `500`
오류입니다. 클라이언트는 이 응답을 후보 또는 정상 결과로 표시하지 않고 운영자가 같은
`request_id`를 조사하게 해야 합니다.

## 13. 프론트·백엔드 구현 규칙

1. Resolver 첫 후보를 자동 확정하지 않습니다.
2. 백엔드가 인증된 현장 확인 레코드를 만든 후 확인 객체를 전송합니다.
3. UI는 `confirmation_gate.all_required_confirmed=true`이고
   `conflict_review.executed=true`일 때만 `risk_level_ko`를 표시할 수 있습니다.
4. 서수 등급을 백분율로 바꾸지 않고 `LOW`를 안전 보장으로 표현하지 않습니다.
5. `expert_reviewed=false`와 공개 근거 파일럿 라벨을 결과 근처에 표시합니다.
6. `mapping_provenance`와 `evidence_provenance`를 “대응 근거”에서 확인할 수 있게 합니다.
7. `reference_assurance.status`를 “공식근거 교차확인” 또는 “CAMEO 단일체계 근거”로 표시합니다.
8. `NOT_PROVEN` 항목과 `human_expert_substitute=false`를 숨기지 않습니다.
9. 시설 이력은 “과거 공개 이력 후보”로 표시합니다.
10. `required_next_steps`와 업무 상태를 사용자에게 그대로 전달합니다.
11. `X-Request-Id`, `analysis_id`, `incident_id`를 함께 기록해 장애를 추적합니다.
12. 물질 탐색 후보는 `현장 물질 확인` 이후에만 확인 객체로 변환합니다.
13. 기록 저장은 BE 성공 응답 뒤 화면을 초기화합니다.

## 14. TypeScript 호출 예시

```ts
type AnalyzeRequest = Record<string, unknown>;

export async function analyzeIncident(
  baseUrl: string,
  apiKey: string,
  payload: AnalyzeRequest,
) {
  const response = await fetch(`${baseUrl}/api/v1/incidents/analyze`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": apiKey,
      "X-Request-Id": crypto.randomUUID(),
    },
    body: JSON.stringify(payload),
  });

  const body = await response.json();
  if (!response.ok) {
    throw new Error(`${body.error?.code ?? "UNKNOWN"}: ${body.error?.message ?? "API 오류"}`);
  }
  return body;
}
```

API Key는 서버 환경변수나 Secret Manager에서 읽어야 하며 프론트 번들에 포함하면 안 됩니다.

## 15. 관련 문서

- [README](../README.md)
- [아키텍처](ARCHITECTURE.md)
- [데이터와 모델](DATA_AND_MODEL.md)
- [공식근거 교차검증](EVIDENCE_ASSURANCE.md)
- [배포](DEPLOYMENT.md)
- [안전 및 한계](SAFETY_AND_LIMITATIONS.md)
- [대시보드 적용 흐름](DASHBOARD_FLOW.md)
