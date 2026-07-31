# 케미체크119 최종 브리핑

기준일: 2026-07-31
발표용 결론: **AI API 로컬 데모 준비 완료, 내부 shadow 파일럿은 조건부, 외부 상용 운영은
검증·라이선스 gate가 의도적으로 차단**

> 발표 전 명칭 확인: 회의안은 `케미가드`, 현재 코드·API·저장소 문서는 `케미체크119`입니다.
> FE·BE와 함께 확정해 한 번에 변경하기 전에는 두 이름을 섞어 발표하지 않습니다.

## 1. 60초 발표문

케미체크119는 화학사고 신고를 생성형 AI 하나에게 맡기는 서비스가 아닙니다. 신고문에서
물질·사고 상황을 구조화하고, 약 4,300개 물질 카탈로그에서 CAS 후보를 찾은 뒤, KOSHA와
CAMEO 공식 근거를 질문에 맞는 MSDS 절 단위로 검색합니다. 사고물질과 시설물질의 CAS를
현장에서 각각 확인했을 때만 결정론적 CAMEO 충돌 규칙을 실행합니다.

물질명을 모르는 상황도 고려했습니다. 상태·색상·냄새·용도 관찰을 두 가지 이상 입력하면
소방청 공개자료 749개 성상 프로필에서 후보를 찾고, 같은 CAS의 공식 근거를 연결합니다.
후보는 절대 자동 확정하지 않으며 현장 확인 뒤에만 충돌 검토로 넘어갑니다.

핵심 참신성은 **근거 잠금형 하이브리드 AI(Evidence-Gated Hybrid AI)**입니다. TF-IDF·BM25
검색은 표현 차이를 처리하고, 규칙 엔진은 충돌 검토를 재현 가능하게 만들며, 현장 확인 gate는
후보를 사실로 승격하지 못하게 합니다. LLM은 신고문 구조화·근거 요약 같은 보조 기능으로만
확장할 수 있어 외부 LLM 장애가 나도 핵심 기능이 유지됩니다.

이번 고도화에서는 같은 CAS 문서이면 모두 정답으로 보던 평가 오류를 고쳤습니다. 내부 12개
section 회귀에서 MRR@5는 약 0.09에서 0.94, nDCG@5는 약 0.16에서 0.93으로 개선됐습니다.
추가로 전체 Recall 0.875를 분석한 결과, 핵심 근거 14개는 모두 찾았고 빠진 것은 보조 근거
3개였습니다. 문서 수뿐 아니라 실제 답변 사실도 같은 결과인지 다시 검사했습니다. 핵심 사실은
모두 찾았지만 12건 성공의 95% 신뢰구간 하한은 약 75.8%이므로 상용 정확도라고 주장하지
않습니다. 코드가 DRAFT 평가와 검수 완료 평가를 분리하며, 현재 데이터로 운영 릴리스를
시도하면 자동 차단합니다.

## 2. 실제 파이프라인

```text
물질검색 탭(물질명을 모를 때)
  관찰 정보 → /substances/discover → 복수 후보·공식 출처 → 현장 확인

대응충돌검토
  신고문 → /incidents/analyze
  → 입력·인증 검증
  → 규칙 기반 신고문 구조화
  → 표준명·CAS·별칭 + 문자 TF-IDF 물질 후보
  → ICIS·PRTR 과거 취급 이력 후보
  → KOSHA·CAMEO section 근거 검색
  → 사고물질·시설물질 현장 확인 gate
  → 확인된 CAS 두 개만 CAMEO 결정론 규칙 실행
  → 위험·근거·한계·버전이 포함된 FastAPI JSON
  → 소방 대시보드
```

대시보드에는 다음처럼 연결합니다.

| 화면 | API 정보 | 표시 원칙 |
|---|---|---|
| 물질검색 | `/substances/discover` 후보·성상·출처 | 복수 후보, 현장 확인 필요, 점수는 확률 아님 |
| 대응충돌검토 | `conflict_review` | CAS 두 개 확인 전 위험도·구체적 반응 숨김 |
| 업체 물질 | `facility_history_candidates` | 현재 재고가 아닌 과거 취급 후보로 표시 |
| 대응 근거 | `evidence[].source_url`, 문서 버전 | 질문에 맞는 MSDS section을 우선 표시 |
| 상태 배지 | `state`, `confirmation_gate` | 확인 대기·검토 완료를 색과 문구로 구분 |

## 3. 수치로 확인한 현재 범위

| 항목 | 현재 수치 | 정확한 의미 |
|---|---:|---|
| 물질 카탈로그 | 약 4,300 CAS | 후보 검색 범위 |
| 관찰 검색 프로필 | 749 CAS | 울산 원천 4,378행 중 카탈로그 연결 성상 |
| 근거 문서 | 약 5,858건 | KOSHA 9종 상세 section + CAMEO 문서 |
| 업체 이력 후보 | 약 168,424건 | 현재 재고가 아닌 과거 취급 후보 |
| CAMEO 공개 검증 | 6 CAS·15쌍 | 지원된 조합만 결정론적으로 검토 |
| Resolver 회귀 | 21건 DRAFT | 현장 정확도가 아닌 코드 회귀 |
| 기존 Retriever 회귀 | 10건 DRAFT | 같은 CAS 묶음 도달 검사 |
| Section 회귀 | 12건 DRAFT | 질문에 맞는 MSDS 절 순위 검사 |
| Parser seed | 6건 DRAFT | 성능 평가가 아닌 형식 시드 |
| 사고 분석 E2E | 8건 DRAFT | 확인 gate·기권·충돌 규칙 상태 전이 회귀 |
| 파인튜닝 승인 데이터 | 0건 | 현재 파인튜닝 실행 불가 |

## 4. 이번 고도화의 실제 결과

회의에서 요청한 물질검색 탭을 mock이 아닌 별도 모델 API 계약으로 만들었습니다.

| 입력 | 결과 |
|---|---|
| 무색·투명·박하 냄새·휘발성 액체 | 메틸 에틸 케톤 `78-93-3` 1순위 |
| 냄새가 나는 액체 | 정보 부족으로 후보 없음 |
| 염산 | 염화수소 `7647-01-0`, KOSHA·CAMEO 근거 카드 3개 |

성상 검색 50회 로컬 warm 실행은 평균 8.634ms, p95 12.618ms였습니다. 이 값은 실제
네트워크·컨테이너를 포함하지 않은 기능 smoke이므로 운영 SLO나 정확도로 발표하지 않습니다.
성상 후보는 최소 두 물성 영역이 맞아야 하며, 모두
`requires_responder_confirmation=true`, `rule_eligible=false`입니다.

현재 KOSHA 상세 근거가 9종이므로 메틸 에틸 케톤 후보에는
`CAS_EVIDENCE_NOT_LOADED`가 표시됩니다. 이를 다른 물질 근거로 채우지 않는 것이 안전
설계입니다.

### 실제 사고 분석 E2E 결과

기존에는 물질검색 21건, 검색 10건, 파서 시드 6건처럼 모듈을 따로 확인했습니다. 이제
실제 `analyze_incident` 경로로 8가지 상태 전이를 자동 검사합니다.

```text
8/8 시나리오 통과
미확인 Rule 실행 0건
미확인 위험 노출 0건
기권 필요 7/7 통과
평균 86.610ms / p95 112.537ms
```

이 결과는 `INTERNAL_REGRESSION_ONLY`입니다. “정확도 100%”라고 발표하지 않고 “설계한
안전 상태 전이 8종이 실제 artifact에서 재현됐다”고 설명합니다.

### 기존 section 검색 고도화 결과

동일 artifact·동일 12개 내부 질의에서 비교했습니다.

| 지표 | 기존 CAS 순서 편향 | section 중심 검색 | 변화 |
|---|---:|---:|---:|
| nDCG@5 | 0.1595 | 0.9284 | +0.7688 |
| Recall@5 | 0.3333 | 0.8750 | +0.5417 |
| Precision@5 | 0.0833 | 0.2333 | +0.1500 |
| MRR@5 | 0.0875 | 0.9444 | +0.8569 |
| 같은 CAS의 판정된 오답 절 비율 | 0.6667 | 0.0000 | 감소 |
| 평균 지연시간 | 20.14ms | 21.73ms | +1.58ms |

현재 `Recall@5=0.875`를 한 단계 더 분해하면 다음과 같습니다.

| 현재 지표 | 값 | 쉬운 의미 |
|---|---:|---|
| 핵심 근거 Recall@5 | 1.0000 | grade 2~3 핵심 근거 14개를 모두 찾음 |
| 보조 근거 Recall@5 | 0.0000 | grade 1 참고 절 3개는 Top-5 밖 |
| 중요도 가중 Recall@5 | 0.9688 | 핵심 근거에 더 큰 가중치를 둔 회수율 |
| 필수 사실 coverage@5 | 0.8750 | 문서가 아니라 답변에 필요한 사실을 찾은 비율 |
| 핵심 사실 coverage@5 | 1.0000 | 핵심 답변 사실은 내부 12건에서 모두 회수 |
| 핵심 성공률의 Wilson 95% 구간 | 0.7575~1.0000 | 12건뿐이므로 1.0을 과신할 수 없음 |

해석:

- CAS는 같은 물질의 문서만 남기는 **필터**로 사용합니다.
- 물질명·CAS가 모든 section에 반복되는 효과를 제거합니다.
- 보호구·저장·정화·소화제 같은 질문 의도로 BM25와 단어/문자 TF-IDF를 결합합니다.
- CAS 제한을 전역 Top-N 절단 전에 적용해, 다른 물질 문서가 같은 CAS 근거를 밀어내지
  못하게 했습니다.
- 반환 문서의 76.7%가 아직 unjudged이므로 높은 nDCG·MRR을 외부 성능으로 인용하지
  않습니다.
- 평가기 v3는 누락한 핵심·보조 `evidence_id`와 `required_fact_ids`를 따로 기록해 다음
  개선 대상을 바로 찾습니다.
- DRAFT 12건에 맞춘 section 규칙으로 Recall을 억지로 1.0으로 만들지 않았습니다.
- 이 수치는 `INTERNAL_REGRESSION_ONLY`이며 지연시간도 로컬 단일 실행이라 운영 SLO가
  아닙니다.

원본 비교 기록은
[`retrieval_section_comparison_2026-07-28.json`](../data/evaluation/retrieval_section_comparison_2026-07-28.json)에
있고, 핵심·보조 근거 분해 결과는
[`retrieval_section_metric_audit_2026-07-31.json`](../data/evaluation/retrieval_section_metric_audit_2026-07-31.json)에
있습니다.

개발 환경에서 물질 검색 40건을 동시성 10으로 호출한 smoke는 40/40 성공, 평균 6.59ms,
p95 8.24ms였습니다. 이는 실제 네트워크·컨테이너 제한을 포함하지 않은 TestClient 검사이며
운영 SLO로 사용하지 않습니다.

## 5. 파인튜닝을 하지 않은 이유

`finetune-check` 실행 결과는 다음과 같습니다.

```text
status: NOT_READY
승인 train / valid / locked_test: 0 / 0 / 0
DRAFT 시드: 6건
준비도 정책 목표: train 500+, valid 100+, locked_test 100+
```

6개 DRAFT 문장을 증식해 파인튜닝하면 문장 패턴을 외우는 모델은 만들 수 있지만 일반화
성능은 증명할 수 없습니다. 따라서 이번에는 검색 오류를 수치로 찾아 고치는 편이 실제 품질과
공모전 설명력 모두에서 타당했습니다.

향후 파인튜닝 대상은 신고문 개체 추출 또는 후보 reranker로 제한합니다. 화학 충돌 위험,
위험 확률, 출처 없는 대응 절차를 생성하는 모델은 학습하지 않습니다.

## 6. 상용 준비 판정

| 단계 | 판정 | 이유 |
|---|---|---|
| AI API 로컬 시연 | 준비 완료 | API·CLI·안전 계약·내부 회귀를 실제 재현 |
| FE→BE→AI 통합 시연 | 별도 확인 필요 | 이 저장소 밖 실제 staging 호출은 아직 검증하지 않음 |
| 팀 내부 파일럿 | 조건부 가능 | FE→BE→AI staging 검증 전에는 의사결정 비영향 shadow mode만 가능 |
| 외부 스테이징 | 현재 차단 | reviewed 평가·Python 3.11 release bundle·Secret·URL 검증 필요 |
| 공개 컨테이너 배포 | 현재 차단 | CAMEO·ICIS 파생 데이터 재배포 검토 미완료 |
| 실제 상용·현장 운영 | 현재 차단 | 독립 locked test, 부하/SLO, 키 회전, 로그 정책, 현장 pilot 필요 |

이 판정은 실패가 아니라 상용 시스템에 필요한 **fail-closed gate가 작동한다는 증거**입니다.
현재 코드는 `PILOT_REVIEWED` 평가와 모든 데이터 재배포 승인이 없으면 staging·production
manifest를 거부합니다. profile 이름이나 사례 수 JSON만 바꿔 우회할 수도 없습니다. 배포
gate는
원본 dataset·evaluator report SHA, locked-test provenance, 실제 품질 임계값, 0건 위험 CAS
자동확정의 95% 신뢰상한과 별도 Secret으로 서명된 현장검증 attestation을 다시 확인합니다.
Resolver 1,200건, 안전 hard case 300건, section qrel 400건, parser 400건, E2E 200건은
정확도를 보장하는 숫자가 아니라 작은 표본의 성능 과장을 막는 릴리스 하한입니다.
Section 400건은 답변 가능 300건 이상과 답변 불가 100건 이상으로 나눕니다. 전체 Recall
0.90·핵심 Recall 0.98·중요도 가중 Recall 0.95뿐 아니라 핵심 사실 coverage 0.98,
답변 불가 기권율 0.95와 핵심 사실 완전 회수율의 95% 신뢰구간 하한 0.95를 동시에
요구합니다. 현재 DRAFT 결과는 핵심·가중 기준을 넘지만 전체 Recall 0.875가 낮고 독립
검수 사례와 답변 불가 사례가 0건이므로 사례 수만 채우거나 지표 하나만 골라도
릴리스되지 않습니다.
이 임계값은 보편적인 업계 인증값이 아니라 프로젝트가 정한 보수적 파일럿 진입 정책입니다.
안전 회귀 12건에서 오류 0건이었지만 95% 단측 오류율 상한은 약 0.2209로 운영 기준 0.01을
크게 넘습니다. 그래서 0건이라는 숫자만으로 안전성을 주장하지 않고 최소 300건 hard case
gate를 둡니다.

## 7. 최신 AI 기술과 참신성

사용할 발표 표현은 다음과 같습니다.

> **Evidence-Gated Hybrid Incident Copilot**
> 검색 계층이 후보와 공식 근거를 찾고, 결정론 규칙이 충돌을 검토하며, 현장 확인과 provenance
> gate가 후보를 사실로 오인하지 못하게 하는 소방 MDT용 의사결정 지원 시스템

이 구조는 LLM 단독 챗봇보다 다음 점이 다릅니다.

- deterministic rule + human confirmation + graceful degradation
- hybrid lexical retrieval + section-level graded evaluation
- abstention/unknown을 정상 결과로 취급
- 모델·데이터·규칙·근거 URL의 provenance
- LLM 장애 시에도 핵심 기능 유지

설계 방향은 [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework)와
[NIST의 중요 인프라 AI RMF 프로필 작업](https://www.nist.gov/programs-projects/concept-note-ai-rmf-profile-trustworthy-ai-critical-infrastructure),
[NIST SSDF 1.1](https://csrc.nist.gov/pubs/sp/800/218/final)의 위험관리·안전한 개발 원칙에
맞춥니다. 검색 고도화는
[HYRR의 retrieve-then-rerank 및 데이터 품질 관점](https://aclanthology.org/2024.lrec-main.748/)을
참고하되, 현재 gold data로 개선이 증명되지 않은 dense·reranker를 장식처럼 운영 경로에
넣지 않았습니다. CAMEO 판정 근거는
[NOAA CAMEO Chemicals](https://cameochemicals.noaa.gov/)를 사용합니다.

## 8. 심사 질문에 대한 짧은 답

**“물질명을 모르면 어떻게 하나요?”**
색상·냄새·상태·용도 중 구별되는 관찰을 두 가지 이상 입력해 공개자료 기반 후보를 좁힙니다.
후보와 출처를 보여줄 뿐 자동 확정하지 않고, 라벨·현장 MSDS로 확인한 뒤 충돌 검토합니다.

**“정확도가 몇 퍼센트인가요?”**
현재 21·10·12건은 회귀 테스트이므로 현장 정확도라고 말하지 않습니다. 대신 잘못된 CAS 확정
방지, section 핵심·보조·가중 Recall, 필수 사실 coverage와 충돌 규칙 전수 회귀를
분리합니다. 현재 12건에서 핵심 근거와 핵심 사실은 모두 찾았지만 95% 구간 하한은 약
75.8%이고 답변 불가 사례도 0건입니다. 독립 locked set 전에는 staging·production을
차단합니다.

**“왜 최신 LLM을 파인튜닝하지 않았나요?”**
승인 데이터가 0건이어서 파인튜닝 수치를 제시하면 과적합을 성능으로 포장하게 됩니다. LLM은
안전 판정자가 아니라 신고 구조화·근거 요약 보조로 제한하고, 충분한 locked set이 생긴 뒤
기준선과 비교합니다.

**“이 서비스의 AI는 무엇인가요?”**
물질 후보 검색, section 근거 검색, 신고 구조화, 결정론적 화학 충돌 엔진을 결합한
근거 잠금형 하이브리드 파이프라인입니다. 단일 생성형 모델 호출이 아니라, 사고 분석 통합
API와 물질탐색 전용 API가 목적별 안전 모듈을 실행합니다.

**“지금 상용화할 수 있나요?”**
코드는 상용 진입 심사를 자동화할 수준까지 보강됐지만, 현장 성능과 데이터 재배포 권리가
미승인이라 실제 운영은 차단됩니다. 공모전 데모는 가능하지만, 내부 shadow pilot도
독립 검수 데이터와 FE·BE 스테이징 연동을 먼저 완료해야 합니다.

**“이 서비스가 진짜 필요한가요?”**
미국 CSB가 기록한 Blues City Brewery 사고에서는 산성 용액이 표백제 용액에 잘못
이송돼 염소가스가 발생했고 10명이 병원으로 이송됐습니다. 케미체크119는 바로 이런 상황에서
시설 내 다른 물질 후보, 현장 확인 상태와 공개 혼합 위험 근거를 출동 중 한 화면에 모으는
서비스입니다. 생성형 답변보다 “후보를 확정으로 오인하지 않는 빠른 정보 정리”가 핵심입니다.

## 9. 브리핑 직전 실행

```bash
# 전체 테스트
python -m pytest

# 물질명을 모르는 현장 관찰 후보 데모
chemiguard119 discover "무색 투명하고 박하 냄새가 나는 휘발성 액체"

# 내부 section 평가
chemiguard119 evaluate \
  --only retriever \
  --evaluation-profile INTERNAL_REGRESSION \
  --json

# 신고부터 확인 gate·충돌 규칙까지 E2E 안전 회귀
chemiguard119 evaluate-e2e \
  --evaluation-profile INTERNAL_REGRESSION \
  --json

# 검수 완료 데이터가 없는 현재 상태에서 배포 평가가 차단되는지 확인
chemiguard119 evaluate \
  --only retriever \
  --evaluation-profile PILOT_REVIEWED \
  --json

# 파인튜닝 준비도 확인
chemiguard119 finetune-check \
  --dataset-path data/evaluation/incident_parser_seed.jsonl \
  --json
```
