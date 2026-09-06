# 케미체크119 모델 평가

> 이 문서의 21건·10건 결과는 내부 회귀 기준선입니다. 세 파일이 생긴 이력, 현재 지표가
> 측정하지 못하는 항목과 독립 보류 평가 설계는
> [평가 V2](EVALUATION_V2.md)를 먼저 확인하세요. `incident_parser_seed.jsonl` 6건은
> 평가셋이 아니라 `DRAFT` 형식 시드이며 파서 성능 수치가 없습니다. 파서는 별도로
> 화학물질안전원 전국 사고 1,026건을 584건 개발·442건 미래 보류 구간으로 나눈 공식
> 외부 감사를 수행합니다.

## 사고 분석 E2E 안전 회귀

모듈별 21건·10건·6건만으로는 신고 입력부터 현장 확인 gate, 근거 검색과 충돌 규칙까지
연결된 상태 전이를 검증할 수 없습니다. `evaluate-e2e`는 실제 `analyze_incident` 경로를
실행해 다음을 함께 검사합니다.

- 두 CAS 확인 전 Rule Engine 미실행
- 한쪽만 확인된 상태의 안전한 보류
- 확인된 CAS와 근거 검색 CAS의 역할별 일치
- 모호한 일반명과 포함 문자열의 기권
- 잘못된 CAS checksum 거부
- 공개 규칙 미지원 조합의 위험등급 기권

```bash
chemiguard119 evaluate-e2e \
  --evaluation data/evaluation/e2e_scenarios_draft.jsonl \
  --evaluation-profile INTERNAL_REGRESSION \
  --report outputs/modeling/e2e_scenario_evaluation.json \
  --json
```

2026-07-31 DRAFT 8건은 8/8 통과했고 미확인 Rule 실행과 미확인 위험 노출은 각각
0건이었습니다. 평균 86.610ms, p95 112.537ms는 저장소 밖의 같은 로컬 artifact를 사용한
단일 실행입니다. 이 결과는 안전 상태 전이 회귀일 뿐 현장 정확도나 운영 SLO가 아닙니다.
릴리스 정책은 별도 `PILOT_REVIEWED` E2E 200건 이상을 요구합니다.

원본 측정값은
[`e2e_scenario_regression_2026-07-31.json`](../data/evaluation/e2e_scenario_regression_2026-07-31.json)에
있습니다.

### 향후 사람 검수용 E2E 50건 후보

8건 DRAFT에서 바로 “정확도”를 주장하지 않도록, 공개 검증 CAMEO 15쌍의 확인 상태 전이
45건과 안전 hard case 5건을 `e2e-review generate`로 만들었습니다. 이 50건은 정답이 없는
`COMPETITION_REVIEW_CANDIDATE_ONLY` 후보입니다.

2026-07-31 preflight에서는 출력 계약 실패 0건, 확인 CAS 두 개 전 충돌 실행 0건, 확인 전
위험 노출 0건이었습니다. 평균 99.061ms, p95 118.312ms는 단일 로컬 실행 관찰값입니다.
`is_accuracy_evaluation=false`, `field_validated=false`이므로 정확도나 현장 성능으로 사용할
수 없습니다.

서로 다른 두 사람이 빈 시트를 독립 작성하고 모든 기대 필드가 일치해야만
`DOUBLE_REVIEWED_NON_EXPERT` locked test가 생성됩니다. 자세한 명령과 라벨 규칙은
[E2E 독립 검수 가이드](E2E_REVIEW_GUIDE.md)를 따릅니다.

현재는 독립 검수 인력을 확보하지 못했으므로 이 후보를 공모전 정확도 근거로 사용하지
않습니다. 공모전 시연은 정확도 대체 주장이 아니라 [공모전 공식근거 보증
기준](COMPETITION_ASSURANCE.md)에 따라 공식기관 근거 교차확인, 안전 gate 회귀, 출처 drift
검사와 배포 smoke를 분리해 제시합니다. 사람 검수 gate는 향후 현장 파일럿 조건으로
그대로 유지합니다.

## Agent trajectory 안전 회귀

`evaluate-agent-trajectories`는 LLM의 자유 추론 품질을 채점하지 않습니다. 결정적
`IncidentAgentRunner`에 사전 정의한 합성 `AnalysisResponse` 관찰을 주입하고 다음 정책이
정확한 순서로 실행되는지 검사합니다.

- 새 요청에서 분석→안전 계약 재검증→필요한 사람 확인 요청 순서를 지키는가
- 같은 요청·같은 runtime·새 관찰 없음 상태에서는 도구를 다시 호출하지 않는가
- runtime fingerprint가 바뀌면 이전 memory만 믿지 않고 다시 분석하는가
- request·incident·memory 식별자와 fingerprint가 호출 사이에 섞이지 않는가
- 두 CAS가 각각 확인된 뒤에만 결과 제시 도구를 선택하는가
- CAMEO 미지원 상태에서는 공식 근거 검토를 요청하는가
- timeout은 재시도 가능한 실패로, 안전 계약 위반은 재시도 불가 실패로 중단하는가
- 실패 뒤 다른 도구를 계획하거나 내부 오류·분석 결과를 노출하지 않는가

```bash
chemiguard119 evaluate-agent-trajectories \
  --evaluation data/evaluation/agent_trajectory_scenarios_draft.jsonl \
  --evaluation-profile INTERNAL_REGRESSION \
  --report outputs/modeling/agent_trajectory_evaluation.json \
  --json
```

2026-09-06 DRAFT 10개 시나리오·13개 step은 10/10 통과했습니다. memory checksum 실패,
실패 후 도구 계획, 두 CAS 확인 전 결과 제시, 실패 분석 노출, 내부 timeout 상세 노출은
각각 0건입니다. 판정은 `ADOPT_FOR_INTERNAL_REGRESSION_ONLY`입니다.

사실 상태는 **부분 구현 또는 개발용 데모**입니다. 합성 관찰에 대한 정책 회귀이므로 실제
LLM Agent의 자율 추론 성능, 실제 네트워크 SLO, 화학적 위험 정답, 현장 정확도 또는 운영
안전성을 증명하지 않습니다. `COMPETITION_REVIEWED`와 `PILOT_REVIEWED` profile은 독립
검수 행이 생기기 전까지 의도적으로 차단됩니다.

## 평가 profile과 section 평가

```bash
chemiguard119 evaluate --evaluation-profile INTERNAL_REGRESSION --json
chemiguard119 evaluate --evaluation-profile COMPETITION_REVIEWED --json
chemiguard119 evaluate --evaluation-profile PILOT_REVIEWED --json
```

- `INTERNAL_REGRESSION`: DRAFT fixture를 허용하지만
  `claim_scope=INTERNAL_REGRESSION_ONLY`
- `COMPETITION_REVIEWED`: 서로 다른 라벨러·검수자와 locked test provenance 요구
- `PILOT_REVIEWED`: staging·production manifest가 요구하는 최소 평가 claim

현재 평가행은 모두 DRAFT이므로 뒤의 두 명령은 의도적으로 차단됩니다.

`retrieval_section_regression.jsonl`은 같은 CAS만 확인하던 기존 10건 평가와 달리
`evidence_id`별 0~3 relevance를 사용합니다. 평가기 v3는 핵심 근거와 보조 근거를
같은 무게로 보지 않습니다.

```text
내부 12건: nDCG@5 0.9284 / Recall@5 0.8750 / Precision@5 0.2333 /
MRR@5 0.9444 / unjudged rate 0.7667
핵심 근거(grade 2~3) Recall@5: 1.0000
보조 근거(grade 1) Recall@5: 0.0000
중요도 가중 Recall@5: 0.9688
필수 사실 coverage@5: 0.8750
핵심 사실 coverage@5: 1.0000
핵심 근거 12/12 성공의 Wilson 95% 구간: 0.7575~1.0000
답변 불가 사례: 0건(기권 성능 평가 불가)
claim_scope: INTERNAL_REGRESSION_ONLY
```

쉽게 말하면 현재 `Recall@5=0.875`의 누락은 핵심 답 3건이 아니라, 세 질문에서 참고용으로
라벨한 grade 1 보조 절 3건입니다. 12개 질문의 grade 2~3 핵심 근거 14개는 모두 Top-5에
있었습니다. 그렇다고 “핵심 근거 정확도 100%”라고 발표하면 안 됩니다. 질문이 12개뿐이라
12/12 성공이어도 Wilson 95% 구간의 하한은 약 75.8%입니다.

| 지표 | 무엇을 세는가 | 현재 내부 값 |
|---|---|---:|
| `recall_at_k` | grade 1~3 문서를 모두 같은 1건으로 계산 | 0.8750 |
| `high_relevance_recall_at_k` | grade 2~3 핵심 근거만 계산 | 1.0000 |
| `supporting_recall_at_k` | grade 1 보조 근거만 계산 | 0.0000 |
| `graded_gain_recall_at_k` | `2^grade-1`로 중요도를 반영 | 0.9688 |
| `required_fact_coverage_at_k` | 관련 문서에 라벨한 필수 사실을 계산 | 0.8750 |
| `high_relevance_fact_coverage_at_k` | 핵심 답변에 필요한 사실만 계산 | 1.0000 |

검수 완료 파일럿 릴리스 정책은 400건을 한 덩어리로 세지 않습니다. 답변 가능한 질의
300건 이상과 답변 불가 질의 100건 이상을 각각 요구하고, 전체 Recall 0.90·핵심 Recall
0.98·가중 Recall 0.95·핵심 사실 coverage 0.98·답변 불가 기권율 0.95를 함께 검사합니다.
핵심 사실 완전 회수율의 Wilson 95% 하한도 0.95 이상이어야 합니다. 이는 보편적인 업계
인증값이 아니라 케미체크119가 정한 보수적 파일럿 진입 정책입니다.

쉽게 말해 “쉬운 질문 1개를 맞히고, 모르는 질문 399개에는 답하지 않는 방식”으로 400건
조건을 통과할 수 없습니다.

표준 nDCG·Precision은 unjudged 문서를 비관련으로 계산하지만, qrel pool 자체가 불완전해
반환 문서 76.7%는 실제 관련 여부를 아직 판정하지 못했습니다. 따라서 이 수치는 section
제목 기반 DRAFT 회귀일 뿐 현장 검색 정확도가 아닙니다. 비교와 artifact hash는
`data/evaluation/retrieval_section_comparison_2026-07-28.json`에, 지표 분해와 누락
evidence 감사 결과는
`data/evaluation/retrieval_section_metric_audit_2026-07-31.json`에 고정했습니다.
평가기 v2 보고서는 v3 릴리스 정책을 통과하지 못하므로 artifact와 보고서를 v3로 다시
생성해야 합니다.

## 공개 검증 CAMEO 물질쌍 회귀 평가

공개 검증 crosswalk가 늘어날 때마다 모든 고유 물질쌍을 실제 CAMEO 원자료 DB에 연결해
검사합니다. 이는 현장 성능 평가가 아니라 배포 전 데이터 연결 회귀 검사입니다.

```bash
python scripts/evaluation/evaluate_verified_pairs.py \
  --db artifacts/chemiguard119.sqlite \
  --config-dir config \
  --output data/evaluation/verified_pair_snapshot_2024.json
```

출력에는 DB와 crosswalk의 SHA-256, 예상·실행 조합 수, 상태·서수 등급·공식근거 보증 분포가
포함됩니다. `offline_regression_only=true`, `does_not_confirm_on_site_presence=true`,
`is_probability=false`가 항상 함께 기록됩니다.

2026-08-01 스냅샷에서는 공개 검증 6종의 고유 조합 15개를 모두 실행했고 15개 모두
`SCREENING_COMPLETED`였습니다. 서수 등급 분포는 `HIGH=8`, `MEDIUM=2`, `LOW=5`입니다.
공식근거 보증은 `REFERENCE_TRIANGULATED=2`, `PRIMARY_AUTHORITY_ONLY=13`입니다.
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
| `resolver_hint_safety_queries.csv` | 12 | 자동 CAS 힌트 허용·보류·모호성 보존 | 내부 초안 |
| `retrieval_regression_queries.csv` | 10 | KOSHA·CAMEO 근거 검색 회귀 검사 | 내부 초안 |
| `retrieval_section_regression.jsonl` | 12 | 질문별 핵심·보조 MSDS 절 순위 검사 | 내부 초안 |
| `incident_parser_seed.jsonl` | 6 | 신고문 구조화 데이터 형식 시드 | 학습·성능평가 불가 |
| `agent_trajectory_scenarios_draft.jsonl` | 10개 시나리오·13 step | Agent 도구 순서·인자·재시도·중단·2-CAS Gate 회귀 | 합성 내부 초안 |
| `material_ranker_self_retrieval_2026-08-01.json` | 300 | 공개 물성 프로필 검색·재순위화 회귀 | 자기검색, 현장 정확도 아님 |
| `incident_adapted_resolver_temporal_2026-08-01.json` | 353/419 | 2019 검증·2020 잠금 시간 분할 | 반복 표현 적응 평가, 전국 정확도 아님 |
| `incident_adapted_resolver_temporal_2026-08-02.json` | 353/419 | source-only CAS exact 후보 확장 전후 비교 | v4 채택 평가, 전국 정확도 아님 |
| `official_national_incident_parser_locked_2026-08-01.json` | 442 | 다른 기관·미래 연도의 사고유형·물질 언급 추출 | 공식 외부 감사, CAS·현장 정확도 아님 |

세 파일은 표본설계로 정한 규모가 아니라 2026-07-23 커밋 `6246c49`에서 함께 추가된
정적 fixture입니다. 원본 표본추출 기록, 외부 검증자와 현장 대표성 근거가 없습니다.

이 데이터는 다음 수치로 해석하면 안 됩니다.

- 실제 현장 정확도
- 전국 화학물질 전체 성능
- 사고 대응 성공률
- 화학사고 발생 또는 피해 확률

현장 성능을 주장하려면 실제 신고 표현을 비식별화한 별도 보류 테스트셋이 필요합니다.

### 2.1 물질 Ranker 회귀 감사

749개 공개 물성 프로필 중 서로 다른 CAS 300개에서 최대 세 물성 필드로 질의를 만들어 같은
FTS5 인덱스를 다시 찾았습니다. 첫 재순위화 설계는 Top-1을 0.0067 낮춰 채택하지 않았고,
기존 BM25 순위를 강한 사전값으로 보존하도록 수정했습니다. 최종 결과는 Top-1 0.6933,
Top-3 0.8000, MRR 0.7452로 기준선과 같았으며 77.33%의 사례에서 후보를 구분할 다음 확인
항목을 생성했습니다. 평균 추가 처리시간은 약 0.69ms, p95 약 0.96ms였습니다.

이 평가는 같은 프로필에서 질의를 만든 자기검색 일관성 감사입니다. 실제 신고문·제품명·오인
관찰에 대한 독립 성능이 아니며, 지도학습·파인튜닝 근거로 사용하지 않습니다.

### 2.2 소방 사고 표현 source adaptation

소방안전 빅데이터 플랫폼의 2015~2020 유해물질판단 1,868행에서 checksum이 유효한 단일
CAS와 비모호 한글·영문 물질 표현 1,530건을 추출했습니다. 2015~2018년으로 검증 모델을
학습해 2019년 353건에서 확인한 뒤, 2015~2019년 최종 모델을 2020년 잠금 419건에 한 번
평가했습니다.

기존 v3은 기본 ICIS·KOSHA 카탈로그에 없는 CAS의 사고 표현 272건을 학습에서 제외했습니다.
v4는 2015~2019 소방 원천에 유효 CAS와 함께 반복 기록된 표현 중 기본 카탈로그 밖 35개
CAS·113개 표현을 `NFA_BIGDATA119_INCIDENT_CATALOG_CANDIDATE`로 추가했습니다. 이 후보는
정확한 과거 표현이 다시 입력될 때만 반환하고 문자 유사도 검색에서는 제외합니다.

| 2020 잠금 지표 | 기준선 v2 | 기존 적응 v3 | 확장 적응 v4 |
|---|---:|---:|---:|
| Top-1 | 0.3246 | 0.6706 | 0.8974 |
| Top-3 Recall | 0.3461 | 0.6754 | 0.9021 |
| MRR | 0.3333 | 0.6722 | 0.8990 |
| 평가 CAS의 artifact 포함률 | 0.7422 | 0.7422 | 0.9714 |
| 잘못된 단일 exact 확정 | 0 | 0 | 0 |
| 처음 보는 표현 Top-1, 60건 | 0.2833 | 0.2833 | 0.2833 |
| 처음 보는 CAS Top-3, 58건 | 0.3103 | 0.3103 | 0.3103 |

2019 검증 353건에서는 v4 Top-1 0.8102·Top-3 0.8187을 확인한 뒤 정책을 고정했고, 2020
잠금셋은 최종 확인에만 사용했습니다. 전체 향상은 과거 연도에 이미 등장한 물질과 표현을
다시 인식한 효과입니다. 처음 보는 물질 표현과 CAS 일반화는 개선되지 않았으므로 전국 현장
정확도나 미등록 물질 해결로 해석하지 않습니다. 상세 스냅샷은
`incident_adapted_resolver_temporal_2026-08-02.json`입니다.

학습 시 모호 표현 제거도 각 cutoff 이하 연도만 사용합니다. 2020년에 처음 다른 CAS와
공유된 표현을 2018 학습기가 미리 아는 시간 누수를 막았습니다. 전체 기간에서 여러 CAS가
공유한 표현은 단일 정답을 정의할 수 없어 평가 사례에서 제외했습니다.

### 2.3 전국 공식 사고 파서 외부 감사

기후에너지환경부 화학물질안전원의 전국 화학사고 파일 1,026건을 원 개발 원천과 다른 기관의
외부 데이터로 사용했습니다. 2014~2020년 584건은 개발 구간, 2021~2025년 442건은 잠금
구간입니다. 잠금 구간의 사고 원문과 실패 사례는 보고서에 노출하지 않았습니다.

개발 구간에서 변경 전·후를 같은 데이터로 비교했습니다.

| 2014~2020 개발 지표 | 변경 전 | 변경 후 |
|---|---:|---:|
| 공식 사고유형 Recall | 0.8702 | 0.8940 |
| 공식 사고유형 exact set | 0.8026 | 0.8172 |
| 추가 유형 반환율 | 0.0841 | 0.0914 |
| 관찰 가능 물질명 Recall | 0.7470 | 0.7727 |
| Resolver 도달 가능 라벨 Recall | 0.9225 | 0.9599 |
| 평균 / p95 | 72.25 / 78.68ms | 70.51 / 76.51ms |

사고유형과 물질 언급은 개선됐지만 추가 유형 반환율은 0.73%p 증가했습니다. 공식 원천은
한 사고에 단일 유형만 표시하고 본문에는 복합 사건이 함께 적힐 수 있어 이것만으로 오탐률을
확정할 수 없습니다. 따라서 단일 공식 유형과 정확히 같은 결과가 필요한 화면에서는
`exact_set_accuracy`도 함께 봐야 합니다.

| 2021~2025 잠금 지표 | 결과 | 의미 |
|---|---:|---|
| 공식 사고유형 Recall | 0.8376 | 누출·화재·폭발 431건 중 공식 단일 라벨 재현 |
| 공식 사고유형 exact set | 0.7587 | 추가 유형 없이 공식 라벨만 반환 |
| 관찰 가능 물질명 Recall | 0.8150 | 공식 물질명이 사고문에 직접 적힌 319개 라벨의 포함 추출 |
| 사례별 하나 이상 추출 | 0.8196 | 관찰 가능 물질명이 있는 316건 중 하나 이상 추출 |
| Resolver 도달 가능 라벨 Recall | 0.9658 | 현재 사전에 정확히 있는 라벨을 문장에서 다시 찾는 일관성 |
| 미확인 후보의 Rule 입력 승인 | 0건 | 이름 후보만으로 충돌 판정을 실행하지 않음 |
| 평균 / p95 | 71.65 / 77.32ms | 단일 로컬 CPU 실행 관찰값 |

이 원천에는 CAS 정답이 없습니다. 따라서 `Resolver 도달 가능`은 같은 사전 안에서의 일관성일
뿐 CAS 정확도가 아닙니다. 또한 사고 정리 문장이지 실제 119 신고 음성 전사가 아니므로 현장
파서 성능이라고 부르지 않습니다. 개발 구간에서는 직접적인 방출 표현과
`물질명+가스/용기/탱크` 경계만 보수적으로 추가했고, `폐산·폐액`처럼 CAS가 없는 명칭은 억지로 확정하지
않았습니다.

원천: [기후에너지환경부 화학물질안전원 화학 사고 정보](https://www.data.go.kr/data/15069200/fileData.do)

```bash
PYTHONPATH=src python scripts/data/download_csi_official_incidents.py

chemiguard119 evaluate-official-incidents \
  --source data/raw/09_CSI_전국_화학사고정보_20250430.csv \
  --resolver-model artifacts/incident_adaptation/resolver_incident_adapted_through_2019.joblib \
  --split locked_test \
  --report outputs/modeling/official_national_incident_parser_locked.json \
  --json
```

## 3. 재현 명령

검증된 SQLite와 모델 artifact가 준비된 상태에서 실행합니다.

```bash
chemiguard119 evaluate \
  --db artifacts/chemiguard119.sqlite \
  --resolver-model artifacts/resolver.joblib \
  --retriever-model artifacts/retriever.joblib \
  --resolver-evaluation data/evaluation/resolver_regression_queries.csv \
  --resolver-safety-evaluation data/evaluation/resolver_hint_safety_queries.csv \
  --retriever-evaluation data/evaluation/retrieval_regression_queries.csv \
  --retriever-section-evaluation data/evaluation/retrieval_section_regression.jsonl \
  --report-dir outputs/modeling \
  --json
```

생성 파일:

```text
outputs/modeling/resolver_evaluation.json
outputs/modeling/resolver_hint_safety_evaluation.json
outputs/modeling/retriever_evaluation.json
outputs/modeling/retriever_section_evaluation.json
```

물질 Ranker 회귀 감사:

```bash
python scripts/evaluation/evaluate_material_ranker.py \
  --db artifacts/chemiguard119.sqlite \
  --max-cases 300 \
  --output data/evaluation/material_ranker_self_retrieval_2026-08-01.json
```

소방 사고 표현 Resolver 파인튜닝:

```bash
chemiguard119 finetune-resolver \
  --base-model artifacts/resolver.joblib \
  --incidents /private/derived/ulsan-resolver-source.csv \
  --source-manifest /private/derived/ulsan-resolver-source.manifest.json \
  --output-dir artifacts/incident_adaptation \
  --report outputs/modeling/incident_adapted_resolver_evaluation.json \
  --json
```

공식 원본의 위치·대응 관련 열은 학습 입력에 직접 사용하지 않습니다. 먼저 최소
6개 열 파생본과 intake sidecar를 만들고, 학습 시 두 파일의 SHA-256 및 행 수가
일치하는지 검증합니다.

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

### 4.1 자동 CAS 힌트 안전 회귀

`resolver_hint_safety_queries.csv`는 일반 후보 정확도와 별도로 다음 세 동작을 검사합니다.

| 기대 동작 | 의미 |
|---|---|
| `ALLOW_EXACT_HINT` | 독립된 공식명은 기대 CAS로 근거 검색을 좁힐 수 있음 |
| `WITHHOLD_AUTO_HINT` | 부분 문자열·다중 물질에서는 단일 CAS 힌트를 보류 |
| `PRESERVE_AMBIGUITY` | 같은 표현이 여러 CAS면 모호 상태와 전체 후보를 유지 |

보고서의 배포 차단 지표는 다음과 같습니다.

| 지표 | 통과 기준 |
|---|---:|
| `unsafe_auto_hint_count` | 0 |
| `wrong_cas_auto_hint_count` | 0 |
| `resolver_rule_eligibility_violation_count` | 0 |
| `ambiguous_preservation_rate` | 1.0 |

`resolver_rule_eligibility_violation_count`는 Resolver 응답이나 후보가 Rule 입력 가능
상태로 잘못 승격됐는지만 검사합니다. 실제 pipeline의 Rule 미실행은 별도 통합 테스트로
검증합니다. CAS 힌트는 근거 검색용일 뿐이며 현장 확인 두 건 없이 Rule Engine을 실행할 수
없습니다.

다섯 기준 중 하나라도 실패하면 `deployment_gate.passed=false`가 되고 `evaluate` 명령은
`BLOCKED_SAFETY_GATE`로 종료합니다. 전체 오프라인 pipeline도 release manifest를 만들기
전에 실패합니다.

2026-07-28 Python 3.11 개발 환경과 4,300종 artifact에서 합성·내부 회귀 12건을 실행한
결과 12건 모두 통과했고 위험 힌트와 Resolver Rule 입력 승인 위반은 0건이었습니다. 평균
지연시간은 4.300ms, p95는 6.361ms였습니다. 이 시간은 해당 장비에서 Resolver 호출과 문장 내 별칭
스캔을 함께 측정한 값으로 운영 SLO가 아닙니다.

수정 전에는 `염산염`, `염산성`, `톨루엔느`, `에탄올성`의 일부 문자열이 공식 물질명
exact로 승격돼 해당 CAS 문서만 반환되는 사례를 재현했습니다. 공유 원문 span matcher를
적용한 뒤에는 이 표현들에서 CAS 힌트를 보류합니다. 데이터의 `review_status`는 여전히
`DRAFT_INTERNAL_REGRESSION`이므로 현장 정확도나 외부 검증 성능으로 인용할 수 없습니다.
보고서에는 평가 CSV와 resolver artifact의 SHA-256을 함께 기록합니다.

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

## 6. 2026-07-28 내부 기준선 재현

현재 로컬 artifact와 내부 회귀셋을 사용해 다시 실행한 결과입니다.

| 구성요소 | 케이스 | 지표 | 결과 |
|---|---:|---|---:|
| Resolver | 21 | 단일후보 확정 정확도 | 0.9524 |
| Resolver | 21 | Top-3 Recall | 1.0000 |
| Retriever 전체 흐름 | 10 | Recall@5 | 0.9000 |
| Retriever 전체 흐름 | 10 | MRR@8 | 0.6500 |
| Retriever 단독·정답 CAS 제공 | 10 | Recall@5 | 0.9000 |
| Retriever 단독·정답 CAS 제공 | 10 | MRR@8 | 0.6500 |
| 자동 CAS 힌트 | 10 | Coverage | 0.9000 |
| 자동 CAS 힌트 | 10 | Precision when present | 1.0000 |

현재 회귀셋에서는 잘못된 CAS 힌트는 없었고 모호한 `암모니아` 한 건에서 힌트를 보류했습니다.
그러나 이 수치는 질문에 맞는 MSDS 장을 찾았다는 뜻이 아닙니다. 현재 gold 판정은 주로
출처와 CAS가 같으면 통과합니다.

실제 상위 결과를 확인하면 다음 문제가 재현됩니다.

| 질문 | 현재 1위 | 회귀 평가 |
|---|---|---|
| 염화수소 누출 시 보호구 | `염화수소 MSDS 01장 제품명` | 정답 처리 |
| 염산 응급조치 | `염화수소 MSDS 03장 CAS 번호` | 정답 처리 |

따라서 다음 개선 대상은 Retriever 교체 자체가 아니라 section-level gold label을 만들고,
신고문에서 물질별 역할을 분리한 뒤 각각의 CAS·질문 의도로 검색하는 전체 흐름입니다.

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

## 8.1 Grounded RAG 안전 회귀

`tests/test_rag.py`의 12개 수집 케이스는 다음 실패를 검사합니다.

- 현장 확인 전 LLM 미호출
- 공식 URL 없는 Rule에 가짜 인용을 만들지 않음
- KOSHA·CAMEO 공식 도메인이 아닌 URL을 RAG 입력에서 제외
- 알 수 없는 `source_id`, Rule과 다른 위험등급, 안전 단정 차단
- timeout·잘못된 설정 시 extractive fallback
- 메타데이터에 LLM 주소·API Key가 노출되지 않음

추가 API 통합 테스트는 새 엔드포인트 없이 `/api/v1/incidents/analyze`의 완료 응답에
`grounded_rag`가 포함되고 기존 `conflict_review` 위험등급이 그대로 유지되는지 확인합니다.

2026-08-01 로컬 1,000회 extractive micro-benchmark는 평균 0.0131ms, p95 0.0132ms였습니다.
이는 작은 고정 입력에서 **요약 조립 함수의 추가 비용만** 측정한 값이며 검색·Rule·네트워크를
포함한 API SLO가 아닙니다. 실제 LLM 품질·지연시간은 모델이 확정되지 않아 평가 불충분입니다.
운영 `llm` 모드 채택 전에는 별도 locked 근거 질의셋에서 인용 정확성, 근거 밖 주장률, 형식
성공률과 p95를 측정해야 합니다.

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
