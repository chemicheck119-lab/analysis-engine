"""공식근거 registry의 외부 URL·문서 위치 drift를 점검한다.

온라인 API 요청 경로에서는 네트워크를 호출하지 않는다. 이 모듈은 CI의 정기 검사와
릴리스 전 수동 감사에서만 사용하며, 실패한 외부 근거를 조용히 정상으로 취급하지 않는다.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from chemiguard119.evidence_assurance import _load_registry, _validate_registry
from chemiguard119.utils import sha256_file


REFERENCE_DRIFT_SCHEMA_VERSION = "chemicheck119-reference-source-drift-v1"
MAX_RESPONSE_BYTES = 8_000_000
USER_AGENT = (
    "Mozilla/5.0 (compatible; ChemiCheck119ReferenceAudit/1.0; "
    "+https://github.com/chemicheck119/llm)"
)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style", "noscript"}:
            self._hidden_depth = max(0, self._hidden_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._hidden_depth == 0:
            self.parts.append(data)


FetchSource = Callable[[str, float], dict[str, Any]]


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _html_visible_text(body: bytes) -> str:
    parser = _VisibleTextParser()
    parser.feed(body.decode("utf-8", errors="ignore"))
    return _normalize_text(" ".join(parser.parts))


def _fetch_source(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("응답 크기가 8MB 제한을 초과했습니다.")
            return {
                "status_code": int(response.status),
                "final_url": response.geturl(),
                "content_type": response.headers.get_content_type().casefold(),
                "body": body,
            }
    except HTTPError as error:
        return {
            "status_code": int(error.code),
            "final_url": error.geturl(),
            "content_type": "",
            "body": b"",
            "error": f"HTTP {error.code}",
        }
    except (URLError, TimeoutError, OSError, ValueError) as error:
        return {
            "status_code": None,
            "final_url": url,
            "content_type": "",
            "body": b"",
            "error": f"{type(error).__name__}: {error}",
        }


def check_reference_sources(
    config_dir: Path,
    *,
    timeout_seconds: float = 15.0,
    fetch_source: FetchSource | None = None,
    checked_at_utc: str | None = None,
) -> dict[str, Any]:
    """registry의 모든 외부 근거를 가져와 host·형식·문서 위치를 검사한다.

    HTML은 registry의 ``content_probe``가 실제 본문에 있는지 검사한다. PDF처럼 추가
    파서가 필요한 바이너리는 URL·host·MIME까지만 검사하고 ``METADATA_ONLY`` 한계를
    보고한다. 이 결과는 화학 주장 자체의 전문가 승인을 뜻하지 않는다.
    """

    registry, registry_path = _load_registry(config_dir)
    authorities = _validate_registry(registry)
    fetch = fetch_source or _fetch_source
    checked_at = checked_at_utc or datetime.now(timezone.utc).isoformat()
    source_results: list[dict[str, Any]] = []

    for claim in registry["pair_claims"]:
        for source in claim["sources"]:
            authority = authorities[source["authority_id"]]
            expected_hosts = {
                str(host).casefold() for host in authority["allowed_hosts"]
            }
            fetched = fetch(source["source_url"], timeout_seconds)
            final_url = str(fetched.get("final_url") or source["source_url"])
            final_parsed = urlparse(final_url)
            status_code = fetched.get("status_code")
            content_type = str(fetched.get("content_type") or "").casefold()
            expected_content_type = str(source["expected_content_type_prefix"])
            errors: list[str] = []

            if not isinstance(status_code, int) or not 200 <= status_code < 400:
                errors.append("SOURCE_UNREACHABLE")
            if (
                final_parsed.scheme != "https"
                or (final_parsed.hostname or "").casefold() not in expected_hosts
            ):
                errors.append("FINAL_URL_HOST_DRIFT")
            if not content_type.startswith(expected_content_type.casefold()):
                errors.append("CONTENT_TYPE_DRIFT")

            probe = str(source.get("content_probe") or "").strip()
            locator_status = "METADATA_ONLY"
            if probe:
                body = fetched.get("body")
                visible_text = (
                    _html_visible_text(body)
                    if isinstance(body, bytes) and content_type.startswith("text/html")
                    else ""
                )
                locator_status = (
                    "VERIFIED" if _normalize_text(probe) in visible_text else "DRIFT"
                )
                if locator_status == "DRIFT":
                    errors.append("LOCATOR_PROBE_DRIFT")

            source_results.append(
                {
                    "claim_id": claim["claim_id"],
                    "source_id": source["source_id"],
                    "authority_id": source["authority_id"],
                    "source_url": source["source_url"],
                    "final_url": final_url,
                    "status_code": status_code,
                    "content_type": content_type or None,
                    "expected_content_type_prefix": expected_content_type,
                    "locator": source["locator"],
                    "locator_status": locator_status,
                    "status": "PASS" if not errors else "FAIL",
                    "errors": errors,
                    "fetch_error": fetched.get("error"),
                }
            )

    failed = [item for item in source_results if item["status"] == "FAIL"]
    limited = [
        item for item in source_results if item["locator_status"] == "METADATA_ONLY"
    ]
    overall_status = (
        "FAIL" if failed else ("PASS_WITH_LIMITATIONS" if limited else "PASS")
    )
    return {
        "schema_version": REFERENCE_DRIFT_SCHEMA_VERSION,
        "checked_at_utc": checked_at,
        "registry_sha256": sha256_file(registry_path),
        "status": overall_status,
        "summary": {
            "source_count": len(source_results),
            "passed_source_count": len(source_results) - len(failed),
            "failed_source_count": len(failed),
            "locator_verified_count": sum(
                item["locator_status"] == "VERIFIED" for item in source_results
            ),
            "locator_metadata_only_count": len(limited),
        },
        "sources": source_results,
        "expert_reviewed": False,
        "human_expert_substitute": False,
        "limitations": [
            "링크와 문서 위치 검사는 화학 주장 자체의 사람 전문가 검토가 아닙니다.",
            "PDF 자료는 본문 파서 없이 URL·기관 host·MIME과 locator 메타데이터만 검사합니다.",
            "외부기관의 일시적 장애도 실패로 기록되므로 담당자가 원문을 재확인해야 합니다.",
        ],
    }


__all__ = [
    "REFERENCE_DRIFT_SCHEMA_VERSION",
    "check_reference_sources",
]
