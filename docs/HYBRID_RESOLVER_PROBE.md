# Hybrid Resolver 사전 진단

## 목적과 사실 상태

현재 운영 후보 생성기는 exact alias와 문자 n-gram TF-IDF를 사용한다. 2020년 울산 잠금
419건에서 Top-1 0.8974를 기록했지만, 학습 구간에 없던 표현 60건의 Top-1은 0.2833으로
개선되지 않았다. 다음 정식 목표는 Sparse, Dense, Sparse+Dense, Reranker, Selective
Abstention을 동일한 전체 시간 분할 데이터에서 비교하는 것이다.

다만 공식 원본 CSV와 intake manifest가 현재 로컬에 없으므로 419건·미관측 60건을 다시
만들 수 없다. 이 상태에서 새 모델을 채택하거나 잠금 점수를 갱신하면 재현 불가능한 주장이
된다. 따라서 먼저 저장소에 이미 공개된 다음 표본에서 BGE-M3가 후보 회복 신호를 보이는지만
확인한다.

| 입력 | 건수 | 용도 | 해석 제한 |
|---|---:|---|---|
| 2019 개발 결과의 실패 예시 | 최대 20 | 계속 실험할 가치 판단 | 실패 사례만 모은 선택 편향 표본 |
| 2020 잠금 결과에 이미 노출된 실패 예시 | 최대 20 | 방향성 확인 | 새 잠금 테스트가 아님 |
| Resolver 내부 회귀 | 21 | 기존 exact·별칭 회귀 확인 | DRAFT 내부 자료 |

사실 상태는 **부분 구현 또는 개발용 데모**다. 이 결과는 전체 정확도, 전국 일반화, 현장
정확도 또는 CAS 자동 확정을 증명하지 않는다.

## 사전 등록 Gate

- 가설: 다국어 Dense encoder가 철자 변형·한영 표기의 일부를 Sparse보다 Top-3 안에 더
  자주 복구한다.
- 기준선: 기존 exact alias + 문자 2~5-gram TF-IDF.
- 후보: 로컬 캐시의 `BAAI/bge-m3`, CLS pooling, L2 정규화.
- 결합: exact identifier·exact alias·ambiguous alias는 기존 결과를 그대로 보존한다.
  fuzzy·unresolved 입력에만 Sparse와 Dense 상위 100개의 RRF를 적용한다.
- 안전: Dense 후보는 항상 `rule_eligible=false`이며 CAS 확정·Rule 입력이 아니다.
- 정식 ablation 진행 조건: 2019·2020 노출 실패 예시 모두에서 Hybrid Top-3가 Sparse보다
  높고, 21건 내부 회귀 Top-3가 낮아지지 않으며 기존 CAS hint 안전 Gate가 통과한다.
- runtime 채택 조건: 이번 probe만으로는 충족 불가능하다. 공식 원본으로 419건과 미관측
  60건 전체를 재현하고 Reranker·Selective Abstention까지 비교해야 한다.
- 중단 조건: Dense가 두 실패 cohort 모두에서 후보 회복을 보이지 않거나 내부 회귀를
  악화시키면 BGE-M3 zero-shot 경로를 기각한다.
- 예상 추가 서버 비용: 0원. 이미 로컬에 있는 가중치와 Apple MPS만 사용한다.

SapBERT는 동의어를 같은 표현 공간에 정렬하는 Entity Linking 방향을 제시하지만 UMLS 중심
영문 의생명 모델이므로 한국어 화학물질에 바로 적용된다고 가정하지 않는다. BGE-M3 역시
다국어 범용 검색 모델이지 한국어 화학물질 Resolver 정답 모델이 아니다. 이번 probe는 두
논문의 일반 원리를 제품 주장으로 바꾸기 전에 로컬 데이터에서 반증 가능하게 시험하는 단계다.

- [SapBERT 논문](https://aclanthology.org/2021.naacl-main.334/)
- [BGE-M3 논문](https://arxiv.org/abs/2402.03216)

## 재현 명령

`torch`와 `transformers`는 운영 dependency가 아니라 실험 환경에만 둔다. 모델 경로는 완전
다운로드된 로컬 snapshot을 지정한다.

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
PYTHONPATH=src <experiment-python> scripts/experiments/probe_hybrid_resolver.py \
  --resolver-model <private-runtime>/artifacts/resolver.joblib \
  --temporal-snapshot data/evaluation/incident_adapted_resolver_temporal_2026-08-02.json \
  --regression data/evaluation/resolver_regression_queries.csv \
  --safety-evaluation data/evaluation/resolver_hint_safety_queries.csv \
  --embedding-model <local-huggingface-snapshot>/BAAI--bge-m3 \
  --report <private-output>/hybrid-resolver-probe/report.json \
  --device mps \
  --batch-size 64 \
  --max-length 32
```

보고서는 입력 artifact·평가 파일 SHA-256, cohort별 Top-1·Top-3·MRR, 행별 순위, 기존 안전
Gate, claim scope를 함께 기록한다. 원본 데이터나 모델 가중치는 Git에 넣지 않는다.

## 정식 실험에 필요한 원본

- 공식 상품: `울산 화학사고별 유해물질 판단 정보`
- 파일: `유해물질판단_2020_2015.csv`, 535.98KB
- 범위: 2015~2020년 울산 공개 사고 기록
- 추가 산출물: 최소 6개 열 파생 CSV와 원본·파생 SHA-256을 고정한 intake manifest

원본을 확보하면 2015~2018 개발, 2019 검증, 2020 잠금 분리를 다시 검증하고 A~E ablation을
처음부터 실행한다. 2020은 임계값·RRF 가중치·기권 기준 조정에 사용하지 않는다.

## 2026-09-08 실행 결과

로컬에 이미 있던 BGE-M3 snapshot과 현재 v4 Resolver artifact를 사용했다. Dense corpus는
source-only CAS와 숫자 CAS 별칭을 제외한 5,626개 별칭이며, 서버·GPU 임대비는 들지 않았다.

| cohort | 시스템 | Top-1 | Top-3 | MRR |
|---|---|---:|---:|---:|
| 2019 과거 실패 예시 20 | Sparse | 1.0000 | 1.0000 | 1.0000 |
| 2019 과거 실패 예시 20 | Dense | 1.0000 | 1.0000 | 1.0000 |
| 2019 과거 실패 예시 20 | Sparse+Dense RRF | 1.0000 | 1.0000 | 1.0000 |
| 2020 노출 실패 예시 20 | Sparse | 0.0000 | 0.0500 | 0.0167 |
| 2020 노출 실패 예시 20 | Dense | 0.0500 | 0.1500 | 0.1000 |
| 2020 노출 실패 예시 20 | Sparse+Dense RRF | 0.0000 | 0.0500 | 0.0167 |
| 내부 회귀 21 | Sparse | 1.0000 | 1.0000 | 1.0000 |
| 내부 회귀 21 | Dense | 0.9048 | 0.9524 | 0.9286 |
| 내부 회귀 21 | Sparse+Dense RRF | 1.0000 | 1.0000 | 1.0000 |

2019 행이 모두 맞은 것은 현재 artifact가 2019까지 학습한 최종 v4이기 때문이다. 따라서 이
cohort는 개선 평가가 아니라 현재 artifact와 과거 검증 artifact가 같지 않다는 재현성 문제를
드러낸다. 2020 표본은 실패 예시만 골라 놓았으므로 0.05나 0.15를 전체 정확도로 해석할 수
없다.

기존 CAS hint 안전 Gate는 통과했고, unsafe hint·wrong hint·Rule 입력 승격은 모두 0건이다.
하지만 Dense의 일부 후보 회복 신호가 균등 RRF의 Top-3 개선으로 이어지지 않았다.

**결정: `REJECT_ZERO_SHOT_BGE_M3_EQUAL_RRF_KEEP_SPARSE_RUNTIME`.** 현재 운영 Sparse를 유지한다.
BGE-M3 자체를 영구 기각한 것은 아니다. 공식 원본과 2018 validation artifact를 확보한 뒤에만
개발 구간에서 fusion·reranker·기권 기준을 정하고, 2020 전체 419건과 미관측 60건에서 한 번
검증할 수 있다. 이번 결과의 정식 ablation 진행 Gate는 `false`다.
