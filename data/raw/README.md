# 원천 데이터 배치 위치

모델 artifact를 다시 만들 때 아래 8개 CSV를 이 디렉터리에 배치합니다.
원천 데이터와 생성 artifact는 용량·배포권한·버전 관리 문제 때문에 Git에 커밋하지 않습니다.

1. `01_KOSHA_물질안전보건자료.csv`
2. `02_CAMEO_화학물질_반응성.csv`
3. `03_CAMEO_화학물질_반응성그룹_매핑.csv`
4. `04_CAMEO_반응성그룹_목록.csv`
5. `05_CAMEO_반응성그룹_호환성_고유조합.csv`
6. `06_울산소방_화학물정보.csv`
7. `13_ICIS_2024_화학물질_취급현황.csv`
8. `19_ICIS_2024_시설후보_통합모델입력.csv`

전국 파서 외부 감사에는 다음 공식 파일을 별도로 사용합니다. 이 파일은 운영 artifact가 아니며
업체명·주소·사고 원문은 평가 결과에 포함하지 않습니다.

9. `09_CSI_전국_화학사고정보_20250430.csv`
   - 출처: `https://www.data.go.kr/data/15069200/fileData.do`
   - SHA-256: `a1ef8e4b6b0c6ef96fb7277edce2b1cf5c1b935a88f4274c88d878713bb7fba5`

```bash
PYTHONPATH=src python scripts/data/download_csi_official_incidents.py
```

준비 후 다음 명령으로 입력 계약을 먼저 검사합니다.

```bash
chemiguard119 audit --data-dir data/raw
```

전체 학습·평가·manifest 생성은 다음 명령으로 실행합니다.

```bash
chemiguard119 pipeline --data-dir data/raw --include-hash
```

GitHub Actions 릴리스에서는 이 8개 파일을 루트에 담은 `tar.gz` 번들을 사용합니다.
조직 Secret `CHEMIGUARD119_DATA_BUNDLE_URL`과
`CHEMIGUARD119_DATA_BUNDLE_SHA256`에 다운로드 주소와 SHA-256을 등록해야 합니다.

## 선택: 소방 사고–CAS source adaptation

물질명 후보 Resolver 파인튜닝에는 다음 공개 파일을 추가로 사용합니다.

- `07_울산소방_화학사고별_유해물질판단.csv`
- 출처: `https://bigdata-119.kr/goods/goodsInfo?goods_mng_sn=5`

원본 전체를 Git에 넣지 않고 `발생연도·CAS·한글/영문 물질명·일반명`만 학습에
사용합니다. 2015~2018년으로 검증 모델을 학습하고 2019년으로 검증한 뒤,
2015~2019년 최종 후보 모델을 2020년 잠금셋에서 한 번 평가합니다.

```bash
chemiguard119 finetune-resolver \
  --base-model artifacts/resolver.joblib \
  --incidents data/raw/07_울산소방_화학사고별_유해물질판단.csv \
  --output-dir artifacts/incident_adaptation \
  --report outputs/modeling/incident_adapted_resolver_evaluation.json
```
