# 공모전 통합용 Cloud Run preview

## 성격

이 이미지는 FE·BE 연동과 공모전 시연을 위한 비운영 preview입니다.

- 배포 환경: `development`
- API 인증: `X-API-Key` 필수
- 전문가 검토: `expert_reviewed=false`
- 충돌 실행: 확인된 사고물질·시설물질 CAS 두 개가 있을 때만 허용
- RAG: 검색된 KOSHA·CAMEO 근거 범위 안에서만 생성하며 실패 시 extractive fallback
- 확장: min instances 0, max instances 3

`PILOT_REVIEWED` 품질·재배포·서명 gate를 통과한 운영 이미지가 아닙니다. 현장 출동과
실제 지휘 판단에 사용하면 안 됩니다. 운영 릴리스는 `Dockerfile.bundle`과
`release-model.yml`만 사용합니다.

## 2026-08-01 실제 데이터 기술 검수

`main` commit `646580e162196297ecd19e3f592da5f33776a5e4`와 실제 8개 원천 CSV로
`INTERNAL_REGRESSION` 파이프라인을 재실행했습니다.

| 항목 | 확인 결과 | 해석 |
|---|---:|---|
| 물질 / 별칭 | 4,300 / 9,685 | 검색 artifact 정상 생성 |
| 물성 프로필 | 749 | 관찰 기반 후보 검색 최소 700건 통과 |
| 근거 문서 | 5,858 | KOSHA·CAMEO 하이브리드 검색 대상 |
| 시설 이력 후보 | 168,424 | 현재 재고가 아닌 과거 취급 후보 |
| 물질 식별 | Top-1 0.9524, Top-3 1.0 | DRAFT 21건이므로 현장 정확도 주장 금지 |
| CAS 안전 회귀 | 12/12, 잘못된 자동 확정 0건 | 작은 DRAFT 안전 회귀만 통과 |
| 근거 section 검색 | Recall@5 0.875, MRR@5 0.9444 | 운영 기준 Recall@5 0.90 미달 |
| 잘못된 CAS 근거 | 0.0 | DRAFT 12건 기준 |
| 사고분석 E2E | 8/8, p95 193.3ms | 미확인 충돌 실행 0건, DRAFT 회귀 |

따라서 preview 실행은 허용하지만 운영·현장 사용은 승인하지 않습니다. API 응답의
`expert_reviewed=false`와 최종 지휘관 판단 문구를 유지합니다.

## 이미지 생성

먼저 실제 데이터로 `INTERNAL_REGRESSION` 파이프라인을 실행해 artifact를 생성합니다. 그 뒤
다음 변수를 주입해 Cloud Build를 실행합니다.

```bash
PREVIEW_ARTIFACT_DIR=/absolute/path/to/artifacts \
RUNTIME_MANIFEST_SHA256=64자리-sha256 \
MODEL_GIT_COMMIT=40자리-모델-commit \
GCP_PROJECT_ID=chemi-check \
GCP_REGION=asia-northeast3 \
GCP_ARTIFACT_REPOSITORY=chemicheck119 \
scripts/deployment/build_cloud_run_preview.sh
```

스크립트는 다음을 보장합니다.

1. 필수 artifact 존재와 runtime manifest SHA-256 검사
2. clean commit만 빌드
3. Git 추적 파일과 검증 artifact만 임시 build context에 포함
4. API Key와 attestation key를 이미지에 넣지 않음
5. Artifact Registry digest 반환

## 백엔드 계약

백엔드는 모델 API URL과 API Key를 서버 환경변수로만 보관합니다. FE에 Key를 전달하지
않습니다.

```text
CHEMICHECK119_MODEL_API_BASE_URL=https://배포된-preview-url
CHEMICHECK119_MODEL_API_KEY=Desktop의 보안 파일 값
CHEMICHECK119_MODEL_API_TIMEOUT_SECONDS=15
```

호출 시 다음 헤더가 필요합니다.

```http
X-API-Key: ${CHEMICHECK119_MODEL_API_KEY}
Content-Type: application/json
```

통합 분석은 `POST /api/v1/incidents/analyze`, 근거 기반 물질 검색은
`POST /api/v1/substances/discover`를 사용합니다.
