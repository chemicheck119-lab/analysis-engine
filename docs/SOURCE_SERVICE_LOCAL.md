# 실제 원천에서 로컬 화면까지

2026-10-07. 운영 배포 또는 화학 안전성 검증이 아니다.

## 구현과 범위

보관된 KOSHA XML → 기존 KoshaMsdsClient 파서 → 기존 정규화·품질 검증 → PostgreSQL staging → 사람 승인 → 기존 버전 반영 → 로컬 BFF 어댑터 → 기존 `/main` 화면의 별도 기준자료 조회.

이번 실제 입력은 2026-10-06 수집한 메탄올(CAS 67-56-1) XML 17개다. 원본 checksum을 대조한 뒤 다시 파싱했다. 이번에 외부 API를 새로 호출하지 않았다. 입력 146 = 정상 94 + 제외 52(내용 없는 구조 항목 17, 정보 없음 35). 필드 오류 제외는 0이다. 제외율 35.6%의 기존 사람 확인 게이트를 유지한다. CAS 형식은 물질 매핑 또는 대응 정확성의 보장이 아니다.

전체 DB·검색모델 빌더가 요구하는 다른 원천은 **합성 baseline**으로 명시한다. 저장 96 = 실제 메탄올 94 + 합성 기존 근거 2다. 이 어댑터는 provenance의 CAS만 제공하므로 합성 근거 2건을 실제 원천으로 표시하지 않는다. 원본·DB·모델 파일은 저장소 밖 private-data에 보관한다. 테스트용 baseline 기준 조정은 `pytest.MonkeyPatch.context()` 안에 한정하며 운영 기본값을 바꾸지 않는다. 이 준비 스크립트는 개발용이며 pytest와 저장소 tests를 필요로 한다.

화면은 `/main`의 기존 물질 검색과 대응 참고사항으로 통합했다. `VITE_ENABLE_APPROVED_REFERENCE=true`를 명시한 환경에서 켜며 `referenceLab` 매개변수는 필요하지 않다. `/local-reference` 요청은 Vite proxy를 통해 loopback 어댑터(8004)로 전달된다. 승인되지 않은 후보는 본문 없이 대기 상태만 표시한다. 승인 파일 hash와 선택 버전이 맞지 않으면 503으로 차단한다. 연결 오류를 합성 데이터로 대체하지 않는다. 기존 사고 분석·두 CAS 게이트·전화 흐름은 별도다. **기존 Spring BFF/RAG가 새 근거로 답변한다는 검증은 하지 않았다.**

## 명령

engine 루트에서 환경을 불러온다.

```sh
cd /Users/hywznn/Documents/chemicheck119-lab/analysis-engine
set -a
source local/lakehouse/env.example
set +a
```

새 외부 수집이 필요하면 기존 도구의 숨김 입력으로 키를 입력한다. 공개 키/응답/개인정보를 Git에 넣지 않는다. 출력 경로는 매번 새로 지정한다.

```sh
.venv/bin/python scripts/data/verify_live_kosha.py --cas 67-56-1 --output /Users/hywznn/Documents/chemicheck119-lab/private-data/kosha-new-collection
.venv/bin/python scripts/data/prepare_source_service.py --archive /Users/hywznn/Documents/chemicheck119-lab/private-data/kosha-new-collection --output /Users/hywznn/Documents/chemicheck119-lab/private-data/source-service-new
```

이번 보관본 후보는 private-data/source-service-20261007이다. 그 후보를 읽는 서버:

```sh
.venv/bin/python -m chemiguard119.reference_service --directory /Users/hywznn/Documents/chemicheck119-lab/private-data/source-service-20261007
```

검토자가 원천·제외 사유·사용 범위를 확인한 **뒤에만** 아래 명령으로 반영한다. reviewer/note를 실제 판단으로 바꾼다. 일반적인 안전 승인으로 표현하지 않는다.

```sh
.venv/bin/python scripts/data/publish_source_service.py --directory /Users/hywznn/Documents/chemicheck119-lab/private-data/source-service-20261007 --reviewer REVIEWER --note '검토한 제외 사유 및 로컬 조회 범위' --accept-expected-exclusions
```

FE 개발 서버 `/main` → 기존 물질 검색에서 메틸 알코올 또는 CAS 67-56-1 → 검색. 승인 후 응급조치(4장)·화재(5장)·누출(6장)·노출방지(8장)·안정성(10장)을 항목별 원문으로 바로 보여준다. 전체 정상 94개 원천 중 해당 장의 항목만 사용하며, 없으면 미제공으로 표시한다. 검색은 현장 물질 확인 기록을 생성하지 않는다. 자유문장의 미확인 이명·혼합물 표현을 자동으로 CAS에 연결하지 않는다. 자동 사고 대응 권고가 아니다. Kafka DAG는 이후 committed activation을 전달하며 Trino 대조는 별도 수행한다. 이번 화면은 PostgreSQL을 조회하므로 CDC 지연으로 원천 본문을 임의 대체하지 않는다.

## 파일 보존 검증

메모리 Moto를 Versity Gateway v1.8.0의 disk-backed S3로 변경했다. 접속 주소·기존 객체 키를 유지하며 named volume `chemicheck119-lakehouse-local_reference-objects`의 `/data`에 저장한다. [공식 Docker 안내](https://github.com/versity/versitygw/wiki/Docker)를 참고했다.

변경 전 원본 545개(1,525,819 bytes)를 private-data/lakehouse-object-migration-20261007에 백업하고 각 SHA256을 inventory.json에 기록했다. 복사 후, 컨테이너 restart 후, force-recreate 후 각각 545개 hash가 모두 일치했다. restart/recreate 후 Trino 전체 필드 대조는 선택 버전 28개 MATCHED였다. PostgreSQL은 재생성하지 않았다.

```sh
docker compose -f local/lakehouse/compose.yaml restart object-fixture
docker compose -f local/lakehouse/compose.yaml up -d --no-deps --force-recreate object-fixture
.venv/bin/python scripts/data/verify_object_preservation.py --inventory /Users/hywznn/Documents/chemicheck119-lab/private-data/lakehouse-object-migration-20261007/inventory.json
.venv/bin/python -m chemiguard119.reference_lakehouse verify
```

named volume은 백업과 다르다. `down -v`, Docker 데이터 초기화, 디스크 장애에서는 보존을 보장하지 않는다. PostgreSQL은 현재 익명 Docker volume을 쓰므로 이 문서의 객체 저장소 재생성 명령을 DB에 그대로 적용하지 않는다. 원본 XML·검색모델은 호스트 private-data에 보관된다. 다른 PC 이전·DB 전체 영속성·복구 자동화는 별도 범위다.

## 검증

Python 실제 로컬 PostgreSQL 테스트: 승인 전 본문 차단, 승인된 fixture의 DB/HTTP 본문 일치, 다른 CAS 미노출, 승인 hash 변조 503. 실제 메탄올 후보는 사람 승인 전까지 staging이다. FE 테스트: 미승인 본문 차단·연결 실패 시 대체 없음·승인 fixture 본문/출처 표시. `corepack pnpm check` 전체 검사 수행. 실제 원천 화면 조회는 승인 대기 146/94/52와 원 수집 시각을 확인했다. 세부 상태는 SOURCE_SERVICE_VERIFICATION.json에 기록한다.
