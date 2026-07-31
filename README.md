# 케미체크119 AI·모델 API

화학사고 신고를 **물질 후보 검색 → 공식 근거 검색 → 물질 간 충돌 검토**로 연결하는
FastAPI 서비스와 CLI입니다. 백엔드는 대응충돌검토용 통합 분석 API와 물질검색 탭용 탐색
API를 호출하며, 이 저장소의 파이프라인이 여러 모델·규칙 구성요소를 순서대로 실행합니다.

> 현재 충돌 검토 정책은 `PUBLIC_SOURCE_PILOT_V1`입니다. 공식 CAMEO 페이지에서 CAS와
> 물질 형태를 직접 대조해 기록한 매핑을 사용하고, 코드는 그 출처·형식·일관성을 검사합니다.
> 다만 전문가 검토 완료 결과는 아니므로
> `expert_reviewed=false`가 항상 함께 제공됩니다. 결과는 사고 확률이 아니며 현장 명령도
> 아닙니다.

## 1. 30초 이해하기

```text
사고 신고문·구조화 입력
        ↓
신고문 구조화
        ↓
물질명·CAS 후보 검색
        ↓
색상·냄새·상태·용도 기반 관찰 후보 검색
        ↓
KOSHA·CAMEO 근거 검색
        ↓
사고물질과 시설물질을 현장에서 각각 확인
        ↓
공개 근거 기반 충돌 스크리닝
        ↓
근거·정책·버전이 포함된 JSON 응답
```

하나의 거대한 LLM이 화학적 위험을 직접 판단하는 구조가 아닙니다.

- **Resolver**: 물질명·CAS·화학식에서 후보를 찾습니다.
- **Discovery**: 두 가지 이상 성상 관찰에서 공개자료 기반 후보와 출처를 찾습니다.
- **Retriever**: KOSHA·CAMEO 문서에서 관련 근거를 검색합니다.
- **Rule Engine**: 현장에서 확인된 두 CAS를 CAMEO 반응성 데이터와 대조합니다.
- **FastAPI**: 사고 분석은 통합 API 하나로, 관찰 기반 물질탐색은 별도 API로 제공합니다.
- **LM Studio**: 신고문 구조화 비교 실험에만 선택적으로 사용합니다. 운영 API에 필요하지 않습니다.

## 2. 현재 구현 상태

| 기능 | 상태 | 현재 범위 |
|---|---|---|
| 물질 후보 검색 | 구현 | ICIS 중심 총 4,300개 물질 카탈로그 |
| 관찰 기반 물질 탐색 | 구현 | 울산소방 원천 4,378행 중 카탈로그 연결 성상 프로필 749 CAS |
| 자동 CAS 힌트 안전 회귀 | 구현 | 부분 문자열·모호 표현 12건, 위험 힌트 0건 |
| 신고문 구조화 | 구현 | 기본 결정적 파서, LM Studio는 선택 실험 |
| 공식 근거 검색 | 구현 | KOSHA 상세 근거 9종과 CAMEO 근거, section 중심 BM25·TF-IDF |
| Section 검색 평가 | 구현 | 핵심·보조 문서와 필수 사실 회수율·기권 성능·95% 구간을 분리, DRAFT 상용 주장은 차단 |
| 사고 분석 E2E 평가 | 구현 | 확인 gate·기권·충돌 규칙·근거 CAS 귀속 8건 DRAFT 안전 회귀 |
| KOSHA 근거 확장 | 수집기 구현 | 공식 OpenAPI staging 수집·검토 필요, 현재 artifact는 9종 |
| 유사 사고 사례 RAG | 미구현 | 검증된 사고–대응 사례 corpus와 출처·라벨 부족 |
| 시설 물질 후보 | 구현 | ICIS·PRTR 공개 **과거 취급 이력** 검색 |
| 물질 충돌 검토 | 공개 근거 파일럿 | 공개 검증 crosswalk CAS 6개·물질쌍 15개, pair별 표시 계약, `expert_reviewed=false` |
| 생성형 파인튜닝 | 준비도 점검만 | 데이터 gate만 구현, 실제 학습·운영 적용 안 함 |
| FastAPI·CLI | 구현 | API Key, health check, 구조화된 오류 응답 |
| 대시보드 BFF 계약 | 계약·fixture 구현 | FE용 OpenAPI·TypeScript 타입·성공/실패 예제 제공, 실제 BE 구현은 미완료 |
| 운영 요청 추적 | 구현 | 본문·Secret을 제외한 요청 ID·상태·지연시간 JSON 로그 |
| 배포 무결성 검사 | 구현 | manifest, SHA-256, Git commit, 평가 report·서명·재배포 gate |
| 배관 피해 예측 | 사용하지 않음 | 현재 서비스 문제와 직접 관련이 없어 제외 |

시설 이력은 현재 재고·수량·저장 위치를 의미하지 않습니다. 물질 후보 점수와 충돌 등급도
사고 확률이 아닙니다. 자세한 경계는 [안전 및 한계](docs/SAFETY_AND_LIMITATIONS.md)를
확인하세요. 이 기능이 실제로 필요한 이유와 살릴·축소할 기능은
[서비스 타당성 판단](docs/SERVICE_VALIDITY.md)에 정리했습니다.

## 3. 처음 보는 용어

| 용어 | 뜻 |
|---|---|
| CAS 번호 | 화학물질을 구분하는 국제 등록 번호 |
| Resolver | 입력한 이름을 물질·CAS 후보로 연결하는 검색 모델 |
| RAG / Retriever | 저장된 공식 근거 중 질문과 관련된 내용을 찾는 검색 계층 |
| CAMEO | NOAA가 제공하는 화학물질 반응성·대응 정보 체계 |
| Artifact | 학습·전처리로 생성된 SQLite DB와 모델 파일 |
| 현장 확인 게이트 | 사고물질과 시설물질을 각각 확인하기 전 충돌 결과를 내지 않는 규칙 |

## 4. 5분 빠른 시작: artifact가 없을 때

Python 3.11이 필요합니다. 이 과정은 저장소의 코드와 테스트를 확인하며 대용량 모델
artifact는 필요하지 않습니다.

```bash
git clone https://github.com/chemicheck119/llm.git
cd llm

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

python -m pytest
chemiguard119 --help
```

테스트가 통과하면 API 계약과 핵심 모델 로직을 개발할 준비가 된 것입니다. 다만 전체 CLI와
API를 실행하려면 다음 네 artifact가 필요합니다.

```text
artifacts/
├── chemiguard119.sqlite
├── resolver.joblib
├── retriever.joblib
└── runtime_manifest.json
```

이 저장소에는 대용량 원천 데이터와 생성 artifact를 직접 커밋하지 않습니다. 검증된 bundle은
팀의 GitHub Actions 실행 결과 또는 승인된 내부 전달 경로에서 받아야 합니다. 아직 공개 Release
다운로드 주소는 제공하지 않습니다.

## 5. 전체 실행: artifact가 있을 때

bundle을 저장소의 `artifacts/`에 풀고 설정 파일이 `config/`에 있는지 확인합니다.

```bash
source .venv/bin/activate
chemiguard119 doctor --json
```

`doctor`는 runtime artifact뿐 아니라 재학습용 원천 CSV 8개도 함께 검사합니다. artifact만
받은 개발 환경에서는 `final_csv_count=0`과 `NEEDS_SETUP`이 표시될 수 있으며, 이 경우 API
실행 가능 여부는 아래 `/health/ready` 결과로 판단합니다.

개발 환경에서만 익명 접근을 명시적으로 허용해 API를 실행할 수 있습니다.

```bash
CHEMIGUARD119_ALLOW_ANONYMOUS=true chemiguard119-api
```

다른 터미널에서 확인합니다.

```bash
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
```

- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

staging·production에서는 익명 접근이 차단되며 API Key와 artifact 신뢰 기준점이 필요합니다.
[배포 가이드](docs/DEPLOYMENT.md)를 먼저 확인하세요.

## 6. 첫 API 요청 보내기

가장 단순한 물질 후보 검색부터 확인합니다.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/substances/resolve \
  -H "Content-Type: application/json" \
  -d '{"query":"아세톤","top_k":3}'
```

등록되지 않은 제품명이나 현장 속칭을 임의의 물질로 확정하지 않습니다. 공식 물질명·CAS로
식별할 수 없는 입력은 `UNRESOLVED` 또는 확인이 필요한 퍼지 후보로 남기고, 현장 라벨이나
MSDS 확인을 요청합니다.

물질명을 모를 때는 구별되는 관찰 정보를 두 가지 이상 입력합니다.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/substances/discover \
  -H "Content-Type: application/json" \
  --data @examples/api/material_discovery_request.json
```

응답은 후보와 공개자료 출처를 제공하지만 물질을 확정하지 않습니다. 대원이 용기 라벨·현장
MSDS 등으로 CAS와 현장 존재를 확인해야 충돌 검토에 사용할 수 있습니다.

사고 분석은 먼저 현장 확인 정보 없이 후보만 요청합니다.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents/analyze \
  -H "Content-Type: application/json" \
  --data @examples/api/incident_unconfirmed_request.json
```

이 응답에서는 `confirmation_gate.all_required_confirmed=false`이며 충돌 등급이 제공되지
않습니다. 백엔드가 인증된 현장 확인 레코드 두 개를 만든 뒤 다시 요청합니다.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/incidents/analyze \
  -H "Content-Type: application/json" \
  --data @examples/api/incident_confirmed_request.json
```

운영 API를 호출할 때는 모든 `/api/v1/*` 요청에 다음 헤더를 추가합니다.

```text
X-API-Key: 배포-Secret으로-주입한-키
```

전체 요청·응답 계약은 [API 문서](docs/API.md)에 있습니다.

## 7. CLI로 확인하기

다음 명령은 artifact가 준비된 뒤 실행할 수 있습니다.

```bash
# 물질명·CAS·화학식 후보 검색
chemiguard119 resolve "아세톤"

# 물질명을 모를 때 관찰 정보로 후보와 출처 검색
chemiguard119 discover "무색 투명하고 박하 냄새가 나는 휘발성 액체"

# 임의 신고문을 전체 파이프라인으로 실행
chemiguard119 incident "황산 저장탱크에서 누출 중이며 옆 창고에 아세톤이 있습니다."

# 공식 근거 검색
chemiguard119 search "차아염소산나트륨 산 접촉"

# 신고문 구조화
chemiguard119 parse "염산 저장탱크에서 누출 중입니다."

# 실제 사고 분석 경로의 확인 gate·기권·충돌 규칙 회귀검사
chemiguard119 evaluate-e2e --evaluation-profile INTERNAL_REGRESSION

# JSON으로 출력
chemiguard119 --json resolve "황산"
```

명령 전체는 다음으로 확인합니다.

```bash
chemiguard119 --help
chemiguard119 incident --help
```

## 8. 원천 데이터에서 artifact 만들기

이 단계는 일반 API 사용자에게 필요하지 않습니다. 데이터 담당자나 모델 릴리스 담당자가
검증된 원천 데이터 bundle을 보유한 경우에만 실행합니다.

```bash
chemiguard119 pipeline \
  --data-dir data/raw \
  --db artifacts/chemiguard119.sqlite \
  --resolver-model artifacts/resolver.joblib \
  --retriever-model artifacts/retriever.joblib \
  --config-dir config \
  --report-dir outputs/modeling \
  --include-hash \
  --json
```

기본 profile은 CI용 `INTERNAL_REGRESSION`이며 결과를 현장 정확도로 사용할 수 없습니다.
실제 production bundle은 `PILOT_REVIEWED`를 요구하고 DRAFT 평가행이 하나라도 있으면
manifest 생성을 차단합니다.

```bash
chemiguard119 evaluate \
  --only retriever \
  --evaluation-profile INTERNAL_REGRESSION \
  --json

# 현재 저장소의 DRAFT 평가셋에서는 의도적으로 exit code 1
chemiguard119 evaluate \
  --only retriever \
  --evaluation-profile PILOT_REVIEWED \
  --json
```

파이프라인은 다음 순서로 실행됩니다.

```text
audit → prepare → train resolver → train retriever → evaluate → release manifest
```

공모전 시연 물질과 다음 공식 데이터 수집 대상을 정할 때는 별도의 지원 물질
우선순위 파이프라인을 사용합니다. 이 순위는 위험 확률이나 업체의 현재 재고 확률이
아닙니다.

```bash
python scripts/data/build_support_material_priority.py --help
```

자세한 입력과 해석 기준은
[지원 물질 우선순위 문서](docs/SUPPORT_MATERIAL_PRIORITY.md)에 있습니다.
우선순위 CAS를 KOSHA 공식 API에서 수집하는 방법은
[KOSHA 수집·검토 문서](docs/KOSHA_COLLECTION.md)에 있습니다. 수집 결과를 곧바로
운영 데이터로 승인하지 않으며, API 키는 환경변수로만 주입합니다.

원천 bundle은 Git에 넣지 않고 GitHub Actions Secret을 통해 릴리스 작업에만 복원합니다.
Secret 이름과 bundle 계약은 [배포 가이드](docs/DEPLOYMENT.md)에 설명되어 있습니다.

## 9. API 엔드포인트

| 메서드 | 경로 | 역할 |
|---|---|---|
| `GET` | `/health/live` | 프로세스 생존 확인 |
| `GET` | `/health/ready` | artifact·인증·정책 준비 상태 확인 |
| `GET` | `/api/v1/meta` | 서비스·스키마·정책 메타데이터 |
| `POST` | `/api/v1/incidents/analyze` | 전체 사고 분석 파이프라인 |
| `POST` | `/api/v1/substances/discover` | 관찰 정보 기반 확인 전 물질 후보·출처 검색 |
| `POST` | `/api/v1/substances/resolve` | 물질 후보 검색 |
| `POST` | `/api/v1/evidence/search` | 공식 근거 검색 |
| `POST` | `/api/v1/facilities/candidates` | 시설의 과거 취급 이력 후보 검색 |
| `POST` | `/api/v1/conflicts/review` | 현장 확인된 두 물질 충돌 검토 |

## 10. 공개 근거 파일럿 정책

`PUBLIC_SOURCE_PILOT_V1`은 “전문가가 승인했다”는 뜻이 아닙니다. 공개 CAMEO 페이지에서
CAS·물질 형태를 직접 대조해 provenance로 기록한 매핑만 사용하며, 런타임은 그 형식·출처·
일관성이 정책과 맞을 때만 파일럿 스크리닝을 허용합니다.

파일럿 결과에는 다음 정보가 포함됩니다.

- `policy_mode=PUBLIC_SOURCE_PILOT_V1`
- `expert_reviewed=false`
- 매핑과 근거의 URL·버전·확인 시각
- `risk_scale.is_probability=false`
- `risk_scale.probability_percent=null`
- `human_confirmation_required=true`

전문가 승인은 서비스 실행의 선행조건이 아니지만, 두 물질의 현재 현장 존재 확인은 별개의
필수 게이트입니다. 공개 자료에 없는 물질쌍은 결과를 추측하지 않고 미분류 상태로 반환합니다.

## 11. 데이터 사용 범위

| 데이터 | 파이프라인에서 하는 일 | 해석하면 안 되는 것 |
|---|---|---|
| ICIS | 4,300개 일반 물질 카탈로그와 별칭 후보 | 현장에 실제 존재한다는 확정 |
| 울산소방 화학물정보 | 749 CAS의 상태·색상·냄새·용도 후보 검색 | 물질 확정·울산 전용 서비스라는 주장 |
| KOSHA MSDS | 현재 스냅샷의 상세 근거 9종 검색 | 전체 4,300개에 상세 MSDS가 있다는 주장 |
| ICIS·PRTR 시설 이력 | 업체명·지역 기반 과거 취급 후보 | 현재 재고·보유량·저장 위치 |
| CAMEO | 반응성 그룹 기반 충돌 스크리닝 | 사고 발생 확률 또는 현장 명령 |

상세한 전처리와 모델 설명은 [데이터와 모델](docs/DATA_AND_MODEL.md)을 참고하세요.
대시보드 버튼·저장·파서 역할은
[대시보드 적용 흐름](docs/DASHBOARD_FLOW.md)에 정리했습니다.
현재 FE 코드와 연결할 정확한 BFF 경로·타입·적용 체크리스트는
[FE·BE 연동 인수인계서](docs/FE_BE_HANDOFF.md)를 확인하세요.

## 12. 프로젝트 구조

```text
.
├── src/chemiguard119/   # 모델·파이프라인·API 소스
├── tests/               # 단위·계약·안전 불변조건 테스트
├── config/              # 물질 별칭과 충돌 정책 설정
├── data/evaluation/     # 작은 내부 평가 입력
├── examples/api/        # API 요청 예시
├── examples/bff/        # FE용 BE/BFF 성공·실패 계약 예시
├── contracts/           # 모델 API·대시보드 BFF OpenAPI와 TypeScript 타입
├── config/              # CAMEO crosswalk·규칙 정책·15쌍 표시 계약
├── docs/                # 설계·연동·배포 문서
├── scripts/             # 데이터 준비와 선택 실험 도구
├── Dockerfile           # 외부 artifact mount 방식
├── Dockerfile.bundle    # 검증된 artifact 포함 이미지
├── compose.yaml
└── pyproject.toml
```

공개 패키지명은 `chemiguard119`로 유지하지만 서비스명은 **케미체크119**입니다.

## 13. 개발과 테스트

```bash
python -m pytest
chemiguard119 evaluate --only resolver
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m compileall -q src scripts
python -m pip check
```

변경 전에는 [기여 가이드](CONTRIBUTING.md)를 읽고, API 스키마·정책·문서를 함께
수정해 주세요.

## 14. 배포 개요

운영 배포는 Python 3.11과 다음 신뢰 기준점을 사용합니다.

- `openssl rand -hex 32`로 생성한 64자리 hex 또는 동등한 43자리 base64url API Key
- `runtime_manifest.json`의 SHA-256인 `CHEMIGUARD119_RUNTIME_MANIFEST_SHA256`
- manifest를 생성한 40자리 commit인 `CHEMIGUARD119_GIT_COMMIT`
- 빌드·독립 검수 단계에서만 사용하는 별도
  `CHEMIGUARD119_RELEASE_ATTESTATION_HMAC_KEY`(실행 서버 주입 금지)
- 충돌 정책 `CHEMIGUARD119_RULE_POLICY=PUBLIC_SOURCE_PILOT_V1`
- 읽기 전용 SQLite·모델·config
- `GET /health/ready` 기반 readiness 확인

`staging`과 `production`은 같은 무결성·인증 gate를 적용하며, 외부 bind mount가 아닌
`Dockerfile.bundle` 불변 이미지와 registry digest로 배포합니다.

자세한 절차는 [배포 가이드](docs/DEPLOYMENT.md)를 확인하세요.

## 15. 문서 안내

- [아키텍처](docs/ARCHITECTURE.md): 구성요소와 전체 처리 흐름
- [API](docs/API.md): 백엔드·프론트 연동 계약
- [FE·BE·AI 연동](docs/BACKEND_INTEGRATION.md): 저장소별 책임, 호출·병합 순서
- [FE·BE 인수인계](docs/FE_BE_HANDOFF.md): 실제 FE 코드 차이, BFF DTO, 적용 체크리스트
- [데이터와 모델](docs/DATA_AND_MODEL.md): 출처, 전처리, 모델 역할과 평가
- [모델 평가](docs/EVALUATION.md): 지표 정의, 기준선, 실패 원인 분리
- [평가 V2](docs/EVALUATION_V2.md): 21·10·6의 출처, 상용 타당성, 공모전 AI 고도화 기준
- [최종 브리핑](docs/BRIEFING.md): 발표문, 최신 AI 주제, 수치와 상용 준비 판정
- [배포](docs/DEPLOYMENT.md): artifact, Secret, Docker, CI/CD, 롤백
- [운영](docs/OPERATIONS.md): 구조화 로그, 요청 추적, 장애 확인 절차
- [현재 상태](docs/PROJECT_STATUS.md): 실제 완료 범위, 재현 결과, P0~P3 기술 부채
- [안전 및 한계](docs/SAFETY_AND_LIMITATIONS.md): 반드시 지켜야 할 해석 경계
- [기여 가이드](CONTRIBUTING.md): 브랜치, 테스트, 커밋 규칙

## 16. 자주 묻는 질문

### 총 모델 호출은 하나인가요?

항상 한 번은 아닙니다. 일반 사고 분석은 `POST /api/v1/incidents/analyze`, 물질명을 모를
때는 별도 `POST /api/v1/substances/discover`를 사용합니다. 현장 확인 뒤 충돌 검토를 위해
`incidents/analyze`를 다시 호출할 수 있습니다. 각 요청 내부에서는 외부 LLM 호출 없이
Resolver·Retriever·Rule Engine이 목적에 맞게 실행됩니다.

### 서버에 LM Studio를 설치해야 하나요?

아니요. 기본 운영 경로는 FastAPI, SQLite, scikit-learn 모델과 결정적 규칙 엔진만 사용합니다.
LM Studio는 선택적인 로컬 실험 도구입니다.

### 전문가 검토가 없으면 충돌 기능을 사용할 수 없나요?

아니요. `PUBLIC_SOURCE_PILOT_V1`에서 공개 출처로 검증된 스크리닝을 실행할 수 있습니다.
다만 응답은 항상 `expert_reviewed=false`이며 결과를 전문가 승인 또는 현장 명령으로 표현하면
안 됩니다.

### 업체별 보유 물질을 정확히 알 수 있나요?

아니요. ICIS·PRTR에서 확인되는 과거 취급 이력을 후보로 제시할 뿐입니다. 현재 존재 여부는
현장 라벨, 현장 MSDS, 운송 문서 등으로 다시 확인해야 합니다.

### 위험도가 백분율인가요?

아니요. CAMEO 기반 서수 등급이며 `is_probability=false`입니다. 백분율로 변환하거나 사고
확률처럼 표시하지 않습니다.

---

케미체크119의 모든 출력은 의사결정 보조 정보입니다. 물질과 시설 상태를 현장에서 확인하고,
최종 결정은 현장 지휘관이 수행합니다.
