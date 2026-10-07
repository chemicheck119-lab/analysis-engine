# 소방대원 정보 전달을 위한 기준정보 공급 설계

## 목표와 현재 상태

주목표는 확인된 물질·사고 상황에 필요한 근거를 짧게 보여주고, 근거가 없으면 없는 상태를 보존하는 것이다. 도구 설치를 현장 효과로 표현하지 않는다. 이 문서는 AI가 수행한 로컬 구현 기록이며 사용자의 직접 실습 기록이 아니다.

현재 구현: 승인된 전사 → BFF 분석 요청 → 모델의 SQLite/resolver/retriever → 상황별 KOSHA 발췌와 CAMEO 참고 → 대응 화면. 두 CAS 확인·결정적 규칙·출처 검사 게이트는 유지한다. ClawOps는 신고 접수/전사 전달이며 대원 대상 결과 음성 전달은 구현되어 있지 않다.

기존 PostgreSQL 배치는 별도 개발 경로다. 이번 변경도 운영 API를 전환하지 않는다. 대원 화면은 기존 계약의 자료 버전과 조회 데이터 버전을 접힌 상세에 표시한다. 버전은 개정일·수집일·최신성의 증명이 아니다. 정보가 없으면 확인되지 않음으로 표시한다.

## 데이터 흐름과 역할

```text
KOSHA XML → 원본/시각/checksum → 기존 정규화/품질 검증 → PostgreSQL staging
→ 기존 사람 검토/승인 → PostgreSQL 버전 반영 transaction
→ 같은 transaction의 activations INSERT를 WAL에서 읽음 (CDC)
→ Iceberg에 해당 승인 버전 전체 근거를 atomic overwrite
→ snapshot 선택/내용 checksum 확인 → PostgreSQL 처리 receipt → WAL 확인 위치 진전
→ Trino로 receipt snapshot과 원천 DB의 키/내용 대조
```

Airflow는 원천 배치 순서를 관리한다. CDC는 공공 API 조회가 아니라 PostgreSQL의 실제 WAL logical decoding이다. Iceberg는 버전 이력을 보관하고 Trino는 그 자료를 SQL로 조회한다. 사고 요청은 Trino·CDC 작업의 완료를 기다리지 않는다.

이 첫 CDC 구현은 test_decoding을 사용하는 fixture 전용 DB 실습이다. Kafka/상용 CDC 운영을 주장하지 않는다. raw WAL에는 staging 행도 포함될 수 있으므로 저장·로그에 남기지 않고 activations INSERT만 처리한다. 다른 형식의 activation 변경은 무시하지 않고 차단한다. 운영 적용에는 pgoutput publication 또는 Debezium, 제한된 복제 권한, slot lag/유효성 감시가 필요하다.

고유 키는 (stream, version, evidence_id)이며 CAS 단독 키가 아니다. 내용 변경이 식별자에 영향을 줄 수 있으므로 논리 항목 변경의 의미를 단정하지 않는다. 원천의 전체 스냅샷/삭제 계약이 미확인이므로 수집에서 사라진 행을 삭제로 전파하지 않는다. 기존 loss gate를 유지한다. 승인된 이전 버전으로 복구하는 경우 그 버전의 행 집합을 정확히 재현한다.

## 일관성과 실패 처리

- 검증·승인되지 않은 staging에는 activation이 없어 전달되지 않는다.
- activations와 head 변경은 기존 PostgreSQL transaction이다. 실패하면 둘 다 rollback된다.
- 같은 버전 활성화는 새 activation을 만들지 않는다. CDC 재실행은 receipt 또는 snapshot activation_id로 중복을 방지한다.
- Iceberg commit 후 receipt 기록 전 중단하면 최신 matching snapshot의 전체 내용 checksum을 확인하고 receipt를 복구한다. 다시 append하지 않는다.
- PyIceberg overwrite는 한 metadata transaction에 DELETE/APPEND 등 여러 snapshot을 남길 수 있다. 중간 DELETE snapshot을 서비스 버전으로 선택하지 않는다. receipt는 최종 검증 snapshot을 가리킨다.
- 전역 consumer advisory lock으로 sink 쓰기를 직렬화한다. receipt commit 이후 WAL을 acknowledge하여 실패 시 replay 가능하다. exactly-once 전달을 주장하지 않으며 replay의 멱등성을 검증한다.
- Trino 대조는 행 수뿐 아니라 전체 키와 필드 값을 비교한다. PostgreSQL/Trino의 문자열 정렬 차이가 누락으로 오인되지 않도록 동일 키 정렬로 비교한다.
- bootstrap은 현재 head만 가져온다. 이미 대기 중인 activation WAL이 있으면 먼저 consume하도록 차단한다. 과거 이벤트 전체 복구로 표현하지 않는다.

## 실제 서비스 전환 계획 — 아직 미구현

Iceberg를 사고 요청 중 직접 조회하지 않는다. Trino 대조가 통과한 snapshot을 고정해 기존 SQLite DB·resolver·retriever bundle을 생성하는 export 작업을 추가한다. bundle manifest에 source version, snapshot_id, 입력 checksum을 연결한다. 기존 승인·readiness 검사를 거친 하나의 bundle만 모델 runtime으로 선택한다. DB/검색모델은 같은 bundle이고 한 요청은 같은 runtime 버전을 사용해야 한다. BFF 응답의 기존 provenance.dataVersion으로 추적한다. 새로운 bundle 준비·CDC·Trino 장애는 현재 승인 runtime을 변경하지 않는다.

출처 URL·원천 장/항목·개정일·수집일은 실제 원천 필드와 매핑을 확인한 뒤 계약을 확장한다. 날짜를 버전 문자열에서 추정하지 않는다. 서비스 전환 전 다음 사례를 BFF→모델→화면에서 확인한다: 누출/화재/노출, 물질 미확인, 자료 없음, 물질 확인 취소, 버전 불일치, 이전 bundle 복구. 현장 전문가가 내용과 화면의 사용성을 검토해야 현업 적합성을 평가할 수 있다.

## 로컬 실행

저장소 루트에서 실행한다. 기존 환경과 다른 loopback 포트/독립 Compose 프로젝트다. 2026-10-07부터 객체 저장소는 Versity Gateway의 named volume을 사용한다. 이전 Moto 객체를 백업·이관하고 restart/recreate 후 checksum을 확인했다. [실제 원천·파일 보존 검증](SOURCE_SERVICE_LOCAL.md)을 참고한다. 실제 AWS 연결/비용은 없다. volume 삭제·디스크 장애의 백업 보장을 뜻하지 않는다.

```sh
uv pip install --python .venv/bin/python -e '.[dev,postgres]'
uv pip install --python .venv/bin/python -r local/lakehouse/requirements.txt
docker compose -f local/lakehouse/compose.yaml up -d
source local/lakehouse/env.example
.venv/bin/python local/lakehouse/prepare_fixture_bucket.py
.venv/bin/python -m chemiguard119.reference_lakehouse init
.venv/bin/python -m pytest tests/test_reference_lakehouse.py
.venv/bin/python -m chemiguard119.reference_lakehouse consume
.venv/bin/python -m chemiguard119.reference_lakehouse verify
.venv/bin/python -m chemiguard119.reference_lakehouse status
```

기존 기준정보 실행/승인/복구 CLI는 reference_postgres.py와 reference_batch.py를 재사용한다. 복구 이후 consume/verify를 실행한다. source API 최신 응답을 과거 날짜 응답으로 표현하지 않는다. 이 DAG는 수동 실행이며 소스 갱신 주기를 CDC 폴링 주기와 혼동하지 않는다. Airflow는 CHEMICHECK_LAKEHOUSE_PYTHON으로 지정한 별도 worker Python을 호출한다. Iceberg 의존성을 Airflow 환경에 설치하지 않는다. 두 환경의 SQLAlchemy 요구사항 충돌을 피한다. DAG 재현은 scripts/data/verify_lakehouse_airflow.py를 Airflow Python에서 실행한다.

## 검증 및 한계

실제 PostgreSQL 17 WAL, PyIceberg 0.10.0 Parquet/metadata, Trino 483에서 합성 fixture를 실행했다. 승인 전 전달 없음, 정상 2행, 같은 버전 재실행, 3행 버전 반영, commit/receipt 사이 실패·재개, 이전 snapshot 시간여행, 2행 이전 버전 복구와 SQL 키/내용 일치를 검사했다. 원천 API의 지속적 가용성에 의존하지 않는다.

남은 제약: 실제 전체 원천 승인, 서비스 runtime export/전환, BFF·화면 전체 연결 검증, schema evolution·논리 항목 update/delete·slot 손실 복구·동시 worker fault 실험, 전문가 검토, 사람 시간 및 서비스 효과 측정. 로컬 성공은 운영 안정성·현장 안전성·시간 절감의 증명이 아니다.

공식 참고: [PyIceberg API](https://py.iceberg.apache.org/api/), [Trino JDBC catalog](https://trino.io/docs/current/object-storage/metastores.html#jdbc-catalog), [PostgreSQL logical decoding](https://www.postgresql.org/docs/17/logicaldecoding-example.html), [Moto server fixture](https://docs.getmoto.org/en/latest/docs/server_mode.html).
