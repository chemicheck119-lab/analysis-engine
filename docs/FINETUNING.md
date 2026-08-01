# 파인튜닝 결정과 결과

## 한눈에 보기

이번에 실제 학습한 모델은 화학 위험을 말하는 LLM이 아니라 물질명 후보를 찾는 Resolver입니다.
과거 소방 사고에서 CAS와 함께 기록된 현장 표현을 기존 문자 TF-IDF 모델에 추가해, 반복되는
현장 명칭을 더 잘 찾도록 만들었습니다.

```text
소방안전 빅데이터 플랫폼 사고 기록
→ 유효한 단일 CAS만 통과
→ 서로 다른 CAS가 공유한 모호 표현 제거
→ 2015~2018 학습 / 2019 검증
→ 2015~2019 최종 학습 / 2020 잠금 평가
→ 기존 안전 회귀 통과
→ artifact + SHA-256
```

## 데이터

- 자료명: 울산소방 화학사고별 유해물질판단 2015~2020
- 기관: 소방청·소방안전 빅데이터 플랫폼
- URL: <https://bigdata-119.kr/goods/goodsInfo?goods_mng_sn=5>
- 원천 행: 1,868
- 유효 비모호 물질 표현: 1,530
- 제외: checksum 오류 또는 복합 CAS 212행, 다중 CAS 공유 표현 2개
- 사용 필드: 발생연도, CAS, 한글·영문 물질명, 한글·영문 일반명
- 사용하지 않은 필드: 상세 주소, 관할서, 센터, 사고 대응·위험 판정
- 원천 SHA-256: `f013ebed301c5306178ad72f8dbbb62bdb5ecae1122ae3dfe7d1050f7ea0d765`

공모전 데이터 활용과 모델 학습은 가능하지만 파생 별칭 artifact를 공개 컨테이너에 재배포하는
조건은 별도로 확인해야 하므로 registry 상태는 `REVIEW_REQUIRED`입니다.

## 결과

| 2020 잠금셋 419건 | 기준선 | 파인튜닝 |
|---|---:|---:|
| Top-1 | 32.46% | 67.06% |
| Top-3 Recall | 34.61% | 67.54% |
| MRR | 33.33% | 67.22% |
| 잘못된 단일 CAS 확정 | 0건 | 0건 |

과거에 없던 표현 60건의 Top-1은 양쪽 모두 28.33%, 과거에 없던 CAS 58건의 Top-3는 양쪽
모두 31.03%였습니다. 즉 새 모델은 과거 소방 기록의 표현을 기억하는 데 효과가 있지만 처음
보는 물질을 일반화하는 능력은 늘지 않았습니다.

## 왜 LLM QLoRA는 실행하지 않았나

현재 실제 원천은 구조화된 사고표이며 실제 119 신고 음성 전사, 부정 범위, 사고물질과 시설물질
역할, 누락 필드를 사람이 확인한 라벨이 아닙니다. 표에서 템플릿 문장을 대량 생성해 QLoRA를
실행할 수는 있지만, 모델은 화학사고 언어가 아니라 템플릿을 외우게 됩니다. 따라서 그 수치를
현장 성능처럼 제시하는 것보다 즉시 배포 가능한 Resolver source adaptation을 선택했습니다.

생성형 파서 파인튜닝 재검토 조건은 다음과 같습니다.

- 비식별 실제 신고 전사 train 500건 이상
- validation·locked test 각각 100건 이상
- 물질·시설 역할, 부정, 불확실성, 복합물질의 이중 검수 라벨
- 규칙 기반 파서보다 field F1과 JSON schema 준수율 개선
- LLM 장애 시 결정적 파서 fallback 유지

## 재현

```bash
chemiguard119 finetune-resolver \
  --base-model artifacts/resolver.joblib \
  --incidents data/raw/07_울산소방_화학사고별_유해물질판단.csv \
  --output-dir artifacts/incident_adaptation \
  --report outputs/modeling/incident_adapted_resolver_evaluation.json \
  --json
```

배포 후보 artifact:

```text
artifacts/incident_adaptation/resolver_incident_adapted_through_2019.joblib
```

학습 결과 점수는 물질 정답 확률이나 위험 확률이 아니며, 모든 후보는 현장 확인 전 충돌 Rule
Engine에 전달되지 않습니다.
