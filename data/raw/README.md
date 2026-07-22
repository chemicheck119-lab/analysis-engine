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
