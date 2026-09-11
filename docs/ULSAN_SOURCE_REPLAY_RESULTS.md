# 울산 원본 확보와 고정 Resolver 재평가

확인일: 2026-09-11 KST. **원본 접근 blocker는 해소됐다.**
사용자가 제공한 ZIP으로 현재 모델을 다시 실행했고, 과거 보고서의 데이터 집계와 평가 지표가 일치했다.
모델을 새로 학습하거나 교체한 결과는 아니다.

## 쉬운 설명

Resolver는 물질 이름을 보고 CAS 후보를 찾는 부분이다.
이번에는 “예전에 적어 둔 점수가 실제 파일로도 다시 나오는가?”를 확인했다.
공식 사고표에 이미 있는 물질명–CAS 관계를 평가 기준으로 사용하므로,
검색 문서의 관련성을 사람이 채점하는 Retriever 독립 검수와는 다른 작업이다.

독립 검수는 사용자 일정상 보류한다. 기존 171질의·1,848쌍을 DRAFT로 보존하고 정답을 자동 승인하지 않는다.

## 실제 결과

| 평가 범위 | 사례 수 | 과거 보고서 Top-1 / Top-3 | 현재 고정 모델 Top-1 / Top-3 |
|---|---:|---:|---:|
| 2020년 전체 유효 표현 | 419 | 89.74% / 90.21% | **89.74% / 90.21%** |
| 과거 기록에 없던 표현 | 60 | 28.33% / 31.67% | **28.33% / 31.67%** |
| 과거 기록에 없던 CAS | 58 | 27.59% / 31.03% | **27.59% / 31.03%** |

- 전체 Top-1은 376/419, Top-3는 378/419. 미관측 표현 Top-1은 17/60, Top-3는 19/60.
- 잘못된 단일 exact 후보: **0/419**. 모든 종류의 오답이 0이라는 뜻이 아니며, CAS 확인이나 Rule 실행 실험도 아니다.
- 전체 MRR: 0.898966. 미관측 표현 MRR: 0.294444.
- 419건 중 **359건은 과거 이력에도 있던 표현–CAS 쌍**이다. 전체 점수를 새로운 표현의 일반화 성능으로 설명하면 안 된다.
- 미관측 표현 60건 중 12건의 정답 CAS는 현재 artifact에 없다. 이 60건을 보고 별칭을 보충하거나 threshold를 튜닝하지 않았다.
- 후보 검색 지연: 평균 0.370348ms, p95 3.026042ms. 단일 로컬 실행·모델 로드 이후 측정이며 STT·HTTP·동시 사용자·cold start를 포함하지 않는다. 응답속도 개선 성과로 사용하지 않는다.

결정: **현재 Sparse Resolver 유지.** 이번 결과는 새 모델 채택 근거가 아니라 기존 고정 모델의 재평가 증거다.
전체 419건/미관측 60건을 사용한 Hybrid 비교는 다음 실험으로 분리한다.

## 원본과 분할

- 공식 데이터: [울산 화학사고별 유해물질 판단 정보](https://www.bigdata-119.kr/goods/goodsInfo?goods_mng_sn=5).
- 사용자 제공 ZIP 64,330 bytes, 내부 공식 CSV 548,842 bytes·1개.
- CSV 31개 원본 열에서 연도·CAS·명칭 6개 열만 private 파생 파일에 보존. 주소·대응 열은 평가에 사용하지 않는다.
- 원본 행 1,868, checksum 오류/복합 CAS 제외 212행, 공유 모호 표현 2개.
- 유효 표현 1,530개: 2015~2019 이력 1,111개, 2020 평가 419개.
- checksum은 CAS 형식 검사이며 원천 라벨의 화학적 정확성을 사람이 검수했다는 뜻이 아니다.
- 기존 평가와 같은 전체 기간 모호 표현 제외 정책을 사용했다. 미래 표현을 보고 학습한 것은 아니지만, 평가 모집단 자체가 유효 단일 CAS·비모호 표현으로 제한된다.
- 신규 학습·튜닝 없음. 이미 공개된 2020 진단 slice이므로 새로운 secret test로 부르지 않는다.
- ZIP·파생 CSV·개별 실패 예시는 비공개, Git에는 집계·hash·코드만 추가한다. 공개 재배포 권한은 확인 완료로 표시하지 않는다.

비공개 보관 위치:
`/Users/hywznn/Documents/chemicheck119-lab/private-data/evaluation/ulsan-source-replay-20260911-v1/`.

## 과거 기록과 다른 부분

| 항목 | 과거 2026-08-02 보고서 | 이번 2026-09-11 실행 | 정확한 표현 |
|---|---|---|---|
| 모델 SHA-256 | `e80d612b…` | `2696fa7f…` | 동일 schema 계열이지만 파일 bytes는 다름 |
| 가공 CSV SHA-256 | `f013ebed…` | `5237194a…` | 서로 다른 가공 파일이며 hash 동일성은 주장하지 않음 |
| 공식 CSV SHA-256 | 당시 보고서에 독립 원본 hash 없음 | `b7cabc5d…` | ZIP·CSV·파생 hash를 이번에 각각 기록 |
| 평가 집계·지표 | 419건 / 미관측 60건 | 동일 집계·지표 | 현재 고정 모델의 공식 원본 재평가 결과 |

따라서 “과거 모델을 byte-identical하게 복원했다”는 표현은 사용하지 않는다.
과거 전체 사례 목록의 canonical hash는 없으므로, 집계 일치만으로 과거 개별 사례/순위까지 완전히 동일했다고 단정하지 않는다.
더 강한 원본 동일성 증명에는 당시 가공 CSV·구체적 변환 기록·과거 모델 파일이 추가로 필요하다.
전체 SHA-256은 [집계 JSON](../data/evaluation/ulsan_source_replay_2026-09-11.json)에 기록했다.

## 구현과 검증

사전 계획: [평가 전 고정 조건](ULSAN_SOURCE_REPLAY_PLAN.md).
실행 코드 commit: `c350017f2930ab554d8fe16f37ba0f8119e5172f`.

- ZIP 크기·멤버 수·경로·symlink·암호화·hash 확인, 멤버 경로를 파일 시스템에 그대로 풀지 않음.
- 기존 최소 열 intake·시간 분할 evaluator 재사용.
- 원본 ZIP 사본/파생/보고서는 private 경로에 새로 생성. 기존 파일과 모델을 덮어쓰지 않음.
- 새 출력 폴더 0700, 최초 출력 파일 0600. 원본 CSV 임시 파일은 투영 후 정리하며 ZIP 사본은 보존.
- 코드·안전·계약 회귀 **678개 통과**, Ruff 정적·형식 및 API 계약 drift 통과.
- 실제 모델의 별도 내부 회귀 21/21 후보 Top-1·Top-3 적중.
- 내부 hint 안전 회귀 12/12 통과, unsafe hint·wrong hint·Rule eligibility 위반 각각 0.
- 위 21/12는 내부 합성 회귀이며 공식 419건 평가와 합치지 않는다.
- Docker 빌드와 최종 완료 판정은 이 변경의 PR CI에서 확인한다. 기존 dependency deprecation warning 1개는 유지했다.

## 재현 명령

평가 코드 revision이 같은 환경에서 실행한다. 다른 코드면 실제 revision을 새로 기록한다.
원본이 있는 팀원만 승인된 private 경로로 실행하며, 같은 출력 폴더를 다시 쓰면 중단한다.

```bash
.venv/bin/python scripts/data/replay_ulsan_resolver.py \
  --archive ../private-data/evaluation/ulsan-source-replay-20260911-v1/source.zip \
  --model ../private-data/analysis-runtime/action-brief-local-v1/resolver.joblib \
  --reference-report data/evaluation/incident_adapted_resolver_temporal_2026-08-02.json \
  --private-output-dir ../private-data/evaluation/ulsan-source-replay-reproduction \
  --expected-archive-sha256 d0ee041579a953084f8037763ac7d0a3d901d11278a6854d348374c69363f976 \
  --expected-model-sha256 2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff

.venv/bin/python -m chemiguard119.cli evaluate --only resolver \
  --resolver-model ../private-data/analysis-runtime/action-brief-local-v1/resolver.joblib \
  --report-dir ../private-data/evaluation/ulsan-source-replay-reproduction/internal-regression

.venv/bin/python -m pytest -o addopts='' -q
.venv/bin/python -m ruff check src tests scripts
.venv/bin/python -m ruff format --check src tests scripts
.venv/bin/python scripts/contracts/export_contracts.py --check
```

출력 summary는 집계만 담는다. 개별 실패 원문이 있는 `locked_evaluation_private.json`은 공개하지 않는다.
새 실행 시각에 따라 manifest/보고서 hash와 지연시간은 달라질 수 있으나 원본·모델·canonical 사례 hash는 비교할 수 있다.

## 다음 범위와 사실 상태

| 항목 | 상태 |
|---|---|
| ZIP intake·재평가 실행 및 회귀 도구 | 부분 구현 또는 개발용 데모 — 실제 실행, 최종 CI에서 구현 완료 범위 확인 |
| 공식 사고표 기반 419건/60건 측정 | 부분 구현 또는 개발용 데모 — 공개 기록의 제한된 평가 |
| 전체 범위의 고정 Sparse/Dense/RRF 비교 | 설계 완료·구현 전 |
| Retriever 171질의 독립 qrel | 설계 완료·구현 전 — 사용자 일정으로 보류, DRAFT 유지 |
| 현장 안전성·무전 일반화·실사용 시간 절감 | 검증되지 않은 가설 |

다음 목표는 이미 사전 진단한 고정 Sparse/Dense/RRF 설정을 전체 범위에서 비교하는 것이다.
새 사람이 정답을 매기거나 2020 정답으로 튜닝하는 방식은 사용하지 않는다.
Dense가 일부 회복하더라도 기존 exact 후보·기권·2-CAS 확인 조건을 우회하는 구성은 채택하지 않는다.

새 서버 임대·GPU·유료 LLM·GCP 실행: 0건. 이번 추가 서버 비용 0원, 과거 누적 비용은 미확인.
Notion 수정·운영 배포·main 병합·기존 runtime artifact 변경은 하지 않았다.
