# 변경 이력

이 프로젝트는 [Semantic Versioning](https://semver.org/)과
[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 참고합니다.

## [Unreleased]

### Added

- 요청 ID·route·상태 코드·처리시간을 기록하는 `chemicheck119-log-v1` JSON 운영 로그
- API Key·query string·요청 본문이 로그에 포함되지 않는 회귀 테스트
- 운영 모니터링과 장애 확인 절차 문서

## [0.3.0] - 2026-07-28

### Added

- 실제 데이터 기반 물질 지원 우선순위와 KOSHA 공식 MSDS 수집 경로
- 공개 검증 CAMEO 물질 6종과 15개 물질쌍 회귀 평가
- 런타임 검색 인덱스와 FE·BE·AI 연동 계약
- 모델 릴리스 bundle 검증 및 배포 smoke test

### Security

- 운영 artifact manifest, SHA-256과 Git commit 신뢰 기준점 검증
- 현장 확인 전 충돌 판정을 차단하는 확인 게이트
