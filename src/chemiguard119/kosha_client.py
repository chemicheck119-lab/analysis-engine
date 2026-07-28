"""KOSHA 물질안전보건자료 공식 OpenAPI 수집 클라이언트.

API 응답은 검색·검토용 staging 데이터로만 변환한다. 수집 결과를 제조사·수입자가
작성한 최신 MSDS나 현장 물질 확인의 대체물로 간주하지 않는다.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

from chemiguard119.utils import normalize_cas, valid_cas_checksum


KOSHA_API_BASE_URL = "https://apis.data.go.kr/B552468/msdschem"
KOSHA_SOURCE_PAGE = "https://www.data.go.kr/data/15157612/openapi.do"
KOSHA_SEARCH_PATH = "/getChemList"
KOSHA_DETAIL_PATH_TEMPLATE = "/getChemDetail{section:02d}"
KOSHA_DETAIL_SECTIONS = tuple(range(1, 17))
KOSHA_MAX_XML_RESPONSE_BYTES = 10 * 1024 * 1024
KOSHA_STAGING_COLUMNS = (
    "레코드ID",
    "화학물질ID",
    "CAS번호",
    "화학물질명_국문",
    "MSDS_장번호",
    "MSDS_항목명_국문",
    "상세내용",
    "MSDS_항목코드",
    "상위항목코드",
    "계층수준",
    "표시순서",
    "EC번호",
    "국내기존화학물질번호",
    "UN번호",
    "최종개정일",
    "시나리오역할",
    "검색기준_CAS번호",
    "검색기준_화학물질명",
    "KOSHA확인값",
    "공개여부",
    "자료출처",
    "원본데이터셋ID",
    "수집일시",
)

FetchXml = Callable[[str, float], bytes]
Sleep = Callable[[float], None]


class KoshaApiError(RuntimeError):
    """KOSHA API 또는 응답 계약 오류."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def _element_text(element: ET.Element, tag: str) -> str:
    for found in element.iter():
        if found.tag.rsplit("}", 1)[-1] == tag:
            return (found.text or "").strip()
    return ""


def _item_dict(item: ET.Element) -> dict[str, str]:
    return {
        child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in list(item)
    }


def _stable_record_id(
    chem_id: str,
    section: int,
    item: dict[str, str],
) -> str:
    parts = (
        chem_id,
        str(section),
        item.get("msdsItemCode", ""),
        item.get("upMsdsItemCode", ""),
        item.get("msdsItemNo", ""),
        item.get("ordrIdx", ""),
        item.get("lev", ""),
        item.get("msdsItemNameKor", ""),
        item.get("itemDetail", ""),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"KOSHAAPI-{digest[:24].upper()}"


class KoshaMsdsClient:
    """공식 XML API의 정확 CAS 검색과 16개 MSDS 장 조회를 담당한다."""

    def __init__(
        self,
        service_key: str,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        request_interval_seconds: float = 0.0,
        fetch_xml: FetchXml | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        decoded_key = urllib.parse.unquote((service_key or "").strip())
        if not decoded_key:
            raise KoshaApiError(
                "KOSHA_API_KEY_MISSING",
                "KOSHA_API_SERVICE_KEY 환경변수가 비어 있습니다.",
            )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds는 0보다 커야 합니다.")
        if max_retries < 0:
            raise ValueError("max_retries는 0 이상이어야 합니다.")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds는 0 이상이어야 합니다.")

        self._service_key = decoded_key
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._request_interval_seconds = request_interval_seconds
        self._fetch_xml = fetch_xml or self._default_fetch_xml
        self._sleep = sleep
        self._has_requested = False
        self.request_count = 0

    @staticmethod
    def _default_fetch_xml(url: str, timeout_seconds: float) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/xml",
                "User-Agent": "Chemicheck119-Research/1.0 (KOSHA official OpenAPI)",
            },
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read(KOSHA_MAX_XML_RESPONSE_BYTES + 1)
        if len(payload) > KOSHA_MAX_XML_RESPONSE_BYTES:
            raise KoshaApiError(
                "KOSHA_RESPONSE_TOO_LARGE",
                "KOSHA API 응답이 허용 크기를 초과했습니다.",
            )
        return payload

    def _url(self, path: str, params: dict[str, str]) -> str:
        query = urllib.parse.urlencode(
            {**params, "serviceKey": self._service_key},
            doseq=False,
        )
        return f"{KOSHA_API_BASE_URL}{path}?{query}"

    def _request_items(self, path: str, params: dict[str, str]) -> list[dict[str, str]]:
        url = self._url(path, params)
        for attempt in range(self._max_retries + 1):
            if self._has_requested and self._request_interval_seconds:
                self._sleep(self._request_interval_seconds)
            self._has_requested = True
            self.request_count += 1
            try:
                payload = self._fetch_xml(url, self._timeout_seconds)
                upper_payload = payload.upper()
                if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
                    raise KoshaApiError(
                        "KOSHA_RESPONSE_UNSAFE_XML",
                        "KOSHA API 응답에 허용하지 않는 XML 선언이 포함됐습니다.",
                    )
                root = ET.fromstring(payload)
                result_code = _element_text(root, "resultCode")
                result_message = _element_text(root, "resultMsg")
                if not result_code:
                    result_code = _element_text(root, "returnReasonCode")
                    result_message = result_message or _element_text(root, "errMsg")
                if result_code and result_code not in {"0", "00", "000"}:
                    raise KoshaApiError(
                        f"KOSHA_API_{result_code}",
                        result_message or "KOSHA API가 오류를 반환했습니다.",
                    )
                return [
                    _item_dict(item)
                    for item in root.iter()
                    if item.tag.rsplit("}", 1)[-1] == "item"
                ]
            except KoshaApiError:
                raise
            except urllib.error.HTTPError as error:
                raise KoshaApiError(
                    f"KOSHA_HTTP_{error.code}",
                    f"KOSHA API가 HTTP {error.code} 오류를 반환했습니다.",
                ) from None
            except (TimeoutError, urllib.error.URLError):
                if attempt < self._max_retries:
                    self._sleep(min(0.25 * (2**attempt), 1.0))
                    continue
            except ET.ParseError as error:
                raise KoshaApiError(
                    "KOSHA_RESPONSE_INVALID_XML",
                    "KOSHA API 응답을 XML로 해석할 수 없습니다.",
                ) from error
            break
        raise KoshaApiError(
            "KOSHA_NETWORK_ERROR",
            "KOSHA API 통신에 실패했습니다. 잠시 후 다시 시도하세요.",
            retryable=True,
        ) from None

    def search_by_cas(self, cas_number: str) -> list[dict[str, str]]:
        cas = normalize_cas(cas_number)
        if not valid_cas_checksum(cas):
            raise KoshaApiError(
                "INVALID_CAS_NUMBER",
                f"CAS 형식 또는 체크섬이 유효하지 않습니다: {cas!r}",
            )
        items = self._request_items(
            KOSHA_SEARCH_PATH,
            {
                "searchWrd": cas,
                "searchCnd": "1",
                "numOfRows": "100",
                "pageNo": "1",
            },
        )
        return [item for item in items if normalize_cas(item.get("casNo")) == cas]

    def fetch_section(self, chem_id: str, section: int) -> list[dict[str, str]]:
        chemical_id = (chem_id or "").strip()
        if not chemical_id:
            raise KoshaApiError(
                "KOSHA_CHEM_ID_MISSING",
                "KOSHA 화학물질ID가 비어 있습니다.",
            )
        if section not in KOSHA_DETAIL_SECTIONS:
            raise KoshaApiError(
                "KOSHA_SECTION_INVALID",
                f"MSDS 장번호는 1~16이어야 합니다: {section}",
            )
        return self._request_items(
            KOSHA_DETAIL_PATH_TEMPLATE.format(section=section),
            {"chemId": chemical_id},
        )

    def collect_cas(
        self,
        cas_number: str,
        *,
        sections: Iterable[int] = KOSHA_DETAIL_SECTIONS,
    ) -> dict[str, Any]:
        cas = normalize_cas(cas_number)
        request_count_before = self.request_count
        candidates = self.search_by_cas(cas)
        by_chemical_id: dict[str, dict[str, str]] = {}
        for candidate in candidates:
            chemical_id = candidate.get("chemId", "").strip()
            if not chemical_id:
                raise KoshaApiError(
                    "KOSHA_SEARCH_CONTRACT_ERROR",
                    f"CAS {cas} 검색 결과에 chemId가 없습니다.",
                )
            by_chemical_id.setdefault(chemical_id, candidate)

        if not by_chemical_id:
            return {
                "cas_number": cas,
                "status": "NOT_FOUND",
                "candidate_count": 0,
                "request_count": self.request_count - request_count_before,
                "records": [],
            }
        if len(by_chemical_id) > 1:
            return {
                "cas_number": cas,
                "status": "AMBIGUOUS_EXACT_CAS",
                "candidate_count": len(by_chemical_id),
                "candidate_chemical_ids": sorted(by_chemical_id),
                "request_count": self.request_count - request_count_before,
                "records": [],
            }

        selected = next(iter(by_chemical_id.values()))
        chemical_id = selected["chemId"].strip()
        normalized_sections = sorted(set(int(section) for section in sections))
        collected_at_utc = datetime.now(timezone.utc).isoformat()
        records: list[dict[str, str]] = []
        section_item_counts: dict[str, int] = {}
        for section in normalized_sections:
            items = self.fetch_section(chemical_id, section)
            section_item_counts[str(section)] = len(items)
            for item in items:
                records.append(
                    {
                        "레코드ID": _stable_record_id(chemical_id, section, item),
                        "화학물질ID": chemical_id,
                        "CAS번호": cas,
                        "화학물질명_국문": selected.get("chemNameKor", "").strip(),
                        "MSDS_장번호": str(section),
                        "MSDS_항목명_국문": item.get("msdsItemNameKor", "").strip(),
                        "상세내용": item.get("itemDetail", "").strip(),
                        "MSDS_항목코드": item.get("msdsItemCode", "").strip(),
                        "상위항목코드": item.get("upMsdsItemCode", "").strip(),
                        "계층수준": item.get("lev", "").strip(),
                        "표시순서": item.get("ordrIdx", "").strip(),
                        "EC번호": selected.get("enNo", "").strip(),
                        "국내기존화학물질번호": selected.get("keNo", "").strip(),
                        "UN번호": selected.get("unNo", "").strip(),
                        "최종개정일": selected.get("lastDate", "").strip(),
                        "시나리오역할": "",
                        "검색기준_CAS번호": cas,
                        "검색기준_화학물질명": selected.get("chemNameKor", "").strip(),
                        "KOSHA확인값": selected.get("koshaConfirm", "").strip(),
                        "공개여부": selected.get("openYn", "").strip(),
                        "자료출처": KOSHA_SOURCE_PAGE,
                        "원본데이터셋ID": "data.go.kr:15157612",
                        "수집일시": collected_at_utc,
                    }
                )
        return {
            "cas_number": cas,
            "status": "COLLECTED",
            "candidate_count": 1,
            "chemical_id": chemical_id,
            "chemical_name_ko": selected.get("chemNameKor", "").strip(),
            "last_date": selected.get("lastDate", "").strip(),
            "section_item_counts": section_item_counts,
            "request_count": self.request_count - request_count_before,
            "records": records,
        }


__all__ = [
    "KOSHA_API_BASE_URL",
    "KOSHA_DETAIL_SECTIONS",
    "KOSHA_SOURCE_PAGE",
    "KOSHA_STAGING_COLUMNS",
    "KoshaApiError",
    "KoshaMsdsClient",
]
