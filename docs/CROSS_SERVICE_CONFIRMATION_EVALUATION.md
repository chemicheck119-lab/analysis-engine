# Backend·Model API 실제 HTTP 확인 상태 전이 평가

## 목적

서로 분리된 보고서를 결합하는 데서 한 단계 더 나아가, 공개 합성 사고 한 건을 실제
Backend HTTP와 실제 Model API HTTP 사이로 전달합니다. 다음 상태 전이가 하나의
`incidentId`에서 이어지는지 검증합니다.

```text
0개 CAS 확인
→ Rule 차단·위험 표시 차단
→ 사고물질 CAS 확인
→ Rule 차단·위험 표시 차단
→ 시설물질 CAS 확인
→ 제한된 CAMEO 규칙 실행·서수 결과 표시
→ 시설물질 확인 취소
→ Rule 차단·기존 위험 표시 차단
```

각 HTTP 요청은 고유 `requestId`를 사용합니다. 하나의 사고 workflow는 `incidentId`로
연결하고, 각 분석 요청의 `requestId`만 FE 요청→Backend→Model API→Backend 응답 사이에서
같게 유지합니다. 여러 상태 변경 요청이 하나의 `requestId`를 재사용한다고 설명하면 안 됩니다.

## 실행 경계

평가기는 다음 조건을 요구합니다.

- Model API: 잠금 runtime manifest와 artifact가 준비된 로컬 API
- Backend: 실제 `RestModelApiClient`, H2 PostgreSQL compatibility mode
- 인증: 로컬 staging public-pilot session adapter
- 입력: `PUBLIC_SYNTHETIC` replay만 허용
- 원격 호출: 기본 차단, 승인된 환경에서만 `--allow-non-loopback`을 명시
- 원문 저장: 신고문·incident ID·confirmation ID를 보고서에 저장하지 않음

Model API와 Backend를 각각 실행한 뒤 다음 명령을 사용합니다.

```bash
chemiguard119 evaluate-cross-service-flow \
  --backend-git-commit <40자리 Backend commit> \
  --model-git-commit <40자리 Model API release commit> \
  --runtime-manifest <잠금 runtime_manifest.json> \
  --runtime-manifest-sha256 <64자리 runtime manifest SHA-256> \
  --database-runtime H2_POSTGRESQL_COMPATIBILITY_MODE \
  --report <private-data>/experiments/e2e/cross-service-confirmation-flow-v1-r1.json
```

평가기는 Model API `/health/ready`와 `/api/v1/meta`, Backend `/actuator/info`를 먼저
검사합니다. 입력으로 지정한 commit과 실제 서비스가 보고하는 commit이 다르면 평가를
통과시키지 않습니다.

## 2026-09-08 결과

| 항목 | 관측값 |
|---|---:|
| 전체 검사 | 69/69 통과 |
| Model runtime integrity | `VERIFIED` |
| 0개 확인 상태 | Rule 실행 `false`, 위험 표시 `false` |
| 1개 확인 상태 | Rule 실행 `false`, 위험 표시 `false` |
| 2개 확인 상태 | CAMEO Rule 실행 `true`, 위험 표시 `true` |
| 시설 확인 취소 뒤 | Rule 실행 `false`, 위험 표시 `false` |
| 재실행 재현성 | r1·r2 byte-identical |

- Backend commit: `5df879b3bd80a61340190ff4cefb97436fcc1c75`
- Model API release commit: `68beeb48e2c48a8fc3ae9adb8aa8afef10988035`
- Runtime manifest SHA-256: `637074a44fbc969baf292435f570800937ef75a72b42a6970034bc0416990b2e`
- Evaluator source SHA-256: `dc79d1ad7d270cd30ac2c7fef5141c679bd21f66fa3b74076bbc7618360e5158`
- Report SHA-256: `3325e3d1da0c7e86d0e4b2299a839ae4018881fce4c15c5daf371e9d714c7331`

## 사실 상태와 주장 범위

| 상태 | 내용 |
|---|---|
| 구현 완료 | 실제 Backend→Model API HTTP 호출, 0→1→2→취소 상태 전이 평가기 |
| 부분 구현 또는 개발용 데모 | 공개 합성 replay 1건·H2로 실행했다고 선언한 69개 내부 회귀 검사 |
| 설계 완료·구현 전 | Speech API 음성 입력부터 같은 incident workflow로 이어지는 평가, 인계 record 저장까지 연결 |
| 검증되지 않은 가설 | 현장 정확도·현장 안전성·Cloud SQL 고가용성·실제 대원 사용성 |

이 결과는 “공개 합성 지령이 실제 Backend와 실제 Model API 사이를 통과했다”는 근거입니다.
신고 음성이나 현장 무전을 입력하지 않았고, 실제 대원이 CAS를 확인한 것도 아니며, Cloud Run이나
Cloud SQL을 사용하지 않았습니다. DB 종류는 실행자가 `H2_POSTGRESQL_COMPATIBILITY_MODE`로
선언했고 Backend 시작 로그에서 관찰했지만 평가기가 자동 판별하지는 않으므로
`database_runtime_verified=false`입니다. 따라서 “음성→인계 전체 E2E”, “현장 검증”, “상용
운영 검증”으로 표현하면 안 됩니다.
