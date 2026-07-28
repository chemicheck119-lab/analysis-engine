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

### Fixed

- `염산염`·`염산성`처럼 다른 표현에 포함된 물질명 부분 문자열이 정확 CAS 힌트로
  승격되어 다른 물질의 근거로 검색을 제한하던 문제
- 현장 확인 전 응답에 서수 위험등급·구체적 반응 또는 완료 상태가 섞여 대시보드에 노출될
  수 있던 출력 계약 문제

## [0.3.0] - 2026-07-28

### Added

- 실제 데이터 기반 물질 지원 우선순위와 KOSHA 공식 MSDS 수집 경로
- 공개 검증 CAMEO 물질 6종과 15개 물질쌍 회귀 평가
- 런타임 검색 인덱스와 FE·BE·AI 연동 계약
- 모델 릴리스 bundle 검증 및 배포 smoke test

### Security

- 운영 artifact manifest, SHA-256과 Git commit 신뢰 기준점 검증
- 현장 확인 전 충돌 판정을 차단하는 확인 게이트
