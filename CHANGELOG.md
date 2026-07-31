# 변경 이력

이 프로젝트는 [Semantic Versioning](https://semver.org/)과
[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 참고합니다.

## [Unreleased]

### Added

- 요청 ID·route·상태 코드·처리시간을 기록하는 `chemicheck119-log-v1` JSON 운영 로그
- API Key·query string·요청 본문이 로그에 포함되지 않는 회귀 테스트
- 운영 모니터링과 장애 확인 절차 문서
- 자동 CAS 힌트의 허용·보류·모호성 보존을 분리한 12건 안전 회귀 평가
- 문장 내 공식 별칭 탐색용 첫 글자 runtime 인덱스와 Unicode 경계 검사
- 21·10·6 내부 데이터의 출처와 한계를 바로잡은 평가 V2·상용 타당성 문서
- 확인 전·후 카드 표시, 현재 검색 기능과 v1 단일 물질쌍 범위를 고정한 대시보드 계약
- DRAFT·이중 검수·파일럿 검수를 분리하는 평가 profile과 provenance·split 누수 gate
- evidence ID별 0~3 relevance로 nDCG·Recall·MRR을 계산하는 12건 section 회귀 평가
- 데이터 출처별 재배포 상태를 기록하는 `data_source_registry.json`
- 발표용 모델 파이프라인·수치·상용 준비 판정을 정리한 최종 브리핑
- `chemicheck119-runtime-release-v4` manifest, 버전 고정 품질 정책, 평가
  dataset·report digest 결합과 독립 검수 attestation
- MSDS 핵심(grade 2~3)·보조(grade 1)·중요도 가중 Recall을 분리하고 작은 표본의
  Wilson 95% 신뢰구간과 누락 evidence ID를 제공하는 section 평가기 v3
- 문서 수가 아닌 실제 답변 사실을 측정하는 `required_fact_ids` coverage와 핵심 사실
  완전 회수율

### Fixed

- `염산염`·`염산성`처럼 다른 표현에 포함된 물질명 부분 문자열이 정확 CAS 힌트로
  승격되어 다른 물질의 근거로 검색을 제한하던 문제
- 현장 확인 전 응답에 서수 위험등급·구체적 반응 또는 완료 상태가 섞여 대시보드에 노출될
  수 있던 출력 계약 문제
- CAS 일치 문서의 ID 순서를 강한 순위 신호로 사용해 제품명·CAS 번호 절이 보호구·저장
  질문보다 먼저 노출되던 근거 검색 문제
- 확인 완료 응답의 미실행 Rule, 상태·CAS 불일치, 확률형 위험도와 후보 승격을 허용하던 문제
- 전체 Recall 하나가 핵심 답변 누락과 보조 문서 누락을 같은 실패로 보여주던 평가 해석 문제

### Security

- Uvicorn 원 URL access log와 raw exception traceback을 비활성화하고 예외 타입·request ID만
  구조화 로그로 기록
- 알 수 없는 배포 환경과 staging·production의 미검증 runtime을 readiness에서 fail-closed
- `PILOT_REVIEWED` 평가 또는 데이터 재배포 승인이 없으면 staging·production manifest
  검증 차단
- 저수준 runtime 검증에서도 알 수 없는 배포 환경을 차단
- staging에도 production과 같은 외부 trust anchor·검수 평가·인증 gate 적용
- staging·production API Key를 32바이트 난수 형식으로 제한
- attestation HMAC 서명키를 실행 컨테이너에서 제거하고 외부 manifest digest로 빌드 검증
  결과를 고정
- 외부 bind mount Compose를 로컬 개발로 제한하고 배포 이미지는 GHCR registry digest로 기록
- 검수 완료 section 검색 릴리스에 핵심 Recall 0.98·중요도 가중 Recall 0.95를 추가 요구
- section 평가 400건을 답변 가능 300건·답변 불가 100건으로 분리하고, 답변 불가 기권율과
  핵심 사실 회수율·Wilson 하한을 함께 검사

## [0.3.0] - 2026-07-28

### Added

- 실제 데이터 기반 물질 지원 우선순위와 KOSHA 공식 MSDS 수집 경로
- 공개 검증 CAMEO 물질 6종과 15개 물질쌍 회귀 평가
- 런타임 검색 인덱스와 FE·BE·AI 연동 계약
- 모델 릴리스 bundle 검증 및 배포 smoke test

### Security

- 운영 artifact manifest, SHA-256과 Git commit 신뢰 기준점 검증
- 현장 확인 전 충돌 판정을 차단하는 확인 게이트
