# 케미체크119 모델 평가

> 이 문서의 21건·10건 결과는 내부 회귀 기준선입니다. 세 파일이 생긴 이력, 현재 지표가
> 측정하지 못하는 항목과 독립 보류 평가 설계는
> [평가 V2](EVALUATION_V2.md)를 먼저 확인하세요. `incident_parser_seed.jsonl` 6건은
> 평가셋이 아니라 `DRAFT` 형식 시드이며 파서 성능 수치가 없습니다.

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

### 공모전용 E2E 50건 독립 검수 후보

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

세 파일은 표본설계로 정한 규모가 아니라 2026-07-23 커밋 `6246c49`에서 함께 추가된
정적 fixture입니다. 원본 표본추출 기록, 외부 검증자와 현장 대표성 근거가 없습니다.

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
