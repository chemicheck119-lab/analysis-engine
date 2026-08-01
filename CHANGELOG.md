# 변경 이력

이 프로젝트는 [Semantic Versioning](https://semver.org/)과
[Keep a Changelog](https://keepachangelog.com/ko/1.1.0/) 형식을 참고합니다.

## [Unreleased]

### Added

- 금속 나트륨–염산 조합의 수소·화재·폭발 위험을 CAMEO·ILO/WHO ICSC·OSHA 3개
  독립 공식기관으로 교차증빙
- 공식근거 URL·최종 host·MIME·HTML 문서 위치를 주 1회 검사하고 보고서를 보관하는
  source drift 감사 workflow
- 공식근거 교차확인 2쌍이 포함된 main preview의 서울 Cloud Run Blue/Green 배포 증빙
- 전국 현장대응 에이전트가 포함된 main preview의 서울 Cloud Run 실제 배포 증빙
- 운영 릴리스 gate와 분리된 공모전 preview 후보 smoke·Blue/Green·롤백 workflow

- 전국 17개 시·도의 시설 과거 이력 범위를 artifact에서 자동 계산하고 API metadata와
  `chemiguard119 coverage` CLI로 공개
- 신고 분석 응답에 10단계 workflow, 8개 도구 상태, 다음 행동을 포함하는 결정론적
  현장대응 에이전트 추가
- 사고 위치·MDT/GPS 현재 위치·서버 길찾기 GeoJSON·ETA·이동 진행률 계약과 오래된 위치,
  누락 경로, 발표용 시뮬레이션 구분 추가
- 검증된 확산 모델 없이 위험 반경을 표시하지 않는 지도 fail-closed 상태 추가
- BE용 이동 갱신 BFF OpenAPI, TypeScript 타입·클라이언트와 요청·응답 fixture 추가
- 위험 주장마다 공식기관 URL·문서 위치·독립기관 수·미증명 조건을 반환하는
  `chemicheck119-reference-assurance-v1`
- 차아염소산나트륨–염산 조합의 CAMEO·CDC·ILO/WHO·UKHSA 5개 자료, 4개 독립기관 교차증빙
- 나머지 14개 CAMEO 조합을 `PRIMARY_AUTHORITY_ONLY`로 구분하는 커버리지 회귀 평가
- 근거 registry 변조·출처 host 오류·예상 생성물 불일치 시 위험등급을 차단하는 fail-closed gate
- 운영 릴리스 gate를 약화하지 않는 공모전 FE·BE 통합용 `development` preview 이미지 경로
- preview artifact hash 검사·Cloud Build digest 생성 스크립트와 백엔드 전달 계약
- 검증된 bundle을 Artifact Registry digest로 고정하는 선택형 모델 릴리스 단계
- 후보 리비전 0% smoke, Blue/Green 전환과 이전 리비전 자동 롤백을 수행하는 Cloud Run workflow
- GitHub OIDC Workload Identity Federation·Secret Manager 기반 서울 스테이징 배포 문서
- 완료된 CAMEO Rule 결과와 KOSHA·CAMEO 검색 근거만 요약하는 선택형 Grounded RAG
- 문장별 `source_id` 검증, 위험등급 불일치 차단과 LLM 장애 시 extractive fallback
- 모델 API와 대시보드 BFF 계약의 선택형 `grounded_rag`/`groundedRag` 응답
- LM Studio 또는 배포형 OpenAI-compatible 서버를 위한 환경변수·운영 문서
- 공개 검증 CAMEO 15쌍의 확인 상태 45건과 안전 hard case 5건을 정답 없이 생성하는
  E2E 공모전 검수 후보팩
- 서로 다른 라벨러·검수자의 빈 CSV를 내보내고 완전 일치·안전 불변조건을 통과한 경우에만
  `DOUBLE_REVIEWED_NON_EXPERT` locked set으로 승격하는 이중 검수 gate
- 정답과 비교하지 않고 충돌 실행 gate·확인 전 위험 노출·출력 계약·artifact hash·지연시간만
  기록하는 E2E candidate preflight
- 실제 `analyze_incident` 경로에서 확인 gate·근거 CAS 귀속·모호성 기권·충돌 규칙을
  함께 검사하는 `evaluate-e2e`와 8건 DRAFT 안전 시나리오
- 서비스가 해결하는 실제 문제와 살릴·축소·제외할 기능, 공모전 참신성과 상용화 조건을
  구분한 서비스 타당성 문서
- FE가 BE만 호출하도록 고정한 `chemicheck119-dashboard-bff-v1` OpenAPI, TypeScript 타입,
  fetch 예제와 확인 전·확인 후·저장 성공·실패 fixture
- FastAPI 코드에서 결정적으로 생성하고 drift를 검사하는 모델 API OpenAPI snapshot
- 공개 검증 15개 물질쌍마다 CAS–CAMEO ID·물리적 형태·위험등급·hazard/gas·근거·현장
  확인 문구를 고정한 `dashboard-public-pair-presentation-v1` 계약
- 실제 FE `App.tsx`의 mock·legacy DTO·저장 전 초기화 차이를 정리한 FE·BE 인수인계서
- 물질명·CAS 또는 두 가지 이상 성상 관찰에서 확인 전 후보와 출처를 반환하는
  `POST /api/v1/substances/discover`
- 울산소방 공개 물성 749 CAS를 위한 `substance_profile`·FTS5 인덱스와 최소 건수 gate
- 대시보드 물질검색·현장 확인·대화 기록 저장 순서를 고정한 연동 계약과 예제
- 같은 CAS로 제한한 후보별 KOSHA·CAMEO 근거 카드와 `CAS_EVIDENCE_NOT_LOADED` 상태
- 근거 미적재·검색 순위·CAMEO–CAS 연결 경고를 보존하는 물질탐색 응답 필드
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

- GitHub Runner의 안정 채널 `gcloud run deploy`가 지원하지 않는 readiness probe 옵션 때문에
  후보 리비전 생성 전에 preview·staging 배포가 실패하던 문제
- 단일 CAMEO 체계 결과와 다기관 교차증빙 결과가 화면에서 같은 근거 수준으로 보이던 문제
- AI 자동 감사가 사람 화학 전문가 승인으로 오해될 수 있던 응답 계약 공백
- 확인 전 `hasIncompatible=false`가 `낮음/없음`으로 보이거나 과거 시설 이력이 현재 보유로
  보일 수 있는 대시보드 계약 공백
- 현장 물질 확인과 전체 대응기록 저장이 한 동작으로 섞이고 저장 전 화면이 초기화될 수 있는
  FE 연동 순서
- BFF 변환 중 CAS·위험등급·CAMEO class·규칙 버전·공개근거 provenance가 손상돼도 완료
  결과로 표시될 수 있는 계약 공백
- 유효한 CAMEO ID나 hazard/gas 값이라도 해당 물질쌍의 검증 snapshot과 다르면 대시보드
  완료 결과를 통과할 수 있던 계약 공백
- 생성된 모델·BFF·15쌍 계약의 drift를 CI가 놓치고 릴리스 artifact에서 15쌍 표시 계약이
  누락될 수 있던 배포 공백
- 일반어 한 개만 맞는 성상 질의가 임의 물질 후보를 반환하거나, 물질명 열이 냄새·색상
  순위를 왜곡할 수 있던 문제
- 물성 프로필 또는 FTS 인덱스가 없거나 행 수가 다를 때도 readiness가 통과하던 문제
- 물질탐색 후보 CAS와 다른 CAS의 근거 카드가 출력 계약을 통과할 수 있던 문제
- `NO_RELIABLE_CANDIDATE`를 물질 부재나 위험 없음으로 오해할 수 있던 안내 계약
- `염산염`·`염산성`처럼 다른 표현에 포함된 물질명 부분 문자열이 정확 CAS 힌트로
  승격되어 다른 물질의 근거로 검색을 제한하던 문제
- 현장 확인 전 응답에 서수 위험등급·구체적 반응 또는 완료 상태가 섞여 대시보드에 노출될
  수 있던 출력 계약 문제
- CAS 일치 문서의 ID 순서를 강한 순위 신호로 사용해 제품명·CAS 번호 절이 보호구·저장
  질문보다 먼저 노출되던 근거 검색 문제
- 확인 완료 응답의 미실행 Rule, 상태·CAS 불일치, 확률형 위험도와 후보 승격을 허용하던 문제
- 전체 Recall 하나가 핵심 답변 누락과 보조 문서 누락을 같은 실패로 보여주던 평가 해석 문제

### Security

- 브라우저에는 모델 API Key를 전달하지 않고 HttpOnly 서비스 세션으로 BE/BFF만 호출하는
  배포 경계와 계약 테스트
- 임시 디렉터리 처리 취약점이 수정된 개발 테스트 의존성 `pytest 9.0.3`으로 갱신
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
