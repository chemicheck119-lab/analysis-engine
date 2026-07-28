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
| 업체 이력 후보 | 부분 완료 | ICIS·PRTR 과거 이력 후보 168,424건, 현재 재고 확정 기능 아님 |
| 공식 근거 검색 | 부분 완료 | 근거 문서 5,858건, 내부 회귀 10건 Recall@5 0.9·MRR@8 0.85 |
| 충돌 검토 | 파일럿 | 공개 검증 CAMEO CAS 6종, 15개 조합 회귀 검사 |
| 유사 사고사례 RAG | 미완료 | 출처와 대응 라벨이 검증된 corpus 없음 |
| 파인튜닝 | 보류 | 준비도 검사만 존재, 기준선 대비 필요성이 입증되지 않음 |
| FastAPI | 완료 | 통합 분석과 보조 API, 인증·오류 계약·확인 게이트 구현 |
| 운영 로그 | 작업 브랜치 완료 | 요청 ID·route·상태·지연시간 JSON 로그, 본문·Secret 제외 |
| Docker | 부분 완료 | 일반·bundle Dockerfile과 CI 구성 존재, 로컬 Docker CLI 없음 |
| 실제 배포 | 미완료 | 검증된 공개 스테이징 URL 없음 |
| FE·BE 연동 자료 | 완료 | JSON 계약, curl·Python·JavaScript와 smoke 절차 존재 |

내부 평가 데이터 규모가 작으므로 위 수치는 회귀 방지용입니다. 독립된 현장 보류셋의 정확도나
전국 단위 성능을 의미하지 않습니다.

## 2. 이번 재현 결과

Python 3.11.15 환경에서 다음을 확인했습니다.

```text
전체 테스트: 154 passed
Ruff: 통과
형식 검사: 통과
compileall: 통과
pip check: 통과
```

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

### P1 — 제한된 파일럿 전에 필요한 항목

1. parser 6건, resolver 21건, retrieval 10건인 평가셋을 독립 보류셋으로 확장해야 합니다.
2. API 동시성·부하·timeout·장애 복구 시험이 없습니다.
3. 중앙 로그 저장소, 보존 기간, 알림 기준이 정해지지 않았습니다.
4. 최신 GitHub Actions 실행 상태를 현재 GitHub 앱 권한으로 확인할 수 없습니다.

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

1. 구조화 운영 로그 PR을 병합하고 스테이징 로그에서 민감정보 미노출을 확인합니다.
2. parser·resolver·retrieval 독립 평가셋의 스키마와 수집 양식을 먼저 확정합니다.
3. KOSHA 상세 근거와 CAMEO 공개 검증 범위를 우선순위 CAS부터 확대합니다.
4. Python 3.11 릴리스 workflow로 bundle을 생성하고 스테이징에 배포합니다.
5. 기준선 평가가 안정된 뒤 임베딩·reranker를 오프라인 실험으로 비교합니다.

## 6. GitHub 관리 상태

- 원격 `main`: PR #4 병합 commit까지 로컬 Git으로 확인
- GitHub 앱: 설치된 계정이 없어 저장소 작업이 `403` 또는 `404`
- 사용자 터미널 `gh`: `hywznn` 계정과 `repo` scope 인증 확인
- Codex 샌드박스: keyring과 GitHub DNS에 접근하지 못해 원격 쓰기 미검증
- 최신 Actions 결과: 현재 권한으로 직접 확인하지 못함

권한이 복구되기 전에는 코드·테스트·문서와 브랜치 작업을 계속할 수 있지만, 이슈·PR·마일스톤
생성과 CI 결과 확인·병합은 완료했다고 주장하지 않습니다.
