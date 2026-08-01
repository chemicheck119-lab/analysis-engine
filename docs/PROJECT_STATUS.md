# 케미체크119 AI 저장소 실제 상태

기준일: 2026-08-01
대상: `chemicheck119/llm`

이 문서는 계획이 아니라 코드, 로컬 실행 결과와 저장소에 존재하는 파일을 기준으로 작성합니다.
작은 내부 평가 결과를 현장 또는 상용 성능으로 해석하지 않습니다.

## 1. 한눈에 보는 상태

| 영역 | 상태 | 직접 확인한 내용 |
|---|---|---|
| 신고문 구조화 | 부분 완료 | 결정적 파서 구현, 내부 seed 6건뿐이라 성능 평가 불충분 |
| 물질 후보 검색 | 부분 완료 | 4,300개 카탈로그, 내부 회귀 21건 Top-1 0.9524·Top-3 Recall 1.0 |
| 관찰 기반 물질 탐색 | 기능 smoke 완료 | 성상 프로필 749 CAS, 최소 두 영역 일치·현장 확인 gate |
| 자동 CAS 힌트 안전성 | 부분 완료 | 합성·내부 회귀 12건 통과, 부분 문자열 위험 힌트 0건 |
| 사고 분석 E2E | 부분 완료 | 8건 DRAFT 회귀 + 50건 이중 검수 후보·gate, 사람 독립 검수는 미실시 |
| 업체 이력 후보 | 부분 완료 | ICIS·PRTR 과거 이력 후보 168,424건, 현재 재고 확정 기능 아님 |
| 공식 근거 검색 | 부분 완료 | 근거 5,858건, DRAFT section 12건의 핵심 Recall@5 1.0·가중 Recall@5 0.9688 |
| 근거 제한형 RAG | 구현 | Rule·공식 근거만 문장별 인용, 기본 extractive·선택 LLM·실패 fallback |
| 충돌 검토 | 파일럿 | 공개 검증 CAMEO CAS 6종, 15개 조합 회귀 검사·pair별 표시 계약 |
| 공식근거 보증 | 구현 | 대표 1쌍 5개 문서·4개 독립기관 교차증빙, 나머지 14쌍 단일 공식체계 표시 |
| 유사 사고사례 검색 | 미완료 | 출처와 대응 라벨이 검증된 corpus 없음; 공식 근거 RAG와 별도 범위 |
| 파인튜닝 | 보류 | 준비도 검사만 존재, 기준선 대비 필요성이 입증되지 않음 |
| FastAPI | 구현 | 통합 분석과 보조 API, 인증·오류 계약·확인 게이트 구현 |
| 대시보드 표시 계약 | 구현 | BFF OpenAPI·TypeScript 타입·fixture, 확인 전 위험 결과 금지, 15쌍별 정확한 값 고정 |
| 운영 로그 | 구현 | 안전 JSON 로그, Uvicorn 원 URL access log와 traceback 비활성화 |
| Docker | 부분 완료 | 일반·bundle Dockerfile과 CI 구성 존재, 로컬 Docker CLI 없음 |
| 실제 배포 | 차단 | reviewed 평가·재배포 승인·검증된 공개 스테이징 URL 없음 |
| FE·BE 연동 자료 | 완료 | 모델·BFF OpenAPI, TypeScript 타입, 성공·실패 fixture와 체크리스트 |
| FE·BE 실제 연동 | 미완료 | 현재 FE는 물질검색 mock·legacy DTO, BE는 BFF 구현 필요 |

내부 평가 데이터 규모가 작으므로 위 수치는 회귀 방지용입니다. 독립된 현장 보류셋의 정확도나
전국 단위 성능을 의미하지 않습니다.

## 2. 이번 재현 결과

Python 3.11.15 환경에서 다음을 확인했습니다.

```text
전체 테스트: 346 passed
Ruff: 통과
형식 검사: 통과
compileall: 통과
pip check: 통과
```

추가한 자동 CAS 힌트 안전 회귀 결과:

```text
cases: 12
passed: 12
unsafe_auto_hint_count: 0
wrong_cas_auto_hint_count: 0
resolver_rule_eligibility_violation_count: 0
ambiguous_preservation_rate: 1.0
mean latency: 4.300ms
p95 latency: 6.361ms
```

이는 `DRAFT_INTERNAL_REGRESSION` 합성·내부 회귀 결과이며 현장 정확도가 아닙니다.

Grounded RAG는 12개 안전 회귀가 통과했습니다. 고정 입력 1,000회 extractive 조립은 평균
0.0131ms, p95 0.0132ms였지만 검색·Rule·네트워크를 제외한 micro-benchmark입니다. 실제 LLM
품질과 지연시간은 모델이 확정되지 않아 평가 불충분입니다.

질문에 맞는 MSDS section을 보는 신규 12건 DRAFT 회귀 비교:

```text
기존: nDCG@5 0.1595 / Recall@5 0.3333 / Precision@5 0.0833 / MRR@5 0.0875
개선: nDCG@5 0.9284 / Recall@5 0.8750 / Precision@5 0.2333 / MRR@5 0.9444
핵심 근거(grade 2~3) Recall@5: 1.0000
보조 근거(grade 1) Recall@5: 0.0000
중요도 가중 Recall@5: 0.9688
필수 사실 coverage@5: 0.8750
핵심 사실 coverage@5: 1.0000
핵심 성공 12/12의 Wilson 95% 구간: 0.7575~1.0000
평균 지연시간: 20.14ms → 21.73ms
개선 경로 unjudged rate: 0.7667
claim_scope: INTERNAL_REGRESSION_ONLY
```

CAS 일치 문서 전체를 evidence ID 순으로 가산하던 편향과 전역 Top-N 뒤 CAS 필터링 오류를
제거한 결과입니다. 12건은 section 제목 기반 내부 회귀이고 반환 문서의 76.7%가 아직
unjudged이므로 독립 현장 성능으로 해석하지 않습니다. 전체 Recall 0.875에서 누락된 3개는
모두 grade 1 보조 근거였고 grade 2~3 핵심 근거 14개는 모두 Top-5에 포함됐습니다. 하지만
표본이 12건뿐이어서 1.0을 상용 정확도로 표현하지 않습니다.
12건 모두 답변 가능한 질의라 답변 불가 기권 성능도 아직 측정하지 못했습니다. 배포
정책은 이를 숨길 수 없도록 독립 locked set 400건을 답변 가능 300건 이상과 답변 불가
100건 이상으로 나누어 검사합니다.

원천 CSV 8개로 임시 release artifact를 다시 생성한 결과:

```text
pipeline status: COMPLETED
last stage: release_manifest
runtime: Python 3.11.15 / NumPy 2.4.6
development readiness: HTTP 200
integrity: VERIFIED
POST /api/v1/incidents/analyze: HTTP 200
state: AWAITING_SUBSTANCE_CONFIRMATION
conflict executed: false
```

현장 확인 두 건이 없는 요청에서 충돌 규칙이 실행되지 않은 것은 정상적인 안전 동작입니다.

같은 artifact를 실제 사고 분석 E2E 8개 시나리오에 연결한 결과:

```text
scenario pass: 8/8
output contract pass: 8/8
unsafe conflict execution: 0
unconfirmed risk exposure: 0
expected abstention: 7/7
mean latency: 86.610ms
p95 latency: 112.537ms
claim_scope: INTERNAL_REGRESSION_ONLY
```

개발자가 만든 DRAFT 안전 회귀이므로 현장 정확도나 상용 성능으로 해석하지 않습니다.

독립 평가 구축을 위해 별도로 만든 E2E 후보 50건의 preflight 결과:

```text
candidate: 50 (공개 검증 15쌍 × 확인 상태 3 = 45, hard case 5)
pipeline contract failure: 0
unsafe conflict execution: 0
unconfirmed risk exposure: 0
mean latency: 99.061ms
p95 latency: 118.312ms
is_accuracy_evaluation: false
```

이 수치는 현재 모델이 안전 gate와 JSON 계약을 지켰다는 기계 관찰입니다. 두 사람이
독립적으로 정답을 작성하기 전에는 정확도·Recall·상용 성능이 아닙니다. 같은 사람이 두
역할을 맡거나 두 시트를 미리 공유하면 병합 도구가 차단합니다.

관찰 기반 물질 탐색 artifact를 같은 원천 CSV에서 다시 생성한 결과:

```text
울산 원천 행: 4,378
카탈로그 연결 성상 프로필: 749 CAS
Resolver: 4,300 물질 / 9,685 별칭
Retriever: 5,858 문서
질의: 무색 투명하고 박하 냄새가 나는 휘발성 액체
Top-1: 메틸 에틸 케톤 / 78-93-3
50회 로컬 warm run: 평균 8.634ms / p95 12.618ms
정보 부족 질의 “냄새가 나는 액체”: NO_RELIABLE_CANDIDATE
```

이는 원천 프로필을 다시 찾는 기능 smoke이며 독립 정확도나 운영 SLO가 아닙니다. 현재
상세 KOSHA 근거가 9종뿐이므로 위 Top-1도 `CAS_EVIDENCE_NOT_LOADED` 상태입니다.

## 3. 발견한 운영 주의사항

저장소 밖 로컬 `artifacts/`에 있던 기존 manifest는 Python 3.13.9·NumPy 2.5.1로 생성돼
있었습니다. 현재 배포 기준 Python 3.11.15·NumPy 2.4.6과 달라 API 무결성 검사가 readiness를
`503`으로 차단했습니다.

이 차단은 정상입니다. joblib artifact를 다른 런타임에서 억지로 로드하면 안 됩니다.
배포할 때는 반드시 릴리스 workflow 또는 Python 3.11 고정 환경에서 artifact와 manifest를
같이 다시 생성해야 합니다.

## 4. 기술 부채 우선순위

### P0 — 배포·안전 검증을 막는 항목

1. 50건 검수 후보와 이중 검수 gate는 있으나 실제 사람 검수가 끝난 독립 보류셋은 없습니다.
2. KOSHA 상세 근거는 현재 artifact 기준 9종으로 전체 카탈로그보다 매우 적습니다.
3. 공개 검증 CAMEO 범위가 CAS 6종·물질쌍 15개로 제한됩니다.
4. 검증된 스테이징 URL과 실제 서버 배포 성공 기록이 없습니다.
5. 릴리스 artifact는 반드시 고정된 Python 3.11 환경에서 새로 생성해야 합니다.
6. CAMEO·ICIS 파생 artifact의 컨테이너 재배포 조건 검토가 완료되지 않았습니다.
7. 신규 section 회귀는 핵심 Recall 1.0이지만 전체 Recall 0.875로 운영 정책 0.90에
   미달하고, 12건 모두 DRAFT라 어떤 지표도 운영 승인에 사용할 수 없습니다.
8. 관찰 기반 탐색은 독립 오인·거부 평가셋이 없고, 현재는 source-derived smoke만 있습니다.
9. BFF 계약은 검증됐지만 FE·BE 저장소의 실제 구현과 staging 통합 시험은 아직 없습니다.

현재 브랜치는 미확인·확인 완료 응답의 중첩 위험 필드, 확률형 위험도, 상태·실행·CAS 모순을
차단합니다. Uvicorn 원 URL access log와 raw traceback도 비활성화했습니다.
staging·production manifest는 `PILOT_REVIEWED` 평가와 데이터 재배포 승인 없이는
통과하지 않습니다.

### P1 — 제한된 파일럿 전에 필요한 항목

1. parser 6건, resolver 21건, legacy retrieval 10건과 section retrieval 12건은 모두
   내부 DRAFT이므로 독립 보류셋을 새로 구축해야 합니다.
2. TestClient 동시성 smoke 40/40만 통과했습니다. 실제 서버·gateway·네트워크 환경의
   부하·timeout·장애 복구 시험은 없습니다.
3. 중앙 로그 저장소, 보존 기간, 알림 기준이 정해지지 않았습니다.
4. 원격 CI와 기본 Docker build는 통과했지만 실제 release workflow·태그 실행 기록은
   없습니다.
5. 현재 독립 검수 서명은 HMAC 기반입니다. 실행 서버에서는 키를 제거했지만 장기 상용
   릴리스에는 오프라인 private key와 runtime public key를 분리하는 KMS·비대칭 서명이
   필요합니다.
6. 배포 workflow는 bundle 이미지를 사용하지만 코드 외부에서 bind mount production을
   구성하면 시작 후 파일 교체 위험이 있으므로 배포 정책과 admission control로 금지해야 합니다.

### P2 — 성능 고도화 항목

1. BM25·TF-IDF 기준선과 임베딩 하이브리드 검색을 같은 평가셋에서 비교하지 않았습니다.
2. Cross-Encoder reranker의 정확도·지연시간·메모리 효과를 검증하지 않았습니다.
3. 공식 근거 RAG는 구현했지만 출처가 검증된 유사 사고사례 corpus가 없어 사고사례 검색은 구현하지 않았습니다.
4. 파인튜닝은 라벨 데이터와 기준선 개선 근거가 부족합니다.

### P3 — 저장소 운영 정리

1. 이슈·PR template과 라벨·마일스톤 자동 관리가 아직 없습니다.
2. GitHub Wiki와 Project는 접근 권한 및 사용 여부를 확인하지 못했습니다.
3. 이전 병합 브랜치 정리 여부를 확인하고 저장소 정책으로 고정해야 합니다.

## 5. 다음 권장 순서

1. FE·BE가 물질탐색·사고분석 계약을 구현하고 배포된 BFF를 대상으로 contract smoke를
   실행합니다.
2. 서로 다른 두 사람이 E2E 50건을 독립 라벨링해 locked set으로 병합하고 실제 E2E 성능을 검증합니다.
3. CAMEO·ICIS 파생 데이터의 재배포 조건을 확인해 registry 승인을 기록합니다.
4. 독립 검수 evidence bundle과 attestation을 생성합니다.
5. Python 3.11 bundle 이미지를 registry digest로 고정해 staging 부하·장애 시험을 수행합니다.
6. 기준선 평가가 안정된 뒤 임베딩·reranker를 오프라인 실험으로 비교합니다.

21·10·6의 정확한 출처, 단계별 목표 규모와 화면 적용 기준은
[평가 V2](EVALUATION_V2.md)에 정리했습니다.
E2E 50건의 실제 검수 절차는 [E2E 독립 검수 가이드](E2E_REVIEW_GUIDE.md)에 있습니다.

## 6. GitHub 상태 확인 원칙

브랜치·PR·CI 상태는 수시로 변하므로 이 영구 문서에 특정 PR 번호나 인증 오류를 고정하지
않습니다. 병합 직전에는 `git status`, `gh pr view`, `gh pr checks`의 실제 결과를 확인하고
최종 작업 보고에 기록합니다.
