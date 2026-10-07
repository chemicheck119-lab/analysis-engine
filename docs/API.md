# 케미체크119 모델 API 명세서

> Backend 개발자용 · 전사문을 물질 후보·공식 근거·확인·보류·인계 카드로 바꾸는 API

**기준일: 2026-09-11 · 서비스 0.4.0 · 12개 endpoint.** 서비스 사실 상태는
**부분 구현 또는 개발용 데모**입니다. 문구 전문 검수·현장 안전성·상용 운영을 보장하지 않습니다.
이 문서는 `analysis-engine`의 계약이며 `back`의 인증·사고 CRUD나 `speech-service`의 음성
업로드 API 명세가 아닙니다. 모델 API는 사용자 로그인·사고 영구 저장을 수행하지 않습니다.

| 먼저 필요한 것 | 바로가기 |
|---|---|
| 기능별 API 선택 | [전체 API 목록](#endpoint-index) |
| 실제 GCP 연결과 테스트 | [인증](#authentication), [Swagger·Postman·curl 시작](#quick-test) |
| 화면에 보여줄 카드 만들기 | [행동 카드 JSON·SSE](#action-brief-contract) |
| 필수 필드·타입을 기계적으로 확인 | [OpenAPI 원본](../contracts/generated/model-api-v1.openapi.json) |
| 클릭해서 요청·응답 예시 읽기 | [공개 Swagger](https://chemicheck119-api-docs-w6s6lwanpa-du.a.run.app/) — 읽기 전용 |
| Postman에서 합성 요청 실행 | [Collection JSON](../examples/api/chemicheck119-model-api.postman_collection.json) — 키 없음 |
| 장애 응답·Backend 책임 | [오류 계약](#error-contract), [상태 교체·재시도](#state-and-timeout) |

명세 기준 OpenAPI SHA-256:
`684fdcb91cda55056ed16ef8b916b2433a837627c5a72371792c208d8a239856`.
서비스 배포 코드 `0533371523b53470ae19ec1e9a04586858111fc3`와 공개 문서 배포 commit은
별개입니다. [모델 배포 기록](https://github.com/chemicheck119-lab/analysis-engine/issues/68),
[문서 배포 기록](https://github.com/chemicheck119-lab/analysis-engine/issues/69)을 구분하세요.
이 명세 추가는 모델 재학습·교체가 아닙니다. OpenAPI에는 표현되지 않는 조건부 필수값은
아래 표와 실제 Pydantic validator를 함께 따릅니다.

## 1. 기본 정보

| 항목 | 값 |
|---|---|
| 실제 모델 Base URL | `https://chemicheck119-model-api-preview-w6s6lwanpa-du.a.run.app` — 비공개 IAM |
| 공개 문서 URL | `https://chemicheck119-api-docs-w6s6lwanpa-du.a.run.app/` — 분석 호출 대상 아님 |
| 로컬 기본 주소 | `http://127.0.0.1:8000` |
| 기존 분석·오류 schema | `chemiguard119-api-v1` |
| 행동 카드 body schema | `action-brief-v1` |
| Agent step body schema | `chemicheck119-incident-agent-v1` |
| 서비스 ID | `chemicheck119-model-api` |
| 서비스명 | 케미체크119 |
| Swagger UI | `/docs` |
| OpenAPI JSON | `/openapi.json` |
| 인증 헤더 | `X-API-Key` |
| 요청 추적 헤더 | `X-Request-Id` |
| 전송 형식 | POST `Content-Type: application/json`, UTF-8; stream 응답만 `text/event-stream` |

모델 API는 데이터 저장용 CRUD 서버가 아닙니다. 사고 기록, 사용자 인증, 현장 확인 원본은 서비스
백엔드가 관리하고 모델 API에는 분석에 필요한 값만 전달합니다.

<a id="authentication"></a>

## 2. 인증

### GCP는 두 단계 인증

| 계층 | 무엇을 보내는가 | 누가 관리하는가 |
|---|---|---|
| Cloud Run IAM | `Authorization: Bearer <Google 서명 ID token>` | 호출 Backend의 서비스 계정. 대상 서비스의 `roles/run.invoker` 필요 |
| 모델 애플리케이션 | `X-API-Key: <모델 키>` | Backend의 Secret. 모든 분석 POST에서 필요 |

서비스 간 ID token의 audience는 **실제 모델 Base URL**입니다. 공개 문서 URL이나
`/docs`를 audience로 넣지 않습니다. 사용자 로그인 JWT·OAuth access token을 대신 보내지
않습니다. 실제 서비스에서는 Google 인증 라이브러리로 만료 전에 갱신하고 서비스 계정 키
JSON을 브라우저·Git에 넣지 않습니다.
[Google의 서비스 간 인증 안내](https://docs.cloud.google.com/run/docs/authenticating/service-to-service)

Health·meta·`/docs`는 앱 API 키가 없어도 되지만 **배포된 비공개 Cloud Run의 IAM은
여전히 필요**합니다. API 키만 있어도 IAM 없이는 접근할 수 없습니다. 이 문서의 키 표시는
placeholder이며 실제 키나 ID token은 제공하지 않습니다.

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

애플리케이션 응답의 공통 헤더입니다. Cloud Run·프록시가 앱 진입 전에 차단한 응답에는
아래 헤더나 JSON 오류 envelope가 없을 수 있습니다.

| 헤더 | 의미 |
|---|---|
| `X-Request-Id` | 요청 추적 ID |
| `X-API-Schema-Version` | API schema version |
| `X-Service-Id` | 모델 API 식별자 |
| `X-Content-Type-Options: nosniff` | MIME sniffing 차단 |
| `Cache-Control: no-store` | `/api/*` 응답 캐시 금지 |

클라이언트가 `X-Request-Id`를 전달할 수 있습니다. 허용 문자는 영문자, 숫자, `_ . : -`이며
최대 128자입니다. 이 값은 추적용이며 중복 요청을 막는 idempotency key가 아닙니다.
body의 `request_id`가 헤더보다 우선하므로 동일한 값을 보내세요. brief·step에서는
`analysis.request_id`입니다. 유효하지 않은 추적 헤더는 새 ID로 대체될 수 있고 잘못된 body ID는
`422`입니다. 행동 카드 응답도 헤더는 `chemiguard119-api-v1`, body는 `action-brief-v1`이며
서로 다른 버전 계층입니다. 입력 모델은 알 수 없는 필드를 거부하고 문자열 양끝 공백을 제거합니다.
STT 원본은 Backend가 별도로 보존하세요. `null`, 필드 생략, 빈 문자열을 임의로 같은 값으로 바꾸지 않습니다.

<a id="endpoint-index"></a>

## 4. 엔드포인트 요약

| 메서드 | 경로 | 인증 | 설명 |
|---|---|---|---|
| `GET` | `/health/live` | 없음 | 프로세스 생존 여부 |
| `GET` | `/health/ready` | 없음 | runtime·인증·충돌 정책 준비 여부 |
| `GET` | `/api/v1/meta` | 없음 | 버전·정책·인증 방식·OpenAPI 위치 |
| `POST` | `/api/v1/agents/incidents/brief` | 필요 | **화면 연동 권장**: 확인·근거·보류·인계 카드 최종 JSON |
| `POST` | `/api/v1/agents/incidents/brief/stream` | 필요 | 같은 결과를 initial/final 전체 snapshot으로 순차 전달 |
| `POST` | `/api/v1/incidents/analyze` | 필요 | 전체 사고 분석 |
| `POST` | `/api/v1/agents/incidents/step` | 필요 | 상태 기반 도구 선택·재계획 |
| `POST` | `/api/v1/substances/discover` | 필요 | 관찰 정보 기반 확인 전 물질 후보·출처 검색 |
| `POST` | `/api/v1/substances/resolve` | 필요 | 물질 후보 검색 |
| `POST` | `/api/v1/evidence/search` | 필요 | KOSHA·CAMEO 근거 검색 |
| `POST` | `/api/v1/facilities/candidates` | 필요 | 시설 과거 취급 이력 후보 검색 |
| `POST` | `/api/v1/conflicts/review` | 필요 | 현장 확인된 두 물질 충돌 검토 |

로컬 익명 모드에서는 표의 “필요” 엔드포인트도 API Key 없이 호출할 수 있습니다.
표의 인증은 **앱 API 키** 기준입니다. GCP IAM은 모든 경로에 별도로 적용됩니다.

새 카드 화면은 `brief` 또는 `brief/stream` **하나부터** 연결하세요. 모든 개별 API를 순서대로
호출할 필요는 없습니다. `analyze`는 상세 분석 DTO, `step`은 외부 memory 기반 도구 조율이
필요한 별도 연동점입니다. brief가 step memory를 받거나 여러 LLM을 병렬 호출하는 구조는 아닙니다.

<a id="quick-test"></a>

### 4.1 Swagger·Postman·curl로 테스트

1. GCP 호출 권한이 있는 계정으로 로그인합니다. 팀원에게 권한이 없다면 담당자에게 요청하세요.
   다음 명령은 로그인·권한 부여를 대신하지 않습니다.
2. 터미널에서 아래 **인증 프록시**를 실행하고 켜 둡니다. 해당 포트가 이미 사용 중이면 다른
   포트로 바꾸세요. `127.0.0.1`은 연결 입구이고 실제 모델 계산은 GCP에서 수행됩니다.

```bash
gcloud run services proxy chemicheck119-model-api-preview \
  --project=chemi-check --region=asia-northeast3 --port=8087
```

3. `http://127.0.0.1:8087/health/ready`의 HTTP 200·`READY`를 확인합니다.
4. `http://127.0.0.1:8087/docs` → Authorize에 모델 키 입력 → `brief` 펼치기 →
   **합성 예시: 미확인** → Try it out → Execute. 공개 Swagger에서는 실행할 수 없습니다.

모델 키는 권한 있는 담당자가 Secret Manager에서 안전하게 전달합니다. 현재 배포가 참조하는
Secret은 `chemicheck119-model-api-key` 버전 `1`이며, 회전 시 실제 배포 참조를 재확인합니다.
키를 FE·소스코드·이슈·스크린샷에 포함하지 마세요.

**Postman**: 위 Collection JSON을 Import한 뒤 개인 환경에서 `base_url`을
`http://127.0.0.1:8087`, `api_key`를 모델 키로 설정합니다. 기본 collection에는 비밀 값이
없습니다. GET은 앱 키를 사용하지 않습니다. `Authorization` 헤더는 프록시가 붙이므로 기본
비활성 상태로 두세요. Cloud Run 직접 호출을 선택할 때만 실제 모델 Base URL과 유효한
`id_token`을 설정하고 각 요청의 비활성 Authorization 헤더를 켭니다. 키·토큰이 포함된
환경/collection을 팀 공유·클라우드 동기화·재export하지 마세요.

18개 합성 요청이 준비되어 있습니다. 개별 **Send**로 확인하며 일괄 반복·부하 테스트를
기본 실행하지 않습니다. Postman의 test script는 HTTP·카드 계약·일부 Gate만 검사합니다.
GUI 실행 검증·현장 정확도·전문 검수 완료 증명이 아닙니다. SSE의 이벤트 소비는 아래 CLI를 사용합니다.

**curl**: 별도 터미널의 환경변수 `CHEMIGUARD119_API_KEY`에 안전하게 키가 주입된 상태에서
아래 합성 요청을 사용합니다. 키 값을 명령행에 직접 쓰지 마세요.

```bash
curl --silent --show-error --fail-with-body --max-time 35 \
  http://127.0.0.1:8087/api/v1/agents/incidents/brief \
  -H "X-API-Key: $CHEMIGUARD119_API_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"revision":1,"analysis":{"request_id":"REQ-DEMO-1","incident_id":"INC-SYNTHETIC-1","input":{"type":"VOICE_TRANSCRIPT","text":"차아염소산나트륨 탱크에서 누출이 있고 옆 저장고에는 염산이 있습니다."},"evidence_top_k":5}}'
```

`curl -v`·HTTP debug 로그에 인증 헤더를 남기지 마세요. 이후 장의 8000 포트 예시는 별도로
띄운 **로컬 모델 서버** 기준입니다. GCP 테스트는 8087 프록시로 바꾸고 X-API-Key를 추가합니다.

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
- Resolver schema와 `resolver_training_metadata` source-adaptation provenance

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
`conflict_review_capability`를 `/health/ready`와 같은 의미로 제공합니다. 추가로
`incident_agent_capability`에서 agent endpoint, planner 방식, 외부 memory 방식, 도구 수와
자율 위험판정 금지 여부를 확인할 수 있습니다.

## 6. 행동 카드와 전체 분석 API

<a id="action-brief-contract"></a>

### 6.0 `POST /api/v1/agents/incidents/brief`

**용도:** 지금 확인할 정보·자료 링크·보류 이유·인계 내용을 짧은 한국어 카드로 반환합니다.
입력 snapshot 단위로 계산하며 대화·사고 상태를 서버에 저장하지 않습니다. 응답은
`BriefResponse`이고 `phase=final`입니다. 전술 생성 LLM은 사용하지 않습니다.

#### 요청 · BriefRequest

| 필드 | 타입 | 필수·기본값 | 제약·의미 |
|---|---|---|---|
| `analysis` | `IncidentAnalyzeRequest` | 필수 | 기존 상세 분석 요청 그대로 |
| `revision` | integer | 필수 | 0 이상. Backend가 관리하는 사고 정보 버전 |
| `invalidated_confirmation_ids` | string[] | `[]` | 최대 2개, 각 1~128자. 해당 ID 확인을 이번 요청에서 제외 |
| `reported_evidence_conflict` | boolean | `false` | true면 양쪽 확인을 보류. false는 상충 없음의 증명이 아님 |
| `analysis.incident_id` | string | **brief에서는 필수** | 1~128자, 영문·숫자·`_ . : -`. analyze의 선택 조건과 다름 |
| `analysis.request_id` | string 또는 null | 선택 | 같은 ID 제약. 새 요청마다 새로운 추적 ID 권장 |
| `analysis.input` | `IncidentInput` | 필수 | `text`: 1~4,000자, `type`: 아래 값 중 하나 |
| `analysis.input.type` | enum | `MANUAL_TEXT` | `MANUAL_TEXT`, `DISPATCH_TEXT`, `VOICE_TRANSCRIPT`, `STRUCTURED_FORM` |
| `analysis.input.occurred_at` | datetime 또는 null | 선택 | 신고 시각. 시간대 포함 ISO 8601 권장 |
| `analysis.evidence_top_k` | integer | `5` | 1~10 |
| `analysis.confirmed_incident_substance` | `ConfirmedSubstanceInput` 또는 null | 선택 | 사고물질 확인 기록. `role=INCIDENT` |
| `analysis.confirmed_facility_substance` | 같은 타입 또는 null | 선택 | 시설물질 확인 기록. `role=FACILITY` |
| `analysis.location` | `IncidentLocation` 또는 null | 선택 | Backend가 제공한 위치. 삐 처리 주소 복원 불가 |
| `analysis.operations_context` | `OperationsContext` 또는 null | 선택 | Backend의 출동·경로 정보. API가 길찾기를 대신 호출하지 않음 |
| `analysis.planned_actions` | `{raw_text: string}[]` | `[]` | 최대 20개, 문장 1~120자. 입력했다고 전술 승인되지 않음 |

확인 객체의 모든 필드와 검증은 [6.3 현장 확인 입력](#63-현장-확인-입력)을 따릅니다.
두 **확인 ID는 달라야** 하지만 두 역할의 CAS까지 반드시 달라야 하는 것은 아닙니다.
위치·출동 객체의 전체 중첩 필드·enum은 OpenAPI의 `IncidentLocation`·`OperationsContext`를
사용하세요. 위도·경도는 쌍으로 입력하고, 지오코딩 제공자는 `GEOCODING_PROVIDER` 출처일 때만
보냅니다. 요청 타입 검사 통과가 실제 대원 확인·현재 재고·위치의 진위를 인증하지는 않습니다.

#### 응답 · BriefResponse

| 필드 | 타입 | Backend·화면에서 쓰는 방법 |
|---|---|---|
| `schema_version` | `action-brief-v1` | body 계약 버전 확인 |
| `request_id`, `incident_id` | string | 현재 활성 요청·사고와 일치하는지 확인 |
| `revision` | integer | Backend 최신 버전과 비교 |
| `state_fingerprint` | string | 같은 요청의 initial/final 입력·버전 비교. 인증 증명 아님 |
| `phase` | `initial` 또는 `final` | JSON 경로는 final. SSE는 두 단계 |
| `status` | 아래 enum | HTTP 성공과 별도로 업무 상태 확인 |
| `summary` | string, 최대 300자 | 화면 상단 요약 |
| `cards` | `ActionCard[]` | 확인·대응 참고·보류·인계 안내 |
| `missing_information` | string[] | 다음 확인 질문과 누락 정보 |
| `sources` | `BriefSource[]` | 카드의 source_ids와 연결 |
| `facts` | object | Parser가 추출한 원문 표현·부정·추정. 검증된 사실 아님 |
| `substance_candidates` | object[] | 확정되지 않은 후보. 첫 후보 자동 선택 금지 |
| `discovery` | object 또는 null | 필요할 때만 실행한 보조 탐색 |
| `confirmation_state` | object | 정확히 `INCIDENT`, `FACILITY` 두 boolean |
| `rule_review` | object | `executed`와 규칙 상태 확인. 확률·전술 승인 아님 |
| `versions` | object | 서비스·Resolver·Retriever·문구·정책·계획 방식·LLM 사용 여부 |
| `handoff` | object | 받은 확인 기록과 미확인 사항을 분리한 인계 정보 |
| `processing` | object | 단계별 수행 상태·처리시간. 빠르다는 SLA가 아님 |
| `input_provenance` | object | 입력 hash·문자 수·STT 포함 여부. STT 실행시간은 별도 |
| `limitations` | string[] | 검수 전·확인 책임·자료 적용 범위 등 표시할 한계 |

| `status` | 의미 | 소비자 동작 |
|---|---|---|
| `PENDING` | 초기 안내, 분석 중 | 초기 확인 카드만 표시 |
| `NEEDS_CONFIRMATION` | 물질 확인 추가 필요 | 질문·확인 입력 표시, 조합 규칙 잠금 |
| `HELD` | 근거·조건 부족 등으로 보류 | 보류 이유 표시, 결과를 임의 보완하지 않음 |
| `COMPLETED` | 현재 요청의 분석 완료 | 출처와 한계를 함께 표시. **현장 승인 아님** |
| `TIMEOUT` | 조율 제한시간 초과 | 확인·보류 안내만 사용. 이전 결과를 현재 결과로 복원하지 않음 |

정상 응답의 **일부 필드 발췌**입니다. 전체 응답 fixture로 사용하지 마세요.
완전한 예시는 OpenAPI 응답 `examples.normal_unconfirmed` 등에 포함되어 있으며,
실제 artifact에 과거 합성 입력을 실행한 기록입니다. 현재 응답의 수치·문구·지연시간을
고정 보장하는 정답이 아닙니다.

```json
{
  "schema_version": "action-brief-v1",
  "phase": "final",
  "status": "NEEDS_CONFIRMATION",
  "confirmation_state": {"INCIDENT": false, "FACILITY": false},
  "rule_review": {
    "executed": false,
    "status": "NOT_RUN_REQUIRES_TWO_CONFIRMED_CAS",
    "is_probability": false,
    "tactical_authorization": false
  }
}
```

#### 카드 · ActionCard

| 필드 | 타입·조건 | 의미 |
|---|---|---|
| `card_id` | string, 응답 내 고유 | 카드 식별자. 전역 저장 ID라고 가정하지 않음 |
| `phrase_id`, `phrase_version` | string | 허용 문구와 문구 버전 |
| `category` | `확인`, `대응 참고`, `보류`, `인계` | 카드 종류 |
| `priority` | integer 1~4 | 작은 값부터 표시. 위험등급 아님 |
| `title`, `message`, `reason` | string, 최대 80·240·400자 | 제목·짧은 안내·별도 이유 |
| `role` | `INCIDENT`, `FACILITY`, `UNKNOWN` | 대상 역할 |
| `target_label` | string | 사고물질·시설물질·전체 안내 등 한국어 표시 |
| `cas_number` | string 또는 null | 대상 CAS. 존재만으로 확인 완료 아님 |
| `confirmation_status` | `UNCONFIRMED`, `CONFIRMED_INPUT`, `NOT_APPLICABLE` | Backend 입력 기준 확인 상태 |
| `required_conditions`, `unmet_conditions` | string[] | 필요한 조건·현재 미충족 조건 |
| `source_ids` | string[], 최소 1개 | 같은 응답 sources의 ID에 연결 |
| `review_status` | `DRAFT_NOT_EXPERT_REVIEWED` | 전문 검수 전이라는 표시 유지 |
| `tactical_authorization` | `false` | 진입·방수·대피 전술 승인 아님 |
| `state_fingerprint` | string | 부모 응답과 반드시 일치 |

`대응 참고` 카드는 같은 CAS·역할의 외부 근거가 있고 입력 확인·필수 조건을 충족한 경우만
허용합니다. 다른 카드의 조건이 미충족이면 보류·확인 안내로 표시합니다. API가 돌려준 문구를
프런트에서 명령형 전술로 다시 생성하지 마세요.

#### 출처 · BriefSource

`source_id`, `document_id`, `source_type`, `url`, `section`, `document_version`,
`license_status`, `freshness_status`가 필수입니다. `source_type`은 KOSHA/CAMEO/INTERNAL_POLICY,
`role`은 INCIDENT/FACILITY/UNKNOWN이고 `cas_number`·`content_sha256`는 null일 수 있습니다.
`selection_method` 기본값은 `BASELINE_RETRIEVAL`입니다.

- 외부 자료: `license_status=LINK_ONLY_TERMS_REVIEW_REQUIRED`, `freshness_status=INDEX_VERSION_ONLY`.
- 내부 정책: `PROJECT_AUTHORED`, `VERSIONED_POLICY`. `url`은 저장소 상대 경로일 수 있으므로
  고정된 신뢰 저장소 주소와 결합합니다. 모든 출처를 외부 HTTPS라고 가정하지 않습니다.
- `section`은 현재 인덱스 항목 제목입니다. 독립 검수된 SDS section 정답이라고 주장하지 않습니다.
- `EXACT_CAS_LINK_FALLBACK_NOT_SECTION_RELEVANCE`는 같은 CAS의 보조 링크일 뿐 질문에
  맞는 절 검색 성공이 아닙니다. `content_sha256`도 검색 반환 내용 hash이지 전체 원문 hash가 아닙니다.

### 6.0.1 `POST /api/v1/agents/incidents/brief/stream`

요청·인증은 brief와 같습니다. 응답은 `Content-Type: text/event-stream`,
`Cache-Control: no-store`, `X-Accel-Buffering: no`이며 성공 시 initial/final 두 이벤트입니다.
각 data는 **완전한 BriefResponse JSON**입니다. 아래 꺾쇠 설명은 실제 JSON이 아닌 wire 형식 표기입니다.

```text
event: initial
data: <phase=initial인 전체 BriefResponse JSON 한 줄>

event: final
data: <phase=final인 전체 BriefResponse JSON 한 줄>

```

네이티브 `EventSource`는 이 POST body·인증 헤더 요구에 맞지 않습니다. Backend의 streaming
HTTP client나 fetch 기반 SSE 소비자를 사용하고 TCP chunk를 곧바로 이벤트 하나로 취급하지
마세요. UTF-8·줄·빈 줄 경계를 누적 파싱합니다. 이벤트 ID/Last-Event-ID·heartbeat·서버 이어받기는
제공하지 않습니다. 연결이 final 전에 끝나면 미완료입니다. 이미 200 헤더가 전송된 뒤의 오류는
항상 일반 JSON 오류로 바꿔 보낼 수 없으므로 종료·파싱 오류를 별도로 처리합니다.

저장소 설치 후 키 환경변수가 있는 별도 터미널에서:

```bash
python scripts/action_brief_client.py --url http://127.0.0.1:8087 --example unconfirmed
python scripts/action_brief_client.py --url http://127.0.0.1:8087 --example cancelled
```

이 클라이언트는 검증한 전체 snapshot만 교체합니다. curl에서는 앞 brief 요청 URL에
`/stream`을 붙이고 `-N`을 추가하세요. Swagger의 SSE 경로 표시가 실시간 소비자 검증을 대신하지 않습니다.

<a id="state-and-timeout"></a>

### 6.0.2 확인·취소·동시 요청·timeout

| 입력 변화 | Backend 요청 | 기대 동작 |
|---|---|---|
| 최초 신고 | 미확인, revision 1 | 후보·확인 안내. Rule 미실행 |
| 사고물질 확인 | 확인 기록 추가, revision 증가 | 한 역할 근거 참고, 조합 잠금 |
| 시설물질도 확인 | 서로 다른 확인 ID 두 개 | 지원 조건 통과 시만 제한된 규칙 조회 |
| 확인 취소 | 기록 제거 또는 invalidated_confirmation_ids, revision 증가 | 취소된 확인 사용 금지 |
| 새 근거 상충 | reported_evidence_conflict=true, revision 증가 | 양쪽 확인 보류 |
| 이전 요청 지연·중복 | 활성 요청 ID·revision과 비교 | 오래된 결과 폐기 |

모델 API는 알려주지 않은 Backend 상태 변경을 감지하지 못합니다. 소비자는 다음을 지킵니다.

1. Backend가 사용자 권한·확인 원본·사고 revision을 저장합니다. 모델이 사람 승인 기록을 만들지 않습니다.
2. 새 요청 **발송 전** 이전 카드를 제거하고 활성 `(incident_id, revision, request_id)`를 갱신합니다.
3. 응답 ID가 활성 요청과 다르면 폐기합니다. 같은 요청의 fingerprint 불일치도 폐기합니다.
4. initial→final은 cards/sources를 포함해 **전체 교체**합니다. append하지 않습니다.
5. final 뒤 initial·중복 final은 폐기합니다. 같은 request_id 재전송도 서버가 중복 실행을 막아주지 않습니다.
6. 확인 취소 요청의 원본 analysis가 잘못된 CAS를 갖고 있으면 취소 적용 전에 422가 날 수 있습니다.
   유효하지 않은 기록은 Backend에서 제거해 보내세요.

참조 구현: [`BriefConsumer`](../src/chemiguard119/brief_consumer.py).
brief revision은 Backend 사고 버전이며 step memory.revision과 같은 카운터로 가정하지 않습니다.

| 항목 | 현재 구현/설정 | 연동 시 주의 |
|---|---|---|
| brief 조율 deadline | 기본 15초 | 전체 네트워크·cold start 시간 보장 아님 |
| 배포 Cloud Run 요청 timeout | 30초 | 앱 TIMEOUT 전에 프록시/연결 종료가 생길 수 있음 |
| 예제 클라이언트 timeout | 35초 | 테스트용 상한. 응답속도 목표/측정값이 아님 |
| 도구 실행 | 자동 재시도 0회, 요청당 호출 수 제한 | 클라이언트의 무한 자동 재시도 금지 |
| 조율/worker 슬롯 | 프로세스당 조율 2개, 도구 worker 6개, 무제한 대기열 없음 | 포화 시 503 BRIEF_CAPACITY_EXCEEDED |
| 작업 취소 | 협력적 취소, 늦은 결과 폐기 | 실행 중 thread가 즉시 죽는 것은 아님. 종료까지 슬롯 점유 |

Backend 재시도 **권장 정책(실제 BE 구현 여부 별도)**: 네트워크 연결 실패나 retryable 503만
현재 입력을 재확인한 뒤 최대 1회 backoff 재시도. 401/403/422/안전 검증 500은 자동 재시도하지
않습니다. HTTP 200의 TIMEOUT도 성공 카드로 저장하지 않습니다. timeout만으로 작업 종료를
확정하지 않으며 새 요청을 무제한 쌓지 않습니다.

### 6.0.3 계약 테스트와 한계

```bash
python scripts/contracts/export_contracts.py --check
python scripts/contracts/export_postman.py --check
python -m pytest tests/test_api_spec.py
```

예시와 OpenAPI·Pydantic의 일치 검사입니다. 원본 음성·가중치 없이 실행되며 모델 정확도
평가가 아닙니다. 실제 artifact·실패 조건 평가 이력은 [행동 카드 평가](ACTION_BRIEF_RESULTS.md),
현재 연결 확인은 health/meta를 따릅니다. Postman GUI에서 모든 요청을 실행했다고 주장하지 않습니다.
미지원 조합·누락 근거·의미 상충의 완전 탐지·실제 무전·현장 안전성은 검증 범위 밖입니다.

2026-09-11 로컬 Python 3.11에서 전체 795개 테스트·Ruff·OpenAPI drift 검사를 통과했습니다.
Postman 18개 요청은 공식 v2.1 JSON Schema 검증과 JavaScript test 구문 검사를 통과했습니다.
공식 Postman schema SHA-256은
`90000a561d00b1a06ee37a6d3606e911e1357f757d8d91afc690d87dadacb9b2`입니다.
CI·병합·공개 문서 배포 상태는 [이슈 #72](https://github.com/chemicheck119-lab/analysis-engine/issues/72)에서
확인합니다. 합성 계약 테스트 수를 현장 사례 수로 표현하지 않습니다.

### 6.1 `POST /api/v1/incidents/analyze`

일반 사고분석 응답은 상황실·현장 사용자가 확인할 사실, 물질 후보, 공식 근거,
2-CAS 확인 상태와 다음 행동만 반환합니다. 내부 agent workflow, tool trace, 경로와 map
context는 실행·감사 영역에만 보존하며 사용자 응답에는 포함하지 않습니다.

상세 분석 DTO가 필요한 기존 연동점입니다. 새 카드 화면은 brief를 우선 사용합니다.
한 번의 요청 안에서 파서, Resolver, Retriever, 시설 이력 검색과
조건부 Rule Engine을 실행합니다.

### 6.1.1 `POST /api/v1/agents/incidents/step`

현장 확인을 기다리며 같은 사고를 여러 번 이어갈 때 사용하는 실제 에이전트 API입니다.
`analysis`에는 기존 사고 분석 요청을 그대로 넣고, 첫 응답 이후에는 반환된 `memory`를 BE가
저장했다가 다음 요청에 포함합니다.
`max_actions`는 현재 안전 임계 경로를 한 번에 끝내도록 4~8만 허용하며 기본값은 6입니다.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/agents/incidents/step \
  -H "Content-Type: application/json" \
  -H "X-API-Key: ${CHEMIGUARD119_API_KEY}" \
  --data @examples/api/incident_agent_step_request.json
```

| 응답 상태 | 의미 | BE 동작 |
|---|---|---|
| `WAITING_FOR_HUMAN` | 사고·시설물질 확인 또는 추가 공식근거가 필요 | memory 저장 후 새 관찰이 생길 때 재호출 |
| `PARTIAL_MAX_ACTIONS` | 한 요청의 도구 실행 한도에 도달 | 같은 최신 입력과 memory로 다음 step 호출 |
| `GOAL_COMPLETED` | 확인 gate와 안전 재검증 후 결과 제시 준비 | 분석·근거·trace를 대응 기록에 저장 |
| `FAILED_RETRYABLE` | 내부 도구 일시 실패 | `request_id`로 로그 확인 후 제한 재시도 |
| `FAILED_SAFETY` | 확인·Rule·응답 안전 계약 불일치 | 결과 사용 금지, 운영 조사 |

`events`에는 `PLAN`, `ACT`, `OBSERVE`, `REPLAN`과 선택한 도구·결정 코드만 들어갑니다.
숨겨진 chain-of-thought는 반환하지 않으며 `trace_is_chain_of_thought=false`입니다. memory의
SHA-256은 손상 탐지용이고 인증 서명이 아닙니다. API Key를 유지하고 BE는 revision을
compare-and-swap 방식으로 저장해야 합니다. memory 자체는 Rule 실행 권한이 될 수 없습니다.
`runtime_state_fingerprint`가 달라지면 신고가 같아도 새 artifact·정책으로 다시 분석합니다.

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

분석 응답의 `agent.workflow`는 실행 에이전트 trace가 아니라 실제 분석 결과를 태블릿용
10단계로 투영한 상태입니다. 동적 도구 trace는 agent step 응답의 `events`를 사용합니다.
전국 지도와 이동 갱신의 전체 설명은
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
| `ranking_score` | 0~1 후보 정렬값. 정답·위험 확률이 아님 |
| `ranking_features` | 검색 사전값·식별·출처·물성·공식근거별 값과 기여도 |
| `ranking_model` | 모델·확인 정책 버전, 비지도학습 상태, 점수 의미 |
| `next_best_checks` | 물질을 확정하는 대신 대원이 다음에 확인할 항목과 이유 |
| `requires_responder_confirmation` | 항상 `true` |
| `rule_eligible` | 항상 `false` |
| `risk_determination_allowed` | 항상 `false` |
| `candidate_score_is_probability` | 항상 `false` |

`CAS_EVIDENCE_NOT_LOADED`는 후보가 안전하다는 뜻이 아니라 해당 CAS의 상세 KOSHA·CAMEO
근거가 현재 artifact에 없다는 뜻입니다. 클라이언트는 `evidence_warning`,
`evidence_notice`, `cas_link_warning`을 숨기지 않아야 합니다. 후보 순위만으로 현장 물질을
확정하지 않습니다.

`ranking_model.training_status`는 현재
`NOT_SUPERVISED_INSUFFICIENT_REVIEWED_LABELS`입니다. BE·FE는 이를 “학습 정확도”로
표현하면 안 됩니다. `next_best_checks`의 냄새 항목도 직접 냄새를 맡으라는 지시가 아니라 기존
신고 기록·계측기·MSDS의 기술을 확인하라는 안전 안내입니다.

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

`score`는 `candidates[].score`를 뜻합니다. 기본 `top_k=3`이고 후보 수는 이보다 적거나
0개일 수 있습니다. `resolve`, `evidence/search`, `facilities/candidates`, `conflicts/review`의
최상위 응답은 OpenAPI에서 `object/additionalProperties`인 부분이 있습니다. 이 API들의 전체
중첩 DTO가 자동생성 타입만으로 엄격히 보장된다고 가정하지 말고, 문서·원본 예시·회귀 테스트를
함께 고정하세요. 카드 화면은 명시적 `BriefResponse` 계약을 우선 사용합니다.

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
`query`는 1~500자, `top_k`는 1~10(기본 5)이며 CAS hint는 checksum 검사를 받습니다.
`RESPONDER_CONFIRMED`라는 문자열만으로 사람 확인을 증명하거나 Rule을 실행할 수 없습니다.
응답은 `results`와 검색 상태를 확인하고, 빈 결과를 안전·위험 없음으로 해석하지 않습니다.

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

<a id="error-contract"></a>

## 12. 오류 형식

애플리케이션의 표준 오류는 같은 envelope를 사용합니다. IAM 차단·프록시·네트워크 오류와
readiness의 503 응답은 이 envelope가 아닐 수 있습니다. status code와 Content-Type을 먼저
확인하고 HTML·빈 응답을 무조건 JSON으로 파싱하지 마세요.

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
| `403` | Cloud Run IAM 권한·ID token 문제 등 | 앱 진입 전 응답일 수 있음. Google 인증 확인 |
| `422` | 요청 schema·CAS·역할 오류 | 요청 수정 후 재시도 |
| `500` | 출력 안전 검증 또는 내부 오류 | 같은 `request_id`로 운영자 확인 |
| `503` | artifact·manifest·인증 구성 미준비 | readiness 복구 후 재시도 |
| `503` | `BRIEF_CAPACITY_EXCEEDED`, 조율 슬롯 포화 | 현재 입력 확인 후 제한 재시도 |
| `504`·연결 종료 | Cloud Run·프록시 timeout 등 | 앱 envelope 보장 없음. 이전 카드 복원 금지 |

`BACKEND_AUTH_REQUIRED`는 401, `INVALID_SCHEMA`는 422입니다. 준비 상태가 잘못되면 인증
키 검사보다 먼저 503이 반환될 수 있습니다. `error.retryable=false`인 설정·무결성 오류는
503이어도 자동 재시도하지 않습니다. `500 INTERNAL_ERROR`가 retryable=true여도 무한 재시도
허가가 아니며 원인 확인을 우선합니다. **HTTP 200 + brief status=TIMEOUT**은 별도의 업무상
미완료 응답이지 ErrorResponse가 아닙니다.

`AWAITING_SUBSTANCE_CONFIRMATION`, `VERIFY_REQUIRED`, `UNCLASSIFIED`는 정상적인 업무 상태일
수 있으며 HTTP 오류와 구분해야 합니다.

`UNCONFIRMED_RISK_OUTPUT_BLOCKED`는 현장 확인 두 건이 없는데 내부 출력에 위험등급·반응·
완료 상태가 섞였거나, 누락된 확인 역할과 상태가 모순될 때 반환하는 fail-closed `500`
오류입니다. 클라이언트는 이 응답을 후보 또는 정상 결과로 표시하지 않고 운영자가 같은
`request_id`를 조사하게 해야 합니다.

## 13. 프론트·백엔드 구현 규칙

1. Resolver 첫 후보를 자동 확정하지 않습니다.
2. 백엔드가 인증된 현장 확인 레코드를 만든 후 확인 객체를 전송합니다.
3. 기존 analyze UI는 `confirmation_gate.all_required_confirmed=true`,
   `conflict_review.executed=true`, 완료 Rule 상태(`COMPLETED`/`SCREENING_COMPLETED`)와
   지원 결과 필드를 모두 확인한 경우에만 `risk_level_ko`를 표시합니다. executed=true만으로는
   미지원·보류 결과를 완료로 표시할 수 없습니다. brief는 해당 endpoint의 별도 카드 계약을 따릅니다.
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

Backend 전용 예시입니다. 프록시·로컬 모델에서는 idToken을 생략할 수 있고 실제 Cloud Run
Base URL을 직접 부를 때는 Google 인증 라이브러리에서 받은 ID token이 필요합니다.
아래 코드는 토큰 발급·갱신이나 최종 DTO 검증까지 제공하는 SDK는 아닙니다.

```ts
type AnalyzeRequest = Record<string, unknown>;

export async function analyzeIncident(
  baseUrl: string,
  apiKey: string,
  payload: AnalyzeRequest,
  idToken?: string,
) {
  const response = await fetch(`${baseUrl}/api/v1/incidents/analyze`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": apiKey,
      "X-Request-Id": crypto.randomUUID(),
      ...(idToken ? { Authorization: `Bearer ${idToken}` } : {}),
    },
    body: JSON.stringify(payload),
  });

  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error(`모델 API 비JSON 응답: HTTP ${response.status}`);
  }
  const body = await response.json();
  if (!response.ok) {
    throw new Error(`${body.error?.code ?? "UNKNOWN"}: ${body.error?.message ?? "API 오류"}`);
  }
  return body;
}
```

API Key는 서버 환경변수나 Secret Manager에서 읽어야 하며 프론트 번들에 포함하면 안 됩니다.

## 15. 관련 문서

- [Postman Collection v2.1 형식](https://schema.postman.com/json/collection/v2.1.0/docs/index.html)
- [행동 카드 로컬 설치·실제 artifact 준비](ACTION_BRIEF.md)
- [공개 Swagger 배포와 비공개 API 구분](PUBLIC_SWAGGER.md)
- [README](../README.md)
- [아키텍처](ARCHITECTURE.md)
- [데이터와 모델](DATA_AND_MODEL.md)
- [공식근거 교차검증](EVIDENCE_ASSURANCE.md)
- [배포](DEPLOYMENT.md)
- [안전 및 한계](SAFETY_AND_LIMITATIONS.md)
- [대시보드 적용 흐름](DASHBOARD_FLOW.md)
