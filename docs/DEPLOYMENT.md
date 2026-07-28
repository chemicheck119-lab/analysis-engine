# 케미체크119 모델 API 배포 가이드

## 1. 배포 원칙

케미체크119는 LM Studio 없이 FastAPI, 읽기 전용 SQLite, scikit-learn artifact와 결정적
CAMEO Rule Engine으로 배포합니다.

운영 릴리스 단위는 다음 네 가지입니다.

1. clean Git commit의 애플리케이션 코드
2. 원천 데이터로 생성한 DB·모델 artifact
3. 버전이 고정된 config
4. 각 파일 해시와 런타임 버전을 기록한 `runtime_manifest.json`

운영 서버가 시작할 때 외부에서 주입한 manifest SHA-256과 Git commit을 먼저 검증합니다.
검증에 실패하면 joblib을 로드하지 않고 readiness를 실패시킵니다.

## 2. 실행 방식 선택

| 방식 | 용도 | Artifact 위치 |
|---|---|---|
| Python 로컬 실행 | 개발·디버깅 | `artifacts/` |
| `Dockerfile` + Compose | 서버가 artifact를 별도 관리 | 읽기 전용 volume |
| `Dockerfile.bundle` | 검증된 모델과 코드를 한 이미지로 배포 | 이미지 내부 |
| GitHub Actions release workflow | 재학습·검증·bundle 생성 | Actions artifact, 선택적 GHCR |

일반 개발자는 원천 데이터로 매번 재학습할 필요가 없습니다. 검증된 runtime artifact bundle을
받아 로컬에서 실행하면 됩니다.

## 3. 환경변수

### 3.1 API와 배포

| 변수 | 개발 기본값 | 운영 |
|---|---|---|
| `CHEMIGUARD119_ENVIRONMENT` | `development` | `production` |
| `CHEMIGUARD119_API_HOST` | `127.0.0.1` | 내부 네트워크의 `0.0.0.0` |
| `CHEMIGUARD119_API_PORT` | `8000` | 배포 포트 |
| `CHEMIGUARD119_ALLOW_ANONYMOUS` | `false` | 반드시 `false` |
| `CHEMIGUARD119_API_KEY` | 익명 개발 시 생략 가능 | 32자 이상 Secret |
| `CHEMIGUARD119_RUNTIME_MANIFEST_SHA256` | 선택 | 64자리 SHA-256 필수 |
| `CHEMIGUARD119_GIT_COMMIT` | 선택 | 40자리 릴리스 commit 필수 |
| `CHEMIGUARD119_RULE_POLICY` | `PUBLIC_SOURCE_PILOT_V1` | 기본값 유지 권장 |

### 3.2 경로

| 변수 | 기본 상대경로 |
|---|---|
| `CHEMIGUARD119_PROJECT_ROOT` | 저장소 루트 |
| `CHEMIGUARD119_DATA_DIR` | `data/raw/` |
| `CHEMIGUARD119_CONFIG_DIR` | `config/` |
| `CHEMIGUARD119_EVALUATION_DIR` | `data/evaluation/` |
| `CHEMIGUARD119_ARTIFACT_DIR` | `artifacts/` |
| `CHEMIGUARD119_DB_PATH` | `artifacts/chemiguard119.sqlite` |
| `CHEMIGUARD119_RESOLVER_MODEL` | `artifacts/resolver.joblib` |
| `CHEMIGUARD119_RETRIEVER_MODEL` | `artifacts/retriever.joblib` |
| `CHEMIGUARD119_REPORT_DIR` | `outputs/modeling/` |

다른 위치에 원천 데이터 bundle을 수동으로 풀 때는 `CHEMIGUARD119_DATA_DIR` 또는 CLI의
`--data-dir`로 위치를 명시하세요.

## 4. Artifact가 없는 개발 환경

코드와 테스트는 artifact 없이 확인할 수 있습니다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest
python -m compileall -q src scripts
```

API 프로세스를 실행해도 artifact가 없으면 모델 runtime은 준비되지 않습니다.

- `/health/live`: 프로세스가 살아 있으면 `200`
- `/health/ready`: `503 NOT_READY`
- 분석 API: `503 ARTIFACT_NOT_READY`

이 상태는 설치 실패가 아니라 아직 runtime bundle을 받지 않은 상태입니다.

## 5. Artifact가 있는 로컬 개발

다음 파일을 배치합니다.

```text
artifacts/
├── chemiguard119.sqlite
├── resolver.joblib
├── retriever.joblib
└── runtime_manifest.json
```

config에는 최소 다음 파일이 필요합니다.

```text
config/
├── cameo_crosswalk.csv
├── conflict_policy.json
├── pair_rules.csv
└── substance_overrides.csv
```

환경을 진단하고 로컬 익명 모드로 실행합니다.

```bash
chemiguard119 doctor --json
CHEMIGUARD119_ALLOW_ANONYMOUS=true chemiguard119-api
```

`doctor`는 재학습용 원천 CSV 8개도 함께 검사하므로 artifact만 있는 환경에서는
`NEEDS_SETUP`이 표시될 수 있습니다. Runtime API의 실제 준비 여부는 `/health/ready`로
판단합니다.

```bash
curl http://127.0.0.1:8000/health/ready
curl -X POST http://127.0.0.1:8000/api/v1/substances/resolve \
  -H "Content-Type: application/json" \
  -d '{"query":"아세톤","top_k":1}'
```

익명 모드로 로컬호스트 외 주소에 bind하려 하면 실행이 차단됩니다.

## 6. 원천 데이터 bundle

### 6.1 GitHub Actions Secrets

모델 릴리스 workflow에는 다음 Repository Secret 세 개가 필요합니다.

| Secret | 값 |
|---|---|
| `CHEMIGUARD119_DATA_BUNDLE_URL` | 인증정보가 URL에 포함되지 않은 HTTPS bundle URL |
| `CHEMIGUARD119_DATA_BUNDLE_SHA256` | `tar.gz` 바이트의 64자리 SHA-256 |
| `CHEMIGUARD119_API_KEY` | 32자 이상, 공백·예시 문자열이 없는 운영 API Key |

URL은 공개 문서에 쓰지 않습니다. 접근 제어가 필요하면 Secret에 만료 시간이 짧은 HTTPS 서명
URL을 저장하고 릴리스 실행 후 교체합니다.

### 6.2 Bundle 포맷

bundle은 루트에 필수 CSV 8개만 있는 flat `tar.gz`입니다.

```text
01_KOSHA_물질안전보건자료.csv
02_CAMEO_화학물질_반응성.csv
03_CAMEO_화학물질_반응성그룹_매핑.csv
04_CAMEO_반응성그룹_목록.csv
05_CAMEO_반응성그룹_호환성_고유조합.csv
06_울산소방_화학물정보.csv
13_ICIS_2024_화학물질_취급현황.csv
19_ICIS_2024_시설후보_통합모델입력.csv
```

작업 디렉터리 `data/bundle-source/`에 이 파일만 준비했다고 가정하면 다음처럼 생성할 수
있습니다.

```bash
tar -C data/bundle-source -czf chemicheck119-data-bundle.tar.gz \
  01_KOSHA_물질안전보건자료.csv \
  02_CAMEO_화학물질_반응성.csv \
  03_CAMEO_화학물질_반응성그룹_매핑.csv \
  04_CAMEO_반응성그룹_목록.csv \
  05_CAMEO_반응성그룹_호환성_고유조합.csv \
  06_울산소방_화학물정보.csv \
  13_ICIS_2024_화학물질_취급현황.csv \
  19_ICIS_2024_시설후보_통합모델입력.csv

shasum -a 256 chemicheck119-data-bundle.tar.gz
```

Secret의 SHA-256은 업로드 전 로컬에서 계산한 값과 정확히 같아야 합니다.

### 6.3 안전 추출

릴리스 workflow는 다운로드 후 다음을 확인합니다.

- HTTPS만 허용하며 redirect도 HTTPS로 제한
- bundle SHA-256 검증
- 필수 파일 정확히 8개
- 추가 파일, 하위 경로, 중복, 빈 파일 차단
- symbolic link와 hard link 차단
- 경로 순회 차단
- Git LFS pointer 차단
- 압축 bundle 1GB, 해제 합계 2GB 상한

검증된 파일만 `data/raw/`에 새로 추출됩니다.

## 7. 수동 모델 릴리스

신뢰된 Python 3.11 환경과 원천 데이터가 있을 때 실행합니다.

먼저 릴리스 commit을 확인하고 clean 상태인지 검사합니다.

```bash
git rev-parse HEAD
git status --short
```

확인한 40자리 commit을 환경변수로 설정한 뒤 파이프라인을 실행합니다.

```bash
export CHEMIGUARD119_GIT_COMMIT=40자리-clean-commit-sha

chemiguard119 pipeline \
  --data-dir data/raw \
  --db artifacts/chemiguard119.sqlite \
  --resolver-model artifacts/resolver.joblib \
  --retriever-model artifacts/retriever.joblib \
  --config-dir config \
  --resolver-evaluation data/evaluation/resolver_regression_queries.csv \
  --retriever-evaluation data/evaluation/retrieval_regression_queries.csv \
  --report-dir outputs/modeling \
  --include-hash \
  --json
```

결과를 확인합니다.

```bash
test -f artifacts/chemiguard119.sqlite
test -f artifacts/resolver.joblib
test -f artifacts/retriever.joblib
test -f artifacts/runtime_manifest.json
shasum -a 256 artifacts/runtime_manifest.json
```

manifest SHA-256은 artifact와 별도 신뢰 경로에 기록해야 합니다. artifact와 같은 bundle에
있는 hash 파일만 믿으면 변조 여부를 판단할 수 없습니다.

## 8. GitHub Actions 릴리스

`.github/workflows/release-model.yml`은 `workflow_dispatch`로 수동 실행합니다.

1. clean commit checkout
2. Secret URL에서 데이터 bundle 다운로드
3. URL·SHA-256·archive 구조 검증 후 `data/raw/`에 추출
4. 고정 운영 의존성 설치
5. 전체 테스트
6. audit → prepare → train → evaluate → manifest
7. 호스트에서 운영 무결성 검증
8. `Dockerfile.bundle` 이미지 빌드
9. 읽기 전용 컨테이너 readiness·인증 smoke test
10. runtime bundle을 Actions artifact로 보관
11. 선택하면 immutable 태그로 GHCR push

Actions artifact 이름에는 commit SHA가 포함되며 보관 기간은 workflow 설정을 따릅니다. 아직
공개 GitHub Release 다운로드 링크는 제공하지 않습니다.

## 9. Compose 배포

`compose.yaml`은 외부 artifact mount 방식을 사용합니다. `.env.example`을 참고해 실제 값으로
별도 `.env`를 만듭니다. `.env`는 Git에 커밋하지 않습니다.

```text
CHEMIGUARD119_API_KEY=32자-이상의-운영-키
CHEMIGUARD119_RUNTIME_MANIFEST_SHA256=64자리-manifest-sha256
CHEMIGUARD119_GIT_COMMIT=40자리-release-commit
CHEMIGUARD119_RULE_POLICY=PUBLIC_SOURCE_PILOT_V1
```

```bash
docker compose up --build -d
docker compose ps
curl http://127.0.0.1:8000/health/ready
```

기본 Compose 보안 설정은 다음과 같습니다.

- localhost에만 포트 publish
- root filesystem 읽기 전용
- artifact와 config volume 읽기 전용
- 비root 사용자
- `no-new-privileges`
- 임시 파일은 제한된 tmpfs
- 메모리 1GiB 제한, 768MiB reservation

외부 사용자는 TLS를 종료하는 API Gateway나 서비스 백엔드를 통해 접근하게 하세요.

## 10. Artifact 포함 이미지

`Dockerfile.bundle`은 검증된 artifact를 이미지에 포함합니다.

```bash
docker build \
  --file Dockerfile.bundle \
  --build-arg RUNTIME_MANIFEST_SHA256=64자리-manifest-sha256 \
  --build-arg GIT_COMMIT=40자리-release-commit \
  --tag chemicheck119-model-api:release .
```

build argument는 이미지 생성 중 검증에만 사용됩니다. 최종 이미지의 기본 환경변수에 trust
anchor를 넣지 않으므로 실행 시 다시 외부 Secret으로 주입해야 합니다.

```bash
docker run --detach \
  --name chemicheck119-model-api \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --memory 1g \
  --cpus 2 \
  --publish 127.0.0.1:8000:8000 \
  --env CHEMIGUARD119_API_KEY=실제-운영-키 \
  --env CHEMIGUARD119_RUNTIME_MANIFEST_SHA256=64자리-manifest-sha256 \
  --env CHEMIGUARD119_GIT_COMMIT=40자리-release-commit \
  --env CHEMIGUARD119_RULE_POLICY=PUBLIC_SOURCE_PILOT_V1 \
  chemicheck119-model-api:release
```

실제 값은 shell history에 남기기보다 배포 플랫폼의 Secret 주입 기능을 사용하세요.

## 11. 시작 후 smoke test

### 11.1 Readiness

```bash
curl --fail http://127.0.0.1:8000/health/ready
```

다음을 확인합니다.

- `status=READY`
- `ready=true`
- `integrity.status=VERIFIED`
- `integrity.environment=production`
- `integrity.manifest_sha256_verified=true`
- `integrity.git_commit`이 릴리스 commit과 일치
- 공개 근거 파일럿 정책이 준비됨

### 11.2 인증

```bash
curl --fail -X POST http://127.0.0.1:8000/api/v1/substances/resolve \
  -H "X-API-Key: 실제-운영-키" \
  -H "Content-Type: application/json" \
  -d '{"query":"황산","top_k":1}'
```

API Key가 없거나 틀린 요청이 `401`로 차단되는지도 확인합니다.

### 11.3 안전 계약

- 후보 결과에 `rule_eligible=false`
- 현장 확인 전 충돌 검토 `executed=false`
- 확인 후 공개 근거 결과에 `policy_mode=PUBLIC_SOURCE_PILOT_V1`
- `expert_reviewed=false`
- `risk_scale.is_probability=false`
- `probability_percent=null`

## 12. 운영 관측

최소한 다음을 수집합니다.

- HTTP 상태별 요청 수
- endpoint별 latency
- readiness 실패 횟수와 원인 코드
- `X-Request-Id`, `analysis_id`, `incident_id`
- 업무 상태별 건수
- `UNRESOLVED`, `CAS_EVIDENCE_NOT_LOADED`, `UNCLASSIFIED` 비율
- 실행 중인 Git commit, manifest SHA-256, 정책 ID

신고 원문, 사용자 식별정보, 시설 세부정보를 일반 애플리케이션 로그에 그대로 남기지 않습니다.
필요한 감사 로그는 접근 제어·보존 기간이 있는 서비스 백엔드에서 관리합니다.

## 13. 롤백

rollback 단위는 코드만이 아니라 다음 전체 묶음입니다.

```text
container image
runtime artifact
config
runtime manifest SHA-256
Git commit
```

1. 마지막 정상 immutable 이미지와 trust anchor를 선택합니다.
2. 모든 인스턴스를 같은 버전으로 교체합니다.
3. readiness와 인증 smoke test를 다시 실행합니다.
4. 문제가 발생한 commit·manifest·요청 ID를 기록합니다.

새 코드와 이전 모델을 임의로 섞거나 이전 config만 따로 되돌리지 않습니다.

## 14. 배포 체크리스트

### 릴리스 전

- [ ] clean commit인지 확인
- [ ] Python 3.11 고정
- [ ] 데이터 bundle URL·SHA Secret 설정
- [ ] 운영 API Key Secret 설정
- [ ] 전체 테스트 통과
- [ ] 내부 평가 결과 확인
- [ ] manifest에 정확한 commit 기록
- [ ] 공개 근거 정책·crosswalk provenance 확인

### 배포 직후

- [ ] `/health/live` 성공
- [ ] `/health/ready`와 integrity `VERIFIED`
- [ ] API Key 인증 성공·실패 경로 확인
- [ ] 미확인 사고 요청의 Rule 미실행 확인
- [ ] 확인된 파일럿 응답의 비확률·전문가 미검토 표시 확인
- [ ] 로그에 Secret·신고 원문이 노출되지 않는지 확인

### 운영 중

- [ ] unresolved·근거 없음·미분류 비율 모니터링
- [ ] artifact·정책 버전을 요청과 함께 추적
- [ ] 원천 데이터 갱신 시 새 bundle과 새 manifest 생성
- [ ] rollback bundle 유지

배포 직후 공통 계약은 API Key를 환경변수로 주입한 다음 한 명령으로 검사할 수 있습니다.

```bash
CHEMICHECK119_MODEL_API_KEY="배포-Secret" \
PYTHONPATH=src python scripts/integration/smoke_model_api.py \
  --base-url https://모델-api-주소
```

## 15. 관련 문서

- [README](../README.md)
- [아키텍처](ARCHITECTURE.md)
- [API](API.md)
- [FE·BE·AI 연동 및 병합 계약](BACKEND_INTEGRATION.md)
- [데이터와 모델](DATA_AND_MODEL.md)
- [안전 및 한계](SAFETY_AND_LIMITATIONS.md)
