# 케미체크119 데이터와 모델

## 1. 핵심 결론

케미체크119의 모델은 하나의 생성형 LLM이 아닙니다. 서로 다른 책임을 가진 검색 모델과
결정적 규칙을 조합합니다.

```text
일반 물질 식별: ICIS 중심 4,300개 카탈로그
상세 MSDS 근거: 현재 KOSHA 스냅샷 9종
공식 반응성 근거: CAMEO 5,094개 물질·68개 그룹
시설 후보: ICIS·PRTR 과거 공개 취급 이력
관찰 후보: 울산소방 상태·색상·냄새·용도 중 카탈로그 연결 749 CAS
충돌 검토: 공개 검증 CAS–CAMEO 매핑 + 결정적 그룹 호환성 lookup
```

4,300개 물질 모두에 KOSHA 상세 MSDS가 있다는 뜻이 아닙니다. 시설 이력도 현재 재고를
뜻하지 않습니다.

## 2. 데이터가 적용되는 위치

| 데이터 | 사용하는 단계 | 제공하는 정보 | 제공하지 않는 정보 |
|---|---|---|---|
| KOSHA MSDS | Retriever | 현재 스냅샷 9종의 상세 MSDS 근거 | 전체 4,300종 상세 문서 |
| CAMEO Chemicals | Retriever, Rule Engine | 물질 설명, 반응성 그룹, 그룹 호환성 | 사고 발생 확률, 현장 명령 |
| ICIS 화학물질 취급현황 | Resolver | 유효 CAS와 물질명·별칭 후보 | 특정 시설의 현재 재고 |
| ICIS·PRTR 시설 통합 입력 | 시설 검색 | 업체·지역·CAS별 과거 취급·배출·이동 이력 | 현재 존재, 수량, 저장 위치 |
| 울산소방 화학물정보 | Discovery·보조 별칭 | 상태·색상·냄새·용도 기반 확인 전 후보 | 물질 확정, 서비스 지역 제한, 현재 시설 재고 |
| 프로젝트 config | Resolver, Rule Engine | 별칭 보정, 공개 검증 CAMEO 매핑, 정책 | 원천 데이터 자체 |

울산소방 데이터가 일부 포함되어도 서비스가 울산 전용인 것은 아닙니다. 일반 물질 카탈로그와
시설 후보는 전국 ICIS·PRTR 자료를 중심으로 구성합니다.

## 3. 현재 데이터 스냅샷

최근 전처리 검증 스냅샷의 핵심 규모입니다. 릴리스마다 `preprocessing_manifest.json`과
`runtime_manifest.json`을 기준으로 다시 확인해야 합니다.

| 항목 | 건수 | 해석 |
|---|---:|---|
| 릴리스 입력 | 8개 파일, 201,657행 | 현재 파이프라인이 직접 사용하는 원천 CSV |
| 통합 물질 | 4,300 | ICIS 유효 CAS 4,299개와 KOSHA-only 물질을 통합 |
| 검색 별칭 | 9,685 | CAS·국영문명·화학식·검증된 별칭 |
| 성상 검색 프로필 | 749 CAS | 울산 원천 4,378행 중 유효 CAS·현재 카탈로그·성상 연결 |
| KOSHA 상세 대상 | 현재 9종 | 공식 수집·검토 후 확장 가능, 4,300종 전체가 아님 |
| 검색 evidence | 5,858 | KOSHA와 CAMEO 검색 문서 |
| CAMEO 물질 | 5,094 | CAMEO 원자료 물질 |
| CAMEO 반응성 그룹 | 68 | 결정적 lookup용 그룹 |
| 물질–그룹 매핑 | 9,231 | CAMEO 내부 매핑 |
| 그룹 호환성 고유 조합 | 2,346 | 서수 class 조회 데이터 |
| 시설–물질 이력 후보 | 168,424 | 현재 재고가 아닌 과거 공개 이력 |

이 숫자는 모델 성능이 아니라 데이터 행 수와 커버리지입니다.

## 3.1 지원 물질 구축 우선순위

전체 카탈로그 규모와 실제 종단간 지원 범위를 혼동하지 않도록 별도의 오프라인 우선순위
파이프라인을 둡니다. 소방 사고 신호, ICIS·PRTR 과거 이력, KOSHA 상세 적재 여부,
공개 검증 CAMEO 연결 여부를 합쳐 다음 두 순위를 만듭니다.

- `demo_rank`: 현재 근거로 종단간 시연하기 좋은 순서
- `expansion_rank`: 공식 MSDS·CAMEO 연결을 다음으로 확장할 순서

두 순위는 위험 확률이나 현재 재고 확률이 아닙니다. 실행 방법과 등급 정의는
[지원 물질 우선순위 파이프라인](SUPPORT_MATERIAL_PRIORITY.md)에 있습니다.

## 4. 릴리스 원천 데이터 계약

대용량 원천 CSV는 공개 Git 저장소에 커밋하지 않습니다. 릴리스 작업은 검증된 flat
`tar.gz` bundle을 내려받아 `data/raw/`에 복원합니다.

bundle 루트에는 다음 8개 파일만 있어야 합니다.

```text
01_KOSHA_물질안전보건자료.csv
02_CAMEO_화학물질_반응성.csv
03_CAMEO_화학물질_반응성그룹_매핑.csv
04_CAMEO_반응성그룹_목록.csv
05_CAMEO_반응성그룹_호환성_고유조합.csv
06_울산소방_화학물정보.csv
13_ICIS_2024_화학물질_취급현황.csv
19_ICIS_2024_시설후보_통합모델입력.csv
```

릴리스 워크플로는 다음 조건을 검사합니다.

- HTTPS URL이며 URL 안에 인증정보나 공백이 없음
- bundle 전체 SHA-256이 Secret과 일치
- 루트의 일반 파일만 허용
- 추가 파일, 하위 경로, 중복 파일 금지
- symbolic link와 hard link 금지
- 빈 파일과 Git LFS pointer 금지
- 압축 파일 최대 1GB, 압축 해제 합계 최대 2GB

자세한 Secret과 실행 방법은 [배포 문서](DEPLOYMENT.md)를 참고하세요.

## 5. 전처리

### 5.1 데이터 감사

`chemiguard119 audit`는 필수 파일, 헤더, 행 형식, 중복 키, 결측과 예상 건수를 확인합니다.
릴리스에서는 `--include-hash`로 입력 파일 SHA-256도 기록합니다.

### 5.2 CAS 정규화와 검증

- 공백과 표기 변형 정리
- CAS 형식 확인
- 체크디지트 검증
- 유효하지 않은 CAS는 Rule 입력에서 제외
- ICIS 물질명의 세미콜론 구분 별칭을 개별 alias로 분리
- 같은 CAS 안에서 정규화 문자열이 같은 별칭 제거

문자열이 비슷하다는 이유로 CAS를 새로 만들거나 다른 CAS로 교정하지 않습니다.

### 5.3 KOSHA 상세 근거

현재 스냅샷 9종의 장·항목별 상세 내용을 evidence로 변환합니다. 빈 상세와 “정보 없음”
항목은 검색 문서에서 제외합니다. ICIS 카탈로그 물질은 명시적인 CAS join이 있을 때만 KOSHA
상세 보유 상태를 갖습니다.

전처리는 9종을 최소 회귀 기준으로 검사하지만 개수를 9개로 고정하지 않습니다. KOSHA 공식
OpenAPI에서 정확 CAS로 수집하고 검토한 행을 원천 스냅샷에 추가하면 새 물질도 같은 방식으로
적재할 수 있습니다. 별칭 보정 파일에 없는 새 CAS는 공식 KOSHA 국문명과 UN 번호를
기본값으로 사용합니다. 수집 명령과 승인 기준은
[KOSHA MSDS 공식 수집과 검토](KOSHA_COLLECTION.md)를 참고하세요.

### 5.4 CAMEO

- 물질 5,094개를 검색 evidence로 적재
- 반응성 그룹 68개 적재
- 물질–그룹 매핑 9,231개 적재
- 그룹 호환성 조합 2,346개 적재
- CAMEO ID는 숫자로 변환하지 않고 원문 문자열로 보존

CAS에서 CAMEO 물질로 연결하는 운영 crosswalk는 원자료의 내부 물질–그룹 매핑과 별도입니다.
`config/cameo_crosswalk.csv`의 `PUBLIC_SOURCE_VERIFIED` 행만 공개 근거 파일럿 대상입니다.

`config/reference_assurance_registry.json`은 위험 주장별로 CAMEO와 독립 공식기관 자료를
연결합니다. 기관별 허용 host, 독립성 그룹, 문서 내 위치, 주장과의 관계를 고정하고 파일
SHA-256을 runtime manifest와 API 결과에 포함합니다. 현재 다기관 교차증빙은
차아염소산나트륨–염산 1쌍이며, 나머지 공개 검증 14쌍은 CAMEO 단일체계 상태입니다. 이
registry는 현재 재고·실제 혼합·현장 농도나 사람 전문가 승인을 증명하지 않습니다.

### 5.5 시설 이력

시설 통합 입력에서 CAS가 정확히 일치하는 이력만 후보 테이블에 넣습니다. PRTR 결측 배출량을
0으로 채우지 않고 `NULL`로 보존합니다.

다음 해석은 금지합니다.

- 배출량 또는 이동량 = 재고량
- 과거 신고 이력 = 현재 보유
- 업체명 부분 일치 = 동일 사업장 확정
- 이력 건수 = 사고 확률

### 5.6 관찰 기반 물질 프로필

`06_울산소방_화학물정보.csv`의 상온 상태·색상·냄새·사용 용도를 CAS별로 합칩니다.

- CAS 형식과 체크디지트가 유효해야 함
- 현재 4,300개 카탈로그에 같은 CAS가 있어야 함
- 성상 필드가 최소 하나 있어야 프로필 생성
- 한 CAS의 여러 공개 행은 중복을 제거해 합침
- 릴리스 전 최소 700개 프로필을 요구해 빈 인덱스 배포를 차단

실제 재현 결과:

```text
원천 행 4,378
유효 카탈로그 연결 성상 프로필 749 CAS
```

온라인 검색은 물질명 열이 성상 순위를 왜곡하지 않도록 성상 네 필드만 FTS5 BM25로
검색합니다. “액체·냄새” 같은 일반어를 제거하고, 서로 다른 성상 영역이 두 개 이상
일치하지 않으면 후보 반환을 포기합니다.

## 6. 전처리 결과

### 6.1 SQLite

`artifacts/chemiguard119.sqlite`의 핵심 테이블은 다음과 같습니다.

| 테이블 | 역할 |
|---|---|
| `substance` | CAS별 통합 물질 레코드 |
| `alias` | Resolver 후보용 이름·CAS·화학식 |
| `substance_profile` | CAS별 상태·색상·냄새·용도와 소방청 출처 |
| `substance_profile_fts` | 관찰 후보용 SQLite FTS5 BM25 인덱스 |
| `evidence` | KOSHA·CAMEO 검색 문서 |
| `evidence_fts` | SQLite FTS5 BM25 검색 인덱스 |
| `cameo_chemical` | CAMEO 물질 정보 |
| `cameo_mapping` | CAMEO 물질–반응성 그룹 매핑 |
| `compatibility` | 반응성 그룹 조합별 class·근거 |
| `facility_candidate` | 시설–물질 과거 이력 후보 |

운영 API는 DB를 `mode=ro&immutable=1`과 `PRAGMA query_only`로 엽니다.

### 6.2 모델 artifact

| 파일 | task | 내용 |
|---|---|---|
| `resolver.joblib` | `substance_candidate_retrieval` | 별칭 행, 문자 TF-IDF vectorizer와 matrix |
| `retriever.joblib` | `official_evidence_retrieval` | evidence 행, 단어·문자 TF-IDF와 matrix |
| `runtime_manifest.json` | 릴리스 계약 | 파일 해시, 크기, schema, 버전, Git commit |

joblib은 pickle 기반이므로 외부 SHA-256과 manifest를 검증하기 전에 로드하면 안 됩니다.

## 7. 모델 1: 물질 Resolver

### 7.1 목적

신고문이나 검색창에 입력된 물질명·CAS·화학식에서 가능한 물질 후보를 찾습니다. 위험도를
예측하지 않습니다.

### 7.2 피처

- Unicode NFKC 정규화
- 소문자·공백·구두점 정규화
- 문자 2~5-gram TF-IDF
- 정확한 CAS 별도 처리
- 정확한 별칭의 CAS 모호성 보존
- 후보별 출처, 별칭 유형, KOSHA 상세 보유 여부

### 7.3 출력 원칙

- 유효 CAS 정확 일치는 식별자 일치로 반환
- 같은 표현이 여러 CAS에 있으면 `AMBIGUOUS_ALIAS`
- 유사 검색은 복수 후보를 반환
- 등록되지 않은 제품명·현장 속칭은 `UNRESOLVED` 가능
- 모든 이름 기반 결과는 현장 확인 필요
- 후보 점수는 확률이 아님

## 8. 모델 2: 관찰 기반 물질 Discovery

정확한 명칭·CAS는 Resolver가 찾고, 성상 관찰은 `substance_profile_fts`가 찾습니다. 두
후보를 합친 뒤 각 CAS에 대해 KOSHA·CAMEO 상세 근거를 엄격히 같은 CAS로 제한해 검색합니다.

```text
정확 명칭·CAS Resolver
        +
성상 FTS5 BM25
        ↓
확인 전 Top-K 후보
        ↓
후보별 같은 CAS 공식 근거
```

후보는 항상 `rule_eligible=false`이며 상세 근거가 없으면
`CAS_EVIDENCE_NOT_LOADED`로 남깁니다. 현재 smoke는 기능 연결만 검증했으며 독립 정확도
평가는 아직 없습니다.

## 9. 모델 3: 공식 근거 Retriever

### 9.1 검색 조합

Retriever는 다음 검색 결과를 Reciprocal Rank Fusion으로 결합합니다.

1. 정확한 CAS·제목 일치
2. SQLite FTS5 BM25
3. 단어 1~2-gram TF-IDF
4. 문자 3~5-gram TF-IDF

한 분기당 최대 30,000 피처를 사용하고, 매우 긴 CAMEO 본문은 학습 시 8,000자로 제한합니다.

### 9.2 CAS 제한

확인된 CAS가 있으면 결과를 그 CAS 문서로 제한합니다. 해당 CAS의 상세 evidence가 없을 때는
다른 물질의 상위 문서를 보여주지 않고 `CAS_EVIDENCE_NOT_LOADED`를 반환합니다.

### 9.3 점수 해석

RRF·TF-IDF 점수는 검색 순위를 위한 값입니다. 위험도, 사고확률, 근거의 사실성 확률로
표현하면 안 됩니다.

현재 Retriever corpus는 KOSHA·CAMEO 공식 근거입니다.

### 9.4 Grounded RAG

Retriever가 찾은 공식 문서와 완료된 CAMEO Rule 결과를 `src/chemiguard119/rag.py`가 짧은
문장으로 조립합니다. 기본 `extractive` 모드는 외부 모델 없이 원문 발췌를 사용합니다.
선택 `llm` 모드는 같은 근거만 OpenAI-compatible 서버에 보내며 모든 문장의 `source_id`와
Rule 위험등급 일치를 검사합니다. 검증·timeout·호출 실패는 extractive로 되돌아갑니다.

이는 **공식 근거 설명 RAG**입니다. 사고 내용과 당시 대응이 함께 검증된 사례 corpus가 없어
**유사 사고 사례 검색은 아직 구현하지 않았습니다.** 사례 출처, 사고 단위 중복 제거, 대응
결과 라벨과 개인정보 처리 기준이 마련된 뒤 별도 평가해야 합니다.

## 10. 모델 4: 시설 이력 검색

이 구성요소는 피해 예측 모델이 아닙니다. 시설명·주소·시도 조건으로 SQLite를 조회하고 다음
신호를 사용해 후보를 정렬합니다.

- 시설명 정확 일치 여부
- PRTR 업체 정확 일치 여부
- 공개 사고 이력 행 수
- 시설명과 CAS

모든 결과에는 다음 의미가 붙습니다.

```text
evidence_class=REPORTED_HANDLING_HISTORY
current_inventory_confirmed=false
rule_eligible=false
requires_on_site_confirmation=true
```

## 11. 모델 5: CAMEO Rule Engine

### 11.1 입력

Resolver 후보가 아니라 현장에서 각각 확인된 사고물질 CAS와 시설물질 CAS만 받습니다.

### 11.2 공개 근거 매핑

`PUBLIC_SOURCE_PILOT_V1`에서 매핑이 사용되려면 다음 조건을 모두 만족해야 합니다.

- `verification_status=PUBLIC_SOURCE_VERIFIED`
- `verification_method=EXACT_CAS_AND_FORM_ON_OFFICIAL_DATASHEET`
- 공식 CAMEO 물질 URL 존재
- 출처 제품·버전·확인 시각 존재
- 정책 파일의 허용 조건과 일치

이름 일치만 수행한 `CANDIDATE_UNVERIFIED` 행은 Rule 입력으로 사용하지 않습니다.

현재 공개 근거 파일럿에서 사용할 수 있는 crosswalk는 다음 6개 CAS입니다.

| CAS | 선택한 CAMEO 형태 | CAMEO ID |
|---|---|---:|
| `7681-52-9` | `SODIUM HYPOCHLORITE` | `4503` |
| `7647-01-0` | `HYDROCHLORIC ACID, SOLUTION` | `3598` |
| `64-17-5` | `ETHANOL` | `667` |
| `67-64-1` | `ACETONE` | `8` |
| `108-88-3` | `TOLUENE` | `4654` |
| `7440-23-5` | `SODIUM` | `7794` |

각 행은 CAMEO 공식 물질 페이지에서 페이지 제목, CAS, 물질 형태를 직접 대조했습니다.
이는 전문가 승인이 아니라 공개 출처 검증이며, 실제 충돌 스크리닝은 두 물질의 현장 확인과
원자료 반응성 그룹 조합이 모두 존재할 때만 실행됩니다.

### 11.3 계산

1. 두 CAS를 각각 CAMEO chemical ID로 연결
2. 각 물질의 모든 반응성 그룹 조회
3. 가능한 모든 그룹쌍의 compatibility 조회
4. 원자료 class 중 가장 보수적인 class 선택
5. 모든 매핑·근거 provenance를 응답에 포함

출력 `raw_class_id`는 `0`, `1`, `2`의 서수 class이며 확률이 아닙니다.

```json
{
  "type": "ORDINAL_CAMEO_COMPATIBILITY_CLASS",
  "raw_class_id": 2,
  "is_probability": false,
  "probability_percent": null
}
```

## 12. 신고문 파서와 LM Studio

기본 신고문 파서는 결정적 규칙으로 물질 표현, 사고·시설 역할, 부정 표현 등을 제한적으로
구조화합니다.

시설명·주소·지역·좌표·설비는 현재 파서가 추출하지 않으며 `location` 구조화 입력으로
받습니다. `VOICE_TRANSCRIPT`는 외부에서 이미 문자로 변환된 신고문이고 이 API가 ASR을
수행한다는 뜻이 아닙니다.

LM Studio 백엔드는 다음 실험에만 사용할 수 있습니다.

- 같은 신고문의 구조화 결과 비교
- JSON schema 준수율 측정
- 누락·환각·지연시간 측정

운영 API는 LM Studio를 호출하지 않습니다. 생성형 모델이 CAS, 충돌 class, 대응 허용 여부를
만들게 하지 않습니다.

## 13. 평가

평가 입력은 `data/evaluation/`에 있습니다.

- `resolver_regression_queries.csv`: CAS, 국영문명, 별칭, 화학식, 모호 표현
- `resolver_hint_safety_queries.csv`: 부분 문자열·다중 물질·모호 표현의 자동 CAS 힌트 잠금 회귀
- `retrieval_regression_queries.csv`: KOSHA·CAMEO 근거의 기대 출처·CAS
- `retrieval_section_regression.jsonl`: 질문별 근거 section과 사실 단위 relevance
- `incident_parser_seed.jsonl`: 신고문 구조화 시드
- `e2e_scenarios_draft.jsonl`: 실제 사고 분석 경로의 확인 gate·기권·충돌 규칙 상태 전이

최근 내부 회귀 평가 스냅샷은 다음과 같았습니다. Retriever는 자동 CAS 힌트를 포함한
전체 흐름과, 평가용 정답 CAS를 제공한 검색기 단독 진단 결과를 분리합니다.

| 평가 | 케이스 | 결과 |
|---|---:|---:|
| Resolver 단일후보 확정 정확도 | 21 | 0.9524 |
| Resolver Top-3 Recall | 21 | 1.0000 |
| Resolver MRR | 21 | 1.0000 |
| 자동 CAS 힌트 안전 통과율 | 12 | 1.0000 |
| 금지된 자동 CAS 힌트 | 12 | 0건 |
| Resolver Rule 입력 승인 위반 | 12 | 0건 |
| Retriever 전체 흐름 Recall@5 | 10 | 0.9000 |
| Retriever 전체 흐름 MRR@8 | 10 | 0.6500 |
| Retriever 단독·정답 CAS 제공 Recall@5 | 10 | 0.9000 |
| Retriever 단독·정답 CAS 제공 MRR@8 | 10 | 0.6500 |
| 자동 CAS 힌트 Coverage | 10 | 0.9000 |
| 자동 CAS 힌트 Precision when present | 10 | 1.0000 |
| E2E 안전 상태 전이 | 8 | 8/8 |
| 미확인 상태 Rule 실행 | 8 | 0건 |
| 미확인 위험 결과 노출 | 8 | 0건 |

이는 작은 내부 개발셋의 회귀 지표입니다. 현장 정확도, 전체 물질 성능, 사고 대응 성공률로
인용하면 안 됩니다. 릴리스 워크플로가 같은 평가를 다시 실행하고 결과 파일을 artifact와 함께
보관합니다. 지표 정의와 실패 분석은 [모델 평가](EVALUATION.md)를 참고하세요.

문장 안의 물질명은 Resolver와 결정적 파서가 같은 원문 span matcher로 찾습니다. 별칭
내부의 띄어쓰기 차이는 허용하지만, 다른 한글·영숫자에 붙은 부분 문자열은 exact로 승격하지
않습니다. 따라서 `염산 누출`은 염산 후보를 만들지만 `염산염`, `염산성`은 염산 CAS로
근거 검색을 제한하지 않습니다. 찾지 못한 경우에는 잘못된 CAS를 선택하는 대신 CAS 없는
일반 근거 검색과 현장 확인 요청으로 남깁니다.

## 14. 파인튜닝 위치

현재 운영 파이프라인에 파인튜닝 모델은 필수가 아닙니다. 신고문 구조화용 학습 데이터가 충분히
쌓였을 때만 선택적으로 검토합니다.

파인튜닝 전에는 다음을 확인해야 합니다.

- train·validation·test 분리
- 같은 사고·템플릿의 분할 누수 방지
- CAS·위험도·현장 명령을 정답 필드로 생성하지 않음
- hard case와 부정 표현 포함
- baseline보다 schema 준수율과 추출 성능이 실제로 개선됨

`chemiguard119 finetune-check`는 데이터 준비도만 검사합니다. 파인튜닝 여부와 관계없이 운영
Rule Engine은 결정적 공개 근거 경로를 유지합니다.

## 15. 재현 가능한 학습

```bash
chemiguard119 pipeline \
  --data-dir data/raw \
  --db artifacts/chemiguard119.sqlite \
  --resolver-model artifacts/resolver.joblib \
  --retriever-model artifacts/retriever.joblib \
  --config-dir config \
  --report-dir outputs/modeling \
  --include-hash \
  --json
```

재현성의 기준은 다음 묶음입니다.

- clean Git commit
- 원천 bundle SHA-256
- 입력 CSV별 SHA-256
- config 파일 해시
- Python·NumPy·scikit-learn·joblib 버전
- 모델 schema version
- 평가 결과
- 최종 `runtime_manifest.json`

## 16. 공식 출처

- [KOSHA MSDS 검색](https://msds.kosha.or.kr/MSDSInfo/kcic/msdssearchMsds.do)
- [ICIS 화학물질 통계 정보 공개](https://icis.mcee.go.kr/search/searchType6.do)
- [CAMEO Chemicals 소개](https://cameochemicals.noaa.gov/about)
- [CAMEO 반응성 도구](https://cameochemicals.noaa.gov/browse/react)
- [CAMEO 차아염소산나트륨 datasheet](https://cameochemicals.noaa.gov/chemical/4503)
- [CAMEO 염산 수용액 datasheet](https://cameochemicals.noaa.gov/chemical/3598)
- [공공데이터포털 ICIS 관련 OpenAPI](https://www.data.go.kr/data/15157612/openapi.do)
- [소방청 울산 화학물질 정보](https://www.data.go.kr/data/15081005/fileData.do)

각 릴리스의 실제 출처 URL과 문서 버전은 DB evidence와 config provenance를 우선합니다.

## 17. 관련 문서

- [README](../README.md)
- [아키텍처](ARCHITECTURE.md)
- [API](API.md)
- [모델 평가](EVALUATION.md)
- [배포](DEPLOYMENT.md)
- [대시보드 적용 흐름](DASHBOARD_FLOW.md)
- [안전 및 한계](SAFETY_AND_LIMITATIONS.md)
