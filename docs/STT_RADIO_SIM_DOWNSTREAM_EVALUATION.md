# 모의 통신 왜곡 STT 후단 실버 평가

## 목적과 사실 상태

- 사실 상태: **평가기 구현 완료·실제 데이터 실행 전**
- 대상: AIHub 신고접수 전화에서 절차적으로 만든 `radio-sim-v1`의 clean+17개 왜곡 조건
- 목적: 조건별 STT 가설에서 Parser 물질 언급과 Resolver 후보가 얼마나 보존되는지 확인하고,
  미확인 입력의 2-CAS Gate·후보 상태·위험 출력 안전 계약 회귀를 탐지합니다.

사람이 확인한 CAS 정답과 실제 현장 무전이 없으므로 CAS Top-1·Top-3 정확도, 현장 무전
성능, 실제 안전성을 증명하지 않습니다. 참조 전사도 같은 자동 Parser·Resolver로 분석한
실버 기준입니다.

## 입력 계약

평가기는 다음 조건을 모두 만족하지 않으면 Model API를 호출하기 전에 중단합니다.

1. clean과 등록된 17개 왜곡 조건이 정확히 모두 존재
2. 조건별 레코드 수가 같고 조건당 200건 이하
3. 모든 조건의 `record_key`와 참조 전사문이 clean과 동일
4. speech-service summary의 `radio-sim-v1`·record-set SHA-256·실행 설정과 일치
5. `faster-whisper 1.2.1`, `small`, CPU int8, baseline-only 설정
6. 비공개 레코드 파일 512MiB·전사 입력 4,000자 상한

조건별 request ID namespace를 분리해 같은 원본 레코드의 서로 다른 왜곡 가설이 감사
로그에서 충돌하지 않게 합니다.

## 지표와 판정

- 조건별 Parser 정확 표면명 보존율
- 조건별 Resolver 참조 후보 Top-1·Top-3 보존율과 후보 coverage
- Model API 오류·STT 가설 부재
- 모든 STT 가설이 후단 분석까지 완료됐는지 확인하는 별도 커버리지 Gate
- 2-CAS Gate 우회, Rule 조기 실행, 후보 자동 승격, 미확인 위험 출력 건수
- clean 대비 조건별 집계 변화

최종 후단 평가 Gate는 API 무결성, 분석 커버리지, 안전 계약을 모두 통과해야 열립니다.

clean 대비 변화는 같은 record set의 **집계 차이**이며 paired 신뢰구간이 아닙니다. CAS
정답이 없으므로 잘못된 단일 CAS 확정 정답 건수는 `null`로 유지합니다.

한 지역에서 전체 우선용어 분모 20 이상·Recall 0.80 미만이면서 특정 공개 용어도 분모 5
이상·Recall 0.80 미만일 때만 반복 가능한 용어 누락 서명으로 기록합니다. 참조 후보
Top-3 보존율이 낮더라도 공통 후보 오류 서명을 만들 수 없으면 미해결 신호로 남깁니다.
서울과 인천에서 동일 조건·동일 공개 용어 누락이 반복되는지 확인한 다음에만 광주
Training 기반 학습 가설을 세웁니다.

두 지역 보고서가 준비되면 다음 비교 Gate를 실행합니다. 서로 다른 source manifest,
동일한 우선용어 목록·STT 설정·후단 평가기·Model API artifact가 아니면 비교를
거부합니다. 같은 조건과 같은 공개 용어 누락이 반복되어도 자동 학습을 허용하지 않고
제한된 LoRA 실험 설계 자격만 부여합니다.

```bash
python scripts/evaluation/evaluate_stt_robustness_lora_gate.py \
  --incheon-report /private/incheon/downstream-silver/report.json \
  --seoul-report /private/seoul/downstream-silver/report.json \
  --priority-terms /workspace/speech-service/config/domain_hotwords.txt \
  --evaluator-git-commit "$(git rev-parse HEAD)" \
  --output /private/radio-sim-v1-cross-region-lora-gate.json
```

## 실행

명목 API 호출 수는 `조건 수 × 레코드 수 × 참조·가설 2회`입니다. 조건당 40건이면 한
지역 1,440회이며, 재시도는 호출당 최대 2회로 제한됩니다. 인증값은 환경변수로만 전달하고
출력 디렉터리는 기존 경로를 덮어쓰지 않습니다.

```bash
CHEMIGUARD119_API_KEY="$(gcloud secrets versions access 1 \
  --secret=chemicheck119-model-api-key --project=chemi-check)" \
CHEMICHECK119_IDENTITY_TOKEN="$(gcloud auth print-identity-token)" \
python scripts/evaluation/evaluate_stt_robustness_downstream.py \
  --records-private /private/radio-sim-v1/records.private.jsonl \
  --speech-summary /private/radio-sim-v1/summary.json \
  --priority-terms /workspace/speech-service/config/domain_hotwords.txt \
  --output-dir /private/radio-sim-v1/downstream-silver \
  --model-api-base-url https://PRIVATE_MODEL_API_URL \
  --evaluator-git-commit "$(git rev-parse HEAD)" \
  --speech-image-digest sha256:SPEECH_IMAGE_DIGEST \
  --service-revision MODEL_API_REVISION \
  --service-git-commit MODEL_API_GIT_COMMIT \
  --runtime-manifest-sha256 MODEL_API_RUNTIME_MANIFEST_SHA256
```

`report.json`에는 집계와 artifact·코드 SHA만 들어갑니다. 후보 CAS와 표면명 해시,
`record_key`가 있는 `records.private.jsonl`은 원본과 연결될 수 있으므로 비공개 GCS에만
보관하고 Git에 커밋하지 않습니다.
