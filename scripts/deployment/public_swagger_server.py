"""읽기 전용 Swagger 파일 8개만 제공한다. 모델 import·프록시·Secret 사용 없음."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

PUBLIC_FILES = {
    "/",
    "/index.html",
    "/app.js",
    "/style.css",
    "/openapi.json",
    "/build-info.json",
    "/assets/swagger-ui.css",
    "/assets/swagger-ui-bundle.js",
    "/assets/LICENSE.txt",
}


class DocumentationHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        # 방문자가 query에 잘못 넣은 정보도 애플리케이션 로그에 남기지 않는다.
        pass

    def allowed(self):
        return unquote(urlsplit(self.path).path) in PUBLIC_FILES

    def do_GET(self):
        if self.allowed():
            super().do_GET()
        else:
            self.send_error(404, "Not Found")

    def do_HEAD(self):
        if self.allowed():
            super().do_HEAD()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        self.send_error(405, "Read-only documentation")

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-cache")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; font-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        super().end_headers()


def make_server(root: Path, host="127.0.0.1", port=0):
    return ThreadingHTTPServer(
        (host, port), partial(DocumentationHandler, directory=str(root))
    )


if __name__ == "__main__":
    make_server(
        Path("/srv/site"), "0.0.0.0", int(os.environ.get("PORT", "8080"))
    ).serve_forever()
