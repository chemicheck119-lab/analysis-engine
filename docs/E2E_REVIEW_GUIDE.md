# E2E 50건 독립 검수 가이드

> 현재 공모전 제출에서는 독립 검수 인력을 확보하지 못해 이 절차를 완료하지 않았습니다.
> 50건 후보는 정확도 근거로 사용하지 않으며, [공모전 공식근거 보증
> 기준](COMPETITION_ASSURANCE.md)을 별도 적용합니다. 이 문서는 향후 제한된 현장 파일럿을
> 위한 사람 검수 절차로 보존합니다.

## 먼저 이해할 점

`e2e_competition_candidate_pool.jsonl`의 50건은 **정답 데이터가 아닙니다**.
공개 검증 CAMEO 물질쌍 15개에 확인 상태 3가지를 적용한 45건과 입력 거부·모호성·미지원
조합 5건을 기계적으로 만든 **검수 후보**입니다.

현재 모델을 후보에 실행한 preflight는 다음만 확인합니다.

- 현장 확인 CAS 두 개가 없을 때 충돌 규칙이 실행되지 않는가
- 확인 전 위험등급이 노출되지 않는가
- 출력 JSON 계약이 깨지지 않는가
- 로컬 CPU에서 어느 정도 시간이 걸리는가

정답과 비교하지 않았으므로 정확도·Recall·현장 성능으로 발표하면 안 됩니다.

## 왜 두 사람이 필요한가

모델 출력으로 모델의 정답을 만들면 자기 답을 스스로 채점하는 문제가 생깁니다. 따라서
라벨러와 검수자가 서로의 시트를 보지 않고 각각 기대 상태를 작성해야 합니다. 두 사람의
ID가 다르고 모든 필드가 일치하며 안전 불변조건을 통과할 때만
`DOUBLE_REVIEWED_NON_EXPERT` locked test로 승격합니다.

전문가 검토가 없어도 공모전 내부 비교에는 사용할 수 있지만 `expert_reviewed=false`이며,
현장 안전 성능 또는 운영 배포 승인 근거는 아닙니다.

## 1. 후보 50건 생성

```bash
chemiguard119 e2e-review generate \
  --pair-snapshot data/evaluation/verified_pair_snapshot_2024.json \
  --output data/evaluation/e2e_competition_candidate_pool.jsonl \
  --json
```

생성 결과에는 `expected`가 없어야 합니다. `claim_scope`는
`REVIEW_CANDIDATE_ONLY`입니다.

## 2. 서로 다른 두 시트 생성

실제 담당자 ID로 바꾸어 실행합니다. 시트는 처음에 정답 열이 모두 비어 있습니다.

```bash
chemiguard119 e2e-review export \
  --candidates data/evaluation/e2e_competition_candidate_pool.jsonl \
  --actor-role LABELER \
  --actor-id labeler-01 \
  --output outputs/review/e2e-labeler.csv

chemiguard119 e2e-review export \
  --candidates data/evaluation/e2e_competition_candidate_pool.jsonl \
  --actor-role REVIEWER \
  --actor-id reviewer-02 \
  --output outputs/review/e2e-reviewer.csv
```

각 담당자는 다음 필드를 모두 작성합니다.

- `review_decision`: 승인 가능한 행은 `APPROVE`
- `status`, `rule_executed`, `rule_status`
- `missing_confirmations_json`
- `candidate_count`, `candidate_roles_json`
- `evidence_bases_json`
- `output_validation_status`
- `risk_level`, `severity`, `expect_abstention`
- `review_notes`

배열과 객체 열은 JSON으로 작성합니다. 예: `["facility_cas"]`,
`{"INCIDENT":"RESPONDER_CONFIRMED"}`. 위험등급이 없는 행은 `risk_level`과
`severity`를 모두 비워 둡니다.

## 3. 두 검수 결과 병합

```bash
chemiguard119 e2e-review merge \
  --candidates data/evaluation/e2e_competition_candidate_pool.jsonl \
  --labeler-sheet outputs/review/e2e-labeler.csv \
  --reviewer-sheet outputs/review/e2e-reviewer.csv \
  --output data/evaluation/e2e_competition_reviewed.jsonl \
  --report outputs/review/e2e-review-merge.json \
  --json
```

다음 중 하나라도 발생하면 reviewed 파일을 만들지 않습니다.

- 두 actor ID가 같음
- 후보 문장이나 CAS가 시트에서 변경됨
- 누락 행 또는 추가 행이 있음
- 두 사람의 기대값이 다름
- 확인 CAS 두 개 없이 충돌 규칙 실행을 승인함
- 미실행 상태에 위험등급을 기록함
- 입력 상태와 `missing_confirmations`가 모순됨

불일치는 두 사람이 근거를 다시 확인한 뒤 각자의 시트를 수정하고 다시 병합합니다. 모델
출력을 그대로 복사해서 합의를 만드는 방식은 허용하지 않습니다.

## 4. 현재 모델 사전점검

```bash
chemiguard119 e2e-review preflight \
  --candidates data/evaluation/e2e_competition_candidate_pool.jsonl \
  --db artifacts/chemiguard119.sqlite \
  --resolver-model artifacts/resolver.joblib \
  --retriever-model artifacts/retriever.joblib \
  --report outputs/review/e2e-preflight.json \
  --json
```

preflight는 후보·SQLite·Resolver·Retriever의 파일명, 크기와 SHA-256을 기록합니다. 절대
경로와 신고 원문의 전체 로그는 남기지 않습니다.

2026-07-31 로컬 측정에서는 50건 모두 출력 계약을 통과했고, 미확인 충돌 실행과 확인 전
위험 노출은 각각 0건이었습니다. 평균 99.061ms, p95 118.312ms는 한 번의 로컬 실행값이며
운영 SLO가 아닙니다.

## 검수 후 다음 결정

검수 완료 데이터에서 상태·기권·물질 역할·근거 귀속·충돌 규칙을 실제 정답과 비교합니다.
실패가 파서에 집중되면 NER 후보를, 검색 순위에 집중되면 reranker를 별도 실험합니다.
현재처럼 정답 라벨이 없는 상태에서는 파인튜닝하지 않습니다.
