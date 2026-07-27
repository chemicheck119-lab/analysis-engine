# 지원 물질 우선순위 파이프라인

## 1. 왜 필요한가

케미체크119가 CAMEO 전체 물질을 지원한다고 말하려면 국내 CAS와 CAMEO 물질 형태를
공식 페이지에서 검증한 연결표가 필요합니다. 현재 공개 근거 파일럿에서 검증된 연결은
6개 CAS이고, KOSHA 상세 MSDS는 9종입니다. 따라서 무작정 전체 물질을 지원한다고
표현하지 않고 다음 두 목록을 데이터로 관리합니다.

1. **시연 우선순위**: 지금 가진 근거로 종단간 충돌 검토를 시연하기 좋은 물질
2. **확장 우선순위**: 소방·시설 데이터에 등장하지만 MSDS 또는 CAMEO 검증이 부족한 물질

서비스 테마는 “위험을 추측하는 AI”가 아니라 “출동 중 무엇부터 확인할지 근거로
정렬하는 AI”입니다. 이 오프라인 파이프라인은 그 전에 어떤 물질부터 검증할지도 같은
원칙으로 정렬합니다.

## 2. 사용하는 신호

| 신호 | 의미 | 의미하지 않는 것 |
|---|---|---|
| 소방 사고 행 수 | 제공된 소방 사고 CSV에서 CAS가 등장한 행 수 | 전국 사고확률 |
| 시설 수 | ICIS 통합 입력에서 정확 CAS로 연결된 과거 사업장 수 | 현재 재고 보유 확률 |
| PRTR 정확 매칭 행 | 업체·CAS가 정확히 연결된 공개 배출·이동 이력 | 재고량 |
| KOSHA MSDS 적재 여부 | 현재 검색 corpus에 상세 MSDS가 있음 | 모든 제조사 제품의 최신 MSDS |
| CAMEO 공개 검증 여부 | CAS와 물질 형태를 공식 페이지에서 대조함 | 전문가 승인 |

모든 출력에는 `is_probability=false`와
`current_inventory_confirmed=false`가 포함됩니다.

소방 사고 원본의 CAS가 비어 있거나 체크디지트가 유효하지 않으면 해당 행은 순위
집계에서 제외합니다. 임의의 CAS로 교정하지 않으며, 제외 행 수와 정책
`EXCLUDE_AND_REPORT`를 결과 JSON의 `data_quality`에 기록합니다. KOSHA, 시설 통합 입력,
CAMEO crosswalk의 CAS 오류는 핵심 데이터 계약 위반이므로 실행을 중단합니다.

## 3. 출력 등급

| 등급 | 설명 |
|---|---|
| `END_TO_END_READY` | 운영 신호, KOSHA 상세, 공개 검증 CAMEO 연결이 모두 있음 |
| `MSDS_GAP` | 운영 신호와 CAMEO 연결은 있으나 KOSHA 상세가 없음 |
| `CAMEO_GAP` | 운영 신호와 KOSHA 상세는 있으나 CAMEO 공개 검증이 없음 |
| `MSDS_AND_CAMEO_GAP` | 운영 신호는 있으나 두 공식 근거가 모두 부족함 |
| `EVIDENCE_READY_LOW_OPERATIONAL_SIGNAL` | 근거는 준비됐지만 입력 데이터의 운영 신호가 적음 |
| `SEARCH_ONLY` | 물질 검색 후보 수준이며 종단간 지원 근거가 부족함 |

등급은 위험도가 아닙니다.

## 4. 실행

대용량 원천 bundle을 `data/raw/`에 준비한 다음 실행합니다. 소방 사고 CSV는 CAS가
검증된 파일만 `--fire-incident`로 지정합니다.

```bash
python scripts/data/build_support_material_priority.py \
  --facility data/raw/19_ICIS_2024_시설후보_통합모델입력.csv \
  --kosha data/raw/01_KOSHA_물질안전보건자료.csv \
  --crosswalk config/cameo_crosswalk.csv \
  --fire-incident data/raw/07_울산소방_화학사고별_유해물질판단.csv \
  --output outputs/support_priority/support_material_priority.csv \
  --summary outputs/support_priority/summary.json \
  --top-k 50
```

소방 사고 원본이 릴리스 bundle에 아직 없다면 `--fire-incident`를 생략할 수 있습니다.
그 경우 시설 통합 입력의 `울산소방_사고자료행수`를 보조 신호로 사용합니다.

## 5. 해석 방법

- `demo_rank`가 작은 물질부터 공모전 종단간 시나리오를 구성합니다.
- `expansion_rank`는 공식 근거가 부족한 물질을 준비 완료 물질보다 먼저 배치합니다.
- `expansion_rank`가 작은데 `CAMEO_GAP`이면 공식 CAMEO CAS·형태 대조를 우선합니다.
- `expansion_rank`가 작은데 `MSDS_GAP`이면 KOSHA OpenAPI 상세 수집을 우선합니다.
- 순위가 높아도 위험도가 높거나 현재 업체에 존재한다는 뜻은 아닙니다.
- 공식 데이터가 추가되면 동일 명령을 다시 실행해 순위와 커버리지 변화를 비교합니다.

KOSHA 공식 수집 명령과 검토 기준은
[KOSHA MSDS 공식 수집과 검토](KOSHA_COLLECTION.md)를 참고하세요.

## 6. 2026-07-28 실제 스냅샷

제공된 소방·ICIS·PRTR·KOSHA·CAMEO 파일을 직접 검사해 176개 후보를 산출했습니다.

| 커버리지 | 물질 수 |
|---|---:|
| `END_TO_END_READY` | 5 |
| `EVIDENCE_READY_LOW_OPERATIONAL_SIGNAL` | 1 |
| `CAMEO_GAP` | 3 |
| `MSDS_AND_CAMEO_GAP` | 167 |

현재 종단간 시연 우선 후보는 에탄올, 아세톤, 톨루엔, 염화수소, 금속 나트륨입니다.
차아염소산나트륨은 KOSHA·CAMEO 근거는 준비됐지만 현재 입력 스냅샷에서 소방 사고와 시설
이력 신호가 없어 별도 등급으로 분리했습니다.

공식 근거 확장 상위에는 경유, 휘발유, 메탄올, 황산, 비닐아세테이트, 벤젠 등이
나왔습니다. 경유·휘발유 같은 석유계 복합물질은 순위가 높아도 CAMEO의 단일 물질 형태로
자동 연결하면 안 됩니다. KOSHA 정확 CAS 자료는 수집할 수 있지만, 제품·혼합물 범위와
CAMEO 형태는 별도로 검토합니다. 작은 첫 확장 batch는 메탄올·황산·비닐아세테이트·벤젠처럼
형태가 비교적 명확한 물질부터 시작합니다.

소방 사고 입력 1,868행 중 유효 CAS 행은 1,656개였고, 비어 있거나 체크디지트가 유효하지
않은 212행은 임의 교정하지 않고 제외·보고했습니다. 이 지역 데이터는 구축 우선순위 신호
중 하나일 뿐 전국 사고확률이나 서비스 지역 제한을 뜻하지 않습니다.
