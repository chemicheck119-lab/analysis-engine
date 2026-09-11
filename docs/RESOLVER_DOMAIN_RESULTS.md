# Resolver 고도화: 자료 보강·재정렬·검색 학습 결과

2026-09-11 · 상태: **부분 구현 또는 개발용 데모**. 로컬 구현·실제 artifact 평가를 수행했다.
공개 GitHub push/PR/CI는 사용자 확인 전 보류한다. 현재 서비스의 Sparse Resolver는 그대로다.

## 먼저 결론

**세 방법 모두 실험했으며, 가장 큰 관찰 효과는 공식 이름 보강이었다.**
그러나 그 효과에는 평가 표현을 사전에 제공한 효과가 섞여 있다. ‘현장에서도 정확하다’거나
‘새 표현에 71.67% 일반화한다’는 주장은 할 수 없다.

1. **자료 보강:** 검색 사전에 빠진 공식 이름 1,626개를 실험 corpus에 추가했다.
2. **재정렬:** 먼저 찾은 CAS 후보 20개를 query–별칭 cross-encoder로 다시 정렬했다.
3. **도메인 학습:** BGE-M3는 고정하고 벡터 보정층 65,536개 파라미터를 contrastive 학습했다.
   같은 CAS의 다른 이름은 가깝게, 다른 CAS는 멀게 학습한다. BGE 전체 fine-tuning이 아니다.

어느 실험도 사람 확인을 없애거나 화학 위험도를 학습하지 않는다. API·정책·원래 artifact는
변경하지 않았고, 세 방법을 모두 합친 통합 구성은 아직 시험하지 않았다.

## 1. 같은 2020년 표현 60건으로 본 사후 회귀

아래 값은 서로 다른 독립 ablation이다. 앞 단계 결과에 다음 단계를 누적한 값이 아니다.
전체 419건 중 과거 사고 표현에 없던 60건만 뽑았다. 이미 관찰된 자료이므로 새 비공개 test가 아니다.
정확 일치·모호성·잘못된 식별자 거절은 기존 Sparse Gate를 그대로 따른다.

| 구성 | Top-1 | Top-3 | Top-20 | 이번 판단 |
|---|---:|---:|---:|---|
| 현재 운영 기준 Sparse | 17/60 (28.33%) | 19/60 (31.67%) | 20/60 (33.33%)* | 유지 |
| 기존 BGE-M3 fallback | 19/60 (31.67%) | 24/60 (40.00%) | 34/60 (56.67%) | 비교 기준 |
| 기존 BGE + Top-20 재정렬 | 25/60 (41.67%) | 31/60 (51.67%) | 34/60 (56.67%) | 조건부 후속 검증 |
| 기존 BGE + 공식 이름 보강 | 37/60 (61.67%) | 43/60 (71.67%) | 44/60 (73.33%) | 자료 보강 가능, 일반화 증거 아님 |
| 기존 corpus + 학습한 보정층 | 24/60 (40.00%) | 33/60 (55.00%) | 38/60 (63.33%) | 조건부 후속 검증 |

*Sparse Top-20은 직전 동일 artifact 진단값이다. 이번 새 결과가 아니다.
상위 20개를 모두 사용자에게 보여주자는 뜻도 아니다. 내부 후보 풀의 회수율을 측정한다.

### 꼭 함께 말해야 하는 손실과 노출

- 재정렬은 Dense 대비 Top-3 오답 **8건을 고치고 정답 1건을 잃었다**. Top-1은 8건 개선·2건 손실.
- 보정층은 Dense 대비 Top-3 **10건 개선·1건 손실**, Top-1 7건 개선·2건 손실이다.
- 이름 보강의 Top-3 순증은 19건, 손실 0건이다. 새 이름과 평가 표현의 직접 일치는 19건이며,
  실제로 새로 맞힌 19건 중 **13건은 직접 일치**, 6건은 직접 일치 없이 개선됐다.
- 보정층 학습 입력에는 2020 미관측 60건 중 **27건의 표현–CAS 쌍**이 공개 물질 표를 통해
  포함됐다. 사고문 2020 정답을 optimizer에 넣지는 않았지만, **이 60건의 보정층 결과를
  독립 일반화 성과로 쓸 수 없다.** 따라서 아래 CAS 분리 264건을 따로 평가했다.
- 전체 점수 비회귀는 개별 사례 무손실을 뜻하지 않는다. 어떤 모델도 자동 CAS 확정 자격을 얻지 않는다.

사후에 학습 표현 중복을 제외한 33건만 보면, 보정층 Top-3는 **7/33 → 12/33**,
Top-20은 12/33 → 14/33이다. 이 작은 사후 부분집합도 새 test는 아니며,
아직 Top-3에서 21건을 놓친다는 점을 숨기지 않는다. 이 분석으로 설정을 바꾸지는 않았다.

## 2. 도메인 학습의 별도 CAS 분리 평가

기존 공개 물질 DB와 소방청 공식 이름을 사용했다. 원문 사고 기록·프로젝트 수동 별칭·
CAS 숫자·UN·formula 타입은 학습 데이터에서 제외했다. 숫자 CAS 이름의 실제 학습 포함도 0건이다.
다른 CAS에 공유되는 모호한 표현은 benchmark에서 제외했다. 이 평가는 그러한 모호 사례를
해결했다고 주장할 수 없다.

| 구분 | 실제 구성 |
|---|---|
| 분할 단위 | CAS별 SHA-256, seed 20260911, 70/15/15 bucket |
| 2개 이상 이름이 있는 CAS | train 1,202 / dev 252 / test 264 |
| 학습 벡터 | 4,852개, 2,907 CAS; 한 이름 CAS도 train negative로만 사용 가능 |
| 학습 양성 쌍 | 3,147개, 동일 CAS의 서로 다른 이름 |
| 음성 쌍 | 다른 train CAS의 batch negative + frozen Dense hard negative |
| 공통 검색 corpus | 6,461개 별칭, 4,195 CAS |
| dev/test 질의 | 각 CAS에서 이름 1개를 숨김; 그 정규화 표현은 corpus/학습 모두 제거 |
| 누수 검사 | train/dev/test CAS 중복 0, 숨긴 표현의 학습/corpus 포함 0 |
| 학습 | frozen BGE-M3 + rank-32 residual projection, trainable 65,536 |
| 설정 | AdamW 0.001, weight decay 0.01, temperature 0.07, batch 64, 20 epoch |
| checkpoint 선택 | dev Top-20 우선·Top-3 다음·동점은 이른 epoch; **5 epoch 선택** |
| test | checkpoint 선택 후 1회 비교; test로 epoch·threshold를 조정하지 않음 |

후보 문서에 test CAS의 **다른 이름**이 있는 것은 검색 문제의 필수 조건이다.
해당 CAS가 optimizer나 negative mining에 들어가는 것과는 다르다.

| 264건 공식 명칭 test | Frozen BGE | 학습한 보정층 | 변화 |
|---|---:|---:|---:|
| Top-1 | 58/264 (21.97%) | 94/264 (35.61%) | +36건 |
| Top-3 | 106/264 (40.15%) | 145/264 (54.92%) | +39건 |
| Top-20 | 166/264 (62.88%) | 193/264 (73.11%) | +27건 |
| MRR@20 | 0.331004 | 0.468844 | +0.137840 |

Top-3는 50건 개선·11건 손실이다. 아직 **119/264건은 Top-3에 정답이 없다**.
공식 표의 CAS 연결을 정답으로 사용했으며 화학 전문가의 독립 검수가 아니다.
사전학습 데이터 오염 여부, 실제 신고 오인식·현장 무전·안전성으로 일반화할 수 없다.

사후 언어별 감사도 분리했다. 한글을 포함하는 질의 106건의 Top-3는 48→67건,
한글 미포함 질의 158건은 58→78건이다. 이는 언어별 현장 성능이나 방언 평가가 아니다.

### 더 오래 학습하면 무조건 좋아지는가?

| epoch | dev Top-3 / 252 | dev Top-20 / 252 | 판정 |
|---|---:|---:|---|
| 0 | 87 | 137 | frozen 기준선 |
| 5 | 117 | 169 | 사전 규칙에 따라 선택 |
| 10 | 114 | 163 | 미선택 |
| 20 | 113 | 153 | 미선택 |

train loss는 3.028 → 0.628로 계속 낮아졌지만 dev는 5 epoch 이후 악화됐다.
훈련 표현에 치우치는 징후이며, epoch 수를 늘리는 것이 다음 목표가 아니다.

## 3. 공식 자료 감사

[소방청 울산시 화학물 데이터](https://www.data.go.kr/data/15081005/fileData.do)의
2021-01-15 기준 CSV를 공식 공개 링크로 내려받았다. 포털은 무료·이용허락범위 제한 없음으로
표시한다(2026-09-11 확인). 326,544 bytes, 4,378행, cp949이다.

- CAS 오류·복합 CAS 행 1,270개 제외. checksum 통과가 화학적 정답 승인은 아니다.
- 같은 표현이 여러 CAS에 연결되는 이름 41종 격리.
- 중복 정리 후 공식 이름 4,311개 / CAS 2,908개.
- 기존 후보 corpus 5,626개에 새 이름 1,626개를 더해 7,252개.
- 기존 중복 784개, 기존 이름과 CAS가 충돌하는 25개를 추가하지 않음.
- 기존 exact-only CAS의 53개 이름과 신규 CAS의 1,823개 이름은 fuzzy 검색에 추가하지 않음.
- 신규 CAS 1,413개는 보강 검토 대상으로만 남김. 진단상 빠져 있던 12개 정답 CAS는
  **모두 이 공식 파일에서 발견**됐지만, 사전 정책에 따라 이번에는 활성화하지 않음.

현재 DB의 `substance_profile` 749행은 가공된 프로필이다. 한글명에는 ICIS 이름이 우선될 수 있어
모두 울산 원문 별칭으로 재표시하지 않았다. 공식 원문 CSV를 따로 확보한 이유다.

‘공식 표에 있는 이름’은 ‘현재 시설 재고’, ‘제품 SDS 확인’, ‘기관 승인 대응’이 아니다.
새 CAS의 fuzzy 허용은 후속 정책·평가 작업이다. 성능을 올리려고 exact-only 경계를 제거하지 않았다.

## 4. 전체 회귀·안전·시간

| 별도 전체 419건 회귀 | Top-1 | Top-3 |
|---|---:|---:|
| 기존 Dense fallback | 378/419 (90.21%) | 383/419 (91.41%) |
| Top-20 재정렬 | 384/419 (91.65%) | 390/419 (93.08%) |
| 공식 이름 보강 | 396/419 (94.51%) | 402/419 (95.94%) |
| 학습 보정층 | 383/419 (91.41%) | 392/419 (93.56%) |

내부 21건은 세 구성 모두 Top-1/3 21/21이다. 모두 기존 Gate 보존 경로였으므로
새 fuzzy 모델의 내부 안전성 표본 21건으로 오해하면 안 된다.

실제 실험 출력에서 공통 Gate 변경, Rule 자격 플래그 위반, fuzzy CAS 허용 집합 이탈,
현재 재고 확인 플래그 위반은 각각 0건이다. 모든 후보는 미확인이다.
**실제 Rule 실행 횟수·Agent E2E·확인 취소·현장 안전성을 새로 평가한 것은 아니다.**

로컬 Mac M4 24GiB, Python 3.11.15, Torch 2.9.0, NumPy 2.3.5, MPS float32:

| 작업 | 실제 측정 | 포함하지 않는 것 |
|---|---:|---|
| 추가 BGE 벡터 1,786개 | 12.45초 | 모델 로드·기존 벡터 준비 |
| 재정렬 46질의 × 20후보 = 920쌍 | 11.40초 | 모델 로드·HTTP·STT |
| 보정층 20 epoch + checkpoint dev 평가 | 8.50초 | 사전 hard negative 준비·BGE 추론 |

위는 단일 로컬 실행의 batch 작업시간이며 cold/warm API 지연·동시 요청 처리량·현장 시간 절감이 아니다.
BGE 추가 텍스트 잘림 21/1,786, reranker pair 잘림 1/920을 기록했다.
신규 reranker 가중치 약 2.27GB는 로컬 HF 캐시에 남아 있다. 서버/GPU 임대·유료 API 호출 0회,
이번 추가 서버 비용 **0원**, 이전 누적 비용은 확인하지 않았다.

## 5. 재현 명령과 artifact

운영 requirements에 Torch를 추가하지 않는다. 실험 환경은 Torch 2.9.0 / Transformers 4.57.6 /
NumPy 2.3.5와 현재 저장소 의존성이 설치된 별도 Python이다. MPS 기준 실행이다.
아래 `/비공개/...` 경로를 승인된 private artifact 위치로 바꾼다. 원문·가중치는 Git에 없다.

```bash
export PYTHONPATH=src
export TOKENIZERS_PARALLELISM=false
EXPERIMENT_PY=/실험용/venv/bin/python
DOMAIN_OUTPUT=/비공개/private-data/evaluation/resolver-domain-new-run
DOMAIN_ARGS=(
  --model /비공개/action-brief-local-v1/resolver.joblib
  --db /비공개/action-brief-local-v1/chemiguard119.sqlite
  --previous /비공개/ulsan-resolver-comparison-20260911-v1
  --query-vectors /비공개/resolver-diagnosis-20260911-yGj0SB/query-vectors.float32
  --embedding-model /비공개/BGE캐시/snapshots/5617a9f61b028005a4858fdac845db406aefb181
  --reranker-model /비공개/reranker캐시/snapshots/953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e
  --output "$DOMAIN_OUTPUT"
)
"$EXPERIMENT_PY" scripts/experiments/resolver_domain.py prepare "${DOMAIN_ARGS[@]}"
"$EXPERIMENT_PY" scripts/experiments/resolver_domain.py rerank "${DOMAIN_ARGS[@]}"
"$EXPERIMENT_PY" scripts/experiments/resolver_domain.py train "${DOMAIN_ARGS[@]}"
"$EXPERIMENT_PY" scripts/experiments/audit_resolver_domain.py \
  --output "$DOMAIN_OUTPUT" --previous /비공개/ulsan-resolver-comparison-20260911-v1
```

첫 단계는 공식 CSV를 다운로드하고 split·corpus·벡터 manifest를 저장한다. 기존 출력 경로를
덮어쓰지 않는다. 이후 두 단계는 manifest의 hash를 확인한다. 원본 웹 파일이 변경되면
아래 CSV hash와 비교하고 동일 실험 재현으로 표현하지 않는다.
이전 query 벡터는 승인된 진단 private artifact가 필요하다. 없는 환경에서 임의의 전사문으로
대체하지 않는다. MPS 계산은 seed를 고정해도 모든 장비에서 가중치 bitwise 일치를 보장하지 않는다.
현재 실험 폴더의 보완 감사는 `--report-name final-audit-v2.json`으로 원래 감사를 보존해 작성했다.

Reranker는 [BAAI 공식 모델](https://huggingface.co/BAAI/bge-reranker-v2-m3), Apache-2.0,
revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`의 model.safetensors·config·tokenizer 파일만 받는다.
다운로드 합계 약 2.30GB, 최대 1개 다운로드 worker. 추론은 offline/local_files_only이다.
재정렬 설정: query와 기존 최고 별칭 1개, max_length 128, batch 16, logit 정렬; 확률 아님.

| artifact | SHA-256 |
|---|---|
| 기존 Resolver, 변경 없음 | `2696fa7f067163055ff556e5e12ccfa15e7dc08c9ff803838f0db074aa002dff` |
| 신규 공식 CSV | `7c98cb460a50cd831ccdcdbfad2fc00434205a5a1b846300ed0d1c3959053691` |
| CAS 분할 fingerprint | `0d35ed0cddfe83270a25d34c142550259c1bb5aeca17a8c258ccf90b44353443` |
| reranker weights | `d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286` |
| 선택한 epoch-5 projection | `c9b3b001f35445f3d2616da1887af8858d91a4ad321933510a64953fcd40dac6` |
| reranker private 예측 | `68318b6b25c91cb6f16c6ed3a1deb260a025606592330068bc97461326e6cfc9` |
| projection private 예측 | `6df4580211d7ab69a57c371f5f6924a28c2e8f0515a118bbb8fc6ff9766ba133` |

[집계 JSON과 전체 artifact hash](../data/evaluation/resolver_domain_2026-09-11.json).
실행 코드 커밋 `6ab87f5`, 사전 계획 커밋 `3d1673d`. 모든 private 파일 0600, 출력 폴더 0700.
기존 runtime Resolver·DB hash는 실행 전후 동일하다.

로컬 검증: `python -m pytest` **718 passed**, Ruff check/format 통과,
`python scripts/contracts/export_contracts.py --check` 통과. 기존 Starlette deprecation 경고 1개.
Docker 빌드·원격 CI 상태는 아래 인계 상태에 별도로 기록한다.

- Docker: 로컬 빌드 2회 모두 베이스 이미지 `python:3.11.15-slim`의 metadata 조회 중
  `DeadlineExceeded`로 중단됐다. 애플리케이션 코드를 빌드하는 단계까지 도달하지 못했다.
  로컬 베이스 이미지 캐시는 없었다. 호스트의 Docker Registry 접근은 401 인증 응답까지
  가능했으므로 네트워크/빌더 경로 문제로 분류하며, 원인을 완전히 확정하거나 해결했다고
  주장하지 않는다. Docker Desktop은 검증 전의 중지 상태로 복원한다.
- 공개 GitHub PR/CI: 사용자 공개 승인 대기. **CI 통과 또는 전체 구현 완료라고 표시하지 않는다.**
- PR/이슈 업데이트: 기존 #25에 연결할 코드·보고서를 로컬 커밋으로 준비했다. 공개 댓글·PR은
  아직 게시하지 않았다. PR 생성 시 base는 `experiment/ulsan-resolver-comparison`이다.

## 6. 완료 범위와 다음 판단

| 사실 상태 | 내용 |
|---|---|
| 부분 구현 또는 개발용 데모 | 세 단계 코드·실제 artifact 평가·누수 감사·로컬 테스트 완료, 공개 PR/CI 확인 전 |
| 설계 완료·구현 전 | 세 방법의 결합 ablation, 신규 CAS의 안전한 노출 범위 평가 |
| 검증되지 않은 가설 | 새 지역·ASR 오인식·현장 무전에도 개선 유지, 사용자 확인 시간 단축 |
| 구현 완료라고 표현하지 않는 것 | 운영 교체, 전문 검수, 실제 소방 안전성, end-to-end 운영 검증 |

사전 조건상 재정렬과 보정층은 **조건부 후속 검증 후보**다. 기준 Sparse는 유지한다.
다음은 모델을 더 크게 만드는 작업보다 **명칭 보강 + 보정층 + 재정렬의 결합/손실 분석**과
신규 CAS 격리 정책 검증이 우선이다. 이번 test도 이제 관찰했으므로 다음 설정을 계속
맞추는 비공개 test로 재사용하면 안 된다. 새 검증 자료 또는 개발셋 내부 분할이 필요하다.

학습 접근은 [SapBERT의 synonym self-alignment](https://arxiv.org/abs/2010.11784)에서 착안했지만,
논문의 전체 모델·데이터·성능을 재현하거나 한국어 화학물질에 그대로 적용했다고 주장하지 않는다.

관련 이슈: [#25](https://github.com/chemicheck119-lab/analysis-engine/issues/25).
작업 브랜치 `modeling/resolver-domain-adaptation`; 공개 업로드 확인 전에는 로컬 커밋만 유지한다.
Notion·Front·Backend·GCP·API 기본 설정은 수정하지 않았다.
