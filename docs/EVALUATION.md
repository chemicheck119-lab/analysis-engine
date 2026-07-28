# 케미체크119 모델 평가

## 공개 검증 CAMEO 물질쌍 회귀 평가

공개 검증 crosswalk가 늘어날 때마다 모든 고유 물질쌍을 실제 CAMEO 원자료 DB에 연결해
검사합니다. 이는 현장 성능 평가가 아니라 배포 전 데이터 연결 회귀 검사입니다.

```bash
python scripts/evaluation/evaluate_verified_pairs.py \
  --db artifacts/chemiguard119.sqlite \
  --config-dir config \
  --output data/evaluation/verified_pair_snapshot_2024.json
```

출력에는 DB와 crosswalk의 SHA-256, 예상·실행 조합 수, 상태와 서수 등급 분포가
포함됩니다. `offline_regression_only=true`, `does_not_confirm_on_site_presence=true`,
`is_probability=false`가 항상 함께 기록됩니다.

2026-07-28 스냅샷에서는 공개 검증 6종의 고유 조합 15개를 모두 실행했고 15개 모두
`SCREENING_COMPLETED`였습니다. 서수 등급 분포는 `HIGH=8`, `MEDIUM=2`, `LOW=5`입니다.
이는 15개 데이터 연결이 실행된다는 회귀 결과이며, 낮음 조합의 안전 보장이나 실제
사고확률·현장 정확도를 뜻하지 않습니다.

## 1. 쉽게 이해하기

케미체크119의 평가는 모델 전체에 점수 하나를 붙이지 않습니다. 다음 질문을 분리해서
측정합니다.

1. 입력한 물질명을 올바른 CAS 후보로 찾았는가?
2. 자동으로 선택한 CAS 힌트를 포함한 전체 검색 흐름이 근거를 찾았는가?
3. 올바른 CAS가 주어졌을 때 Retriever 자체가 근거를 찾았는가?
4. 두 물질이 확인된 뒤 Rule Engine이 같은 입력에 같은 판정을 내리는가?

이 구분이 없으면 물질 식별이 실패한 것인지, 문서 검색이 실패한 것인지 알 수 없습니다.

## 2. 현재 평가 데이터의 한계

`data/evaluation/`의 데이터는 작은 내부 회귀셋입니다.

| 파일 | 건수 | 목적 | 상태 |
|---|---:|---|---|
| `resolver_regression_queries.csv` | 21 | CAS·명칭·별칭·화학식 회귀 검사 | 내부 초안 |
| `retrieval_regression_queries.csv` | 10 | KOSHA·CAMEO 근거 검색 회귀 검사 | 내부 초안 |
| `incident_parser_seed.jsonl` | 6 | 신고문 구조화 데이터 형식 시드 | 학습·성능평가 불가 |

이 데이터는 다음 수치로 해석하면 안 됩니다.

- 실제 현장 정확도
- 전국 화학물질 전체 성능
- 사고 대응 성공률
- 화학사고 발생 또는 피해 확률

현장 성능을 주장하려면 실제 신고 표현을 비식별화한 별도 보류 테스트셋이 필요합니다.

## 3. 재현 명령

검증된 SQLite와 모델 artifact가 준비된 상태에서 실행합니다.

```bash
chemiguard119 evaluate \
  --db artifacts/chemiguard119.sqlite \
  --resolver-model artifacts/resolver.joblib \
  --retriever-model artifacts/retriever.joblib \
  --resolver-evaluation data/evaluation/resolver_regression_queries.csv \
  --retriever-evaluation data/evaluation/retrieval_regression_queries.csv \
  --report-dir outputs/modeling \
  --json
```

생성 파일:

```text
outputs/modeling/resolver_evaluation.json
outputs/modeling/retriever_evaluation.json
```

## 4. Resolver 지표

| 지표 | 의미 |
|---|---|
| `top1_accuracy` | 단일 exact 후보로 안전하게 식별한 비율 |
| `candidate_top1_hit_rate` | 기대 CAS가 후보 1위에 있는 비율 |
| `top3_recall` | 기대 CAS가 상위 3개 후보에 포함된 비율 |
| `mrr` | 기대 CAS가 얼마나 앞 순위에 있는지 나타내는 평균 역순위 |
| `ambiguous_case_count` | 동일 표현이 여러 CAS 후보로 남은 건수 |

후보가 1위에 있더라도 여러 CAS가 같은 별칭을 사용하면 단일 물질로 확정한 것으로 계산하지
않습니다.

## 5. Retriever 지표

Retriever 평가는 두 경로를 함께 출력합니다.

### 5.1 `end_to_end`

Resolver가 신고 질의에서 자동으로 선택한 CAS 힌트를 포함한 실제 검색 흐름입니다.

```text
검색 질의
→ 자동 CAS 힌트 선택
→ BM25·TF-IDF·RRF 검색
→ 기대 근거 순위 측정
```

이 점수가 낮으면 CAS 힌트 선택과 Retriever 양쪽을 확인해야 합니다.

### 5.2 `retriever_with_oracle_cas`

평가 데이터의 정답 CAS를 검색 필터로 제공해 Retriever 자체만 확인하는 진단용 상한선입니다.

```text
검색 질의 + 평가용 정답 CAS
→ BM25·TF-IDF·RRF 검색
→ 기대 근거 순위 측정
```

`oracle`은 운영에서 정답을 미리 안다는 뜻이 아닙니다. 모델 오류의 위치를 분리하기 위한
평가 장치이며 운영 성능으로 인용하면 안 됩니다.

### 5.3 `cas_hint`

| 지표 | 의미 |
|---|---|
| `coverage` | 전체 질의 중 자동 CAS 힌트를 만든 비율 |
| `exact_match_rate` | 전체 질의 중 자동 힌트가 기대 CAS와 같은 비율 |
| `precision_when_present` | 힌트를 만든 질의 중 기대 CAS와 같은 비율 |
| `missing_count` | 모호성 때문에 힌트를 만들지 않은 건수 |
| `mismatch_count` | 기대 CAS와 다른 힌트를 만든 건수 |

잘못된 CAS 힌트보다 힌트를 보류하는 것이 안전하므로 `missing`과 `mismatch`를 구분합니다.

## 6. 2026-07-27 내부 기준선

현재 로컬 artifact와 내부 회귀셋을 사용해 다시 실행한 결과입니다.

| 구성요소 | 케이스 | 지표 | 결과 |
|---|---:|---|---:|
| Resolver | 21 | 단일후보 확정 정확도 | 0.9524 |
| Resolver | 21 | Top-3 Recall | 1.0000 |
| Retriever 전체 흐름 | 10 | Recall@5 | 0.9000 |
| Retriever 전체 흐름 | 10 | MRR@8 | 0.8500 |
| Retriever 단독·정답 CAS 제공 | 10 | Recall@5 | 1.0000 |
| Retriever 단독·정답 CAS 제공 | 10 | MRR@8 | 0.9000 |
| 자동 CAS 힌트 | 10 | Coverage | 0.8000 |
| 자동 CAS 힌트 | 10 | Precision when present | 1.0000 |

현재 회귀셋에서는 잘못된 CAS 힌트는 없었고 두 건에서 힌트를 보류했습니다. 전체 검색 실패
한 건은 `차아염소산나트륨`과 `염소가스`가 함께 포함된 복합 질의였습니다. 정답 CAS를
제공하면 Retriever는 기대 CAMEO 근거를 2위로 찾았습니다.

따라서 다음 개선 대상은 Retriever 교체가 아니라 신고문에서 물질별 역할을 분리한 뒤 각각의
CAS로 근거를 검색하는 전체 흐름입니다.

## 7. 다음 평가 데이터

다음 순서로 별도 보류 테스트셋을 확장합니다.

1. 표준명·CAS·화학식
2. 띄어쓰기·대소문자 변형
3. 실제 발생 가능한 오타와 음성인식 오류
4. 한 신고문에 여러 물질이 있는 경우
5. 사고물질과 시설물질 역할 구분
6. 부정 표현
7. 미등록 제품명과 미확인 물질
8. 시설명이 없거나 시설 이력이 없는 경우

각 행에는 출처, 라벨 작성자, 검토 상태, 중복 그룹과 데이터 분할을 기록해야 합니다.

## 8. 관련 문서

- [데이터와 모델](DATA_AND_MODEL.md)
- [아키텍처](ARCHITECTURE.md)
- [API](API.md)
- [안전 및 한계](SAFETY_AND_LIMITATIONS.md)

## 9. 온라인 경로 상대 성능 측정

검색 정확도 평가와 API 지연시간 측정은 별개입니다. 아래 명령은 동일 장비와 동일
artifact에서 런타임 인덱스 변경 전후를 비교하기 위한 개발용 벤치마크입니다.

```bash
PYTHONPATH=src python scripts/evaluation/benchmark_runtime.py \
  --label local \
  --output outputs/runtime_benchmark.json
```

2026-07-28 Mac ARM64 로컬 비교에서 별칭 9,685개, 근거 문서 5,858개를 사용했습니다.
요청마다 전체 행을 정규화하던 기준선과 서버 시작 시 조회표를 한 번 구성하는 구현의
중앙값은 다음과 같습니다.

| 경로 | 변경 전 p50 | 변경 후 p50 | 감소율 |
|---|---:|---:|---:|
| Resolver 정확 별칭 | 16.648ms | 0.004ms | 99.98% |
| Resolver 유사 후보 | 18.891ms | 2.020ms | 89.31% |
| Retriever 동일 CAS | 34.089ms | 20.176ms | 40.81% |
| Retriever 일반 텍스트 | 33.582ms | 19.881ms | 40.80% |

상세 입력, artifact SHA-256과 반복 횟수는
`data/evaluation/runtime_performance_snapshot_2026-07-28.json`에 기록했습니다.
이 수치는 해당 개발 장비의 상대 비교일 뿐 운영 서버 SLO나 현장 성능 보장이 아닙니다.
배포 후보 이미지에서도 같은 명령을 실행해 별도의 값을 남겨야 합니다.
