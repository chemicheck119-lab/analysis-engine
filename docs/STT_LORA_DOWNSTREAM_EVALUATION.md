# LoRA `wind_snr0` 후단 안전 Gate

## 목적과 사실 상태

- 사실 상태: **부분 구현 또는 개발용 데모**
- 입력: Speech `wind_snr0` Gate를 통과한 B/C development 132건
- 목적: LoRA candidate가 control보다 Parser·Resolver 실버 후보 보존과 확인 전 안전 계약을
  악화시키지 않는지 확인

AIHub label에는 사람이 확인한 CAS 정답이 없습니다. 따라서 이 평가는 CAS Top-1·Top-3
정답률이나 잘못된 단일 CAS 확정 건수를 측정하지 않습니다. 실제 현장 무전·현장 안전·운영
채택을 증명하지도 않습니다.

## 비교와 Gate

| 비교 항목 | 통과 기준 | 해석 한계 |
|---|---|---|
| Parser exact mention retention | C가 B보다 낮지 않음 | 사람 검수 NER Recall 아님 |
| Resolver candidate Top-3 retention | C가 B보다 낮지 않음 | CAS Top-3 정답률 아님 |
| 참조 음성에 없던 후보 신호 | C가 B보다 증가하지 않음 | 실제 오답 CAS 확정 건수 아님 |
| 참조 후보와 불일치한 자동 hint | C가 B보다 증가하지 않음 | 정답 CAS 부재로 검수 신호일 뿐 |
| Model API 실행 | 오류·분석 누락 0건 | 상용 가용성 측정 아님 |
| 확인 전 안전 계약 | 후보 승격·위험 출력·Rule 실행 위반 0건 | 현장 안전 보장 아님 |

모든 조건을 통과하면 `pass_proxy_downstream_keep_adoption_blocked`로 기록합니다. 이는 proxy
downstream Gate 통과일 뿐이며 `automatic_adoption_allowed=false`를 유지합니다. 하나라도
실패하면 candidate를 기각하고 현재 운영 기준선을 유지합니다.

## 실행

Speech summary·private record·wind report는 owner-only 비공개 경로에 둡니다. 인증값은 명령
인자가 아니라 환경변수로 전달합니다.

```bash
CHEMIGUARD119_API_KEY="$(gcloud secrets versions access 1 \
  --secret=chemicheck119-model-api-key --project=chemi-check)" \
CHEMICHECK119_IDENTITY_TOKEN="$(gcloud auth print-identity-token)" \
python scripts/evaluation/evaluate_lora_downstream.py \
  --speech-wind-report /private/wind/wind-development-evaluation.json \
  --b-summary /private/wind/B_same_conversion_base_control/summary.json \
  --b-records /private/wind/B_same_conversion_base_control/records.private.jsonl \
  --c-summary /private/wind/C_lora_merged_candidate/summary.json \
  --c-records /private/wind/C_lora_merged_candidate/records.private.jsonl \
  --output-dir /private/wind/downstream-UNIQUE \
  --model-api-base-url https://PRIVATE_MODEL_API_URL \
  --service-revision MODEL_API_REVISION \
  --service-git-commit MODEL_API_GIT_COMMIT \
  --runtime-manifest-sha256 MODEL_API_RUNTIME_MANIFEST_SHA256 \
  --evaluator-git-commit "$(git rev-parse HEAD)"
```

콘솔에는 arm별 진행 건수와 결과 파일 hash만 표시합니다. `report.json`은 원문 없는 집계이며,
두 private observation 파일도 표면명을 hash 처리하지만 원본과 연결될 수 있으므로 Git에
커밋하지 않습니다.
