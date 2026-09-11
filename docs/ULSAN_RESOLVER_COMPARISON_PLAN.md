# 공식 울산 데이터의 Sparse·Dense·RRF 동조건 비교 사전 계획

상태: **설계 완료·구현 전**. 결과를 보기 전에 별도 커밋으로 고정한다.

## 검증 질문

기존 정확 일치·모호성·식별자 거절 정책을 유지하면서, fuzzy 후보 검색에 BGE-M3 또는 동일 가중치 RRF를 사용하면 미관측 표현의 후보 적중률이 개선되는가? 사용자가 요청한 것은 공식 정답을 이용한 동일 모집단 비교이며, Retriever 사람 검수와는 별개다.

## 고정 입력과 분할

- 공식 ZIP SHA-256: `d0ee041579a953084f8037763ac7d0a3d901d11278a6854d348374c69363f976`.
- 최소 6열 파생 CSV SHA-256: `5237194a1a22bf9fb3666a4009237d6bd243095e262baba3468874283f51d11c`.
- 현재 Resolver SHA-256: `2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff`, 학습 cutoff 2019. 수정·재학습하지 않는다.
- 동일 전처리의 2020년 정규화 표현–CAS 쌍 419건. `217bfa4aa6ff3ac2b98b20049096c76df7249747b8ae2d3c524ee6a0499f2f6a`.
- 과거 2015~2019년 기록에 없는 표현–CAS 쌍 60건. `6c17105cecc2c30254ef2297b94b2d414a8f11166215703d00d552bbd8b6a24a`.
- 미관측 CAS 58건도 별도 집계. 내부 21질의 회귀와 12질의 hint 안전 회귀는 공식 데이터 표에 합치지 않는다.
- 기존 전체 기간 모호 표현 제외 정책과 반복 표현 359건을 그대로 명시한다. 사건 단위 독립 test나 새 비공개 test가 아니다. 공식 표의 CAS를 그대로 쓰며 독립 화학 검수 완료로 표현하지 않는다.

## 비교할 세 시스템

| 시스템 | 공통 경로 | 정확 일치가 없을 때 |
|---|---|---|
| A Sparse | 현재 Resolver의 exact·ambiguous·CAS 식별자 거절 보존 | 현재 문자 TF-IDF, minimum_score=0.20 |
| B Dense fallback | A와 동일 | BGE-M3 CLS cosine, CAS별 최고 별칭 점수 |
| C RRF fallback | A와 동일 | A의 Top-100(minimum_score=0.20) + B의 Top-100, 1/(60+rank) 동일 가중치 |

- 현재 API는 변경하지 않는다. 모든 후보는 미확인이고 Rule 입력 불가이다.
- 빈 입력, checksum 오류 또는 미등록 CAS 식별자는 Dense로 우회하지 않는다.
- source-only exact 전용 행은 두 fuzzy 경로에서 제외한다. 숫자 CAS 문자열은 Dense 의미 임베딩에서 제외하되 Sparse의 기존 벡터 공간은 변경하지 않는다. '같은 조건'은 입력·정답·artifact·확인 정책·Top-K가 같다는 뜻이지 서로 다른 모델의 점수 분포가 같다는 뜻이 아니다.
- BGE-M3는 캐시 revision `5617a9f61b028005a4858fdac845db406aefb181`, CLS, L2, float32, max_length=32, batch_size=64, local_files_only, Mac MPS. 학습/다운로드/LLM 호출 없음. 가중치·토크나이저 hash와 잘림 건수를 기록한다.
- Dense/RRF에 시험 정답으로 정한 confidence threshold를 추가하지 않는다. 후보 반환률은 기권 안전성이나 확률 calibration 성능이 아니다.
- 과거 20개 실패 예시 probe와 달리 B에도 exact 보존을 적용하며 RRF Sparse pool의 0.0을 현재 기본값 0.20으로 맞춘다. 따라서 과거 probe와 새 수치를 직접 성능 개선폭으로 빼지 않는다.

## 지표·판정·중단

- 전체/미관측 표현/미관측 CAS: 후보 Top-1·Top-3, MRR@3, 후보 반환률·빈 후보율, 정답 CAS의 artifact 포함률. 안전한 selective abstention 학습/검증은 이번 범위 밖이다.
- 각 후보가 A보다 고친 사례 수와 A의 정답을 잃은 사례 수를 쌍으로 계산한다. 분모·건수와 비율을 함께 기록한다.
- 안전: 잘못된 단일 exact 후보 수, rule_input_eligible/rule_eligible 위반, 공통 Gate 변경 여부, source-only fuzzy 누출. 실제 Rule 실행이나 현장 안전 평가는 아니다.
- 조건부 후속 검토: 미관측 Top-3가 A보다 높고, 미관측·전체·내부 회귀 Top-1/Top-3 비회귀, 위반 0건일 때만 후속 검증 후보로 남긴다. 하나라도 악화하면 해당 설정을 기각하고 A 유지. 조건을 통과해도 배포하지 않는다.
- 기준선은 앞선 replay의 전체 376/419, 378/419, 미관측 17/60, 19/60과 일치해야 한다. hash/분모/기준선 불일치 시 판정을 보류한다. 기대 점수에 맞추려고 전처리·threshold를 바꾸지 않는다.
- 시간: corpus 일회성 준비/질의 batch 임베딩/캐시된 순위 계산을 분리한다. 단일 호출 HTTP·STT·cold/warm 서비스 지연이나 사용자 시간 절감 실험이 아니다. 속도 우열 채택 근거로 쓰지 않는다.
- 비용: 기존 로컬 Mac MPS/CPU, 신규 서버비 0원. 기존 누적 비용은 미확인. 유료 실행 필요 시 중단한다.
- 공개: Git/Notion에는 집계·해시·설정·재현 명령만 기록. 원본·질의·행별 결과·임베딩은 Git 밖 private(0700/0600). 사람 검수/현장 정확도/위험 판단 승인으로 확대 해석하지 않는다.

## 다음 단계의 범위

Reranker·contrastive learning·selective abstention은 이번 세 방식 비교의 결과와 별도 train/dev 준비를 보고 결정한다. 결과를 본 2020 자료로 후속 모델을 튜닝하지 않는다. Retriever 독립 검수는 사용자 일정에 따라 보류하며 자동 승인하지 않는다.
