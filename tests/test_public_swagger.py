from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import build_public_swagger as builder


def test_public_bundle_contains_only_explicit_allowlist(tmp_path, monkeypatch):
    assets = {name: name.encode() for name in builder.ASSET_FILES}
    monkeypatch.setattr(builder, "download_assets", lambda lock: assets)
    output = tmp_path / "site"
    info = builder.build(output, "a" * 40)
    actual = {
        str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()
    }
    assert actual == {
        "index.html",
        "app.js",
        "style.css",
        "openapi.json",
        "build-info.json",
        *("assets/" + name for name in builder.ASSET_FILES),
    }
    raw = (builder.ROOT / builder.CONTRACT).read_bytes()
    assert (output / "openapi.json").read_bytes() == raw
    assert info["contract_sha256"] == hashlib.sha256(raw).hexdigest()
    assert info["runtime_deployment_verified_by_this_build"] is False
    assert json.loads((output / "build-info.json").read_text()) == info
    with pytest.raises(ValueError, match="기존 출력"):
        builder.build(output, "a" * 40)


@pytest.mark.parametrize("commit", ["main", "abc123", "../main", "A" * 40])
def test_commit_must_be_full_sha(tmp_path, commit):
    with pytest.raises(ValueError, match="40자리"):
        builder.build(tmp_path / "site", commit)


@pytest.mark.parametrize(
    "private_text",
    [
        "/Users/example/private",
        "private-data/raw.wav",
        "someone@example.com",
        "ghp_" + "x" * 30,
    ],
)
def test_publication_rejects_private_patterns_without_echoing(private_text):
    raw = (
        (builder.ROOT / builder.CONTRACT)
        .read_text()
        .replace("케미체크119 모델 API", private_text)
    )
    with pytest.raises(ValueError, match="본문은 출력하지") as error:
        builder.validate_contract(raw.encode())
    assert private_text not in str(error.value)


def test_security_and_local_reference_contracts():
    spec = json.loads((builder.ROOT / builder.CONTRACT).read_bytes())
    original = deepcopy(spec)
    spec["paths"]["/api/v1/agents/incidents/brief"]["post"]["security"] = []
    with pytest.raises(ValueError, match="인증 계약"):
        builder.validate_contract(json.dumps(spec).encode())
    original["components"]["schemas"]["Injected"] = {"$ref": "https://example.com/spec"}
    with pytest.raises(ValueError, match="외부 문서"):
        builder.validate_contract(json.dumps(original).encode())


def test_asset_integrity_and_origin_are_enforced(monkeypatch):
    lock = json.loads((builder.ROOT / builder.ASSET_LOCK).read_text())
    invalid = deepcopy(lock)
    invalid["assets"][0]["url"] = "https://example.com/untrusted.js"
    with pytest.raises(ValueError, match="공식 고정"):
        builder.download_assets(invalid)

    class Response:
        def __init__(self, url):
            self.url = url

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def geturl(self):
            return self.url

        def read(self, limit):
            assert limit == builder.MAX_ASSET_BYTES + 1
            return b"incorrect bytes"

    monkeypatch.setattr(builder, "urlopen", lambda url, timeout: Response(url))
    with pytest.raises(ValueError, match="SHA-256"):
        builder.download_assets(lock)


def test_read_only_browser_and_workflow_boundaries():
    root = builder.ROOT
    app = (root / builder.TEMPLATES / "app.js").read_text()
    for setting in (
        "supportedSubmitMethods: []",
        "tryItOutEnabled: false",
        "persistAuthorization: false",
        "validatorUrl: null",
        "queryConfigEnabled: false",
        'request.credentials = "omit"',
        "requestInterceptor: documentationOnly",
    ):
        assert setting in app
    html = (root / builder.TEMPLATES / "index.html").read_text()
    assert "connect-src 'self'" in html and "script-src 'self'" in html
    assert "https://cdn" not in html and "<script>" not in html
    workflow = (root / ".github/workflows/publish-api-docs.yml").read_text()
    assert 'docker build --tag "$image" "$context"' in workflow
    assert "secrets." not in workflow
    assert "--max=1 --max-instances=1" in workflow
    assert "chemicheck119-docs-public@chemi-check.iam.gserviceaccount.com" in workflow
    assert "chemicheck119-api-docs" in workflow
    assert "--set-secrets" not in workflow
    assert "workflow_run.conclusion == 'success'" in workflow
    assert "workflow_run.event == 'push'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "source-commit" in workflow


def test_input_symlink_is_rejected(tmp_path, monkeypatch):
    (tmp_path / builder.CONTRACT).parent.mkdir(parents=True)
    (tmp_path / builder.CONTRACT).symlink_to(Path(__file__).resolve())
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        builder.build(tmp_path / "site", "a" * 40)


def test_static_server_never_serves_models_or_proxies_requests(tmp_path):
    import threading
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from scripts.deployment.public_swagger_server import make_server

    (tmp_path / "index.html").write_text("public docs")
    (tmp_path / "openapi.json").write_text("{}")
    (tmp_path / ".env").write_text("never public")
    server = make_server(tmp_path)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base, timeout=2) as response:
            assert response.read() == b"public docs"
            assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
        for path in (
            "/.env",
            "/assets/",
            "/%2eenv",
            "/../.env",
            "/server.py",
            "/api/v1/meta",
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(base + path, timeout=2)
            assert error.value.code == 404
        with pytest.raises(HTTPError) as error:
            urlopen(
                Request(base + "/api/v1/agents/incidents/brief", data=b"{}"), timeout=2
            )
        assert error.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
