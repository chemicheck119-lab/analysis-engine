"""공개 계약과 고정 UI 파일만 별도 정적 사이트로 만든다. Secret/모델 로드 없음."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = Path("contracts/generated/model-api-v1.openapi.json")
ASSET_LOCK = Path("config/public_swagger_assets.json")
TEMPLATES = Path("docs/public-swagger")
UI_FILES = ("index.html", "app.js", "style.css")
ASSET_FILES = {"swagger-ui-bundle.js", "swagger-ui.css", "LICENSE.txt"}
MAX_ASSET_BYTES = 3 * 1024 * 1024
PRIVATE_PATTERNS = (
    r"/Users/",
    r"private-data/",
    r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----",
    r"AIza[0-9A-Za-z_-]{30,}",
    r"gh[pousr]_[0-9A-Za-z]{20,}",
    r"Bearer\s+eyJ[0-9A-Za-z_-]+\.",
    r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
)


def validate_contract(raw: bytes) -> dict:
    """명백한 비공개 패턴만 차단한다. 개인정보 전체 검수의 대체물이 아니다."""
    text = raw.decode("utf-8")
    if any(re.search(pattern, text) for pattern in PRIVATE_PATTERNS):
        raise ValueError(
            "공개 계약에 비공개 패턴이 있습니다. 본문은 출력하지 않습니다."
        )
    spec = json.loads(text)
    for path in (
        "/api/v1/agents/incidents/brief",
        "/api/v1/agents/incidents/brief/stream",
    ):
        if spec["paths"][path]["post"]["security"] != [{"APIKeyHeader": []}]:
            raise ValueError("공개 문서에서 API 인증 계약을 제거할 수 없습니다.")
    for ref in re.findall(r'"\$ref"\s*:\s*"([^"]+)"', text):
        if not ref.startswith("#/components/"):
            raise ValueError("외부 문서를 자동 조회하는 $ref는 공개하지 않습니다.")
    return spec


def download_assets(lock: dict) -> dict[str, bytes]:
    """공식 Swagger UI commit의 세 파일을 크기·SHA-256 검증 후 사용한다."""
    if not re.fullmatch(r"[0-9a-f]{40}", lock["commit"]):
        raise ValueError("Swagger UI commit을 고정해야 합니다.")
    prefix = (
        "https://raw.githubusercontent.com/swagger-api/swagger-ui/"
        + lock["commit"]
        + "/"
    )
    entries = lock["assets"]
    if {entry["file"] for entry in entries} != ASSET_FILES or len(entries) != 3:
        raise ValueError("허용된 UI 파일 세 개만 내려받습니다.")
    result = {}
    for entry in entries:
        suffix = (
            "LICENSE" if entry["file"] == "LICENSE.txt" else "dist/" + entry["file"]
        )
        if entry["url"] != prefix + suffix:
            raise ValueError("공식 고정 UI asset URL이 아닙니다.")
        if not 0 < entry["bytes"] <= MAX_ASSET_BYTES:
            raise ValueError("UI asset 크기 한도를 초과했습니다.")
        with urlopen(entry["url"], timeout=30) as response:
            if response.geturl() != entry["url"]:
                raise ValueError("UI asset redirect는 허용하지 않습니다.")
            data = response.read(MAX_ASSET_BYTES + 1)
        if (
            len(data) != entry["bytes"]
            or hashlib.sha256(data).hexdigest() != entry["sha256"]
        ):
            raise ValueError("UI asset 크기 또는 SHA-256 불일치")
        result[entry["file"]] = data
    return result


def build(output: Path, source_commit: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("문서 source commit은 40자리 SHA여야 합니다.")
    if output.exists() or output.is_symlink():
        raise ValueError("기존 출력 경로를 덮어쓰지 않습니다. 새 경로를 사용하세요.")
    inputs = [ROOT / CONTRACT, ROOT / ASSET_LOCK]
    inputs.extend(ROOT / TEMPLATES / name for name in UI_FILES)
    if any(path.is_symlink() for path in inputs):
        raise ValueError("공개 문서 입력에 symlink를 허용하지 않습니다.")
    raw = (ROOT / CONTRACT).read_bytes()
    spec = validate_contract(raw)
    lock = json.loads((ROOT / ASSET_LOCK).read_text())
    assets = download_assets(lock)
    files = {name: (ROOT / TEMPLATES / name).read_bytes() for name in UI_FILES}
    files["openapi.json"] = (
        raw  # 이미 공개된 Git 계약과 byte-identical. runtime에서 수집하지 않음.
    )
    files.update({"assets/" + name: data for name, data in assets.items()})
    info = {
        "documentation_source_commit": source_commit,
        "contract_source": str(CONTRACT),
        "contract_sha256": hashlib.sha256(raw).hexdigest(),
        "swagger_ui_version": lock["version"],
        "swagger_ui_commit": lock["commit"],
        "mode": "PUBLIC_READ_ONLY_DOCUMENTATION",
        "runtime_deployment_verified_by_this_build": False,
        "paths": len(spec["paths"]),
        "files_sha256": {
            name: hashlib.sha256(data).hexdigest()
            for name, data in sorted(files.items())
        },
    }
    files["build-info.json"] = (
        json.dumps(info, ensure_ascii=False, indent=2) + "\n"
    ).encode()
    output.mkdir(parents=True)
    for name, data in files.items():
        target = output / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    info = build(args.output, args.source_commit)
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
