"use strict";

const schemaUrl = new URL("./openapi.json", window.location.href);

// 문서 fetch 외 요청은 만들지 않는다. 서버의 IAM을 대신하는 보안 장치는 아니다.
function documentationOnly(request) {
  const url = new URL(request.url, window.location.href);
  if (url.href !== schemaUrl.href || (request.method || "GET").toUpperCase() !== "GET") {
    throw new Error("이 페이지는 읽기 전용 문서입니다. 실제 API 호출은 Backend에서 수행하세요.");
  }
  request.credentials = "omit";
  return request;
}

window.ui = SwaggerUIBundle({
  url: schemaUrl.href,
  dom_id: "#swagger-ui",
  deepLinking: true,
  docExpansion: "none",
  defaultModelsExpandDepth: -1,
  filter: true,
  displayRequestDuration: false,
  supportedSubmitMethods: [],
  tryItOutEnabled: false,
  persistAuthorization: false,
  withCredentials: false,
  validatorUrl: null,
  queryConfigEnabled: false,
  requestInterceptor: documentationOnly,
  presets: [SwaggerUIBundle.presets.apis],
  layout: "BaseLayout",
});

fetch(new URL("./build-info.json", window.location.href), { credentials: "omit" })
  .then((response) => {
    if (!response.ok) throw new Error("문서 버전 정보 없음");
    return response.json();
  })
  .then((info) => {
    document.getElementById("build-info").textContent =
      `문서 commit ${info.documentation_source_commit.slice(0, 7)} · Swagger UI ${info.swagger_ui_version} · ${info.paths}개 경로`;
  })
  .catch(() => {
    document.getElementById("build-info").textContent = "문서 버전 정보를 읽지 못했습니다. build-info.json을 확인하세요.";
  });
