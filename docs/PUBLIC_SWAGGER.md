# docs(api): 공개 Swagger와 비공개 모델 API

## 팀에 공유할 주소

공개 문서 주소는 `공개 Swagger 문서 배포` Actions의 실행 요약에서 확인합니다.
서비스는 `chemicheck119-api-docs`이며 실제 모델 API와 별도입니다.

문서는 로그인 없이 요청·응답 schema와 합성 예시를 읽는 용도입니다. 실제 모델 실행과는
별개이며 `Try it out`과 API 키 입력 UI를 비활성화합니다. 모델 API의 Cloud Run IAM과
X-API-Key는 그대로 유지합니다. 공개 문서를 만들기 위해 GCP를 공개 전환하지 않습니다.

구현 상태는 PR의 CI·Pages 배포·실제 HTTP/브라우저 검증 결과로 판단합니다. 정적 문서
배포 완료를 실제 모델 배포, Backend 연동 완료, 현장 안전 승인으로 표현하지 않습니다.

## 무엇을 공개하는가

- 이미 공개된 `contracts/generated/model-api-v1.openapi.json`을 byte-identical 복사.
- 프로젝트 작성 HTML/CSS/JS 3개, 문서 commit·파일 SHA-256을 담은 `build-info.json`.
- 공식 Swagger UI 5.32.15의 고정 commit·SHA-256 JS/CSS와 Apache 2.0 LICENSE.
- 원본·가중치·DB·전사문·Secret·일반 docs 폴더 전체는 업로드하지 않음.

빌드 파일 allowlist를 검사하고 크기·hash가 다른 UI asset, 외부 `$ref`, 알려진 비공개 경로·
credential·이메일 패턴은 거부합니다. 이 패턴 검사는 모든 개인정보 탐지나 사람 검수의
대체물이 아닙니다. 향후 예시를 추가할 때도 공개 합성 예시만 사용해야 합니다.

Swagger validator와 query-config를 끄고, 브라우저는 같은 사이트의 정적 파일만 읽습니다.
UI asset도 같은 사이트에서 제공하므로 방문자의 spec을 외부 validator/CDN에 전송하지
않습니다. 분석 endpoint는 공개 문서에서 실행하지 않습니다.

## 재현

```bash
python scripts/build_public_swagger.py \
  --output /새로운/정적문서/site \
  --source-commit "$(git rev-parse HEAD)"
python -m http.server 8092 --bind 127.0.0.1 --directory /새로운/정적문서/site
python -m pytest tests/test_public_swagger.py
```

출력 경로가 이미 있으면 덮어쓰지 않습니다. 빌드는 Python 표준 라이브러리만 사용하고,
공식 GitHub에서 고정 UI 파일 세 개를 내려받습니다. 원본 데이터나 모델 runtime은 필요 없습니다.

## GitHub Actions · 문서 전용 Cloud Run

GitHub Pages 생성은 조직 정책의 `administrators disabled Pages creation`으로 차단됐습니다.
조직 전체 정책을 바꾸지 않고 별도 정적 문서 서비스로 범위를 한정했습니다. 공개 저장소의 `main` push에 대한
`모델 API 검증` 성공 후 `공개 Swagger 문서 배포`가 실행됩니다. 수동 실행도 main과 같은
commit의 성공한 CI를 요구합니다. PR·실패한 CI로부터 문서를 공개하지 않습니다.

```bash
gh workflow run publish-api-docs.yml --repo chemicheck119-lab/analysis-engine --ref main
```

Actions는 기존 deploy 계정의 단기 OIDC 인증으로 정적 이미지와 문서 서비스만 배포합니다.
실행 계정 `chemicheck119-docs-public`에는 프로젝트 역할·모델·DB·Secret 접근 권한을 주지 않습니다.
deploy 계정에는 이 새 실행 계정을 배정할 수 있는 `serviceAccountUser`만 계정 수준으로 부여했습니다.
정적 파일·server.py·Dockerfile만 별도 build context에 복사하고 모델 코드·GCP credential은 이미지에 넣지 않습니다.
서버는 allowlist 파일의 GET/HEAD만 제공하고 분석 POST·경로 탐색·디렉터리 목록은 거부합니다.
문서 서비스만 allUsers 호출을 허용하며 원래 모델 서비스 IAM은 변경하지 않습니다.
최소 0·최대 1 인스턴스, CPU 1·128MiB·동시성 20·요청 timeout 10초로 제한합니다.
이는 비용의 금액 상한이 아니며 과도한 공개 트래픽에는 별도 대응이 필요합니다.
문서 commit과 실제 API 배포 commit은 다를 수 있으므로 실제 연동은
[비공개 모델 실행·인증 안내](ACTION_BRIEF.md) 및 배포 이슈를 함께 확인하세요.
SSE는 검증된 전체 snapshot의 initial/final 교체 계약이지 LLM 토큰 스트리밍이 아닙니다.

설계 검토 자료: [GitHub Pages custom workflow](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages),
[Swagger UI 설정](https://swagger.io/docs/open-source-tools/swagger-ui/usage/configuration/),
[고정 Swagger UI release](https://github.com/swagger-api/swagger-ui/releases/tag/v5.32.15).
