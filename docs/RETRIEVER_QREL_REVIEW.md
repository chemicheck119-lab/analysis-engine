# Retriever section qrel 독립 검수

## 목적과 사실 상태

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
| 배포 artifact 기반 171질의 후보 | 부분 구현 또는 개발용 데모 |
| 171질의 사람 이중 검수 | 설계 완료·구현 전 |
| BM25·Dense·Hybrid·RRF·Reranker 비교 | 설계 완료·구현 전 |
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
