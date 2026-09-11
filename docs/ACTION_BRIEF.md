# 행동 카드 API — 모델만으로 시연하기

## 한 문장으로

신고 내용을 읽고 **지금 무엇을 확인해야 하는지**, **어떤 자료를 찾아봤는지**, **왜 판단을 보류했는지**를 짧은 카드로 돌려주는 모델 API입니다. 소방 전술을 승인하는 시스템은 아닙니다.

예를 들어 “차아염소산나트륨이 새고 옆에 염산이 있다”는 신고를 받으면 이름을 후보로 찾습니다. 하지만 신고가 틀렸을 수도 있습니다. 확인 기록이 없으면 라벨·제품 SDS 확인 카드를 먼저 줍니다. 사고물질이 확인되면 그 CAS의 근거 링크를 보여줄 수 있고, 두 역할의 CAS가 각각 확인돼야 조합 규칙을 조회합니다. **두 CAS 확인만으로 진입·방수·대피를 권고하지 않습니다.**

상태: **부분 구현 또는 개발용 데모**. JSON/SSE·확인 정책·로컬 artifact 평가를 제공하지만, 전문 검수 문구·기관 SOP 승인·현장 검증·Backend 실연동은 이번 작업에 포함되지 않습니다. 테스트와 CI의 정확한 판정은 [평가 결과](ACTION_BRIEF_RESULTS.md)를 따릅니다.

## 실행하기

Python 3.11을 사용합니다. 저장소의 기존 의존성을 재사용하며 추가 프레임워크·GPU·유료 LLM은 없습니다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

팀이 승인한 비공개 저장소에서 기존 DB·Resolver·Retriever 세 파일을 받아야 합니다. Git에는 넣지 않습니다. 이 평가에서 사용한 잠금 hash는 `scripts/prepare_action_runtime.py`에 있습니다. 임의로 구한 joblib 파일은 로드하지 마세요.

```bash
python scripts/prepare_action_runtime.py \
  --source /승인된/기존/artifacts \
  --output /비공개/새로운/action-brief-local
```

이 명령은 기존 묶음을 덮어쓰지 않고 약 286MiB를 별도 복사한 뒤 현재 정책 manifest를 생성합니다. 기존 모델 bytes는 그대로입니다. 옛 preview manifest의 정책 hash가 현재와 다르면 정상적으로 차단되므로, manifest를 삭제하거나 운영 검증을 끄지 마세요. 새 묶음은 로컬 개발용이고 배포 승인·독립 검수 통과를 뜻하지 않습니다.

```bash
export CHEMIGUARD119_ARTIFACT_DIR=/비공개/새로운/action-brief-local
export CHEMIGUARD119_ENVIRONMENT=development
export CHEMIGUARD119_API_KEY=action-brief-local-demo
export CHEMIGUARD119_RAG_MODE=extractive
export CHEMIGUARD119_BRIEF_PARALLEL=false
python -m uvicorn chemiguard119.api:app \
  --host 127.0.0.1 --port 8011 --no-access-log
```

위 키는 **로컬 합성 데모 전용 값**입니다. 외부 서비스에서는 Secret을 통해 별도 키를 주입하고 TLS·Backend 인증을 적용해야 합니다. 이번 작업은 외부 배포하지 않습니다. 종료는 실행 터미널에서 `Ctrl+C`입니다. 로컬 서버는 비용 차단 기능이나 상용 운영 환경이 아닙니다.

- 준비 확인: `http://127.0.0.1:8011/health/ready`
- Swagger: `http://127.0.0.1:8011/docs`
- OpenAPI: `http://127.0.0.1:8011/openapi.json`

Swagger의 **Authorize → X-API-Key → Apply credentials → Close** 후 `POST /api/v1/agents/incidents/brief`를 펼치고 예시를 선택합니다. **Try it out → Execute**로 실제 모델을 실행합니다. artifact가 없으면 `/docs`는 열려도 분석은 `503`이며 정상 실행으로 세지 않습니다.

## 복사 가능한 JSON 요청

다음은 실제 신고가 아닌 합성 예시입니다. `analysis`에는 기존 `IncidentAnalyzeRequest`를 그대로 넣습니다.

```bash
curl -sS http://127.0.0.1:8011/api/v1/agents/incidents/brief \
  -H "X-API-Key: $CHEMIGUARD119_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"revision":1,"analysis":{"request_id":"REQ-DEMO-1","incident_id":"INC-SYNTHETIC-1","input":{"type":"VOICE_TRANSCRIPT","text":"차아염소산나트륨 탱크에서 누출이 있고 옆 저장고에는 염산이 있습니다."},"evidence_top_k":5}}'
```

대표 응답의 **일부 필드만 발췌**하면 다음과 같습니다. 완전한 요청·응답 예시는 Swagger와 `src/chemiguard119/action_examples.py`, `src/chemiguard119/action_response_examples.py`를 참고합니다.

```json
{
  "schema_version": "action-brief-v1",
  "phase": "final",
  "status": "NEEDS_CONFIRMATION",
  "confirmation_state": {"INCIDENT": false, "FACILITY": false},
  "rule_review": {"executed": false, "is_probability": false, "tactical_authorization": false}
}
```

`cards`에는 제목·짧은 문장·이유·대상 역할·확인 여부·필수/미충족 조건·출처 ID·문구 검수 상태가 들어갑니다. `facts`는 신고문에서 추출한 표현이고, `handoff.received_confirmation_records`는 Backend로부터 받은 확인 기록입니다. 이 둘을 같은 “확정 사실”로 표시하면 안 됩니다. `target_label`은 화면용 한국어 역할 이름입니다.

### 확인·취소 입력

한 역할의 확인 기록 형식은 아래와 같습니다. 실제 요청에는 인증된 Backend가 저장한 기록만 넣습니다. 이 합성 기록을 실제 사람의 확인으로 저장하지 마세요.

```json
{
  "confirmation_id": "CNF-DEMO-INCIDENT",
  "cas_number": "7681-52-9",
  "role": "INCIDENT",
  "presence_status": "CONFIRMED_PRESENT",
  "confirmation_basis": "CONTAINER_LABEL",
  "observed_at": "2026-09-01T00:00:00+09:00"
}
```

이 객체를 `analysis.confirmed_incident_substance`에 넣으면 한쪽 확인입니다. 시설 확인은 `analysis.confirmed_facility_substance`에 `role=FACILITY`, 다른 `confirmation_id`로 넣습니다. 서로 다른 **확인 기록**이 필요하다는 의미이지 반드시 서로 다른 화학종이어야 한다는 뜻은 아닙니다.

취소 시 Backend에서 확인을 제거하고 revision을 올리세요. 이전 요청을 기반으로 재현하려면 루트에 `"invalidated_confirmation_ids":["CNF-DEMO-INCIDENT"]`를 넣을 수도 있습니다. `reported_evidence_conflict=true`는 양쪽 확인을 보류합니다. `false`가 의미상 충돌 없음의 증거는 아닙니다.

| 조건 | 반환/동작 |
|---|---|
| 미확인 | `NEEDS_CONFIRMATION`, 확인·보류 안내. 후보는 확정 아님 |
| 한쪽 확인 | 같은 CAS·역할의 공식 자료 링크만 대응 참고로 허용. 조합 규칙 잠금 |
| 두쪽 확인 + 규칙 지원 | 제한된 CAMEO 조회. `COMPLETED`도 현장 승인 아님 |
| 근거 없음·출처 불일치·미지원 조합 | `HELD`와 미충족 조건 |
| CAS checksum 오류 | HTTP `422`, 도구 호출 전 거부 |
| 분석 deadline 초과 | HTTP `200`, `status=TIMEOUT`, 확인·보류 카드만 |
| 작업 슬롯 포화 | HTTP `503`, `BRIEF_CAPACITY_EXCEEDED` |
| 확인 취소·명시적 CAS 충돌 | 재확인 안내, 기존 후보를 자동 재확정하지 않음 |

## SSE 순차 출력

`POST /api/v1/agents/incidents/brief/stream`은 `initial`, `final` 두 이벤트를 반환합니다. 각 `data`는 스키마 검증을 마친 **전체 BriefResponse**입니다. 카드 배열을 누적하지 말고 교체합니다.

```bash
python scripts/action_brief_client.py --url http://127.0.0.1:8011 --example unconfirmed
python scripts/action_brief_client.py --url http://127.0.0.1:8011 --example both_confirmed
python scripts/action_brief_client.py --url http://127.0.0.1:8011 --example cancelled
```

`curl`은 위 JSON 요청의 URL 끝을 `/brief/stream`으로 바꾸고 `-N` 옵션을 추가하면 됩니다. 이벤트 형식은 `event: initial` 다음 줄 `data: {전체 응답}`이며 빈 줄로 구분됩니다. Swagger는 SSE 경로를 표시하지만 이벤트별 갱신 UI로 사용하지 않습니다. 위 클라이언트로 첫 카드 도착 시각과 최종 결과를 확인하세요. 중간 LLM 토큰이나 검증 전 문장은 스트리밍하지 않습니다.

## 역할별 조율과 호출 제한

| 역할 | 실제 구현 | 하지 않는 일 |
|---|---|---|
| 상황정리 | 기존 결정적 Parser의 유형·물질 표현·부정·추정 유지 | 삐 처리 주소 복원, 원문에 없는 관찰 생성 |
| 물질식별 | 기존 Sparse Resolver. 이름 후보/확인이 없을 때만 Discovery 1회 | 전사문 교정·확정 CAS 자동 생성 |
| 대응근거 | 역할별 공식 근거 검색, 필요할 때 exact CAS 공식 링크 보조 조회 | 검색 순위/Agent 합의를 위험 확률로 변환 |
| 카드 정책 | 정해진 문구와 조건을 검증한 뒤 카드 구성 | 여러 LLM이 합의했다고 안전 보장 |

기존 `agent_loop`의 `DETERMINISTIC_POLICY_PLANNER` 원칙과 동일한 도구·파이프라인/확인 Gate를 재사용합니다. 새 API가 기존 `/agents/incidents/step`의 stateful memory 루프를 다시 호출하는 것은 아닙니다. 기존 memory는 외부 Backend 저장용이며 승인 증명이 아닙니다. 이번 API는 요청 snapshot별 **역할 분리형 결정적 오케스트레이션**입니다.

기본 실행은 순차입니다. 로컬 측정에서 병렬 p95 개선이 반복 실험마다 달라서 병렬화의 상시 우위를 채택하지 않았습니다. `CHEMIGUARD119_BRIEF_PARALLEL=true`로 비교할 수 있습니다. 병렬 모드에서는 독립 시설 이력 검색을 먼저 시작하고, Parser/Resolver 뒤 사고·시설 근거를 병렬 검색합니다. 이름을 알아야 하는 검색을 후보 계산보다 먼저 실행하지 않습니다.

프로세스당 동시 조율 2개, 별도 도구 worker 6개, 추가 대기열 없음, 요청 deadline 15초, 자동 재시도 0회입니다. 요청당 역할별 검색 최대 2회, exact CAS 링크 보조 최대 2회, 시설 이력 1회, Discovery 1회(내부 검색 최대 3회), Rule 최대 1회입니다. 모든 최대치가 항상 실행되는 것은 아닙니다. 선택·인자(CAS/역할/top-k)·완료 상태는 `processing.tasks`에 기록합니다. 원문·주소·키는 trace에 넣지 않습니다.

**취소는 협력적 취소입니다.** 실행 중인 Python thread를 강제로 죽이지 않습니다. deadline/연결 종료 이후 새 단계·Rule 진입을 막고 늦은 결과를 폐기합니다. worker 슬롯은 실제 작업 종료까지 반환하지 않으므로 timeout마다 무제한 worker가 늘어나지 않습니다. 이미 시작한 유한 계산을 즉시 중단하거나 프로세스 강제 종료 없이 영구 정지를 회복한다고 주장하지 않습니다. 실제 HTTP 연결 종료/포화 테스트는 별도로 있습니다.

## 출처·카탈로그 정책

`action_catalog.py`는 문구 ID/버전, 작성자, 검수자(null), 출처 문서/항목, 적용 조건, 제외 조건, 이용 범위·최신성 상태를 담습니다. 모든 문구는 프로젝트가 작성한 **확인·자료 조회 안내**이며 기관 SOP를 인용한 승인 전술이 아닙니다. 전문 검수 전에는 `DRAFT_NOT_EXPERT_REVIEWED`와 `tactical_authorization=false`를 변경하지 않습니다.

- `VERIFY_MATERIAL`, `VERIFY_LOCATION`: 확인 기록·위치 자료 요청.
- `HOLD_PAIR`, `ANALYSIS_PENDING`, `HOLD_CONFLICT`, `HOLD_FAILURE`: 미충족 조건에 따른 보류.
- `REFERENCE_AVAILABLE`, `NO_EVIDENCE`: 같은 CAS의 검증 가능한 공식 링크가 있는지에 따른 안내.
- `HISTORY`: 과거 시설 이력의 한계.
- `RULE_REFERENCE`, `HANDOFF`: 조합 조회의 한계와 확인/미확인 구분 인계.

첫 artifact 평가에서 기존 KOSHA 인덱스의 `source_url` 일부가 URL이 아니라 `KOSHA MSDS OpenAPI via data.go.kr`라는 설명임을 확인했습니다. 이를 임의의 개별 SDS URL로 바꾸지 않았습니다. 공식 HTTPS 링크·CAS·연결 상태·버전이 있는 동일 CAS 문서가 별도 인덱스에 있을 때만 `EXACT_CAS_LINK_FALLBACK_NOT_SECTION_RELEVANCE`로 보조 제공합니다. 이는 **질문에 대한 정답 절 검색 성공이 아닙니다.** 기존 Retriever 점수나 모델을 바꾸지 않습니다. 보조 문서도 없으면 보류합니다.

원문 재배포 대신 문서 ID·항목 메타데이터·URL·인덱스 버전·검색 반환 내용 hash를 제공합니다. 이 hash는 전체 원문 hash가 아닙니다. 문구가 출처를 갖는다는 사실과 출처 문서가 현장에 적용된다는 사실은 다릅니다.

2026-09-10 확인한 [KOSHA API 안내](https://www.data.go.kr/data/15157612/openapi.do)는 이용허락범위 제한 없음을 표시하지만 공단 자료의 참고용 범위와 제조·수입자의 MSDS 제공 책임도 안내합니다. 인덱스 자료를 개별 제품 SDS로 대체해 해석하지 않습니다. [CAMEO 이용 조건](https://cameochemicals.noaa.gov/help/reference/terms_and_conditions.htm)은 일부 제3자 데이터의 복제 제한과 정확성·적합성 무보증을 명시합니다. 따라서 전체 원문·보호구 표·NFPA 등 제3자 내용을 새 API에서 자동 재배포하지 않습니다. 서비스 공개 전 dataset별 권리 검토는 별도입니다.

LLM은 이 endpoint에서 **사용하지 않습니다**. 기존 Grounded RAG의 extractive 경로만 재사용하되, 그 원문 문장도 카드에 그대로 전달하지 않습니다. 카드 title/message/reason은 허용 목록과 정확히 일치해야 합니다. 숫자·단위·부정·조건을 LLM이 바꾸는 경로 자체가 없습니다. 기존 API의 선택형 LLM timeout 회귀는 `tests/test_rag.py`로 분리 검증합니다.

## Backend·Front 인계 계약

1. Backend가 인증된 확인 기록, 사고 `revision`, 취소·새 증거를 영속 저장합니다.
2. 새 정보마다 revision을 올리고 새 request_id로 요청합니다. 브라우저에 서비스 API 키를 넣지 않습니다.
3. 새 요청 발송 **전** 기존 카드를 제거합니다. 서버의 완료를 기다리며 옛 카드를 유지하지 않습니다.
4. 응답의 `incident_id/revision/request_id`가 활성 요청과 다르면 버립니다.
5. initial/final의 `state_fingerprint`가 다르거나 final 뒤 initial·중복 final이 오면 버립니다.
6. 동일 요청의 final도 snapshot 전체를 교체합니다. 이전 source/card 배열과 합치지 않습니다.

`brief_consumer.py`는 위 동작을 검증하는 작은 참조 구현입니다. 실제 DB optimistic concurrency·revision transaction·권한 검사 구현은 Backend 팀의 책임입니다. 모델 API는 알리지 않은 Backend 변경을 감지하지 못합니다. `state_fingerprint`는 비교용 hash이며 인증 토큰도, 대원 확인 서명도, 최신 상태 보증도 아닙니다. 공유 캐시는 공통 모델 인덱스뿐이며 요청별 신고문/카드 cache는 없습니다. JSON과 SSE에 `Cache-Control: no-store`를 적용합니다.

STT 연동에서는 `speech-service`의 **`transcript.text`**를 `analysis.input.text`, 입력 종류를 `VOICE_TRANSCRIPT`로 전달합니다. `status=ABSTAINED_NO_TRANSCRIPT` 또는 `abstained=true`이면 분석에 빈 문장을 보내지 말고 재입력 안내를 표시합니다. `transcript.segments[].quality_signals`는 보정된 정답 확률이 아니며 확인 권한으로 쓰지 않습니다. 구간별 신호와 `runtime.processing_seconds`는 Backend가 전사 기록과 함께 별도 보존합니다. 행동 카드 v1이 이 수치를 이용해 ASR 정답이나 전술을 결정하지는 않습니다.

음성 파일 업로드·Whisper 추론은 이 endpoint 범위가 아닙니다. 원문을 그대로 유지하고 STT·분석 지연시간을 따로 계측하세요. 분석 입력 한도는 4,000자이며 초과 입력을 조용히 자르지 않습니다. 주소 비식별 구간은 접수 시스템의 별도 위치 정보로 보완해야 하며 모델이 복원하지 않습니다.

## 테스트·평가

```bash
python -m pytest
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python scripts/contracts/export_contracts.py --check

python -m chemiguard119.action_evaluation \
  --artifact-dir "$CHEMIGUARD119_ARTIFACT_DIR" \
  --output /비공개/평가/action-brief-run-1 --repeats 5

python -m chemiguard119.action_evaluation \
  --artifact-dir "$CHEMIGUARD119_ARTIFACT_DIR" \
  --output /비공개/평가/action-brief-run-2 --repeats 5 --reverse-order

python scripts/evaluate_action_brief_http.py \
  --output /비공개/평가/action-brief-http.json --repeats 10
```

보고서는 기존 경로를 덮어쓰지 않습니다. 실제 artifact와 합성 입력의 SHA-256, 환경, 정책·문구·소스코드 hash, 실패 사례, 순차/병렬 의미 결과 비교를 기록합니다. mock은 모델 성능 증거가 아닙니다. actual artifact 평가도 실제 신고/현장 음성 평가가 아닙니다. 이 결과를 419건 Resolver, 442건 Parser, STT, 12질의 DRAFT 검색 지표에 합치지 않습니다.

외부 서버/GPU/LLM 추가 비용은 이번 작업 **0원**입니다. 계정 누적 과금은 이 로컬 실행으로 확인할 수 없습니다. 외부 배포·새 결제·운영 변경은 별도 승인 범위입니다.
