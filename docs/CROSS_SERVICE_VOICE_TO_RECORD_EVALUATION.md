# 합성 음성부터 기록 저장까지 실제 HTTP 평가

## 한눈에 보는 결론

2026-09-08, SHA-256으로 잠근 공개 합성 WAV 1건을 실제 로컬 Speech API, Backend,
Model API 사이로 전달했습니다. 전사 결과에서 두 물질 표현이 보존된 선택 clip은 후보 조회,
합성 2-CAS 확인, 제한된 CAMEO Rule 실행, Backend 기록 저장까지 이어졌고 64/64 검사가
통과했습니다. 같은 입력으로 두 번 실행한 JSON 보고서는 바이트 단위로 동일했습니다.

이 결과의 사실 상태는 **부분 구현 또는 개발용 데모**입니다. 실제 신고·현장 무전, 사람의
전사 검토와 CAS 확인, Cloud Run·Cloud SQL, 실제 인계나 대응 조치는 검증하지 않았습니다.

## 평가 질문과 채택 기준

**평가 질문**

> 합성 음성의 불확실성을 보존한 전사가 실제 서비스 경계를 통과하더라도, 두 CAS가 각각
> 확인되기 전에는 Rule과 위험 표시가 차단되고, 확인 뒤 생성된 권위 snapshot만 기록에
> 저장되는가?

**채택 조건**

- Speech API가 음성을 보관하지 않고, 화학 식별·CAS 확인·위험 판단을 수행하지 않을 것
- 0개 CAS 확인 상태에서 Rule 실행과 위험 표시가 모두 차단될 것
- 합성으로 두 CAS를 각각 확인한 뒤에만 CAMEO Rule이 실행될 것
- 권위 `analysisId`와 두 `confirmationId`가 있어야 record 저장이 가능할 것
- 같은 payload의 재요청이 같은 record ID를 반환할 것
- 같은 입력의 재실행 보고서가 바이트 단위로 동일할 것

## 실행한 경로

```text
잠긴 공개 합성 WAV
  → Backend 음성 BFF
  → Speech API /transcriptions
  → 검토가 필요한 전사문
  → Backend 분석 BFF
  → Model API /incidents/analyze
  → 0-CAS Gate: Rule·위험 표시 차단
  → 사고물질 CAS 합성 확인
  → 1-CAS Gate: Rule·위험 표시 차단
  → 시설물질 CAS 합성 확인
  → 2-CAS Gate: 제한된 CAMEO Rule 실행
  → Backend record 저장
  → 동일 payload 재요청의 멱등성 확인
```

각 HTTP 요청에는 서로 다른 `requestId`를 사용했습니다. 하나의 workflow는 `incidentId`로
연결하고, 각 분석 호출 안에서만 같은 `requestId`가 Backend→Model API→Backend 응답까지
보존되는지 검사했습니다.

## 입력과 선택 편향 공개

| 항목 | 값 |
|---|---|
| 데이터 분류 | `PUBLIC_SYNTHETIC` |
| 생성 방식 | macOS `say` Yuna `ko_KR`, rate 135 |
| 오디오 | 16 kHz·16-bit·mono WAV, 242,222 bytes |
| 오디오 SHA-256 | `6b584709c1dda8c35f6a900282fc39fa398af84a57b7e5461f3d489684dfd257` |
| Manifest SHA-256 | `2dad97f53b6b1547c6d4aebf534f97ce1571efa032cbd263442929e5c26bccd5` |
| 목적 | 정확도 평가가 아닌 서비스 연결성 회귀 |

처음 만든 붙여 읽기 clip에서는 `차아염소산나트륨`이 `최하염소산나트륨`으로 전사됐고,
Resolver Top-1이 다른 CAS `7775-09-9`를 반환했습니다. 이는 실제 실패 사례이며 성능 결과에서
제외하거나 성공으로 바꾸지 않습니다.

연결성 회귀에는 물질명을 명료하게 띄어 읽은 두 번째 clip을 선택했습니다. 이 clip은
`차아염소산 나트륨`과 `염산`을 보존했지만 `누출`을 `노출`로 잘못 전사했습니다. 따라서
선택 clip의 성공은 STT 정확도·현장 강건성·비선택 표본 성능의 근거가 아닙니다.

## 2026-09-08 관측 결과

| 구간 | 관측값 | 판정 |
|---|---:|---|
| 전체 결정적 검사 | 64/64 | 통과 |
| Speech 실행 장치 | CPU `int8` | 관측 |
| hotword | 사용 안 함 | 기준선 유지 |
| 물질 표면형 보존 | 2/2 | 선택 clip에서만 통과 |
| 0개 확인 Rule 실행 | 0건 | 안전 Gate 통과 |
| 0개 확인 위험 표시 | 0건 | 안전 Gate 통과 |
| 1개 확인 Rule 실행 | 0건 | 안전 Gate 통과 |
| 1개 확인 위험 표시 | 0건 | 안전 Gate 통과 |
| 합성 2-CAS 확인 뒤 Rule 실행 | 1건 | 제한 규칙 실행 |
| record 저장 | 1건 | 성공 |
| 동일 payload 재요청 | 같은 record ID | 멱등성 통과 |
| 재실행 | r1·r2 byte-identical | 재현성 통과 |

- Report SHA-256:
  `ef116d33b8d0f46e04a2477dbe4b69d23481565fec4da154b18abd09e1e6f7ab`
- Evaluator source SHA-256:
  `5d8439371abcb5c5017bde8d4e31c1902f3c67962987665f78bdeed511adf0c1`
- Model runtime manifest SHA-256:
  `637074a44fbc969baf292435f570800937ef75a72b42a6970034bc0416990b2e`

## 재현 절차

잠긴 Model API artifact, Speech API와 Backend를 각각 로컬에서 실행한 뒤 다음 명령을
사용합니다. WAV와 결과 보고서는 개인정보나 대용량 원본을 Git에 넣지 않기 위해
`private-data`에 둡니다.

```bash
chemiguard119 evaluate-cross-service-voice-flow \
  --manifest data/evaluation/synthetic_voice_e2e_manifest.json \
  --audio <private-data>/experiments/e2e/synthetic-voice-v1/input-spaced.wav \
  --backend-git-commit 3bdce869691e50af3f557c98892a98f078cfffd4 \
  --model-git-commit 68beeb48e2c48a8fc3ae9adb8aa8afef10988035 \
  --speech-git-commit 0f8914151ecf1a1b5076a15ad47fd78e907ab3a9 \
  --runtime-manifest <private-data>/analysis-runtime/model-api-preview-68beeb4-prod/artifacts/runtime_manifest.json \
  --runtime-manifest-sha256 637074a44fbc969baf292435f570800937ef75a72b42a6970034bc0416990b2e \
  --database-runtime H2_POSTGRESQL_COMPATIBILITY_MODE \
  --report <private-data>/experiments/e2e/cross-service-voice-to-record-v1-r1.json
```

기본값은 loopback URL만 허용합니다. 원격 서비스 호출은 명시적 `--allow-non-loopback` 없이는
차단됩니다. 실행 전 manifest와 오디오 SHA-256이 다르거나 공개 합성 replay가 아니면 Speech
호출 전에 중단합니다.

## 사실 상태와 주장 경계

| 사실 상태 | 현재 근거 |
|---|---|
| 구현 완료 | SHA 잠금 입력, 실제 3-service HTTP 평가기, 2-CAS Gate·record 멱등성 검사 |
| 부분 구현 또는 개발용 데모 | 선택한 합성 음성 1건, 합성 확인, H2 PostgreSQL 호환 모드 실행 |
| 설계 완료·구현 전 | 사람 전사 검토 UI를 포함한 실제 확인 workflow, Cloud SQL 동시성 평가 |
| 검증되지 않은 가설 | 실제 신고·무전 정확도, 현장 안전성, 실제 인계 효율, 상용 가용성 |

**말할 수 있는 것**

- 공개 합성 WAV 1건이 실제 Speech API·Backend·Model API HTTP를 통과했다.
- 후보 단계에서는 CAS가 Rule 입력으로 자동 승격되지 않았다.
- 합성 2-CAS 확인 뒤 제한된 공개 CAMEO 규칙과 권위 참조를 record에 저장했다.

**말하면 안 되는 것**

- “신고·현장 무전에서 STT가 정확하다.”
- “사람이 전사와 두 CAS를 검토했다.”
- “음성부터 실제 현장 인계까지 운영 검증했다.”
- “H2 결과로 Cloud SQL 고가용성을 증명했다.”
- “Speech 모델 artifact와 commit을 API가 검증했다.”

## GPU를 사용하지 않은 이유와 다음 Gate

이번 실험은 모델 성능 학습이 아니라 서비스 계약과 안전 상태 전이 검증입니다. 합성 clip
1건을 GPU로 전사해도 현장 정확도 근거가 늘지 않고, CPU `int8` 기준선의 실제 배포 경로와
다른 조건을 섞게 됩니다.

GPU는 서울·인천 교차지역과 모의 통신 왜곡에서 반복되는 오류 유형이 확인된 뒤 제한된
Whisper LoRA 실험에만 사용합니다. 기준선보다 CER/WER와 우선용어 F1, STT→Resolver Top-3가
개선되고 false insertion과 잘못된 단일 CAS 승격이 증가하지 않을 때만 채택합니다. 현재
`wind_snr0` LoRA 후보는 이 후단 안전 Gate를 통과하지 못했으므로 기준선을 유지합니다.
