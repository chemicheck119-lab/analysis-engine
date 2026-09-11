# 공식 울산 데이터의 Sparse·Dense·RRF 동조건 비교

평가일: 2026-09-11. **Dense와 RRF는 후속 검증 후보로 조건부 유지하고, 현재 서비스의 Sparse 기본값은 그대로 둡니다.**

공식 원본에서 복원한 같은 419개 표현–CAS 쌍에 세 방식을 적용했습니다. 미관측 60건에서 Top-3 적중은 **Sparse 19건 → Dense 24건 / RRF 21건**입니다. 과거 20개 실패 표본의 기각 판정을 이번 전체 비교에 그대로 적용하면 안 됩니다. 모집단과 exact 보존 정책이 다릅니다.

## 1. 쉬운 설명

- **Sparse**: 이름의 글자가 얼마나 비슷한지 찾습니다. 정확한 이름·CAS 일치 규칙도 있습니다.
- **Dense fallback**: 정확한 이름이 없을 때 BGE-M3가 표현의 의미를 숫자 벡터로 바꿔 비슷한 후보를 찾습니다.
- **RRF fallback**: Sparse와 Dense의 서로 다른 점수를 직접 더하지 않고 두 후보 목록의 순위를 합칩니다.

이번 Dense/RRF는 정확한 이름 찾기를 없애는 방식이 아닙니다. 세 방식 모두 exact·모호성·식별자 거절 경로를 보존하고 **같은 46개 fallback 입력에서만 후보 검색을 바꿉니다.** 물질 후보를 정답 CAS 확인으로 승격하지 않습니다.

BGE-M3는 다국어·다기능 검색 모델이지만 이번에는 **Dense 벡터 기능만** 사용했습니다. BGE-M3 자체의 learned sparse·ColBERT 기능을 비교한 것이 아닙니다. [개발기관 모델 카드](https://huggingface.co/BAAI/bge-m3)

## 2. 공식 자료와 동일 조건

| 항목 | 고정 조건 |
|---|---|
| 출처 | 사용자 제공 한국소방안전원 울산 화학사고별 유해물질 판단 정보 ZIP |
| 데이터 범위 | 2015~2020년 원본 1,868행 → 기존 전처리 유효 기록 1,530개 |
| 과거 이력 | 2015~2019년 1,111개 기록; 현재 artifact 학습 cutoff 2019 |
| 평가 단위 | 2020년 정규화 표현–CAS 쌍 419개; 독립 사고 419건이 아님 |
| 미관측 slice | 과거 이력에 없는 표현–CAS 쌍 60개; 그중 과거 이력에 없는 CAS의 사례 58개 |
| 반복 표현 | 419개 중 359개는 과거 표현–CAS 쌍이 반복됨 |
| 공통 모델·정답 | 같은 Resolver v4 artifact·공식 표 CAS·전처리·Top-3·exact/거절 정책 |
| Sparse | char n-gram TF-IDF, minimum_score=0.20 유지 |
| Dense | BGE-M3 local revision 고정, CLS/L2/float32, max_length=32, batch_size=64 |
| RRF | 각 Top-100, k=60, 동일 가중치 1:1; Sparse pool도 minimum_score=0.20 |
| 별칭 | 기존 artifact만 사용. Dense 5,626개 별칭·4,300 CAS, source-only exact 전용·숫자 CAS 별칭 제외 |
| 새 학습·튜닝 | 없음. 평가 정답을 별칭·threshold·가중치 수정에 사용하지 않음 |

자료 상세 링크: [소방안전 빅데이터의 해당 상품](https://www.bigdata-119.kr/goods/goodsInfo?goods_mng_sn=5). 이번 도구로 포털 상세 페이지의 최신 내용을 재확인하지 못했으므로 웹의 최신판이라고 주장하지 않습니다. 사용자 ZIP의 원본·파생·manifest SHA를 고정했습니다. 원본 재배포 권한은 검증하지 않아 Git/Notion에 원문을 올리지 않습니다.

공식 표의 CAS는 **출처에 기재된 정답**입니다. CAS checksum은 형식 검사이고 화학적 관계의 독립 전문가 승인과는 다릅니다. 전체 기간의 다중 CAS 공유 표현 제외 규칙은 기존 평가 재현을 위해 유지했으며, 사건 단위 독립·완전 비공개 평가로 표현하지 않습니다.

## 3. 실제 측정 결과

Top-1은 첫 후보, Top-3는 세 후보 안에 정답이 있는 비율입니다. MRR@3은 정답이 1·2·3위이면 각각 1·1/2·1/3점을 주고 없으면 0점인 평균입니다. **사람 확인 성공률이 아닙니다.**

### 전체 공식 평가 · 419쌍

| 구성 | Top-1 | Top-3 | MRR@3 |
|---|---:|---:|---:|
| A Sparse | 376/419 · 89.74% | 378/419 · 90.21% | 0.898966 |
| B Dense fallback | 378/419 · 90.21% | 383/419 · 91.41% | 0.906921 |
| C RRF fallback | 376/419 · 89.74% | 380/419 · 90.69% | 0.901750 |

### 미관측 표현 · 60쌍

| 구성 | Top-1 | Top-3 | MRR@3 |
|---|---:|---:|---:|
| A Sparse | 17/60 · 28.33% | 19/60 · 31.67% | 0.294444 |
| B Dense fallback | 19/60 · 31.67% | 24/60 · 40.00% | 0.350000 |
| C RRF fallback | 17/60 · 28.33% | 21/60 · 35.00% | 0.313889 |

Dense는 미관측 Top-3 **+5건·+8.33%p**, RRF는 **+2건·+3.33%p**입니다. 전체 점수보다 이 slice가 현재의 어려움을 잘 보여줍니다. 가장 높은 Dense도 60건 중 36건은 Top-3 밖입니다.

### 고친 정답과 잃은 정답 · 같은 60쌍을 쌍별 비교

| 기준 | Dense가 고친/잃은 수 | RRF가 고친/잃은 수 |
|---|---:|---:|
| 첫 후보 Top-1 | 3 / 1 · 순증 2 | 1 / 1 · 순증 0 |
| 세 후보 Top-3 | 6 / 1 · 순증 5 | 2 / 0 · 순증 2 |

전체 평균의 비회귀와 **모든 개별 정답의 보존**은 다릅니다. Dense는 기존 Top-3 정답 1개를 잃었습니다. 통계적 유의성·다른 지역 일반화·현장 유효성은 확정하지 않습니다.

미관측 CAS 58쌍의 Top-1/Top-3는 Sparse 16/58·18/58, Dense 18/58·23/58, RRF 16/58·20/58입니다. 전체 수치와 별도 분모입니다.

## 4. 기권·확인 정책과 남은 실패

| 실제 검사 | A Sparse | B Dense | C RRF |
|---|---:|---:|---:|
| 전체 후보 반환 | 417/419 | 419/419 | 419/419 |
| 미관측 후보 반환 | 58/60 | 60/60 | 60/60 |
| 잘못된 단일 exact 후보 | 0 | 0 | 0 |
| Rule 입력 자격 플래그 위반 | 0 | 0 | 0 |
| 사람 확인 요구/현재 재고 미확인 정책 위반 | 0 | 0 | 0 |
| 공통 Gate 변경·source-only fuzzy 누출 | 0 / 0 | 0 / 0 | 0 / 0 |

- **후보를 많이 반환한다고 더 안전한 것은 아닙니다.** 이번 Dense/RRF에는 검증된 기권 threshold가 없습니다. 후보 반환률 100%를 안전한 기권 성능이라고 부르지 않습니다.
- Sparse가 빈 후보였던 2개 중 Dense/RRF가 정답을 찾은 것은 1개이고, 나머지 1개는 artifact에 정답 CAS가 없는데도 오답 후보를 반환했습니다. 후보 반환 자체를 성공으로 세지 않습니다.
- 정답 CAS가 현재 artifact에 없는 사례는 **12/419, 미관측 중 12/60**입니다. 현재 후보 집합만 재정렬해서는 이 12개 정답을 찾을 수 없습니다. 이를 뺀 점수를 대표 성능으로 쓰지 않습니다.
- 정확/모호 경로 보존은 공식 평가 373개, fallback은 46개입니다. 미관측 60개 중 보존 14개/fallback 46개입니다.
- 내부 21개 회귀는 세 방식 모두 21/21이나 **21개 전부 공통 exact/모호 경로**입니다. Dense fallback의 폭넓은 회귀 검증이 아닙니다.
- 기존 Sparse hint 안전 회귀는 별도 12/12, unsafe hint·wrong hint·Rule 입력 자격 위반 0건입니다. 새 Dense를 실제 API에 연결한 E2E 실험이 아닙니다.
- 위 표는 후보 플래그와 검색 정책 검사입니다. **실제 Rule 호출·현장 안전·전술 승인·신고 음성 정확도를 측정한 것이 아닙니다.**

## 5. 과거 기록과 왜 결론이 달라졌나

| 비교 자료 | 범위·설정 | 정확한 현재 해석 |
|---|---|---|
| 과거 Hybrid probe `1715e1c` | 이미 공개된 2020 실패 20개, raw Dense, RRF Sparse pool minimum_score=0.0 | Top-3 5%/15%/5%, 그 작은 probe에서 equal RRF 개선이 없어 기각 |
| 이번 실행 `e133919` | 공식 419쌍·미관측 60쌍, Dense/RRF 모두 exact 보존, Sparse pool=0.20 | Top-3 90.21%/91.41%/90.69%, 조건부 후속 검증 후보 |
| 과거 Sparse 최종 artifact `e80d612…` | 2026-08-02 보고서의 과거 파일 | 현 artifact `2696fa7…`과 bytes가 다름. 집계·수치 일치는 확인했지만 byte-identical 과거 모델 재현은 아님 |

**새 표는 같은 실행 내 세 구성을 비교한 결과입니다.** 과거 20개 표와 직접 빼서 모델 개선폭이라고 하지 않습니다. 새 모델이나 threshold를 결과에 맞춰 튜닝하지 않았습니다. 이전 기각을 숨기지 않고 평가 범위와 정책 변경을 함께 기록합니다. 당시 2018 validation artifact는 여전히 확보되지 않았습니다.

## 6. 채택 판단과 다음 단계

사전 계획 커밋 `4185f92`의 기준: 미관측 Top-3 개선 + 전체/미관측/내부 Top-1·Top-3 집계 비회귀 + 확인 정책 위반 0건. 두 구성 모두 충족하므로 판정은 `CONDITIONAL_FURTHER_VALIDATION_ONLY`입니다.

**현재 API·기본 모델은 변경하지 않습니다.** 다음 연구 후보로는 Dense fallback을 우선 검토하되, 기존 정답 손실·무조건 후보 반환·카탈로그 결손을 별도 문제로 다룹니다.

1. 2015~2019 이력/유효한 공개 별칭에서만 train/dev hard negative와 미등록 제품 표현을 구성합니다. 이미 관찰한 2020 결과로 튜닝하지 않습니다.
2. 후보 재정렬과 기권 기준을 개발 구간에서 고정합니다. 더 큰 모델이나 여러 LLM 추가부터 시작하지 않습니다.
3. 새로운 잠금 평가와 실제 API 확인/취소 Gate 검증이 있어야 runtime 채택을 검토합니다. 추가 공식 평가 자료가 없으면 일반화 판단을 유보합니다.
4. Retriever #33의 독립 qrel 검수는 사용자 일정에 따라 보류합니다. 모델이나 에이전트가 사람 승인을 대신 작성하지 않습니다.

| 기능·주장 | 사실 상태 |
|---|---|
| 공식 데이터 동조건 비교 도구·집계 보고 | 부분 구현 또는 개발용 데모 — 로컬 702 tests 통과, PR CI 판정은 PR에서 확인 |
| Dense/RRF 실제 서비스 연결 | 설계 완료·구현 전 |
| 일반화 개선·안전한 기권·현장 안전성 | 검증되지 않은 가설 |
| 현행 Sparse 모델 유지 | 구현 완료 — 이번 변경은 runtime에 연결하지 않음 |

## 7. 환경·비용·재현

- Mac Apple M4·24GiB, Python 3.11.15, Torch 2.9.0, Transformers 4.57.6, NumPy 2.3.5, MPS. 학습·GPU 서버·유료 LLM·GCP 변경 없음.
- 신규 서버 비용 **0원**. 기존 누적 비용은 확인하지 않았습니다.
- corpus 임베딩 5,626개: 40.898초. 공식+내부 질의 batch 440개: 2.656초.
- 총 임베딩 입력 6,066개 중 276개가 max_length=32를 넘었습니다. 긴 별칭 잘림의 영향을 분리 검증하지 않았으며, 결과를 본 뒤 길이를 바꾸지 않았습니다.
- Sparse Top-3 440회 합계 0.245초, Dense 캐시 순위 46회 합계 0.114초, RRF fusion 46회 합계 0.002초. **임베딩·준비 비용을 제외한 캐시 연산을 전체 서비스 속도라고 비교하면 안 됩니다.** cold/warm 반복·HTTP·STT·사용자 과업 시간은 미측정입니다.

승인된 private 원본과 이미 설치된 실험용 Torch/Transformers 환경이 필요합니다. 운영 dependency에 Torch를 추가하지 않습니다. 출력 디렉터리는 새 Git 밖 경로여야 합니다. 설정 변경 없이 재현하려면 실행 코드 커밋 `e1339192aa44d1815fcaabb7569aa5834ef70012`를 사용합니다.

```bash
PYTHONPATH=src HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TOKENIZERS_PARALLELISM=false \
  /실험용/venv/bin/python scripts/experiments/compare_ulsan_resolvers.py \
  --source-dir /비공개/ulsan-source-replay-20260911-v1 \
  --model /비공개/action-brief-local-v1/resolver.joblib \
  --embedding-model /로컬/HuggingFace/snapshots/5617a9f61b028005a4858fdac845db406aefb181 \
  --regression data/evaluation/resolver_regression_queries.csv \
  --safety data/evaluation/resolver_hint_safety_queries.csv \
  --private-output-dir /비공개/새-비교-결과-폴더

python -m pytest
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python scripts/contracts/export_contracts.py --check
```

평가 입력/원본은 [앞선 원본 재현 절차](ULSAN_SOURCE_REPLAY_RESULTS.md), 고정 기준은 [사전 계획](ULSAN_RESOLVER_COMPARISON_PLAN.md), 행별 원문 없는 결과는 [집계 JSON](../data/evaluation/ulsan_resolver_comparison_2026-09-11.json)을 사용합니다. 실제 artifact 평가와 synthetic/mock 단위 테스트는 별개입니다.

### 핵심 SHA-256

| 대상 | SHA-256 |
|---|---|
| 공식 ZIP | `d0ee041579a953084f8037763ac7d0a3d901d11278a6854d348374c69363f976` |
| 공식 원본 CSV | `b7cabc5dba297d45dcea4150e9d9d6c41c51e3129eb9614f5e041bfa08077a5b` |
| Resolver | `2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff` |
| BGE-M3 weights | `b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38` |
| 평가 코드 | `51443c44f5cc351924d06eaf2c07e3661cca01100dadd889564e00b096c4109f` |
| 비공개 행별 결과 | `892f035f4313fef50e47878257ca2db4e5ad92953231ec2313c09b75ac638bea` |
| 공개 집계 JSON | `31a83e47e1d6d61a1ff0dc9157608fc1b4296ec9abc6ea9fe44ea8b7094e5aeb` |

전체 manifest·토크나이저·corpus hash는 집계 JSON에 포함합니다. 원본·전사문·주소·행별 질의·모델 가중치는 Git/Notion에 포함하지 않습니다.
