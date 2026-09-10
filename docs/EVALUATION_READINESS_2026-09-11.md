# 평가 준비 현황과 사람이 필요한 지점

확인일: 2026-09-11 KST. 실행 코드: `d5d052556f1d58d70918e74fb7c36756d30ed36f`.
전체 상태: **부분 구현 또는 개발용 데모**. 이번 작업은 모델 정확도 향상이 아니라 평가 준비 상태를 검사한다.

## 한눈에 보기

| 질문 | 실제 확인 결과 | 의미와 한계 |
|---|---|---|
| 검수할 자료가 준비됐나? | 171질의·1,848 질의-근거 쌍, 12개 배치 | 기계 생성 후보이며 정답이 아니다 |
| 현재 Retriever 결과가 검수 목록에 빠졌나? | 1개 기준선의 반환 803회 중 누락 0회 | Top-5 이내 결과의 포함 검사이며 Recall@5가 아니다 |
| 사람이 얼마나 검수했나? | 시작·승인 행 0, 완료 질의 0 | 독립 검증이나 성능 비교를 시작할 수 없다 |
| 기존 빈 배치가 변했나? | 12개 모두 manifest의 template SHA-256과 일치 | 파일 상태 확인일 뿐 manifest 자체의 독립 진실성 증명이 아니다 |
| Resolver 원본을 구할 경로가 있나? | 공식 무료 CSV 페이지 확인, 로그인 필요 | 전체 파일을 확보한 상태는 아니다 |
| GCS에 원본이 없나? | 내부 prefix 조회가 billing 관련 403으로 실패 | 원본 존재 여부는 확인 불가, 결제 설정 미변경 |

**결정: 검수 준비 도구는 유지하고, 사람 정답을 필요로 하는 모델 비교·학습은 보류한다.**
STT·Parser·Resolver·Retriever의 기존 성능을 이번 수치로 대체하지 않는다.

## 이번 목표와 판정 기준

- 해결할 실패: 검수 목록 누락, 원문 변경, 미작성 시트를 정답으로 취급하는 오류.
- 가설: 사람이 검수하기 전에 현재 기준선의 Top-K 포함 여부와 시트 무결성을 기계적으로 확인할 수 있다.
- 데이터: 기존 비공개 v2 후보와 시트, 승인된 action-brief-local-v1 DB·Retriever. 새 데이터 수집·학습 없음.
- 분리: 후보·시트·모델은 변경하지 않으며, 정답 라벨과 train/dev/test 분할을 임의로 만들지 않는다.
- 주요 검사: 후보/DB/질의 hash, 누락·다른 CAS·알 수 없는 문서 ID, 시트 원문·진행률.
- 채택 조건: 로컬 테스트·계약 검사와 PR CI 통과, 실제 artifact 검사에서 기술적 blocker 없음.
- 보류 조건: 새 검색기의 미포함 결과는 pool 확대 후 사람 검수, 원문·CAS·hash 불일치는 원인 확인 전 중단.
- 주장 가능: 명시한 단일 검색기의 결과 포함 여부와 파일 기반 검수 준비 상태.
- 주장 불가: 검색 정확도, 독립 qrel 완성, 현장 질문 대표성, 소방 안전성, 사용자 시간 절감.

## 구현과 회귀 검사

별도 로컬 브랜치 `experiment/retriever-review-readiness`의 네 커밋을 재사용했다.
현재 Parser·행동 카드 API와 기존 배치 분할·재조립 코드를 보존했다.

- `retriever-review status`: 사람이 쓴 내용의 형식·원문 일치·진행률 확인.
- `retriever-review pool-run`: 현재 검색기를 실제 artifact로 실행하여 질의 hash와 반환 문서 ID 기록.
- `retriever-review pool-audit`: 선언한 검색기의 Top-K가 사람 검수 pool 안에 있는지 확인.
- 잘못된/누락된 문서 ID를 정상 기권으로 숨기지 않고 중단한다.
- 새 감사 보고서는 비공개 파일 권한으로 저장하고 기존 파일은 덮어쓰지 않는다.

로컬 검증: 전체 테스트 663개, Ruff 정적·형식 검사, API 계약 drift 검사.
테스트는 합성 단위·계약 회귀다. Docker 빌드는 PR CI에서 별도 확인하며 CI 전에는 완료로 판정하지 않는다.
기존 Starlette/AnyIO deprecation warning 1개는 남아 있고 의존성을 변경하지 않았다.

## 실제 artifact 실행 결과

환경: Apple M4 / Darwin arm64 / Python 3.11.15, 로컬 CPU.
비교 대상은 `evidence-hybrid-tfidf-v2` 하나이며 새로운 BM25·Dense·Reranker ablation이 아니다.
CAS hint를 제공한 section 검색이므로, 음성에서 올바른 CAS를 추론하는 평가도 아니다.

- 171질의, top_k=5, 반환 합계 803회, 결과가 빈 질의 1개.
- 누락 질의-근거 쌍 0, 다른 CAS·알 수 없는 문서·hash mismatch blocker 0.
- 결과가 빈 1개는 관찰된 동작일 뿐 “올바른 기권”이라고 판정하지 않았다.
- 단일 라벨러 시트: `NOT_STARTED`, 1,848행 중 시작/승인 0, 완료 0/171.
- 두 사람의 독립성은 ID가 다르다는 것만으로 입증되지 않는다. 실제 검수 절차가 필요하다.

집계와 SHA-256은 [기계 판독 요약](../data/evaluation/evaluation_readiness_2026-09-11.json)에 있다.
개별 질의·원문·검수 시트·검색 run은 비공개 경로에만 보관한다.
첫 run의 revision 표기가 불완전해 원본을 보존하고 정확한 `d5d0525` 전체 SHA로 재실행했다.
보고서는 이름에 `20260911_d5d0525`가 있는 run만 채택한다.

### 재현

저장소 루트에서 실행한다. 아래 경로는 이 로컬 작업 환경의 승인된 private-data 위치다.
새 출력 파일명을 사용해야 하며 기존 결과를 덮어쓰면 중단한다.

```bash
.venv/bin/python -m chemiguard119.cli retriever-review status \
  --candidates ../private-data/evaluation/retriever-review/retriever_qrel_candidates_v2.jsonl \
  --review-sheet ../private-data/evaluation/retriever-review/retriever_qrel_labeler_choihj0510_v2.csv \
  --actor-role LABELER --json

.venv/bin/python -m chemiguard119.cli retriever-review pool-run \
  --candidates ../private-data/evaluation/retriever-review/retriever_qrel_candidates_v2.jsonl \
  --db ../private-data/analysis-runtime/action-brief-local-v1/chemiguard119.sqlite \
  --retriever-model ../private-data/analysis-runtime/action-brief-local-v1/retriever.joblib \
  --system-id baseline-lexical-hybrid \
  --system-version evidence-hybrid-tfidf-v2@d5d052556f1d58d70918e74fb7c36756d30ed36f \
  --top-k 5 \
  --output ../private-data/evaluation/retriever-review/baseline_pool_run_reproduction.json --json

.venv/bin/python -m chemiguard119.cli retriever-review pool-audit \
  --candidates ../private-data/evaluation/retriever-review/retriever_qrel_candidates_v2.jsonl \
  --db ../private-data/analysis-runtime/action-brief-local-v1/chemiguard119.sqlite \
  --system-run ../private-data/evaluation/retriever-review/baseline_pool_run_reproduction.json --json

.venv/bin/python -m pytest -o addopts='' -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python scripts/contracts/export_contracts.py --check
```

명령은 실행 코드 revision을 checkout한 환경을 전제로 한다. 이후 코드로 실행하면 실제 revision을 새로 기록한다.
상세 검수 방법은 [Retriever qrel 안내](RETRIEVER_QREL_REVIEW.md)를 따른다.

## Resolver 원본 경로와 충돌 정리

1. [공식 상품 페이지](https://www.bigdata-119.kr/goods/goodsInfo?goods_mng_sn=5)에서
   **울산 화학사고별 유해물질 판단 정보**, `유해물질판단_2020_2015.csv`,
   **535.98KB·무료(0원)**를 확인했다. 파일 표시일은 2021-04-26, 제공기관 표시는 한국소방안전원이다.
   현재 세션에는 로그인·구매하기가 표시됐고 로그인/구매/다운로드를 수행하지 않았다.
   페이지의 일부 예시 행은 전체 419건/60건을 대체하지 않으며 학습·튜닝에 사용하지 않았다.
2. Documents·Downloads·Desktop에서 울산/유해물질/ulsan/unseen 등의 파일명으로 제한 검색했으나
   원본을 찾지 못했다. 다른 이름의 파일이나 검색하지 않은 저장소까지 없다고 주장하지 않는다.
3. GCS bucket/root 목록 일부는 읽었으나 raw/derived/manifests/experiments 내부 조회는
   결제 계정 비활성화 상태의 403으로 실패했다. 원본 부재가 아니라 접근 불가다.
   결제 복구·새 리소스 생성·다운로드를 수행하지 않았다.
4. [종합 Notion](https://app.notion.com/p/3cec4c18d9718002b093ccec91200a75)은
   2026-09-09 수정본을 **읽기만** 했다. 원본 부재·사람 검수 전 상태를 교차 확인했지만,
   최신 Parser PR보다 오래된 코드/테스트 수치다. 모든 하위 페이지를 조사했다고 주장하지 않는다.

충돌: 과거 이슈 댓글의 “원본 SHA”와 실제 평가 입력의 SHA 표현을 구분한다.
`f013ebed301c5306178ad72f8dbbb62bdb5ecae1122ae3dfe7d1050f7ea0d765`는
2026-08-01/02 평가 보고서의 **가공 입력 hash**다.
공식 다운로드 파일에 이 hash 일치를 요구하면 안 된다.
현재 표현은 “과거 보고서의 419건/미관측 60건 수치, 새 원본으로는 미재현”이다.
추가 확인할 원본은 다운로드 CSV·이용 조건과 당시 투영/분할 기록이다.
[기존 전처리·원본 manifest 절차](FINETUNING.md#재현)를 재사용하고 새 원본과 파생 hash를 각각 기록한다.

## 사용자가 해야 할 최소 작업

1. **원본 접근:** 공식 페이지에 직접 로그인하여 이용 조건을 확인한다.
   무료 획득에 동의하고 파일을 확보했다면 비공개 저장 위치만 알려준다.
   계정 비밀번호나 원문을 채팅/Git에 붙여 넣지 않는다.
   원본의 불필요한 주소 등 열을 다루기 전 사용자 승인과 기존 최소 열 투영 절차를 확인한다.
2. **검수 담당 결정:** 본인 포함 실제로 서로 독립적으로 검수할 두 사람을 정한다.
   지금 171질의를 혼자 완료하라는 뜻이 아니다.
   처음에는 첫 배치의 **한 질의와 그 질의의 모든 근거 행**으로 작성법을 확인한다.
   이는 절차 연습이지 독립 잠금 테스트가 아니다. 도구가 정답을 알려주거나 승인 칸을 채우지 않는다.
   본인 평가를 원하지 않으면 두 팀원이 맡아도 된다.

기존 labeler 배치는 private-data/evaluation/retriever-review/labeler-batches-choihj0510-v2에 있다.
독립 reviewer 이름을 임의로 만들거나 상대 시트를 보여주지 않았다.
비전문가 qrel 검수와 소방·화학 전문가의 행동 문구/SOP 승인은 서로 다른 절차다.

## 사실 상태와 비용

- 평가 준비·회귀 도구와 이번 artifact 시연: **부분 구현 또는 개발용 데모** — PR CI와 함께 구현 범위를 확인한다.
- 원본 확보 후 Resolver 재현 / 171질의 이중 검수: **설계 완료·구현 전**.
- 새 모델의 일반화 개선 / 현장 안전성과 시간 절감: **검증되지 않은 가설**.
- 독립 qrel 완료나 새 모델 채택을 **구현 완료**로 표시할 근거는 아직 없다.

새 서버·GPU·유료 LLM·배포 작업은 0건이다. 로컬 계산에 따른 추가 서버 임대 비용은 0원이다.
GCS 메타데이터 요청의 소액 비용과 과거 누적 결제액은 확인하지 못했으므로 전체 비용을 0원이라고 단정하지 않는다.
결제 계정 상태를 복구하거나 70,000원 예산 내 여유가 있다고 추정하지 않는다.
