# 케미체크119 모델 API 운영 가이드

## 1. 쉬운 설명

모델 API는 요청마다 추적 번호인 `request_id`를 부여합니다. 같은 번호를 API 응답과 서버
로그에서 찾으면 어떤 요청이 언제, 어느 API에서, 얼마나 걸려 끝났는지 확인할 수 있습니다.

화학사고 신고문과 API Key는 로그에 남기지 않습니다. 운영자는 신고 내용을 보지 않고도 다음을
확인할 수 있습니다.

- API가 정상 응답했는지
- 인증 실패인지 서버 장애인지
- 어느 API가 느린지
- 문제가 발생한 요청의 추적 번호가 무엇인지

## 2. 구조화 요청 로그

각 요청은 stdout에 `chemicheck119-log-v1` JSON 한 줄로 기록됩니다.

```json
{
  "timestamp": "2026-07-28T10:00:00+00:00",
  "schema_version": "chemicheck119-log-v1",
  "level": "INFO",
  "event": "http_request_completed",
  "request_id": "REQ-BE-20260728-0001",
  "service_name": "chemicheck119-model-api",
  "service_version": "0.3.0",
  "deployment_environment": "staging",
  "authentication_mode": "API_KEY",
  "http_request_method": "POST",
  "http_route": "/api/v1/incidents/analyze",
  "http_response_status_code": 200,
  "duration_ms": 23.418,
  "outcome": "SUCCESS"
}
```

`http_route`에는 query string을 포함하지 않습니다. 동적 주소를 나중에 추가하더라도 원본 URL보다
FastAPI route template을 우선하며, 매칭되지 않은 주소는 `<unmatched>`로 기록해 URL에 들어간
민감값과 불필요하게 많은 로그 종류가 생기지 않도록 합니다.

## 3. 기록하지 않는 정보

다음 값은 공통 요청 로그에 넣지 않습니다.

- `X-API-Key`
- HTTP header 전체
- query string
- 요청·응답 body
- 신고 원문
- 상세 주소와 시설 정보
- 대원 이름과 사용자 식별정보
- artifact bundle URL과 manifest trust anchor

OWASP는 인증정보·접근 토큰·개인정보와 높은 보안등급의 정보를 일반 로그에서 제거하거나
마스킹하도록 권고합니다. 케미체크119는 애초에 공통 요청 로그 필드로 전달하지 않는 방식을
사용합니다.

근거:

- [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)
- [OpenTelemetry HTTP semantic conventions](https://opentelemetry.io/docs/specs/semconv/http/http-spans/)

## 4. 환경변수

```text
CHEMIGUARD119_LOG_LEVEL=INFO
```

지원 값은 `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`입니다. 값이 없거나 잘못되면
`INFO`를 사용합니다. 요청 완료 이벤트는 `INFO`이므로 정상 요청까지 운영 지표로 집계하려면
운영 환경에서 `INFO`를 유지합니다.

## 5. 기본 모니터링 항목

배포 플랫폼의 로그 검색 또는 집계 기능으로 다음을 계산합니다.

| 항목 | 로그 필드 | 처음 적용할 기준 |
|---|---|---|
| 요청 수 | `event`, `http_route` | 5분 단위 |
| 오류율 | `http_response_status_code` | 5xx 비율 |
| 인증 실패 | 상태 코드 `401` | 갑작스러운 증가 확인 |
| 준비 실패 | route `/health/ready`, 상태 `503` | 한 번이라도 반복되면 확인 |
| 지연시간 | `duration_ms` | route별 p50·p95 |
| 요청 추적 | `request_id` | BE 응답·로그와 같은 값 검색 |

현재 저장소는 JSON 로그 생성까지 담당합니다. Prometheus, OpenTelemetry Collector, SIEM과
로그 보존 정책은 배포 플랫폼이 확정된 뒤 별도 평가와 비용 승인을 거쳐 연결합니다.

## 6. 장애 확인 순서

1. `/health/live`와 `/health/ready` 상태를 확인합니다.
2. 백엔드 또는 사용자가 전달한 `request_id`를 로그에서 검색합니다.
3. `http_route`, 상태 코드, `duration_ms`, 배포 환경과 서비스 버전을 확인합니다.
4. `401`이면 백엔드 Secret 주입과 호출 헤더를 확인합니다.
5. `503`이면 readiness의 인증·artifact·정책 상태를 확인합니다.
6. `500`이면 같은 시각의 오류 stack trace를 확인하되 신고 원문을 로그에 추가하지 않습니다.
7. 배포 직후 오류가 시작됐다면 이미지·manifest·Git commit을 한 묶음으로 롤백합니다.

## 7. 로컬 검증

```bash
CHEMIGUARD119_ALLOW_ANONYMOUS=true \
CHEMIGUARD119_LOG_LEVEL=INFO \
chemiguard119-api
```

다른 터미널에서 요청 ID를 지정합니다.

```bash
curl --fail http://127.0.0.1:8000/health/live \
  -H "X-Request-Id: REQ-LOCAL-LOG-001"
```

서버 stdout에 `REQ-LOCAL-LOG-001`이 포함된 JSON 한 줄이 있어야 합니다. 해당 줄에 curl의
query, body, API Key가 출력되면 배포하지 않습니다.

## 8. 현재 한계

- 중앙 로그 저장소와 보존 기간은 아직 정해지지 않았습니다.
- 분산 trace와 metrics exporter는 아직 포함하지 않습니다.
- `analysis_id` 기반 업무 이벤트 집계는 아직 구현하지 않았습니다.
- 애플리케이션 내부 timeout·동시성 제한과 gateway rate limit은 별도 운영 과제입니다.
