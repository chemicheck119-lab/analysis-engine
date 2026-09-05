# STT 후단 Parser·Resolver 실버 평가

## 목적과 사실 상태

- 사실 상태: **부분 구현 또는 개발용 데모**
- 목적: AIHub 신고접수 전화의 참조 전사문에서 얻은 Parser·Resolver 결과가 STT 전사문에서도
  얼마나 보존되는지 측정합니다.
- 이 데이터에는 사람이 확인한 물질 CAS 정답이 없습니다. 따라서 이 평가는 CAS 정확도,
  현장 무전 성능, 실제 안전성을 증명하지 않습니다.

## 지표 해석

| 지표 | 의미 | 의미하지 않는 것 |
|---|---|---|
| `reference_parser_exact_mention_retention` | 참조 전사의 Parser 물질 표면명이 가설 Parser에도 남은 비율 | 사람 검수 NER Recall |
| `reference_candidate_top1_retention` | 참조 Resolver 1위 후보가 가설의 1위 후보 집합에 남은 비율 | CAS Top-1 정답률 |
| `reference_candidate_top3_retention` | 참조 Resolver 상위 3개 중 하나가 가설 상위 3개에 남은 비율 | CAS Top-3 정답률 |
| `reference_inconsistent_auto_hint_count` | 가설의 자동 검색 힌트가 참조 상위 3개와 겹치지 않은 횟수 | 실제 오답 CAS 확정 횟수 |
| 안전 위반 4종 | 확인 전 후보 승격·위험 출력·Rule 실행 회귀 | 현장 안전 보장 |

참조 전사 분석도 자동 Parser·Resolver 출력이므로 **실버 기준**입니다. 참조에 후보가 없거나
가설에만 후보가 생긴 경우는 별도 집계하며 정답·오답으로 단정하지 않습니다.

## 실행

STT `summary.json`과 `records.private.jsonl`을 비공개 경로에 내려받은 뒤 실행합니다.
인증값은 명령 인자가 아니라 환경변수로만 전달합니다.

```bash
CHEMIGUARD119_API_KEY="$(gcloud secrets versions access 1 \
  --secret=chemicheck119-model-api-key --project=chemi-check)" \
CHEMICHECK119_IDENTITY_TOKEN="$(gcloud auth print-identity-token)" \
python scripts/evaluation/evaluate_stt_downstream.py \
  --records-private /private/path/records.private.jsonl \
  --speech-summary /private/path/summary.json \
  --output-dir /private/path/downstream-evaluation \
  --model-api-base-url https://PRIVATE_MODEL_API_URL \
  --service-revision MODEL_API_REVISION \
  --service-git-commit MODEL_API_GIT_COMMIT \
  --runtime-manifest-sha256 MODEL_API_RUNTIME_MANIFEST_SHA256
```

콘솔에는 진행 건수와 파일 해시만 표시합니다. 출력 중 `report.json`은 전사문이 없는 집계이고,
`records.private.jsonl`은 해시 처리한 표면명과 후보 CAS만 포함하지만 원본과 연결될 수 있으므로
비공개로 유지합니다. 두 파일의 SHA-256과 고정 Model API revision을 함께 기록합니다.
