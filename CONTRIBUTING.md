# 케미체크119 AI·모델 API 기여 가이드

이 문서는 처음 참여하는 개발자가 같은 기준으로 코드를 수정하고 검증하기 위한 최소 규칙입니다.

## 1. 개발 환경

Python 3.11을 사용합니다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

artifact가 없어도 단위 테스트와 정적 컴파일 검사는 실행할 수 있습니다.

```bash
python -m pytest
python -m ruff check src tests scripts
python -m ruff format --check src tests scripts
python -m compileall -q src scripts
python -m pip check
```

## 2. 브랜치와 Pull Request

`main`에 직접 작업하지 않고 목적이 드러나는 브랜치를 사용합니다.

```text
feat/substance-resolver
fix/cas-validation
docs/api-onboarding
test/pilot-policy
```

Pull Request에는 다음을 적습니다.

- 무엇을 변경했는지
- 왜 필요한지
- 어떻게 검증했는지
- API·데이터·안전 계약에 영향이 있는지
- 남아 있는 한계가 무엇인지

## 3. 커밋 메시지

접두사는 영문, 설명은 명확한 한국어로 작성합니다.

```text
feat: 공개 근거 파일럿 충돌 검토 추가
fix: 미등록 제품명의 CAS 자동 연결 방지
docs: 초보자용 API 실행 절차 정리
test: 현장 확인 게이트 회귀 테스트 추가
refactor: 근거 검색과 응답 조립 책임 분리
chore: 운영 의존성 버전 고정
```

한 커밋에는 하나의 논리적 변경만 포함합니다.

## 4. 코드 변경 원칙

- 물질 후보와 현장에서 확인된 물질을 같은 값으로 취급하지 않습니다.
- Resolver 점수를 사고·위험 확률로 표현하지 않습니다.
- CAMEO 등급을 백분율로 변환하지 않습니다.
- 근거가 없으면 다른 물질의 근거로 대체하지 않습니다.
- 시설 이력을 현재 재고로 표현하지 않습니다.
- 생성형 모델이 CAS, 위험등급, 충돌 규칙을 임의로 만들게 하지 않습니다.
- 운영 API에 LM Studio 의존성을 추가하지 않습니다.

## 5. 공개 근거 파일럿 변경

`PUBLIC_SOURCE_PILOT_V1` 정책을 수정할 때는 코드, config, 테스트, API 문서를 한 Pull
Request에서 함께 수정합니다.

반드시 유지할 응답 계약은 다음과 같습니다.

- `expert_reviewed=false`
- `risk_scale.is_probability=false`
- `risk_scale.probability_percent=null`
- 출처·매핑·정책 버전 추적 가능
- 두 현장 확인 레코드 전에는 Rule Engine 실행 금지

새 CAMEO 매핑은 단순 이름 유사도로 운영 경로에 넣지 않습니다. CAS 체크디지트, CAMEO
페이지의 CAS, 물질 형태, 출처 URL과 확인 시각을 검증해야 합니다.

## 6. API 계약 변경

요청·응답 필드를 바꾸면 다음을 함께 확인합니다.

1. `src/chemiguard119/api_models.py`
2. `src/chemiguard119/api.py`
3. `tests/test_api.py`
4. `examples/api/`
5. `docs/API.md`
6. 필요하면 API schema version

기존 필드를 조용히 다른 의미로 재사용하지 않습니다. 호환되지 않는 변경은 버전 변경과 마이그레이션
설명을 포함합니다.

## 7. 데이터와 artifact

다음 파일은 저장소에 직접 커밋하지 않습니다.

- 원천 데이터 bundle
- `artifacts/`의 SQLite·joblib 파일
- `outputs/`의 실행 결과
- `.env`와 API Key
- 로컬 가상환경, cache, `*.egg-info`

작은 평가 입력과 공개 가능한 config만 Git으로 관리합니다. 운영 artifact는 clean commit에서 생성하고
`runtime_manifest.json`과 SHA-256을 함께 보관합니다.

## 8. 테스트 기준

최소한 다음 회귀 시나리오를 유지합니다.

- 정확한 CAS와 물질명 검색
- 모호한 이름이 하나의 CAS로 자동 확정되지 않음
- 미등록 제품명이 하나의 CAS로 자동 확정되지 않음
- 현장 확인이 하나라도 없으면 충돌 검토가 실행되지 않음
- 공개 근거 파일럿 출력에 정책·출처·`expert_reviewed=false` 포함
- 위험등급이 확률로 노출되지 않음
- 시설 검색 결과가 과거 이력으로 표시됨
- 운영 모드에서 잘못된 API Key와 artifact hash가 차단됨

## 9. 문서 기준

- 저장소 안의 파일 링크는 상대경로를 사용합니다.
- 존재하지 않는 Release, 서비스 URL, 평가 수치를 미리 적지 않습니다.
- 현재 구현과 향후 계획을 구분합니다.
- 공개 근거 파일럿을 전문가 승인 결과로 표현하지 않습니다.

## 10. 완료 전 체크리스트

- [ ] `python -m pytest` 통과
- [ ] `python -m ruff check src tests scripts` 통과
- [ ] `python -m ruff format --check src tests scripts` 통과
- [ ] `python -m compileall -q src scripts` 통과
- [ ] `python -m pip check` 통과
- [ ] Secret과 로컬 경로가 diff에 없는지 확인
- [ ] API 예시와 문서가 실제 계약과 일치하는지 확인
- [ ] 위험·확률·시설 이력 표현을 다시 검토
- [ ] 변경 범위에 맞는 한국어 커밋 메시지 작성
