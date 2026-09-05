# Cloud Run 무중단 스테이징 배포

## 1. 한눈에 이해하기

대상은 GCP 프로젝트 `chemi-check`, 서울 리전 `asia-northeast3`입니다.

```text
main의 검증된 코드·데이터
→ Dockerfile.bundle 이미지
→ Artifact Registry digest
→ Cloud Run 후보 리비전 0%
→ /health/ready + 인증 API smoke
→ 새 리비전 100%
→ 실패 시 이전 리비전 100% 복원
```

태블릿 FE는 이 URL을 직접 호출하지 않습니다. 서비스 BE만 Cloud Run URL과 모델 API Key를
사용합니다. Cloud Run 호출은 IAM으로 비공개이며, `/api/v1/*`는 추가로 `X-API-Key`가 없으면
차단됩니다. 배포 스모크 테스트도 배포 서비스 계정의 ID 토큰과 API Key를 함께 사용합니다.

## 2. 현재 준비 상태

저장소에는 다음 값이 Repository Variable로 등록돼 있습니다.

| 변수 | 값 |
|---|---|
| `GCP_PROJECT_ID` | `chemi-check` |
| `GCP_REGION` | `asia-northeast3` |
| `GCP_ARTIFACT_REPOSITORY` | `chemicheck119` |
| `GCP_CLOUD_RUN_SERVICE` | `chemicheck119-model-api-staging` |
| `GCP_RUNTIME_SERVICE_ACCOUNT` | `chemicheck119-runtime@chemi-check.iam.gserviceaccount.com` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | `chemicheck119-github-deploy@chemi-check.iam.gserviceaccount.com` |
| `GCP_MODEL_API_KEY_SECRET` | `chemicheck119-model-api-key` |
| `GCP_MODEL_API_KEY_SECRET_VERSION` | `1` |
| `GCP_MIN_INSTANCES` | `0` |
| `GCP_MAX_INSTANCES` | `3` |

GitHub Environment `staging`도 생성돼 있습니다. 아직 없는 것은 GCP 리소스, Workload Identity
Provider 값과 실제 Secret입니다.

## 3. 왜 Service Account JSON을 쓰지 않는가

GitHub Actions는 OIDC Workload Identity Federation으로 짧게 유효한 인증을 받습니다.
장기 Service Account Key JSON을 GitHub Secret이나 저장소에 두지 않습니다.

필요한 GitHub 권한은 다음 두 개뿐입니다.

```yaml
permissions:
  contents: read
  id-token: write
```

`google-github-actions/auth`가 만드는 `gha-creds-*.json`도 `.gitignore`에 포함돼 있습니다.

## 4. 현준이 한 번만 준비할 GCP 설정

이 단계는 GCP 프로젝트 소유자 권한과 결제 활성화가 필요합니다. 아래 명령은 Cloud Shell 또는
Google Cloud CLI가 설치된 터미널에서 실행합니다. 비밀번호나 Service Account Key를 채팅이나
GitHub에 붙이지 않습니다.

```bash
gcloud auth login chjune7777@gmail.com
gcloud config set project chemi-check

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com
```

Artifact Registry를 만듭니다.

```bash
gcloud artifacts repositories describe chemicheck119 \
  --location asia-northeast3 >/dev/null 2>&1 || \
gcloud artifacts repositories create chemicheck119 \
  --repository-format docker \
  --location asia-northeast3 \
  --description '케미체크119 검증 모델 이미지'
```

배포용·실행용 계정을 분리합니다.

```bash
gcloud iam service-accounts create chemicheck119-github-deploy \
  --display-name '케미체크119 GitHub 배포'

gcloud iam service-accounts create chemicheck119-runtime \
  --display-name '케미체크119 Cloud Run 실행'

for role in roles/artifactregistry.writer roles/run.admin; do
  gcloud projects add-iam-policy-binding chemi-check \
    --member 'serviceAccount:chemicheck119-github-deploy@chemi-check.iam.gserviceaccount.com' \
    --role "$role"
done

gcloud iam service-accounts add-iam-policy-binding \
  chemicheck119-runtime@chemi-check.iam.gserviceaccount.com \
  --member 'serviceAccount:chemicheck119-github-deploy@chemi-check.iam.gserviceaccount.com' \
  --role roles/iam.serviceAccountUser
```

## 5. GitHub OIDC 연결

```bash
PROJECT_NUMBER="$(gcloud projects describe chemi-check --format='value(projectNumber)')"
POOL_ID=github-actions
PROVIDER_ID=chemicheck119-llm

gcloud iam workload-identity-pools describe "$POOL_ID" \
  --location global >/dev/null 2>&1 || \
gcloud iam workload-identity-pools create "$POOL_ID" \
  --location global \
  --display-name 'GitHub Actions'

gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
  --workload-identity-pool "$POOL_ID" \
  --location global >/dev/null 2>&1 || \
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
  --workload-identity-pool "$POOL_ID" \
  --location global \
  --issuer-uri 'https://token.actions.githubusercontent.com' \
  --attribute-mapping 'google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref' \
  --attribute-condition "assertion.repository=='chemicheck119/llm' && assertion.ref=='refs/heads/main'"

gcloud iam service-accounts add-iam-policy-binding \
  chemicheck119-github-deploy@chemi-check.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID/attribute.repository/chemicheck119/llm"

PROVIDER_NAME="$(gcloud iam workload-identity-pools providers describe \
  "$PROVIDER_ID" \
  --workload-identity-pool "$POOL_ID" \
  --location global \
  --format='value(name)')"

gh variable set GCP_WORKLOAD_IDENTITY_PROVIDER \
  --repo chemicheck119/llm \
  --body "$PROVIDER_NAME"
```

조건을 `chemicheck119/llm`의 `main`으로 제한했기 때문에 다른 저장소나 PR 브랜치는 이 배포
권한을 사용할 수 없습니다.

## 6. 모델 API Key를 Secret Manager에 넣기

같은 API Key를 GCP Secret Manager와 GitHub Actions smoke용 Secret에 등록합니다. 화면에
값이 보이지 않도록 터미널에서 숨김 입력을 사용합니다.

```bash
gcloud secrets describe chemicheck119-model-api-key >/dev/null 2>&1 || \
gcloud secrets create chemicheck119-model-api-key --replication-policy automatic

read -r -s -p '32바이트 이상 모델 API Key: ' MODEL_API_KEY
echo
printf '%s' "$MODEL_API_KEY" | \
  gcloud secrets versions add chemicheck119-model-api-key --data-file=-
printf '%s' "$MODEL_API_KEY" | \
  gh secret set CHEMIGUARD119_API_KEY --repo chemicheck119/llm
unset MODEL_API_KEY

gcloud secrets add-iam-policy-binding chemicheck119-model-api-key \
  --member 'serviceAccount:chemicheck119-runtime@chemi-check.iam.gserviceaccount.com' \
  --role roles/secretmanager.secretAccessor
```

새 Secret이면 버전은 `1`입니다. 기존 Secret에 새 버전을 추가했다면 다음 변수도 실제 버전으로
바꿉니다.

```bash
gh variable set GCP_MODEL_API_KEY_SECRET_VERSION \
  --repo chemicheck119/llm \
  --body 실제-버전-번호
```

## 7. 검증 이미지 만들기

PR을 병합한 뒤 `main`에서 모델 릴리스 workflow를 실행합니다.

```bash
gh workflow run release-model.yml \
  --repo chemicheck119/llm \
  --ref main \
  -f push_ghcr=false \
  -f push_artifact_registry=true
```

이 workflow는 DRAFT 평가나 재배포 미승인 데이터를 우회하지 않습니다. 현재 데이터·평가 gate가
남아 있으면 Artifact Registry push 전에 실패하는 것이 정상입니다.

성공한 Actions summary에서 다음 세 값을 복사합니다.

- Artifact Registry `image@sha256:...`
- runtime manifest SHA-256
- 40자리 Git commit

## 8. 무중단 스테이징 배포

```bash
gh workflow run deploy-cloud-run.yml \
  --repo chemicheck119/llm \
  --ref main \
  -f image_digest='asia-northeast3-docker.pkg.dev/chemi-check/chemicheck119/model-api@sha256:64자리' \
  -f runtime_manifest_sha256='64자리-manifest-sha256' \
  -f git_commit='40자리-main-commit'
```

workflow는 다음을 보장합니다.

1. 입력 이미지가 지정한 프로젝트·서울 Registry의 digest인지 검사
2. 새 리비전을 `--no-traffic`으로 배포
3. 리비전이 요청한 이미지 digest로 실행되는지 재검사
4. candidate URL에서 readiness·무결성·인증·안전 계약 검사
5. 검사 성공 후 새 리비전으로 트래픽 100% 전환
6. 서비스 URL에서 같은 smoke 재검사
7. 전환 후 실패하면 이전 리비전으로 100% 자동 롤백

첫 배포에는 이전 리비전이 없지만 candidate URL 검사를 통과하기 전까지 기본 URL에 트래픽을
보내지 않습니다. 두 번째 배포부터 완전한 Blue/Green 전환과 자동 롤백이 적용됩니다.

## 9. 비용과 상시 가용성

현재 `GCP_MIN_INSTANCES=0`이므로 유휴 비용을 줄일 수 있지만 cold start는 남습니다. 대회 당일
상시 대기 인스턴스 한 개가 필요하고 비용 발생을 승인했다면 다음처럼 변경합니다.

```bash
gh variable set GCP_MIN_INSTANCES --repo chemicheck119/llm --body 1
```

최대 인스턴스는 기본 3으로 제한해 예상하지 못한 확장을 막습니다. 비용·쿼터는 배포 직전 GCP
Billing과 Cloud Run 화면에서 다시 확인합니다.

## 10. 현재 실제 배포를 막는 조건

- `GCP_WORKLOAD_IDENTITY_PROVIDER` 미등록
- GCP Artifact Registry·Service Account·Secret 생성 여부 미확인
- 모델 릴리스용 데이터·평가 bundle GitHub Secrets 미등록
- CAMEO·ICIS 파생 데이터 `REVIEW_REQUIRED`
- `PILOT_REVIEWED` locked 평가 미완료

이 조건을 workflow에서 삭제하거나 값을 꾸며서 우회하지 않습니다. 조건이 갖춰지면 같은
workflow를 그대로 실행해 스테이징 URL과 배포 리비전을 확인할 수 있습니다.
