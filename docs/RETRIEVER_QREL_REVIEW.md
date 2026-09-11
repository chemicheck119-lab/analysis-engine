# Retriever section qrel 독립 검수

## 목적과 사실 상태

2026-09-11 사용자 일정에 따라 독립 검수는 보류했다. 171질의 후보는 DRAFT로 보존하며
자동 라벨·LLM 판정을 사람 승인으로 대체하지 않는다. 아래는 재개 시 사용할 절차다.

현재 12질의 section 평가는 `DRAFT_INTERNAL_REGRESSION`이며 독립 검증이나 현장 검색
정확도가 아니다. 이 도구는 100~200질의 평가팩을 만들기 전에 다음 단계를 분리한다.

```text
KOSHA 상세가 있는 물질
→ 19개 질문 유형의 기계 생성 후보
→ 현재 Retriever Top-K + 관련 SDS 장의 검수 pool
→ 라벨러·검수자의 독립 CSV
→ 완전 일치 여부 검사
→ DOUBLE_REVIEWED_NON_EXPERT locked JSONL
→ section evaluator 실행
```

| 항목 | 상태 |
|---|---|
| 질문·evidence pool 생성기 | 구현 완료 |
| 독립 검수 CSV export·병합 Gate | 구현 완료 |
| 질의 단위 검수 배치 분할·재조립 Gate | 구현 완료 |
| 검수 진행률·원문 무결성 감사 | 구현 완료 |
| 선언된 검색기 Top-K pool coverage 감사 | 구현·기준선 실행 완료 |
| 배포 artifact 기반 171질의 후보 | 부분 구현 또는 개발용 데모 |
| 171질의 사람 이중 검수 | 설계 완료·구현 전 |
| BM25·Dense·Hybrid·RRF·Reranker 비교 | 설계 완료·구현 전 |

2026-09-09 기준 `baseline-lexical-hybrid` Top-5를 171질의에 실행한 결과 803개 반환
occurrence가 기존 후보 pool에 모두 포함됐고, 누락 pair는 0건이었다. 답변 불가 질의 1건은
`NO_EVIDENCE_FOUND`로 기권했다. 사람 라벨은 0/171로 시작 전이며, 이 결과는 검색 정확도가
아닌 **현재 기준선에 한정된 검수 pool 포함 감사**다. 공개 가능한 집계·artifact hash는
`data/evaluation/retriever_qrel_pool_audit_2026-09-09.json`에 기록했다.
| 현장 검색 정확도 | 검증되지 않은 가설 |

기계 생성 질문은 실제 신고·무전 질문 분포가 아니다. 최종 병합 결과도 비전문가 두 명이
검수한 KOSHA SDS section 평가일 뿐, 현장 안전성이나 전국 소방 검색 정확도를 증명하지
않는다.

## 후보 구성

KOSHA 상세가 적재된 물질마다 다음 19개 질문을 만든다.

- 보호구 2개
- 누출 대응 2개
- 소화 대응 2개
- 응급조치 3개
- 저장·취급 2개
- 안정성·반응성 2개
- 물질 식별 2개
- SDS로 답할 수 없는 현재 재고·누출률·풍향·노출 인원 질문 4개

9종 artifact에서는 총 171질의가 된다. 이 중 36질의는 답변 불가 기권을 검수한다. 각
질문의 검수 pool은 현재 Retriever Top-K와 질문 유형에 대응하는 KOSHA SDS 장을 합친다.
pool에 들어갔다는 사실은 relevance 정답이 아니다. 이후 Dense·RRF·Reranker를 비교하기
전에는 각 시스템의 Top-K도 같은 방식으로 pool에 합치고 새로 검수해야 한다.

답변 불가 질문에서 현재 Retriever가 정상적으로 아무 근거도 반환하지 않으면, 같은 CAS의
공식 문서를 negative control로만 넣어 사람이 모두 비관련인지 확인한다. 답변 가능한 질문의
pool이 비었을 때는 이 대체를 사용하지 않고 생성 자체를 중단한다.

## 1. 검수 후보 생성

원문을 포함한 후보와 CSV는 Git이 아니라 승인된 비공개 경로에 저장한다.

```bash
chemiguard119 retriever-review generate \
  --db artifacts/chemiguard119.sqlite \
  --retriever-model artifacts/retriever.joblib \
  --output /approved/private/retriever_qrel_candidates.jsonl \
  --json
```

후보에는 `answerable`, `qrels`, `relevance_grade`, `required_fact_ids`를 넣지 않는다.
DB·Retriever SHA-256과 각 evidence 본문의 SHA-256을 기록하며 다른 CAS 근거가 섞이면
생성을 중단한다.

## 2. 독립 검수 시트 생성

서로 다른 두 사람이 상대방의 시트를 보지 않고 작성한다.

```bash
chemiguard119 retriever-review export \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --actor-role LABELER \
  --actor-id labeler-01 \
  --output /approved/private/retriever_qrel_labeler.csv \
  --json

chemiguard119 retriever-review export \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --actor-role REVIEWER \
  --actor-id reviewer-02 \
  --output /approved/private/retriever_qrel_reviewer.csv \
  --json
```

각 행에 다음을 입력한다.

| 열 | 입력 규칙 |
|---|---|
| `review_decision` | 완전히 검토했을 때 `APPROVE` |
| `answerable` | 질문 단위로 `true` 또는 `false` |
| `relevance_grade` | 0 비관련, 1 보조, 2 핵심 일부, 3 직접·충분한 핵심 근거 |
| `required_fact_ids_json` | 관련 근거가 담은 사실 ID 문자열 배열 |
| `supporting_sentence` | 관련 근거에서 그대로 확인한 문장 |
| `review_notes` | 판단 근거와 모호성 메모 |

관련 근거에는 fact ID와 원문 안에 실제 존재하는 근거 문장이 필요하다. 답변 가능한 질문은
grade 2 이상의 핵심 근거가 하나 이상 있어야 한다. 답변 불가 질문은 모든 pool 문서가
grade 0이어야 한다.

### 2-1. 171질의를 작은 검수 배치로 나누기

한 파일의 1,848개 evidence 행을 한 번에 검수하지 않아도 된다. `batch`는 한 질문에 속한
모든 evidence 행을 같은 CSV에 유지하고, 질문 의도별 사례가 각 배치에 고르게 들어가도록
결정적으로 분할한다. 기본값은 배치당 최대 15질의이므로 현재 171질의는 12개 배치가 된다.

```bash
chemiguard119 retriever-review batch \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --actor-role LABELER \
  --actor-id labeler-01 \
  --questions-per-batch 15 \
  --output-dir /approved/private/labeler-batches \
  --json

chemiguard119 retriever-review batch \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --actor-role REVIEWER \
  --actor-id reviewer-02 \
  --questions-per-batch 15 \
  --output-dir /approved/private/reviewer-batches \
  --json
```

각 디렉터리는 owner 전용 권한의 CSV와 `batch_manifest.json`을 가진다. manifest의
`template_sha256`은 라벨 입력 전 원본 시트의 해시다. 사람이 값을 입력하면 배치 파일의
해시가 달라지는 것이 정상이며, 재조립 때 수정 가능한 라벨 열과 수정하면 안 되는 질문·근거
컨텍스트를 구분해 검사한다. 재조립기는 candidate와 배치 크기로 공란 template를 다시
렌더링해 `template_sha256`, batch·질의·evidence 수, intent 분포, case ID를 모두 재계산한다.
manifest에 적힌 값을 그대로 provenance로 신뢰하지 않는다. 두 역할은 서로의 디렉터리를
열어보지 않는다.

모든 배치를 완료한 다음 각각 단일 CSV로 재조립한다.

```bash
chemiguard119 retriever-review assemble \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --batch-dir /approved/private/labeler-batches \
  --output /approved/private/retriever_qrel_labeler.csv \
  --json

chemiguard119 retriever-review assemble \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --batch-dir /approved/private/reviewer-batches \
  --output /approved/private/retriever_qrel_reviewer.csv \
  --json
```

재조립은 누락·중복 질의, 누락·추가 evidence, actor 변경, 질문·CAS·원문·URL·버전 변경,
미완료·모순 라벨을 차단한다. 성공해도 한 사람의 완료된 시트일 뿐이다. 두 시트의 독립성과
완전 일치는 다음 `merge` Gate가 별도로 확인한다.

검수 도중에는 다음 명령으로 정답을 추론하지 않고 진행률과 후보 원문 변조 여부만 확인한다.

```bash
chemiguard119 retriever-review status \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --review-sheet /approved/private/retriever_qrel_labeler.csv \
  --actor-role LABELER \
  --report /approved/private/retriever_qrel_labeler_status.json \
  --json
```

`NOT_STARTED`, `IN_PROGRESS`, `NEEDS_CORRECTION`,
`READY_FOR_INDEPENDENT_MERGE`, `BLOCKED_REVIEW_GATE` 중 하나를 반환한다. 이 결과는 한
사람의 작업 상태일 뿐 Retriever 성능이나 이중 검수 완료를 뜻하지 않는다.

## 2.1 신규 검색 시스템의 pool coverage 감사

BM25·Dense·Hybrid·Reranker를 비교하기 전에 각 시스템은 다음 필드를 가진
`chemicheck119-retriever-pool-run-v1` JSON을 만든다.

- `system_id`, `system_version`, `candidate_sha256`, `system_artifact_sha256`
- 후보와 같은 `database_sha256`, 실행 `top_k`
- 모든 `case_id`별 `query_sha256`, `returned_evidence_ids`

현재 배포 artifact의 lexical hybrid 기준선은 원문 질의나 문서 본문을 복제하지 않고 다음
명령으로 실행 기록을 만든다. `system-version`에는 평가 코드 revision과 artifact schema를
함께 고정한다.

```bash
chemiguard119 retriever-review pool-run \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --db /approved/private/chemiguard119.sqlite \
  --retriever-model /approved/private/retriever.joblib \
  --system-id baseline-lexical-hybrid \
  --system-version evidence-hybrid-tfidf-v2@GIT_COMMIT \
  --top-k 5 \
  --output /approved/private/baseline_pool_run.json \
  --json
```

```bash
chemiguard119 retriever-review pool-audit \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --db /approved/private/chemiguard119.sqlite \
  --system-run /approved/private/bm25_pool_run.json \
  --system-run /approved/private/dense_pool_run.json \
  --report /approved/private/retriever_pool_audit.json \
  --json
```

모든 Top-K가 후보에 있으면 `COMPLETE_FOR_DECLARED_SYSTEMS`, 같은 CAS의 누락 문서가 있으면
`POOL_EXPANSION_REQUIRED`, DB·질의·CAS·문서 ID가 맞지 않으면
`BLOCKED_POOL_AUDIT`이다. 전자는 **선언한 시스템에 한정된 포함 검사**일 뿐 관련성이나 전체
pool 완전성을 증명하지 않는다. 새 문서가 발견되면 검수 시작 전에 후보와 빈 시트를 다시
생성해야 한다.

## 3. 합의 병합

```bash
chemiguard119 retriever-review merge \
  --candidates /approved/private/retriever_qrel_candidates.jsonl \
  --labeler-sheet /approved/private/retriever_qrel_labeler.csv \
  --reviewer-sheet /approved/private/retriever_qrel_reviewer.csv \
  --db artifacts/chemiguard119.sqlite \
  --output /approved/private/retriever_sections_locked.jsonl \
  --report /approved/private/retriever_qrel_merge_report.json \
  --json
```

다음 경우에는 결과 파일을 만들지 않고 `BLOCKED_REVIEW_GATE`로 종료한다.

- 같은 사람이 두 역할을 수행함
- 후보·evidence 행이 누락되거나 추가됨
- 질문·원문·URL·문서 버전이 수정됨
- 후보 생성 때의 DB와 현재 DB SHA-256이 다름
- 두 사람의 answerable, grade, fact ID, 근거 문장이 다름
- 관련 근거 문장이 evidence 원문에 없음
- 답변 가능·불가능 상태와 relevance가 모순됨

불일치는 자동 다수결이나 LLM으로 해결하지 않는다. 사람이 원문을 다시 확인하고, 필요한
경우 제3 검수자가 조정한 별도 절차를 정의할 때까지 평가 실행을 멈춘다.

## 남은 Gate

1. 라벨러와 독립 검수자 지정
2. 171질의 검수와 불일치 조정
3. 신규 후보 시스템을 포함한 pool completeness 감사
4. locked JSONL의 `COMPETITION_REVIEWED` 계약 통과
5. 기존 12질의와 분리해 BM25 기준선 측정
6. 그 뒤에만 Dense·Hybrid·RRF·Reranker 비교
