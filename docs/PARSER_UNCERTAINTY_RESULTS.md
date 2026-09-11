# 신고문의 추정·미확인·반복 진술을 보존하는 Parser

판정: **개발용 조건부 채택**. 제품 상태는 **부분 구현 또는 개발용 데모**다.
이번 변경은 신경망 학습이 아니라 기존 Parser의 원문 구간·문맥 규칙 개선이다.
사람 검수 독립 성능·현장 적용 효과는 **검증되지 않은 가설**이다.
브랜치 `modeling/parser-uncertainty`는 행동 카드 PR #61 위에 쌓았으며 main 병합·배포하지 않는다.

## 무엇이 달라졌나

| 합성 신고 표현 | 기존 동작 | 새 동작 |
|---|---|---|
| 염산인 것 같습니다 | 물질 누락 | 염산 후보 + `POSSIBLE` |
| 염산 미확인 | 부정으로 해석 | `UNCONFIRMED`, 부재로 확정하지 않음 |
| 염산은 없습니다. 염산이 누출됩니다. | 같은 CAS라 첫 언급만 남음 | 두 원문 구간·각 진술 보존, 재확인 표지 |

이렇게 남겨야 팀원이 “염산이 확실히 있다”와 “염산 같다고 들었다”를 구분할 수 있다.
원문을 고쳐 쓰거나 후보를 확인된 CAS로 바꾸지 않는다. 더 많은 LLM의 합의는 사용하지 않는다.

## 비교 설계와 실제 결과

[사전 계획](PARSER_UNCERTAINTY_PLAN.md)은 `9c17083`에 고정했다.
합성 문장과 기대 라벨을 먼저 만들고 A(기존), B, 최대 두 번째 후보 B2까지 비교했다.
19개 family의 물질 치환 포함 47건·기대 언급 44개이며, 모두 **AI DRAFT 개발 회귀**다.
비공개 평가·독립 사람 정답·전문 검수가 아니다. 별도 학습 데이터나 학습 실행은 없다.

| 합성 회귀 지표 | A: 기존 | B2: 최종 | 분모·해석 |
|---|---:|---:|---|
| 물질 언급 TP / FN / FP | 28 / 16 / 0 | 44 / 0 / 0 | 원문 exact span |
| 물질 언급 Precision | 100% | 100% | 추출한 언급 |
| 물질 언급 Recall / F1 | 63.64% / 77.78% | 100% / 100% | 기대 언급 44개 |
| 추출+진술 상태 공동 정답 | 21/44 | 44/44 | 누락도 오답에 포함 |
| 추출+역할 공동 정답 | 23/44 | 44/44 | legacy NEGATED 역할 포함 |
| 후보 제공 사례 | 26/47 | 38/47 | 물질이 아닌 대조 사례 9개는 후보 없음이 기대 동작 |
| 필요한 후보를 전부 누락한 사례 | 12 | 0 | 전부 기권해서 얻은 개선이 아님 |
| 부분 문자열·제품명 대조군 오탐 | 0 | 0 | 사전 정의 대조 사례 9개 |
| 원문 밖 물질 / CAS 자동승격 | 0 / 0 | 0 / 0 | 이 합성 분모의 관측값 |

F1 차이 +22.22%p, family 단위 paired bootstrap 95% 구간 **+8.11~+41.18%p**
(seed 119, 2,000회)다. 이는 합성 family 재표집 구간이며 실제 신고 모집단 신뢰구간이 아니다.
전체 assertion/역할 혼동표와 범주별 원본 보고서 hash는
[집계 artifact](../data/evaluation/parser_uncertainty_v1_summary.json)에 연결돼 있다.

B는 언급 44개를 찾았지만 “염산은 없으며, 질산이 누출됩니다.”에서 첫 부정을 잃었다.
접속 어미를 절 경계에서 잘라낸 원인을 B2에서 고쳤다. B를 숨기지 않고 보고서를 보존했다.
이후 새 의미 분류 후보는 탐색하지 않았다. [개발 기록](PARSER_UNCERTAINTY_DEVELOPMENT.md) 참고.

### 전국 공식 사고문 442건 — 별도 회귀

2021~2025년 공식 사고 정리문, 이미 관측한 평가셋이다. 원본·실패 행을 개발용으로 열람하지 않았다.

| 지표 | 이번 A | 이번 B2 |
|---|---:|---:|
| 사고유형 Recall (대상 사례 431건) | 83.7587% | 83.7587% |
| 물질 언급 containment Recall (관찰 가능한 라벨 319개) | 82.4451% | 82.4451% |
| 물질 언급 exact Recall | 71.1599% | 71.1599% |
| 미확인 후보 Rule 입력 승인 | 0건 | 0건 |

여기서는 **개선이 아니라 비회귀**다. 전체 물질·역할·부정 정답이 없는 원천이므로
위 100% 합성 성능을 공식 사고문이나 신고전화 성능으로 옮기지 않는다.

**과거 81.50%와 충돌하는 이유:** 과거 `official_national_incident_parser_locked_2026-08-01.json`은
Resolver SHA `27e64582…`, 이번 A/B2는 `2696fa7f…`다. 같은 442건·관찰 가능 319라벨·원본 SHA지만
artifact/Parser 코드 시점이 다르다. 가장 정확한 표현은 “현재 동일 artifact에서 82.4451% 유지”다.
과거와의 전체 차이 원인을 분해하려면 과거 artifact와 코드로 별도 ablation이 필요하다.

울산 **Resolver 419건은 재실행하지 않았다**. 승인된 원본 경로가 확보되지 않았고 #25가 차단 상태다.
Resolver 코드·모델 bytes 불변을 419건 실측 비회귀로 표현하지 않는다.

### 기존 STT 결과 표본 — 판단 보류

승인된 private 기록에서 내용과 무관한 `sha256(parser-stt-v1:record_key)` 순서로 지역별 50건을 골랐다.
서울 965·인천 129·광주 77건 중 각 50건이며 음성 전사 재실행·새 학습은 없다.
기존 Parser가 참조 전사에서 찾은 표면형/CAS 후보 집합을 고정 분모로 사용한 **실버 보존 감사**다.

| 지역 | 표본 | 참조 CAS 후보 분모 | A 보존 | B2 보존 |
|---|---:|---:|---:|---:|
| 서울 | 50 | 0 | 해당 없음 | 해당 없음 |
| 인천 | 50 | 0 | 해당 없음 | 해당 없음 |
| 광주 | 50 | 1 | 0 | 0 |

총 150건에서도 참조 후보 분모가 1개뿐이었다. 신규 후보 추가·제거는 없었다.
정답 물질이 실제로 없다는 뜻도, 음향 오류임이 입증됐다는 뜻도 아니다.
사람 CAS 정답이 없어 STT→Resolver 정답 Top-3는 **미측정**이다. 이 결과로 LoRA를 재개하지 않는다.
서울·인천 감사는 자원 상한 보완 전 B2, 광주는 보완 후 B2다. 해당 보완은 분류·후보를 바꾸지 않는다.

## 자원·API 검증

- 로컬 단위·계약 테스트: **645 passed**, 기존 TestClient deprecation warning 1개. CI는 해당 PR의 필수 검사를 별도로 확인한다.
- 실제 DB·Resolver·Retriever로 새 Parser 사례·JSON/SSE·Swagger **50개 검사 통과**, 해당 평가의 Rule 호출 0회.
- 기존 행동 카드 **15시나리오**, 순차/병렬 측정 runs 30개에서 실패 0건. 독립 사례 수를 30개로 세지 않는다.
- 실제 localhost HTTP에서 미확인·두쪽 확인·확인 취소 SSE 3개 요청 통과. `/docs`, `/openapi.json`, API Key 계약 확인.
- 실제 JSON의 “염산인 것 같습니다…”에서 POSSIBLE·원문 SHA·span 일치·Rule 미실행 확인.
- 취소·늦은 응답·timeout·작업 슬롯·LLM 실패·문서 명령문은 기존 단위/장애 주입 회귀와 구분한다. 모든 장애를 실제 외부 서비스에서 재현했다는 뜻이 아니다.

4,000자 반복 합성 신고에서 언급 727개·충돌 쌍 132,132개로 JSON이 25,879,956 bytes까지 커지는
문제가 추가로 발견됐다. B2의 분류 결과는 유지하고 충돌 상세를 최대 64쌍으로 제한했다.
동일 입력의 언급 727개·재확인 표지는 보존되며 JSON **801,695 bytes**로 줄었다.
`statement_conflict_limit_reached=true`이면 목록이 완전하지 않을 수 있다. 전체 충돌 수로 세지 않는다.

Apple M4·24GiB·Python 3.11.15·macOS arm64, GPU/LLM 없음.
합성 47건 warm Parser p95 **76.71 → 83.99ms**, 사전 상한 약 115.06ms 이내다.
최대 RSS 관찰값은 각 약 153/152MiB이며 서비스의 최대 메모리 보장은 아니다.
전국 442건 mean/p95는 78.18/81.41 → 80.11/84.06ms다.
이번 목적은 속도 개선이 아니다. 작은 표본·실행 순서·동시 로컬 작업 영향을 포함하며
STT·WAN·Cloud 시간은 제외한다. 기존 병렬 기본값은 변경하지 않는다.

## Backend·Front 연동: 추가 필드

외부 JSON 형태 `action-brief-v1`과 endpoint는 유지했다. 중첩 `facts`/`model_outputs.parser`에
`parser_policy_version=incident-parser-policy-v3-span-uncertainty`를 추가했다.
행동 카드 정책은 `action-policy-v2-statement-clarification`이다.

```json
{
  "mention_id": "mention-0-2",
  "start": 0,
  "end": 2,
  "surface_text": "염산",
  "role": "UNKNOWN",
  "context_role": "UNKNOWN",
  "assertion": "POSSIBLE",
  "assertion_basis": "LOCAL_RULE_NOT_HUMAN_CONFIRMATION"
}
```

위는 합성 입력 “염산인 것 같습니다…”의 언급 필드 발췌다. Resolver 필드는 생략했다.

- `start` 포함/`end` 제외, **Unicode code point** 기준. JavaScript는 `Array.from(originalText).slice(start,end).join('')`로 대응한다. UTF-16 `slice`를 그대로 쓰면 emoji 앞에서 어긋날 수 있다.
- 내부 Parser는 원문을 그대로 보존한다. 기존 API 개인정보 최소화 계약에 따라 전체 `source_text`는 응답에서 제거한다. 클라이언트가 보유한 원문·`input_provenance.text_sha256`·span으로 연결한다. 필드 부재는 원문 교정이 아니다.
- `AFFIRMED`는 긍정 진술일 뿐 사용자 확인이 아니다. `POSSIBLE`, `UNCONFIRMED`, `NEGATED`도 확인 기록과 분리한다. 모르는 상태값을 긍정/확인으로 처리하면 안 된다.
- 호환을 위해 부정 언급은 기존 `role=NEGATED`를 유지하고, 별도 `context_role`에 INCIDENT/FACILITY/UNKNOWN을 남긴다.
- 같은 CAS도 언급마다 보존한다. CAS를 배열 key로 쓰거나 첫 언급만 남기지 않는다. `mention_id`는 해당 입력 snapshot 안에서만 유효하다.
- 부정/비부정·유사 역할·같은 상위 CAS 후보의 병존은 `requires_statement_clarification`으로 표시한다. 실제 모순을 확정하지 않는다. 다른 위치/시점이면 양립할 수 있다.
- **행동 카드 API**는 확인 기록과 위 표지가 함께 있으면 `HELD`, `STATEMENT_CLARIFICATION_REQUIRED`로 Rule을 보류한다. 기존 일반 분석 endpoint 전체에 의미 충돌 차단을 확장한 것은 아니다. 그쪽도 원래 2-CAS Gate는 유지한다.
- revision·취소·오래된 응답 폐기 책임은 기존 [연동 계약](ACTION_BRIEF.md)과 같다. Front/Backend는 이번에 수정하지 않았다.

## 재현하기

설치·artifact 준비·Swagger 실행은 [행동 카드 안내](ACTION_BRIEF.md)를 따른다.
아래 `PARSER_ARTIFACT_DIR`, `PARSER_OUTPUT_DIR`는 사용자가 승인 경로로 바꾼다.
평가기는 기존 보고서를 덮어쓰지 않는다. 공식 사고문 평가의 출력도 반드시 새 경로로 지정한다.

```bash
export PARSER_ARTIFACT_DIR=/승인된/비공개/action-brief-local-v1
export PARSER_OUTPUT_DIR=/비공개/새로운/parser-uncertainty-reproduction
python -m pytest
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python scripts/contracts/export_contracts.py --check

python -m chemiguard119.parser_uncertainty_evaluation \
  --resolver-model "$PARSER_ARTIFACT_DIR/resolver.joblib" \
  --arm B2-bounded --output "$PARSER_OUTPUT_DIR/candidate.json"
python scripts/evaluation/evaluate_parser_api.py \
  --artifact-dir "$PARSER_ARTIFACT_DIR" --output "$PARSER_OUTPUT_DIR/api.json"
python scripts/evaluation/evaluate_parser_resource.py \
  --resolver-model "$PARSER_ARTIFACT_DIR/resolver.joblib" \
  --output "$PARSER_OUTPUT_DIR/resource.json"
python -m chemiguard119.action_evaluation \
  --artifact-dir "$PARSER_ARTIFACT_DIR" --repeats 1 \
  --output "$PARSER_OUTPUT_DIR/action"
chemiguard119 evaluate-official-incidents \
  --source data/raw/09_CSI_전국_화학사고정보_20250430.csv \
  --resolver-model "$PARSER_ARTIFACT_DIR/resolver.joblib" --split locked_test \
  --report "$PARSER_OUTPUT_DIR/official.json" --json
```

A의 동일 평가기는 `9c17083`에서 실행한다(평가 코드는 있고 Parser는 기존 v2 상태).
현재 checkout을 되돌리지 말고 별도 worktree/환경에서 실행한다. 후보 비교에는
`--baseline /비공개/A의/baseline.json`을 추가한다. 입력·Resolver SHA가 다르면 비교가 차단된다.
기존 공식 원본 SHA는 `a1ef8e4b6b0c6ef96fb7277edce2b1cf5c1b935a88f4274c88d878713bb7fba5`다.
잠금 사고 원문·실패 행·업체/주소를 콘솔이나 Git에 넣지 않는다.

STT 표본 감사는 다음과 같이 기존 승인된 baseline 기록과 summary를 쌍으로 전달한다.
모델 표기가 `small`이 아니라 snapshot 경로라면 provenance 확인 후 `--expected-stt-model`로 정확히 지정한다.

```bash
python scripts/evaluation/evaluate_parser_stt_sample.py \
  --records /비공개/승인된/records.private.jsonl \
  --summary /비공개/승인된/summary.json --region seoul \
  --resolver-model "$PARSER_ARTIFACT_DIR/resolver.joblib" \
  --output "$PARSER_OUTPUT_DIR/stt-seoul.json"
```

모든 보고서와 입력·artifact·runtime source·정책·평가기 SHA는
[집계 JSON](../data/evaluation/parser_uncertainty_v1_summary.json)에 있다. 원본 보고서는 승인된
`private-data/experiments/analysis/parser-uncertainty-v1/`에 보관한다. 원본/가중치는 Git에 없다.
수치 원본은 재실행 시 latency·환경·시각 때문에 hash가 달라질 수 있으며 입력/모델 hash와 지표를 함께 비교한다.

## 남은 판단과 다음 단계

### PR 리뷰 회귀 보완

정책 `incident-parser-policy-v3.1-negation-scope`에서 `없지만/없으나`의 부정을 보존하고, `염산이 아닌 질산`에서는 앞 물질의 부정을 뒤 물질에 전파하지 않도록 문맥 경계를 수정했습니다. 접속 어미 4종과 대조 표현 3종을 합성 계약 테스트로 추가했으며, 앞뒤 진술이 충돌하면 두 확인 기록이 있어도 행동 카드 Rule을 보류하는 경계를 검사합니다. 원문·언급 span·후보 미확정 계약은 유지합니다. 위 표의 B2 수치는 당시 잠금 실행 결과이며 이 후속 수정의 새 442건 측정값으로 재사용하지 않습니다. 한국어 전체 부정 의미가 해결되었다는 주장은 하지 않습니다.

- **부분 구현 또는 개발용 데모:** 제한된 문맥 규칙·언급 보존·카드 보류. 한국어 전체 의미 이해나 모든 부정 범위를 처리하지 않는다. 예를 들어 물질 부재와 특정 냄새의 부재 구분, 시간·탱크별 진술 분리는 추가 검수가 필요하다.
- **설계 완료·구현 전:** 사람 검수 자료가 확보된 뒤 작은 불확실성 분류기/도메인 Resolver의 다음 비교. 기존 sparse를 유지한다. 419·60건 원본 확보는 #25에서 관리한다.
- **부분 구현 또는 개발용 데모:** Retriever qrel 제작·검수 도구는 기존에 준비됐다. 171개 후보의 검수 상태는 `NOT_STARTED`이며 사람 검수 전 검색 모델 학습은 #33에서 보류한다.
- **검증되지 않은 가설:** 새 신고문·미관측 물질·실제 무전 일반화, 필요한 질문 감소, 현장 사용자 시간 절감·안전성.

이번 추가 서버 비용 **0원**(로컬 CPU만). 계정의 기존 누적 비용은 **미확인**이다.
새 GPU·유료 LLM·Cloud 실행·Notion 수정·외부 배포·main 병합은 하지 않았다.
원본·사람 라벨 부족을 합성 정답으로 몰래 대체하지 않고, 다음 학습 채택은 보류한다.
