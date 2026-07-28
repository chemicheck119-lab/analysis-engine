# KOSHA MSDS 공식 수집과 검토

## 1. 쉽게 설명

현재 릴리스의 KOSHA 상세 근거는 9종입니다. 새 물질을 늘릴 때 사람이 웹 화면을
복사해 붙이지 않고 다음 순서로 처리합니다.

```text
지원 물질 우선순위
→ KOSHA 공식 API에서 정확 CAS 검색
→ 1~16장 상세 수집
→ staging CSV와 manifest 저장
→ CAS·이름·제품/혼합물 범위 검토
→ 승인한 행만 원천 스냅샷에 병합
→ 전처리·검색모델 재생성
→ 평가 후 새 artifact 배포
```

수집됐다는 사실만으로 충돌 판정이 가능해지는 것은 아닙니다. KOSHA는 대응 근거 검색에
사용하고, 물질 간 충돌 판정에는 별도로 공개 검증한 CAMEO CAS–물질 형태 연결이 필요합니다.

## 2. 공식 출처와 계약

- 제공기관: 한국산업안전보건공단
- 공식 안내: [물질안전보건자료 목록·내용 OpenAPI](https://www.data.go.kr/data/15157612/openapi.do)
- API base: `https://apis.data.go.kr/B552468/msdschem`
- 목록: `GET /getChemList`
- 상세: `GET /getChemDetail01` ~ `GET /getChemDetail16`
- 응답: XML

공식 안내 페이지에서 2026-07-28에 확인한 개발계정 기본 트래픽은 1,000회입니다. 정책과
트래픽은 바뀔 수 있으므로 실제 수집 전에 같은 공식 페이지를 다시 확인합니다. KOSHA
공개자료는 참고용이며 현장 제품의 제조사·수입자 최신 MSDS를 대체하지 않습니다.

## 3. API 키 준비

공공데이터포털에서 활용신청 후 발급받은 키를 환경변수로만 주입합니다.

```bash
read -s KOSHA_API_SERVICE_KEY
export KOSHA_API_SERVICE_KEY
```

키를 CLI 인자, `.env`, 수집 CSV, manifest, Git commit에 기록하지 않습니다. `.env.example`은
변수 이름만 설명하며 실제 키 파일은 `.gitignore` 대상입니다.

## 4. 수집 실행

### 4.1 먼저 지정한 CAS만 시험

첫 확장 후보는 물질 형태가 비교적 명확한 메탄올·황산·비닐아세테이트·벤젠부터 작은
batch로 확인할 수 있습니다.

```bash
PYTHONPATH=src python scripts/data/collect_kosha_msds.py \
  --cas 67-56-1 \
  --cas 7664-93-9 \
  --cas 108-05-4 \
  --cas 71-43-2 \
  --output-csv data/raw/KOSHA_OPENAPI/kosha_msds_staging.csv \
  --manifest data/raw/KOSHA_OPENAPI/kosha_msds_staging.manifest.json
```

### 4.2 실데이터 확장 순위 사용

```bash
PYTHONPATH=src python scripts/data/collect_kosha_msds.py \
  --priority-csv outputs/support_priority/support_material_priority.csv \
  --limit 10 \
  --output-csv data/raw/KOSHA_OPENAPI/kosha_msds_staging.csv \
  --manifest data/raw/KOSHA_OPENAPI/kosha_msds_staging.manifest.json
```

기본값은 물질마다 목록 1회와 상세 16장을 요청합니다. 10종이면 재시도를 제외하고 최대
170회 요청입니다. 필요한 장만 기술 검증할 때는 `--sections 6,7,8,10`처럼 줄일 수 있지만,
릴리스 근거 corpus는 최종적으로 1~16장 전체를 보존하는 것을 원칙으로 합니다.

## 5. 자동 안전장치

- CAS 형식과 체크디지트가 틀리면 요청 전에 중단
- `searchCnd=1` 정확 CAS 검색 사용
- 응답 CAS가 요청 CAS와 정확히 같은 결과만 보존
- 같은 CAS에 서로 다른 `chemId`가 둘 이상이면 `AMBIGUOUS_EXACT_CAS`
- 모호한 결과를 임의로 첫 번째 선택하지 않음
- 통신 오류만 제한적으로 재시도
- API 오류·XML 오류는 구조화된 코드로 기록
- staging 레코드 ID 중복 차단
- 결과 CSV와 입력 우선순위 파일의 SHA-256 기록
- 서비스 키는 manifest에 기록하지 않음
- 일부 CAS 실패 시 성공 행을 보존하되 프로세스 종료코드 `2`로 실패를 알림

## 6. manifest 상태 해석

| 상태 | 뜻 | 다음 작업 |
|---|---|---|
| `COLLECTED` | 정확 CAS의 단일 `chemId` 상세를 수집함 | 내용·범위 검토 |
| `NOT_FOUND` | 정확 CAS 결과가 없음 | 공식 웹 검색과 명칭 재확인 |
| `AMBIGUOUS_EXACT_CAS` | 같은 CAS에 여러 `chemId`가 있음 | 사람이 물질 형태를 선택 |
| `FAILED` | 인증·통신·응답 계약 오류 | 오류 코드 확인 후 재수집 |

`COLLECTED`도 자동 승인을 뜻하지 않습니다.

## 7. 원천 스냅샷 병합 전 체크

- [ ] 요청 CAS와 응답 CAS가 같음
- [ ] `chemId`, 국문명, 최종개정일을 확인함
- [ ] 1~16장 수집 건수와 빈 상세를 확인함
- [ ] 제품명·혼합물·석유계 복합물질 여부를 확인함
- [ ] staging manifest의 CSV SHA-256이 실제 파일과 같음
- [ ] 기존 레코드 ID와 중복되지 않음
- [ ] 수집 실패와 모호한 CAS가 승인 목록에 포함되지 않음
- [ ] CAMEO 연결이 필요한 경우 공식 물질 페이지에서 CAS와 형태를 별도로 검증함

전처리는 기존 9종을 최소 회귀 기준으로 유지하면서, 검토된 KOSHA CAS가 추가된 스냅샷도
받을 수 있습니다. 새 CAS에 `substance_overrides.csv` 행을 억지로 만들 필요는 없습니다.
공식 KOSHA 국문명과 UN 번호를 기본값으로 사용하고, 별칭·화학식 보정이 꼭 필요한 경우에만
override를 추가합니다.

## 8. 현재 남은 작업

이 저장소는 공식 API 수집과 확장 가능한 전처리까지 구현했습니다. 실제 API 키로 내려받은
staging 자료의 내용 검토와 승인 병합은 아직 수행하지 않았습니다. 따라서 현재 배포
artifact의 KOSHA 상세 범위는 계속 9종입니다.
