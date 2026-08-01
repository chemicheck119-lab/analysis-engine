# 공모전 공식근거 보증 기준

기준일: 2026-08-01
판정: **공모전 시연 가능 — 공식근거 검증 완료 범위와 한계를 화면에 함께 표시**

## 1. 한 문장 결론

케미체크119는 사람 화학 전문가의 승인을 받았다고 주장하지 않습니다. 대신 **공식기관
자료의 교차확인, 확인 전 충돌판정 차단, 전체 규칙 회귀검사, 출처 링크 정기검사**를 결합해
공모전 시연 결과를 검증합니다.

이 기준의 이름은 **공식기관 다중근거 자동검증**입니다. `독립검수 완료`, `전문가 승인`,
`현장 정확도 검증`과는 다른 개념입니다.

## 2. 무엇을 검증했는가

| 검증 층 | 실제 검사 | 현재 확인 결과 | 증명하지 않는 것 |
|---|---|---|---|
| 물질 확인 gate | 사고·시설 CAS 두 개가 확인되기 전 Rule 실행 금지 | 50개 후보 preflight에서 미확인 실행 0건, 미확인 위험 노출 0건 | 후보 물질이 현장에 실제 존재함 |
| CAMEO 규칙 회귀 | 공개 검증 CAS 6종의 15개 조합을 원자료와 재연결 | 높음 8·중간 2·낮음 5, 15쌍 계약 통과 | 전국 모든 물질 조합의 안전성 |
| 공식근거 교차확인 | 서로 다른 공식기관이 같은 위험 메커니즘을 지지하는지 검사 | 2쌍 `REFERENCE_TRIANGULATED`, 13쌍 `PRIMARY_AUTHORITY_ONLY` | 사람 전문가 승인·법적 인증 |
| 근거 무결성 | 허용 기관 host·CAS·예상 생성물·registry SHA-256 검사 | 불일치 시 `VERIFY_REQUIRED`로 위험등급 차단 | 실제 누출량·농도·혼합 여부 |
| 출처 생존 검사 | URL·최종 host·MIME·HTML 문서 위치 확인 | 공식 URL 8개 도달, HTML 7개 위치 확인, PDF 1개 메타데이터만 확인 | 원문 내용이 영구히 바뀌지 않음 |
| 배포 검증 | 불변 이미지 digest·readiness·통합 API smoke | 서울 Cloud Run preview 100% 전환 후 READY | 운영 승인·현장 SLO |

50개 E2E 후보는 정답 데이터가 아니므로 정확도를 계산하지 않습니다. 위의 0건 수치는
**안전 상태 전이와 JSON 계약 위반이 관찰되지 않았다**는 뜻이지 정확도 100%가 아닙니다.

## 3. 대표 시연 조합의 공식자료

### 차아염소산나트륨 + 염산

- [NOAA/EPA CAMEO Sodium Hypochlorite](https://cameochemicals.noaa.gov/chemical/4503):
  물질 식별과 산 접촉 시 염소가스 위험
- [CDC MMWR 혼합 사고 보고](https://www.cdc.gov/mmwr/preview/mmwrhtml/00015111.htm):
  차아염소산나트륨과 산 혼합 사고 및 염소가스 발생
- [CDC Chemical Disinfectants](https://www.cdc.gov/infection-control/hcp/disinfection-sterilization/chemical-disinfectants.html):
  hypochlorite와 산 혼합 시 독성 염소가스 위험
- [ILO/WHO ICSC 1119](https://inchem.org/documents/icsc/icsc/eics1119.htm):
  산 접촉 시 독성·부식성 가스와 염소 발생
- [UKHSA Sodium hypochlorite](https://www.gov.uk/government/publications/sodium-hypochlorite-properties-incident-management-and-toxicology/sodium-hypochlorite-general-information):
  산성 제품 혼합 시 염소가스와 건강 영향

두 CDC 문서는 같은 기관 그룹 하나로 계산합니다. 따라서 이 조합은 문서 5개, 독립 공식기관
그룹 4개가 연결된 `REFERENCE_TRIANGULATED`입니다.

### 금속 나트륨 + 염산

- [NOAA/EPA CAMEO Sodium](https://cameochemicals.noaa.gov/chemical/7794)
- [ILO/WHO ICSC 0717](https://inchem.org/documents/icsc/icsc/eics0717.htm)
- [OSHA Chemical Hazards and SDSs](https://www.osha.gov/sites/default/files/2021-03/Chemical%20Hazards.pdf)

이 조합은 3개 독립 공식기관이 산 접촉 시 수소·화재·폭발 위험을 지지합니다. 현장 물질
존재, 실제 접촉, 발생량과 점화 여부는 문헌만으로 증명하지 않습니다.

## 4. 공모전과 실제 운영의 경계

| 사용 단계 | 현재 판정 | 허용 범위 |
|---|---|---|
| 공모전 발표·시연 | `READY_WITH_DISCLOSED_LIMITATIONS` | 대표 교차확인 조합과 CAMEO 파일럿 범위를 출처·한계와 함께 시연 |
| FE·BE 통합 시험 | `PARTIALLY_READY` | preview API 계약 시험 가능, 실제 소비자 계약 검증은 별도 완료 필요 |
| 제한된 현장 파일럿 | `BLOCKED` | 독립 locked 평가, 실제 사용자 검증, 장애·부하 시험 전 사용 금지 |
| 실제 현장 운영·상용 | `BLOCKED` | 기관 승인, 책임체계, 운영 데이터와 모니터링 확보 전 사용 금지 |

API는 모든 결과에 `expert_reviewed=false`, `decision_support_only=true`와 최종 현장 지휘관
판단 문구를 유지합니다. 이 값은 발표 편의를 위해 바꾸지 않습니다.

## 5. 발표에서 사용할 문장

> 케미체크119는 LLM이 화학 반응을 추측하지 않습니다. 현장에서 확인한 두 CAS만 CAMEO
> 규칙에 넣고, 대표 위험 조합은 NOAA/EPA·CDC·ILO/WHO·UKHSA 등 독립 공식기관 자료가
> 같은 메커니즘을 지지하는지 자동 점검합니다. 근거가 부족하거나 변조되면 위험등급을
> 숨기며, 현재 결과는 전문가 승인이 아닌 공개 근거 기반 의사결정 보조입니다.

발표에서 사용하지 않을 표현:

- `전문가 검수 완료`, `화학 전문가 승인`
- `정확도 100%`, `현장 안전 보장`, `전국 모든 물질 지원`
- `PRTR·ICIS로 현재 재고를 확인했다`
- `위험도 80%`처럼 서수 등급을 확률로 바꾼 표현

## 6. 재현과 증빙 위치

```bash
# 공식근거 registry와 주장 일치 회귀
python -m pytest tests/test_evidence_assurance.py tests/test_reference_drift.py tests/test_pair_evaluation.py

# 공식 URL·host·MIME·문서 위치 재검사
python scripts/data/check_reference_sources.py \
  --config-dir config \
  --output outputs/reference-source-drift.json

# 공개 검증 15개 물질쌍 전체 재실행
python scripts/evaluation/evaluate_verified_pairs.py \
  --db artifacts/chemiguard119.sqlite \
  --config-dir config \
  --output outputs/verified-pair-evaluation.json
```

- 원본 registry: [`config/reference_assurance_registry.json`](../config/reference_assurance_registry.json)
- 상세 검증 방식: [공식근거 교차검증](EVIDENCE_ASSURANCE.md)
- 평가 한계: [모델 평가](EVALUATION.md)
- 배포 증빙: [공모전 preview](PREVIEW_DEPLOYMENT.md)
- 향후 사람 검수 절차: [E2E 독립 검수 가이드](E2E_REVIEW_GUIDE.md)

사람 독립검수를 다시 진행할 수 있게 되면 기존 50건 후보를 두 사람이 독립 라벨링해
`DOUBLE_REVIEWED_NON_EXPERT` locked set으로 승격합니다. 이 절차는 실제 파일럿 진입 조건으로
보존하지만, 현재 공모전 공식근거 보증을 거짓 정확도로 바꾸는 데 사용하지 않습니다.
