# 공식근거 교차검증

## 1. 쉽게 설명

사람 화학 전문가를 구하지 못했다고 AI를 전문가로 표시하지 않습니다. 대신 위험 문장 하나마다
공식기관 자료를 연결하고, 서로 독립된 기관이 같은 내용을 말하는지 검사합니다.

```text
CAMEO 충돌 결과
  → 물질쌍과 예상 생성물 일치 검사
  → 공식기관 URL allowlist 검사
  → 같은 기관 자료를 중복 집계하지 않음
  → 주장별 근거·미증명 항목 생성
  → API와 대시보드에 증빙 상태 반환
```

대시보드의 대표 조합인 차아염소산나트륨–염산은 다음 5개 자료와 4개 독립기관 그룹을
연결했습니다.

| 기관 | 자료 | 확인 범위 |
|---|---|---|
| NOAA/EPA | [CAMEO Sodium Hypochlorite](https://cameochemicals.noaa.gov/chemical/4503) | CAS, 형태, 산 접촉 시 염소가스 위험 |
| CDC | [MMWR 실제 혼합 사고 보고](https://www.cdc.gov/mmwr/preview/mmwrhtml/00015111.htm) | 차아염소산나트륨과 산 혼합 메커니즘·사고 |
| CDC | [Chemical Disinfectants](https://www.cdc.gov/infection-control/hcp/disinfection-sterilization/chemical-disinfectants.html) | hypochlorite와 산 혼합 시 독성 염소가스 |
| ILO/WHO | [ICSC 1119](https://inchem.org/documents/icsc/icsc/eics1119.htm) | 산 접촉 시 독성·부식성 가스와 염소 발생 |
| UKHSA | [Sodium hypochlorite general information](https://www.gov.uk/government/publications/sodium-hypochlorite-properties-incident-management-and-toxicology/sodium-hypochlorite-general-information) | 산성 제품 혼합 시 염소가스와 건강 영향 |

두 CDC 문서는 같은 독립기관 그룹 하나로 계산합니다. 따라서 문서는 5개지만 독립기관 수는
4개입니다.

금속 나트륨–염산 조합은 다음 3개 독립 공식기관의 자료를 연결했습니다.

| 기관 | 자료 | 확인 범위 |
|---|---|---|
| NOAA/EPA | [CAMEO Sodium](https://cameochemicals.noaa.gov/chemical/7794) | 염산 접촉 시 폭발 위험 |
| ILO/WHO | [ICSC 0717](https://inchem.org/documents/icsc/icsc/eics0717.htm) | 산 접촉 시 화재·폭발 위험 |
| OSHA | [Chemical Hazards and SDSs](https://www.osha.gov/sites/default/files/2021-03/Chemical%20Hazards.pdf) | 산과 나트륨 등 반응성 금속 접촉 시 수소·화재·폭발 위험 |

세 문헌의 공통 범위만 사용해 “실제 접촉 시 수소가 발생하고 화재·폭발 위험이 있다”고
표현합니다. 현장의 물질 존재, 접촉 여부, 수소 발생량과 점화 여부는 별도 확인 대상입니다.

## 2. API 상태

| 값 | 의미 | 화면 문구 |
|---|---|---|
| `REFERENCE_TRIANGULATED` | 3개 이상 독립 공식기관과 필수 역할이 같은 주장을 지지 | 공식근거 교차확인 |
| `PRIMARY_AUTHORITY_ONLY` | CAMEO 공식체계 스크리닝만 존재 | CAMEO 단일체계 근거 |
| Rule `VERIFY_REQUIRED` | registry 변조·누락, 예상 생성물 불일치 등 | 근거 검증 필요, 위험등급 숨김 |

`REFERENCE_TRIANGULATED`도 `expert_reviewed=false`이며 사람 전문가 승인, 법적 인증 또는 현장
안전 보장을 의미하지 않습니다.

## 3. 주장별 증빙 범위

API의 `reference_assurance.claim_checks`는 다음을 분리합니다.

- `SUBSTANCE_IDENTITY_AND_FORM`: CAMEO CAS·물질 형태 대조
- `PAIR_REACTIVITY_SCREENING`: 물질쌍 위험 메커니즘 공식근거
- `CURRENT_SITE_INVENTORY`: 문헌으로 증명 불가, 항상 `NOT_PROVEN`
- `ACTUAL_MIXING_AND_FIELD_CONDITIONS`: 현장 확인 전 `NOT_PROVEN`
- `HUMAN_CHEMICAL_EXPERT_REVIEW`: 실제 사람이 검토하지 않았으므로 `NOT_PERFORMED`

즉 “산과 접촉하면 염소가스가 발생할 수 있다”는 문헌으로 증빙해도, “지금 공장에서 이미
혼합됐다”거나 “현재 농도에서 피해 반경이 얼마다”라고 확장하지 않습니다.

## 4. fail-closed 규칙

다음 중 하나라도 발생하면 완료 위험등급 대신 `VERIFY_REQUIRED`를 반환합니다.

- registry schema 또는 policy ID 불일치
- `expert_reviewed=true` 또는 `human_expert_substitute=true` 위조
- 공식기관에 등록되지 않은 host
- CAS 형식·정렬 오류
- 중복 source ID 또는 중복 물질쌍 주장
- 최소 독립기관 수 또는 필수 근거 역할 미충족
- registry의 예상 생성물과 CAMEO 결과 불일치

registry 전체 SHA-256을 결과에 포함하고 BFF도 배포된 registry checksum과 일치하는지
검사합니다.

## 5. 외부 근거 drift 검사

공식 웹페이지가 삭제·이동되거나 문서 구조가 바뀌면 과거에 맞았던 URL도 근거로 쓰기
어렵습니다. 다음 명령은 서비스 요청과 분리된 오프라인 검사입니다.

```bash
python scripts/data/check_reference_sources.py \
  --config-dir config \
  --output outputs/reference-source-drift.json
```

검사 범위는 HTTP 상태, redirect 후 최종 기관 host, MIME, HTML 본문의 고정 문구입니다.
PDF는 별도 본문 파서를 운영 의존성에 넣지 않아 URL·기관 host·MIME과 문서 위치
메타데이터까지만 검사하며 결과에 `METADATA_ONLY`로 남깁니다. GitHub Actions의
`공식근거 링크 정기 검사`가 매주 실행되고 수동 실행도 가능합니다. 실패는 registry를
자동 수정하지 않고 담당자가 공식 원문을 다시 확인하게 합니다.

2026-08-01 실제 네트워크 검사 결과는 8개 URL 모두 도달·host·MIME 검사를 통과했고,
HTML 7개는 문서 위치 probe까지 통과했습니다. OSHA PDF 1개는 위 제한 때문에
`PASS_WITH_LIMITATIONS`입니다.

## 6. 현재 커버리지

실제 artifact로 공개 검증 CAS 6종의 고유 조합 15개를 다시 실행했습니다.

- `REFERENCE_TRIANGULATED`: 2쌍 — 차아염소산나트륨 + 염산, 금속 나트륨 + 염산
- `PRIMARY_AUTHORITY_ONLY`: 13쌍
- `expert_reviewed=true`: 0쌍
- 위험등급 분포: 높음 8, 중간 2, 낮음 5

이는 정답 정확도나 현장 성능이 아니라 공식근거 커버리지와 데이터 연결 회귀 결과입니다.
나머지 13쌍은 조합별 독립 공식근거가 추가되기 전 교차확인으로 표시하면 안 됩니다.

## 7. 새 조합을 추가하는 절차

1. `config/reference_assurance_registry.json`에 정규화된 CAS 두 개를 정렬해 등록합니다.
2. 위험 문장은 원문을 과장하지 않는 한 문장으로 작성합니다.
3. CAMEO 물질·반응성 근거와 별도의 사고보고 또는 공중보건 자료를 연결합니다.
4. 같은 기관의 여러 문서는 하나의 `independence_group`으로 묶습니다.
5. 자료의 정확한 절 위치 `locator`, HTML `content_probe`, 예상 MIME, 버전·갱신일, URL을
   기록합니다.
6. 예상 생성물과 CAMEO 결과가 일치하는지 평가합니다.
7. 전체 15쌍 회귀, API 계약, BFF 계약을 실행합니다.

논문·블로그·제조사 SDS는 보조 근거가 될 수 있지만, 공식기관 최소 조건을 대신하지 못합니다.
상충하는 자료는 지지 자료로 등록하지 않고 별도 조사 대상으로 남깁니다.
