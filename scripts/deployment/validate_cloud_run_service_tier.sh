#!/usr/bin/env bash
set -Eeuo pipefail

if [ "$#" -ne 4 ]; then
  echo "사용법: $0 <release-tier> <service-name> <environment> <image-digest>"
  exit 2
fi

release_tier="$1"
service_name="$2"
environment="$3"
image_digest="$4"

case "$release_tier" in
  competition-preview)
    [[ "$service_name" == *-preview ]] || {
      echo "competition-preview는 -preview service에만 배포할 수 있습니다."
      exit 1
    }
    [ "$environment" = "development" ] || {
      echo "competition-preview 실행 환경은 development여야 합니다."
      exit 1
    }
    [[ "$image_digest" == */model-api-preview@sha256:* ]] || {
      echo "competition-preview는 model-api-preview digest만 사용할 수 있습니다."
      exit 1
    }
    ;;
  reviewed-staging)
    [[ "$service_name" == *-staging ]] || {
      echo "reviewed-staging은 -staging service에만 배포할 수 있습니다."
      exit 1
    }
    [ "$environment" = "staging" ] || {
      echo "reviewed-staging 실행 환경은 staging이어야 합니다."
      exit 1
    }
    [[ "$image_digest" == */model-api@sha256:* ]] || {
      echo "reviewed-staging은 model-api digest만 사용할 수 있습니다."
      exit 1
    }
    ;;
  *)
    echo "지원하지 않는 Cloud Run release tier입니다: $release_tier"
    exit 1
    ;;
esac
