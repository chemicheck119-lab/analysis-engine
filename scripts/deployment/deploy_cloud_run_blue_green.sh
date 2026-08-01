#!/usr/bin/env bash
set -Eeuo pipefail

required_variables=(
  IMAGE_DIGEST
  RUNTIME_MANIFEST_SHA256
  RELEASE_GIT_COMMIT
  GCP_PROJECT_ID
  GCP_REGION
  GCP_ARTIFACT_REPOSITORY
  GCP_CLOUD_RUN_SERVICE
  GCP_RUNTIME_SERVICE_ACCOUNT
  GCP_MODEL_API_KEY_SECRET
  GCP_MODEL_API_KEY_SECRET_VERSION
  CHEMIGUARD119_API_KEY
)
for variable_name in "${required_variables[@]}"; do
  test -n "${!variable_name:-}" || {
    echo "필수 배포 환경변수가 없습니다: $variable_name"
    exit 1
  }
done

minimum_instances="${GCP_MIN_INSTANCES:-0}"
maximum_instances="${GCP_MAX_INSTANCES:-3}"
[[ "$minimum_instances" =~ ^[0-9]+$ ]]
[[ "$maximum_instances" =~ ^[1-9][0-9]*$ ]]
(( minimum_instances <= maximum_instances ))
[[ "$GCP_MODEL_API_KEY_SECRET_VERSION" =~ ^[1-9][0-9]*$ ]]
[[ "$RUNTIME_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$RELEASE_GIT_COMMIT" =~ ^[0-9a-f]{40}$ ]]

expected_image_prefix="$GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$GCP_ARTIFACT_REPOSITORY/model-api@sha256:"
[[ "$IMAGE_DIGEST" == "$expected_image_prefix"* ]]
[[ "$IMAGE_DIGEST" =~ @sha256:[0-9a-f]{64}$ ]]

run_attempt="${GITHUB_RUN_ATTEMPT:-1}"
revision_suffix="r${RELEASE_GIT_COMMIT:0:7}${run_attempt}"
candidate_tag="candidate-${RELEASE_GIT_COMMIT:0:7}-${run_attempt}"
service_snapshot="$(mktemp)"
candidate_snapshot="$(mktemp)"
cleanup() {
  rm -f "$service_snapshot" "$candidate_snapshot"
}
trap cleanup EXIT

previous_revision=""
if gcloud run services describe "$GCP_CLOUD_RUN_SERVICE" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --format=json >"$service_snapshot" 2>/dev/null; then
  previous_revision="$(python - "$service_snapshot" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
traffic = (payload.get("status") or {}).get("traffic") or []
active = [item for item in traffic if int(item.get("percent") or 0) > 0]
active.sort(key=lambda item: int(item.get("percent") or 0), reverse=True)
print((active[0].get("revisionName") if active else "") or "")
PY
)"
fi

gcloud run deploy "$GCP_CLOUD_RUN_SERVICE" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --platform managed \
  --image "$IMAGE_DIGEST" \
  --revision-suffix "$revision_suffix" \
  --tag "$candidate_tag" \
  --no-traffic \
  --allow-unauthenticated \
  --ingress all \
  --execution-environment gen2 \
  --service-account "$GCP_RUNTIME_SERVICE_ACCOUNT" \
  --port 8000 \
  --cpu 2 \
  --memory 1Gi \
  --concurrency 4 \
  --timeout 30s \
  --min-instances "$minimum_instances" \
  --max-instances "$maximum_instances" \
  --cpu-boost \
  --deploy-health-check \
  --startup-probe="httpGet.path=/health/ready,httpGet.port=8000,timeoutSeconds=3,periodSeconds=5,failureThreshold=24" \
  --liveness-probe="httpGet.path=/health/live,httpGet.port=8000,timeoutSeconds=3,periodSeconds=30,failureThreshold=3" \
  --set-env-vars="CHEMIGUARD119_ENVIRONMENT=staging,CHEMIGUARD119_RUNTIME_MANIFEST_SHA256=$RUNTIME_MANIFEST_SHA256,CHEMIGUARD119_GIT_COMMIT=$RELEASE_GIT_COMMIT,CHEMIGUARD119_RULE_POLICY=PUBLIC_SOURCE_PILOT_V1,CHEMIGUARD119_RAG_MODE=extractive,CHEMIGUARD119_LOG_LEVEL=INFO,CHEMIGUARD119_API_HOST=0.0.0.0,CHEMIGUARD119_API_PORT=8000" \
  --set-secrets="CHEMIGUARD119_API_KEY=$GCP_MODEL_API_KEY_SECRET:$GCP_MODEL_API_KEY_SECRET_VERSION" \
  --quiet

gcloud run services describe "$GCP_CLOUD_RUN_SERVICE" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --format=json >"$candidate_snapshot"

read -r candidate_revision candidate_url service_url < <(
  CANDIDATE_TAG="$candidate_tag" python - "$candidate_snapshot" <<'PY'
import json
import os
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
status = payload.get("status") or {}
target_tag = os.environ["CANDIDATE_TAG"]
candidate = next(
    (item for item in status.get("traffic") or [] if item.get("tag") == target_tag),
    {},
)
print(candidate.get("revisionName", ""), candidate.get("url", ""), status.get("url", ""))
PY
)

test -n "$candidate_revision"
test -n "$candidate_url"
test -n "$service_url"
test "$candidate_revision" != "$previous_revision"

deployed_image="$(gcloud run revisions describe "$candidate_revision" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --format='value(spec.containers[0].image)')"
if [ "$deployed_image" != "$IMAGE_DIGEST" ]; then
  echo "배포된 리비전 이미지가 요청 digest와 다릅니다."
  echo "요청: $IMAGE_DIGEST"
  echo "배포: $deployed_image"
  exit 1
fi

smoke() {
  local base_url="$1"
  local ready_file
  ready_file="$(mktemp)"
  local http_code="000"
  for _attempt in $(seq 1 40); do
    http_code="$(curl --silent --show-error \
      --output "$ready_file" \
      --write-out '%{http_code}' \
      "$base_url/health/ready" || true)"
    if [ "$http_code" = "200" ]; then
      break
    fi
    sleep 2
  done
  if [ "$http_code" != "200" ]; then
    echo "Cloud Run readiness smoke 실패: HTTP $http_code"
    rm -f "$ready_file"
    return 1
  fi
  READY_FILE="$ready_file" python - <<'PY'
import json
import os

payload = json.load(open(os.environ["READY_FILE"], encoding="utf-8"))
integrity = payload.get("integrity") or {}
if payload.get("ready") is not True or integrity.get("status") != "VERIFIED":
    raise SystemExit("Cloud Run readiness 무결성 검증 실패")
PY
  rm -f "$ready_file"
  CHEMICHECK119_MODEL_API_KEY="$CHEMIGUARD119_API_KEY" \
    PYTHONPATH=src \
    python scripts/integration/smoke_model_api.py \
      --base-url "$base_url" \
      --api-key-env CHEMICHECK119_MODEL_API_KEY
}

smoke "$candidate_url"

promoted=false
rollback() {
  if [ "$promoted" = true ] && [ -n "$previous_revision" ]; then
    echo "전환 후 검사 실패: 이전 리비전 $previous_revision 으로 롤백합니다."
    gcloud run services update-traffic "$GCP_CLOUD_RUN_SERVICE" \
      --project "$GCP_PROJECT_ID" \
      --region "$GCP_REGION" \
      --to-revisions "$previous_revision=100" \
      --quiet
  fi
}
on_exit() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    rollback
  fi
  cleanup
  exit "$status"
}
trap on_exit EXIT

gcloud run services update-traffic "$GCP_CLOUD_RUN_SERVICE" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --to-revisions "$candidate_revision=100" \
  --quiet
promoted=true

smoke "$service_url"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  {
    echo "revision=$candidate_revision"
    echo "service_url=$service_url"
    echo "candidate_url=$candidate_url"
    echo "previous_revision=$previous_revision"
  } >> "$GITHUB_OUTPUT"
fi

trap cleanup EXIT
echo "Cloud Run Blue/Green 배포 완료: $candidate_revision"
