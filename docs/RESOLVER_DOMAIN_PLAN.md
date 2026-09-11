# Resolver 공식 별칭·재정렬·도메인 학습 사전 계획

상태: **설계 완료·구현 전**. 새 실험 결과 전에 커밋한다. 기준 커밋 `af10f87`, 이슈 #25.

## 목표와 경계

공식 별칭 누락, Top-20 안의 순위 오류, Top-20 밖의 검색 실패를 각각 분리한다.
현재 Sparse API와 2-CAS Gate는 수정하지 않는다. 모든 새 산출물은 오프라인 후보이며
자동 CAS 확정·Rule 실행·현장 안전성·운영 채택을 주장하지 않는다.
추가 서버 비용 0원: 로컬 Mac M4 24GiB MPS/CPU만 사용한다. 과거 누적 비용은 미확인.

## 1. 공식 자료 보강 가능성

- 현재 DB 프로필 749 CAS와 검색 alias의 차이를 감사한다. 프로필 한글명은 ICIS 이름을
  우선하는 가공 필드일 수 있으므로 전부 울산 원문 이름으로 재출처 표시하지 않는다.
- 소방청 울산시 화학물 데이터(15081005, 2021-01-15 기준, 포털 4,378행)의 공개 CSV를
  로그인 없는 공식 다운로드로 확보한다. 다운로드 최대 10MB, timeout 60초, 재시도 0.
- 포털은 무료·이용허락범위 제한 없음으로 표시(2026-09-11 확인).
  원문은 private에만 보관하며 CAS checksum, 복합 CAS, 빈 이름, 동일 표현의 다중 CAS,
  기존 corpus 중복을 검사한다. 이름 구분은 명시적인 세미콜론만 사용한다.
- 전체 원문을 동일 규칙으로 처리하며 2020 오답의 CAS만 골라 보강하지 않는다.
- 새 이름은 기존 fuzzy 허용 CAS에만 실험용 Dense corpus로 추가한다. 새 CAS 및 기존
  exact-only CAS는 보강 가능 수만 보고하고 격리한다. 새 이름으로 exact Gate를 넓히지 않는다.
- 기준 corpus와 보강 corpus의 Dense Top-1/3/20을 비교한다. 2021 자료에 2020 표현이
  직접 있는 경우를 별도 집계한다. 이는 사후 자료 보강 효과이지 독립 일반화 성과가 아니다.

## 2. 고정 Top-20 재정렬

- 이전 고정 BGE-M3 corpus·query 벡터와 공통 Sparse Gate를 재사용한다.
- BGE Dense의 **CAS Top-20**을 BAAI/bge-reranker-v2-m3로 재정렬한다. 후보마다 기존
  Dense가 고른 별칭 한 개만 query와 쌍으로 입력한다. 정답·CAS 번호는 모델 입력하지 않는다.
- revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`, Apache-2.0,
  safetensors 약 2.27GB, 다운로드 파일 합계 최대 2.4GB, 동시 다운로드 1.
- float32, MPS, max_length=128, batch=16, 단일 실행 최대 30분, OOM 시 중단.
  logit은 확률이 아니며 threshold 학습/튜닝 없음. 동점은 기존 순위를 보존한다.
- 기존 419건·미관측60건·내부21건을 분리 집계한다. 2020은 이미 관찰된 사후 회귀셋이다.
- 조건부 후속 후보: Dense 대비 미관측 Top-3 증가, 전체/미관측 Top-1·Top-3 비회귀,
  공통 Gate·확인 정책·source-only 경계 위반 0. 미충족 시 해당 구성 기각, runtime 유지.
- corpus 보강과 reranker를 한 번에 섞어 원인을 감추지 않는다.

## 3. 검색 표현 공간의 도메인 학습

가설: 같은 CAS의 서로 다른 공식 명칭을 가깝게 학습하면, 학습에 없는 CAS의 다른
표현도 더 잘 검색한다. 이는 SapBERT의 synonym alignment 발상을 참고한 소규모 실험이며
SapBERT 재현이나 BGE 전체 fine-tuning이 아니다.

- **BGE-M3 encoder는 고정하고 1024차원 벡터의 rank-32 residual projection을 학습**한다.
  검색 벡터를 바꾸는 contrastive metric learning이다. GPU 서버·Whisper 학습 없음.
- 데이터: 기존 DB의 공개 물질 이름 + 신규 공식 물질 CSV의 이름. 프로젝트 수동 별칭,
  숫자 CAS/UN/formula, 사고 2015~2020 표현은 새 학습셋에서 제외한다.
- 양성: 동일 CAS의 다른 정규화 이름. 음성: 다른 CAS의 명칭, frozen Dense가 가까이
  놓은 hard negative. 다른 CAS에 공유되는 표현은 전체 synonym benchmark에서 제외한다.
- CAS 문자열 SHA-256(`20260911:` 접두어)로 train/dev/test=70/15/15 분할한다.
  동일 CAS가 split을 넘지 않는다. 각 dev/test CAS에서 hash 순서 첫 이름을 query로
  숨기고 모든 corpus/학습에서 그 정규화 표현을 제거한다. 다른 이름 하나 이상을 정답
  후보로 남긴다. 동일 표현이 split/corpus에 남으면 실행 중단한다.
- train CAS만 optimizer/negative mining에 사용한다. train/dev/test CAS·표현 누수 자동 검사.
  train 최소 30 CAS, dev/test 각 최소 10 CAS 미달이면 학습 판단 보류.
- seed=20260911, epoch 최대 20, batch=64, AdamW lr=0.001, weight_decay=0.01,
  temperature=0.07, identity residual penalty=0.01. 로컬 학습 최대 10분, 재시도 0.
- epoch 0/5/10/20 중 **dev Top-20 우선, Top-3 다음, 동점은 더 이른 epoch** 선택.
  epoch 0은 identity 기준선. dev에서 개선 없으면 학습본 미채택. test는 선택 이후 1회 평가.
- 공통 후보 목록에서 frozen Dense vs 학습 projection의 Top-1/3/20, MRR, paired gain/loss를
  계산한다. 공식 synonym 검색 benchmark이며 현장 ASR 오류 평가가 아니다.
- 조건부 후속 검토: dev 선택에서 epoch>0, test Top-20 증가 및 Top-1/3 비회귀,
  2020 사후 회귀의 미관측 Top-1/3 비회귀, 구조적 안전 위반 0. 하나라도 미충족하면 기각.
- 2020 정답은 optimizer·epoch 선택·negative mining에 사용하지 않는다. 공식 별칭의
  평가 표현과의 중복은 별도 감사한다. 외부 사전학습 데이터 오염은 확인할 수 없다.

## 증거·검증·인계

기준 Resolver SHA-256 `2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff`.
기존 Dense corpus hash `cbfe18d59e24c39393983d7956d8d53a7e013dc59aee68a34f73a69dd27211bc`.
기존 query 벡터 hash `5a7871ebcf9ecfec77928d0db62689427255df7447624c6fdcda82903affd3aa`.

원본·행별 예측·가중치·벡터·split manifest는 private(0700/0600). Git에는 집계·hash·코드·
재현 명령만 남긴다. 결과를 보고 라벨 수정하지 않는다. 수치가 나빠도 보고한다.
단위/누수/안전 회귀, 실제 artifact 실행, lint·전체 테스트·계약 drift·CI를 확인한다.
시간은 준비·batch 추론·학습을 분리하며 HTTP/현장 지연시간으로 표현하지 않는다.
독립 화학 검수, 안전한 기권 임계값 학습, 실제 사용자 검증·배포는 이번 완료 범위 밖이다.

## 1차 출처

- [소방청 공식 물질 자료](https://www.data.go.kr/data/15081005/fileData.do)
- [BAAI reranker 모델·사용법·라이선스](https://huggingface.co/BAAI/bge-reranker-v2-m3)
- [SapBERT 원 논문](https://arxiv.org/abs/2010.11784)
