#!/usr/bin/env bash
set -Eeuo pipefail

required_variables=(
  PREVIEW_ARTIFACT_DIR
  RUNTIME_MANIFEST_SHA256
  MODEL_GIT_COMMIT
  GCP_PROJECT_ID
  GCP_REGION
  GCP_ARTIFACT_REPOSITORY
)
for variable_name in "${required_variables[@]}"; do
  test -n "${!variable_name:-}" || {
    echo "필수 preview 빌드 환경변수가 없습니다: $variable_name"
    exit 1
  }
done

[[ "$RUNTIME_MANIFEST_SHA256" =~ ^[0-9a-f]{64}$ ]]
[[ "$MODEL_GIT_COMMIT" =~ ^[0-9a-f]{40}$ ]]
test -d "$PREVIEW_ARTIFACT_DIR"

required_artifacts=(
  chemiguard119.sqlite
  resolver.joblib
  retriever.joblib
  runtime_manifest.json
)
for artifact_name in "${required_artifacts[@]}"; do
  test -s "$PREVIEW_ARTIFACT_DIR/$artifact_name" || {
    echo "preview artifact가 없거나 비어 있습니다: $artifact_name"
    exit 1
  }
done

actual_manifest_sha256="$(shasum -a 256 "$PREVIEW_ARTIFACT_DIR/runtime_manifest.json" | awk '{print $1}')"
test "$actual_manifest_sha256" = "$RUNTIME_MANIFEST_SHA256" || {
  echo "preview runtime manifest SHA-256이 입력값과 다릅니다."
  exit 1
}

project_root="$(git rev-parse --show-toplevel)"
test -z "$(git -C "$project_root" status --porcelain)" || {
  echo "preview 이미지는 clean commit에서만 만들 수 있습니다."
  exit 1
}
python "$project_root/scripts/deployment/validate_manifest_runtime_versions.py" \
  --manifest "$PREVIEW_ARTIFACT_DIR/runtime_manifest.json" \
  --requirements "$project_root/requirements-production.txt" \
  --dockerfile "$project_root/Dockerfile.preview"

build_context="$(mktemp -d)"
cleanup() {
  rm -rf "$build_context"
}
trap cleanup EXIT

git -C "$project_root" archive --format=tar HEAD | tar -xf - -C "$build_context"
mkdir "$build_context/artifacts"
for artifact_name in "${required_artifacts[@]}"; do
  cp "$PREVIEW_ARTIFACT_DIR/$artifact_name" "$build_context/artifacts/$artifact_name"
done
cp "$build_context/.dockerignore.preview" "$build_context/.dockerignore"

registry_host="$GCP_REGION-docker.pkg.dev"
image_name="$registry_host/$GCP_PROJECT_ID/$GCP_ARTIFACT_REPOSITORY/model-api-preview"
image_tag="$image_name:$MODEL_GIT_COMMIT-$RUNTIME_MANIFEST_SHA256"

gcloud builds submit "$build_context" \
  --project "$GCP_PROJECT_ID" \
  --region "$GCP_REGION" \
  --config "$build_context/cloudbuild.preview.yaml" \
  --ignore-file "$build_context/.gcloudignore.preview" \
  --substitutions="_IMAGE=$image_tag,_RUNTIME_MANIFEST_SHA256=$RUNTIME_MANIFEST_SHA256,_MODEL_GIT_COMMIT=$MODEL_GIT_COMMIT" \
  --quiet

digest="$(gcloud artifacts docker images describe "$image_tag" \
  --project "$GCP_PROJECT_ID" \
  --format='value(image_summary.digest)')"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]
digest_reference="$image_name@$digest"

if [ -n "${GITHUB_OUTPUT:-}" ]; then
  echo "digest_reference=$digest_reference" >> "$GITHUB_OUTPUT"
fi
echo "$digest_reference"
