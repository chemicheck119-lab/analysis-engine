# 현준의 단계별 직접 실습 계획

아래는 앞으로의 목표다. 완료 기록이 아니며 AI 실행과 사용자 직접 실행을 분리한다.

1. 서비스 목표: 소방대원이 확인해야 할 물질·상황·근거·미확인 항목을 설명한다. Airflow 성공과 현장에 필요한 정보 제공을 구분한다.
2. SQL 첫 실습: PostgreSQL의 head와 evidence를 JOIN하여 버전별 행 수를 조회한다. head는 현재 선택된 버전, evidence는 여러 버전의 자료이다. CAS 하나가 여러 행에 나타나는 이유를 설명한다.
3. 승인 게이트: fixture를 staging까지만 실행해 head가 바뀌지 않는지 직접 조회한다. 승인 fixture 이후 차이를 확인한다. 실제 원천 내용 승인과 구분한다.
4. CDC: 승인 event 발생 전후 status와 consume 출력의 차이를 확인한다. API 정기 수집과 DB 변경 전달을 구분한다.
5. Iceberg/Trino: receipt snapshot_id를 조회하고 같은 snapshot을 Trino SQL로 읽는다. 이전 snapshot과 새 snapshot의 키 차이를 설명한다.
6. 실패/복구: commit/receipt 사이 실패를 테스트에서 직접 재현하고 재실행 후 snapshot·행 수·receipt를 대조한다. 이전 버전 복구 후 서비스용 DB와 Trino의 같은 키를 확인한다.
7. 작은 직접 수정: 설명할 수 있는 SQL 또는 테스트 입력 한 곳을 수정하고 예상/실제 결과를 기록한다. 사용자의 commit/diff와 실행 명령을 남긴다.

각 단계는 목표 → 필요한 개념 → 실행 위치/명령 한 개 → 예상 결과 → 실제 결과 해석 순으로 진행한다. 처음부터 모든 도구를 설치하거나 위험 판단 문구를 수정하는 것을 요구하지 않는다.

첫 직접 실행(로컬 fixture DB가 실행 중일 때, 저장소 루트):

```sh
docker compose -f local/lakehouse/compose.yaml exec postgres psql -U postgres -d reference_lab -c "SELECT h.version, count(e.evidence_id) AS rows FROM reference_lab.heads h JOIN reference_lab.evidence e ON e.stream=h.stream AND e.version=h.version GROUP BY h.stream,h.version;"
```

여러 줄은 여러 테스트 stream의 head이며 운영 데이터가 아니다. 이 명령은 읽기 전용이다. 실행 뒤 현준이 결과를 설명한 경우에만 직접 실습으로 기록한다. 아직 사용자 실행·이해 확인·작업 시간 측정은 없다.
