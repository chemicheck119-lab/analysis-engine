# 케미체크119 AI 저장소 실제 상태

기준일: 2026-07-28
대상: `chemicheck119/llm`

이 문서는 계획이 아니라 코드, 로컬 실행 결과와 저장소에 존재하는 파일을 기준으로 작성합니다.
작은 내부 평가 결과를 현장 또는 상용 성능으로 해석하지 않습니다.

## 1. 한눈에 보는 상태

| 영역 | 상태 | 직접 확인한 내용 |
|---|---|---|
| 신고문 구조화 | 부분 완료 | 결정적 파서 구현, 내부 seed 6건뿐이라 성능 평가 불충분 |
| 물질 후보 검색 | 부분 완료 | 4,300개 카탈로그, 내부 회귀 21건 Top-1 0.9524·Top-3 Recall 1.0 |
| 자동 CAS 힌트 안전성 | 부분 완료 | 합성·내부 회귀 12건 통과, 부분 문자열 위험 힌트 0건 |
| 업체 이력 후보 | 부분 완료 | ICIS·PRTR 과거 이력 후보 168,424건, 현재 재고 확정 기능 아님 |
| 공식 근거 검색 | 부분 완료 | 근거 문서 5,858건, 내부 회귀 10건 Recall@5 0.9·MRR@8 0.85 |
| 충돌 검토 | 파일럿 | 공개 검증 CAMEO CAS 6종, 15개 조합 회귀 검사 |
| 유사 사고사례 RAG | 미완료 | 출처와 대응 라벨이 검증된 corpus 없음 |
| 파인튜닝 | 보류 | 준비도 검사만 존재, 기준선 대비 필요성이 입증되지 않음 |
| FastAPI | 구현 | 통합 분석과 보조 API, 인증·오류 계약·확인 게이트 구현 |
| 대시보드 표시 계약 | 구현 | 확인 전 위험 결과 금지, v1 단일 물질쌍과 검색 기능 범위 고정 |
| 운영 로그 | 완료 | 요청 ID·route·상태·지연시간 JSON 로그, 본문·Secret 제외 |
| Docker | 부분 완료 | 일반·bundle Dockerfile과 CI 구성 존재, 로컬 Docker CLI 없음 |
| 실제 배포 | 미완료 | 검증된 공개 스테이징 URL 없음 |
| FE·BE 연동 자료 | 완료 | JSON 계약, curl·Python·JavaScript와 smoke 절차 존재 |

내부 평가 데이터 규모가 작으므로 위 수치는 회귀 방지용입니다. 독립된 현장 보류셋의 정확도나
전국 단위 성능을 의미하지 않습니다.

## 2. 이번 재현 결과

Python 3.11.15 환경에서 다음을 확인했습니다.

```text
전체 테스트: 188 passed
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

원천 CSV 8개로 임시 release artifact를 다시 생성한 결과:

```text
pipeline status: COMPLETED
last stage: release_manifest
runtime: Python 3.11.15 / NumPy 2.4.6
production readiness: HTTP 200
integrity: VERIFIED
POST /api/v1/incidents/analyze: HTTP 200
state: AWAITING_SUBSTANCE_CONFIRMATION
conflict executed: false
```

현장 확인 두 건이 없는 요청에서 충돌 규칙이 실행되지 않은 것은 정상적인 안전 동작입니다.

## 3. 발견한 운영 주의사항

저장소 밖 로컬 `artifacts/`에 있던 기존 manifest는 Python 3.13.9·NumPy 2.5.1로 생성돼
있었습니다. 현재 배포 기준 Python 3.11.15·NumPy 2.4.6과 달라 API 무결성 검사가 readiness를
`503`으로 차단했습니다.

이 차단은 정상입니다. joblib artifact를 다른 런타임에서 억지로 로드하면 안 됩니다.
배포할 때는 반드시 릴리스 workflow 또는 Python 3.11 고정 환경에서 artifact와 manifest를
같이 다시 생성해야 합니다.

## 4. 기술 부채 우선순위

### P0 — 배포·안전 검증을 막는 항목

1. 실제 현장 검증과 독립 보류 평가셋이 없습니다.
2. KOSHA 상세 근거는 현재 artifact 기준 9종으로 전체 카탈로그보다 매우 적습니다.
3. 공개 검증 CAMEO 범위가 CAS 6종·물질쌍 15개로 제한됩니다.
4. 검증된 스테이징 URL과 실제 서버 배포 성공 기록이 없습니다.
5. 릴리스 artifact는 반드시 고정된 Python 3.11 환경에서 새로 생성해야 합니다.
6. Uvicorn access log와 예외 traceback의 민감정보 제거가 아직 공통 정책으로 고정되지 않았습니다.

현재 브랜치에서는 미확인 응답의 충돌 검토 타입, 상태·게이트·누락 역할 일관성을 강제하고
중첩 위험 필드를 차단했습니다. 이는 API 경계 P0를 줄인 것이며 독립 현장 검증을 대체하지
않습니다.

### P1 — 제한된 파일럿 전에 필요한 항목

1. parser는 평가 없음, resolver 21건과 retrieval 10건은 내부 회귀뿐이므로 독립 보류셋을
   새로 구축해야 합니다.
2. API 동시성·부하·timeout·장애 복구 시험이 없습니다.
3. 중앙 로그 저장소, 보존 기간, 알림 기준이 정해지지 않았습니다.
4. 현재 작업 브랜치는 아직 원격 PR과 GitHub Actions 실행이 없습니다.

### P2 — 성능 고도화 항목

1. BM25·TF-IDF 기준선과 임베딩 하이브리드 검색을 같은 평가셋에서 비교하지 않았습니다.
2. Cross-Encoder reranker의 정확도·지연시간·메모리 효과를 검증하지 않았습니다.
3. 출처가 검증된 유사 사고사례 corpus가 없어 사고사례 RAG를 구현하지 않았습니다.
4. 파인튜닝은 라벨 데이터와 기준선 개선 근거가 부족합니다.

### P3 — 저장소 운영 정리

1. 이슈·PR template과 라벨·마일스톤 자동 관리가 아직 없습니다.
2. GitHub Wiki와 Project는 접근 권한 및 사용 여부를 확인하지 못했습니다.
3. 이전 병합 브랜치 정리 여부를 확인하고 저장소 정책으로 고정해야 합니다.

## 5. 다음 권장 순서

1. 자동 CAS 힌트 경계·안전 회귀 PR을 검토하고 병합합니다.
2. 현재 엄격해진 미확인 충돌 타입에 이어 `model_outputs`·`evidence`·`provenance`의
   중첩 객체도 strict schema로 전환합니다.
3. Uvicorn query access log와 예외 traceback의 민감정보 노출 경로를 차단합니다.
4. Retriever의 실제 정답 evidence·MSDS 장을 포함한 관련성 골드셋을 만듭니다.
5. 데이터 출처·라이선스 manifest를 릴리스 gate와 연결합니다.
6. Python 3.11 릴리스 workflow로 bundle을 생성하고 스테이징에 배포합니다.
7. 기준선 평가가 안정된 뒤 임베딩·reranker를 오프라인 실험으로 비교합니다.

21·10·6의 정확한 출처, 단계별 목표 규모와 화면 적용 기준은
[평가 V2](EVALUATION_V2.md)에 정리했습니다.

## 6. GitHub 관리 상태

- 원격 `main`: GitHub에서 PR #6 병합 완료 확인
- PR #6 head: `e146f7d`, merge commit: `1a383d2`
- 현재 작업 브랜치: `codex/p0-dashboard-output-gate`
- GitHub 앱: 저장소·PR 읽기는 가능하지만 이슈 생성 쓰기는 `403`
- Codex 환경의 `gh auth status`: 저장된 `hywznn` token을 유효하지 않은 상태로 보고
- Codex 샌드박스: `github.com` DNS를 해석하지 못해 fetch·push 불가

현재 연결로는 코드·테스트·문서와 PR·CI 상태 확인은 가능하지만, 이슈 생성·브랜치 push·PR
생성은 완료했다고 주장하지 않습니다. 로컬 한국어 커밋 뒤 사용자의 일반 터미널에서 push가
필요합니다.
