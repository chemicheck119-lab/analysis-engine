# 평가 V2: 공모전 타당성에서 현장 파일럿까지

이 문서는 케미체크119의 작은 내부 회귀셋을 실제 정확도처럼 해석하지 않고, 공모전 발표와
제한된 현장 파일럿에서 검증 가능한 평가 체계로 확장하기 위한 기준입니다.

## 1. 결론부터 보기

현재 알려진 `21건·10건·6건`은 표본 수를 통계적으로 설계해 수집한 평가셋이 아닙니다.
2026-07-23 커밋
[`6246c49`](https://github.com/chemicheck119/llm/commit/6246c49)에서 코드 회귀 확인과
데이터 형식 예시를 위해 함께 추가된 파일의 행 수입니다.

| 표현 | 실제 파일 | 정확한 의미 |
|---|---|---|
| 물질 식별 평가 21건 | `resolver_regression_queries.csv` | 카탈로그에 이미 있는 정확 표현 위주의 내부 회귀 21건 |
| 근거 검색 평가 10건 | `retrieval_regression_queries.csv` | 같은 출처·CAS 문서 묶음 도달 여부를 보는 내부 회귀 10건 |
| 신고문 파서 평가 6건 | `incident_parser_seed.jsonl` | `DRAFT` JSON 형식 시드 6건. 파서 성능평가가 아님 |

따라서 현재 저장소에서 정직하게 말할 수 있는 문장은 다음과 같습니다.

> 내부 회귀 검사는 통과했지만, 실제 현장 일반화 정확도는 아직 평가하지 못했습니다.

## 2. 왜 현재 숫자로 정확도를 주장할 수 없는가

### 2.1 Resolver 21건

- 21건 모두 `DRAFT_INTERNAL_REGRESSION`입니다.
- 9개 CAS만 포함합니다.
- 현재 artifact의 CAS·별칭 exact 항목을 다시 조회하는 형태입니다.
- 미등록 제품명, 실제 오타, 음성인식 오류, 여러 물질, 무관 입력이 없습니다.
- `암모니아`처럼 한 표현이 여러 CAS를 가리키는 안전한 후보 반환도 단일 정답과 다르면
  실패처럼 계산됩니다.

현재 `20/21 = 0.9524`는 현장 정확도가 아니라 해당 회귀 파일에 대한 결과입니다. 20/21의
95% Wilson 신뢰구간은 대략 77.3%~99.2%로 넓습니다.

### 2.2 Retriever 10건

- 10건 모두 `DRAFT_INTERNAL_REGRESSION`입니다.
- 정답 판정이 주로 `출처 + CAS (+ CAMEO ID)` 일치입니다.
- “보호구” 질문에 같은 CAS의 “제품명” 장이 반환돼도 정답으로 처리될 수 있습니다.
- 관련 section, `evidence_id`, relevance 등급, 답변 불가 정답이 없습니다.

즉 현재 Recall·MRR은 “같은 물질 문서 묶음에 도착했는가”에 가깝고 “질문에 답하는 근거
구절을 찾았는가”를 충분히 측정하지 못합니다. 10/10이어도 실패율의 95% 상한은 약 27.8%라
상용 근거가 되지 않습니다.

### 2.3 Parser 시드 6건

- `train=3`, `valid=1`, `locked_test=2`인 데이터 형식 시드입니다.
- 6건 모두 metadata의 `review_status=DRAFT`입니다.
- 승인 데이터는 0건이며 파서 precision·recall·F1 evaluator가 없습니다.
- `valid`와 `locked_test`에 같은 `template_group`도 존재합니다.

따라서 “파서 평가 6건”이 아니라 “파서용 JSON 시드 6건, 성능평가 없음”이라고 표기합니다.

## 3. 평가를 세 층으로 분리한다

### 층 1. CI 회귀 검사

목적은 이미 고친 버그가 다시 생기는지 빠르게 찾는 것입니다.

- 현재 21건·10건·6건과 자동 CAS 힌트 안전 사례가 여기에 속합니다.
- 합성·내부 데이터 사용이 가능합니다.
- 결과는 `내부 회귀`로만 표시합니다.
- 현장 정확도나 상용 성능으로 인용하지 않습니다.

### 층 2. 독립 보류 검증

목적은 선택한 모델·검색 구성의 일반화 성능과 실패 유형을 비교하는 것입니다.

- 모델 선택에 사용하지 않은 locked set을 사용합니다.
- 출처, 라벨러, 검수자, 중복 그룹과 split을 기록합니다.
- 전체 평균뿐 아니라 입력 유형별 최악 그룹과 95% 신뢰구간을 보고합니다.
- BM25, TF-IDF, 임베딩, reranker는 정확히 같은 locked set에서 비교합니다.

### 층 3. 제한된 현장 파일럿

목적은 실제 출동 문구·태블릿 사용 흐름·서버 지연·오류 상태를 함께 확인하는 것입니다.

- 개인정보를 제거한 shadow 요청부터 시작합니다.
- AI 결과가 실제 의사결정에 영향을 주지 않는 병행 관찰 단계가 먼저입니다.
- 화면이 후보와 확정 결과를 혼동하게 만드는지 사용자 과업 시험을 포함합니다.
- 배포 후 데이터 변화와 미지원 물질 비율을 지속적으로 기록합니다.

층 1 통과를 층 2 또는 층 3 통과로 표현하지 않습니다.

## 4. 모델 하나의 정확도가 아니라 여섯 가지 타당성을 측정한다

### 4.1 데이터 변환 정합성

약 4,300개 카탈로그 전체에 대해 다음을 전수 검사합니다.

- 원천 CAS와 artifact CAS 일치
- CAS checksum
- 표준명·공식 별칭 누락
- 중복 레코드
- 동일 별칭의 다중 CAS
- 원천 행 수와 정제 행 수 차이
- 원본·정제 artifact checksum과 생성 코드 버전

이 결과는 `데이터 정합성`이며 모델 정확도와 분리해 보고합니다.

### 4.2 물질 식별: 맞히기보다 잘못 확정하지 않기

정답을 단일 CAS 하나가 아니라 다음처럼 정의합니다.

```text
gold_cas_set
expected_decision = CONFIRM | RETURN_CANDIDATES | REJECT
ambiguity_reason
source
reviewer
duplicate_group
split
```

필수 유형:

| 유형 | 예시 |
|---|---|
| 정확 입력 | 표준명, CAS, 공식명 |
| 표현 변화 | 별칭, 화학식, 영문, 띄어쓰기, 괄호 |
| 현장 노이즈 | 오타, 음성인식 오류, 특수문자 |
| 복합 입력 | 문장형, 다중물질, 사고·시설 역할 혼합 |
| 모호 입력 | 제품명, 혼합물, 짧은 표현 |
| 거부 입력 | 미등록, 무관, 악의적 입력 |

필수 지표:

- Top-1 Accuracy와 Top-3 Recall
- 자동확정 Precision
- 잘못된 CAS 자동확정률
- 미등록 거부율
- 모호성 보존율
- Coverage–Accuracy 곡선
- 유형별·최악 그룹 성능
- 평균·p95 지연시간
- 95% 신뢰구간

### 4.3 신고문 파서: 생성이 아니라 필드 추출

필수 지표:

- JSON schema 준수율
- 물질 span micro/macro F1
- 사고물질·시설물질 role F1
- 긍정·부정·가능성 assertion F1
- 사고유형 macro-F1
- 업체·지역·설비 exact match
- 원문에 없는 물질 생성률
- 확인 필요 상태 recall
- 금지된 위험 판정 필드 출력 건수

같은 사고·템플릿·단순 문장 변형은 한 split에만 배치합니다.

### 4.4 근거 검색: 같은 CAS가 아니라 맞는 section

각 질의의 정답은 다음 단위로 작성합니다.

```text
relevant_evidence_id
relevant_section
relevance_grade = 0..3
answerable
required_fact_ids
```

필수 의도:

- 위험성
- 응급조치
- 화재 대응
- 누출 대응
- 취급·저장
- 보호구
- 안정성·반응성
- 운송·폐기
- 여러 문서가 필요한 질문
- 근거 없음·다른 물질·답변 불가

필수 지표:

- nDCG@5
- Recall@5, Precision@5, MRR
- 같은 CAS지만 잘못된 section 반환률
- 잘못된 CAS 문서 반환률
- 답변 불가 질의의 기권 성능
- URL·인용 coverage
- 평균·p95 지연시간

### 4.5 충돌 엔진: 통계 모델이 아니라 규칙 완전성

CAMEO 충돌 엔진에는 일반적인 “정확도”보다 다음 검사가 적합합니다.

- 공개 검증 지원 CAS의 모든 고유 물질쌍 실행
- 물질 순서를 바꿔도 같은 결과인지 확인
- 동일 물질, 잘못된 CAS, 미지원 CAS, 근거 없는 조합 처리
- 확인 레코드 두 건이 없을 때 실행되지 않는지 확인
- CAMEO 원자료 ID·URL·규칙 버전·checksum 연결
- 서수 등급을 확률로 변환하지 않는지 확인

지원하지 않는 조합은 추측하지 않고 `INSUFFICIENT_EVIDENCE`로 반환합니다.

### 4.6 대시보드 통합 시나리오

모델별 평가가 좋아도 화면이 후보를 확정 결과처럼 표시하면 서비스는 안전하지 않습니다.
다음 전체 흐름을 하나의 시나리오로 검사합니다.

```text
신고문
→ 사고물질 후보
→ 업체 과거 이력 후보
→ 현장 확인 게이트
→ CAMEO 규칙 실행 또는 보류
→ section 근거
→ 대시보드 표시 상태
```

확인 전에는 위험등급·구체적 반응·대응 권고를 표시하지 않는 것이 필수 합격 조건입니다.

## 5. 데이터 규모 로드맵

아래 수치는 성능을 자동 보장하는 마법의 기준이 아니라 검증력을 키우기 위한 수집 목표입니다.

| 단계 | Resolver | Parser locked test | Retriever section qrels | E2E 시나리오 |
|---|---:|---:|---:|---:|
| 공모전 검증팩 | 300+ | 200+ | 120+ | 100+ |
| 제한 파일럿 후보 | 1,200+ | 400+ | 400+ | 200+ |

공모전 검증팩의 Resolver 300건 중 최소 100건은 미등록·모호·제품명, 50건은 다중물질·역할
혼합으로 구성합니다. 파일럿 후보에서는 최소 200개 고유 CAS와 안전 hard case 300건 이상을
포함합니다.

위험한 자동확정 실패가 0건이어도 “오류율 0%”라고 쓰지 않습니다. 독립 사례 300건에서
0건이면 이항분포의 rule of three에 따라 95% 실패율 상한이 약 1%입니다.

## 6. 라벨링과 검수

모든 평가 행에 다음 provenance를 기록합니다.

- `case_id`
- 원문 출처·수집일·사용 조건
- 라벨러·검수자
- `review_status`
- `split`
- `duplicate_group`
- 지원 물질 범위

근거 검색 relevance는 두 사람이 독립적으로 0~3 등급을 부여하고, 불일치는 제3 검수자가
조정합니다. Cohen’s kappa 또는 Krippendorff’s alpha로 일치도를 기록합니다.

LLM은 초벌 후보·오류 군집화·라벨 누락 검사에만 사용할 수 있습니다. 최종 gold label을
LLM 하나가 결정하면 안 됩니다. 비전문가 이중 검수라면 `DOUBLE_REVIEWED_NON_EXPERT`로
정직하게 기록합니다.

## 7. 공모전 참신성과 최신 AI 기술

케미체크119의 추천 주제는 **근거보정형 신경-기호 AI**입니다.

> 다른 AI는 질문에 답하지만, 케미체크119는 답하면 안 되는 순간을 먼저 판별합니다.

기능 이름은 **증거 잠금형 화학충돌 AI(Evidence-Gated Response Loop)**로 정의합니다.

```text
규칙 파서
→ exact CAS·별칭 + sparse 검색
→ 후보 집합과 기권
→ ICIS·PRTR 과거 이력 후보
→ 현장 확인 두 건
→ CAMEO 결정론 규칙 잠금 해제
→ section 근거와 provenance
→ 선택적 LLM 요약
```

### 채택할 최신 기술

| 기술 | 적용 조건 |
|---|---|
| 하이브리드 검색 | BM25 대비 locked set의 nDCG·Recall을 개선하고 잘못된 CAS 문서율을 높이지 않을 때 |
| Cross-Encoder reranker | CPU p95·메모리 예산 안에서 section relevance를 실제 개선할 때 |
| 선택적 예측·기권 | 모호·미등록 입력을 단일 CAS로 강제 확정하지 않는 핵심 정책으로 즉시 적용 |
| Conformal 후보 집합 | 대표성 있는 calibration set을 확보한 뒤 목표 coverage를 실측할 때 |
| provenance graph | CAS→업체 이력→확인 레코드→규칙→근거 URL 경로를 감사 가능하게 연결 |
| 소형 LLM structured output | 검색된 근거 안의 JSON 정리만 수행하고 장애 시 규칙 기반 경로로 fallback |

### 지금 채택하지 않을 기술

- 자율 Agent가 충돌 여부나 대응 절차를 결정하는 구조
- GraphRAG를 핵심 안전 판정 경로로 사용하는 구조
- 데이터가 부족한 상태에서 임베딩·LLM을 파인튜닝하고 성능 향상으로 홍보하는 방식
- LLM judge 하나로 안전 정답을 만드는 평가

최신성은 모델 이름이 아니라 **기권, 근거 추적, 결정론 규칙, 사람의 확인, 재현 가능한
평가**를 하나의 운영 흐름으로 연결하는 데서 만듭니다.

## 8. 대시보드 적용 계약

첨부된 대시보드의 신고문 입력 직후 화면처럼 현장 확인 전 `중간·낮음`, 구체적 반응,
“시설 내 충돌 가능 물질”을 확정 결과처럼 표시하면 안 됩니다.

### 확인 전

| 현재 표현 | 사용할 표현 |
|---|---|
| 대응충돌검토 결과 | 물질 후보 확인 필요 |
| 사고 물질 | 신고문에서 추출한 사고물질 후보 |
| 시설 내 충돌 가능 물질 | 과거 공개 이력 기반 시설물질 후보 |
| 2차 사고 위험 분석 결과 2건 | 현장 확인이 필요한 시설물질 후보 2건 |
| 중간·낮음·구체적 위험 | 표시하지 않음 |

이때 API 상태는 `AWAITING_*_CONFIRMATION`, `conflict_review.executed=false`여야 합니다.

### 확인 후

용기 라벨, 현장 MSDS, 운송 문서, 계측기 결과, 대원 관찰 등으로 사고물질과 시설물질 CAS를
각각 확인한 뒤에만 다음을 표시합니다.

- CAMEO 서수 등급과 `is_probability=false`
- 구체적 반응
- 근거 URL·규칙 버전
- `expert_reviewed=false`
- `최종 판단: 현장 지휘관`

`LOW`는 “알려진 유해 반응 없음”이며 안전 보장이 아니라는 설명을 함께 표시합니다.

현재 v1 통합 API는 시설물질 확인 객체와 `conflict_review`가 각각 하나입니다. 화면처럼 여러
시설물질쌍을 한 번에 검토하려면 versioned batch 계약이 필요하며 이는 하위 호환성 검토 후
별도 PR에서 진행합니다. 그 전에는 여러 항목을 **후보**로만 표시합니다.

기계 판독 가능한 화면 규칙은
[`contracts/model-api-integration-v1.json`](../contracts/model-api-integration-v1.json)의
`presentation_policy`에 고정합니다.

## 9. 상용화를 막는 현재 P0

1. 독립 보류 평가셋과 실제 현장 shadow 검증이 없습니다.
2. 상세 KOSHA 근거와 공개 검증 CAMEO 지원 범위가 좁습니다.
3. KOSHA는 제공 MSDS가 참고용이며 외부·상업적 이용 시 저작권 문제가 생길 수 있다고
   안내합니다. 원문 재배포 전 별도 허가 또는 공식 Open API 이용 조건을 확인해야 합니다.
4. 여러 시설물질쌍 batch API와 화면 계약은 아직 v1에 없습니다.
5. 실제 스테이징 배포·부하·장애·롤백 검증이 없습니다.
6. 대응 문장별 section 인용 계약이 없어 화면의 “예상 대응”을 AI 권고로 표시할 수 없습니다.

따라서 현재 단계의 정확한 표현은 **공개 근거 기반 공모전 파일럿**입니다. 상용 준비 완료나
현장 검증 완료로 표시하지 않습니다.

## 10. 공식 근거와 원 논문

- [NIST AI Risk Management Framework 1.0](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-ai-rmf-10)
- [NIST Generative AI Profile](https://www.nist.gov/publications/artificial-intelligence-risk-management-framework-generative-artificial-intelligence)
- [NOAA CAMEO 반응성 그룹](https://m.cameochemicals.noaa.gov/browse/react)
- [KOSHA MSDS 검색과 이용 안내](https://msds.kosha.or.kr/MSDSInfo/kcic/msdssearchMsds.do)
- [KOSHA MSDS Open API](https://msds.kosha.or.kr/MSDSInfo/kcic/openapi.do)
- [ICIS 화학물질 통계 공개](https://icis.mcee.go.kr/search/searchType6.do)
- [BEIR: Heterogeneous Information Retrieval Benchmark](https://arxiv.org/abs/2104.08663)
- [BGE-M3: Dense·Sparse·Multi-vector Retrieval](https://aclanthology.org/2024.findings-acl.137/)
- [RAGAS: Reference-free RAG Evaluation](https://aclanthology.org/2024.eacl-demo.16/)
- [ARES: Automated RAG Evaluation](https://aclanthology.org/2024.naacl-long.20/)
- [Conformal Risk Control for Selective Prediction](https://proceedings.mlr.press/v244/cattelan24a.html)
- [W3C PROV-O](https://www.w3.org/TR/prov-o/)
- [W3C SHACL](https://www.w3.org/TR/shacl/)

RAGAS·ARES·LLM judge는 평가 보조 도구일 뿐 화학 안전 gold label을 대체하지 않습니다.
