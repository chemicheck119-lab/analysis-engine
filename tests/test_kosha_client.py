from __future__ import annotations

import urllib.error
import urllib.parse

import pytest

from chemiguard119.kosha_client import KoshaApiError, KoshaMsdsClient


SEARCH_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE.</resultMsg></header>
  <body><items>
    <item>
      <lastDate>2026-07-01</lastDate>
      <casNo>67-56-1</casNo>
      <chemId>CHEM-METHANOL</chemId>
      <chemNameKor>\xeb\xa9\x94\xed\x83\x84\xec\x98\xac</chemNameKor>
      <enNo>200-659-6</enNo>
      <keNo>KE-23193</keNo>
      <unNo>1230</unNo>
      <openYn>Y</openYn>
      <koshaConfirm>Y</koshaConfirm>
    </item>
    <item>
      <casNo>64-17-5</casNo>
      <chemId>CHEM-ETHANOL</chemId>
      <chemNameKor>\xec\x97\x90\xed\x83\x84\xec\x98\xac</chemNameKor>
    </item>
  </items></body>
</response>
"""


def _detail_xml(section: int) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <response xmlns="urn:test">
      <header><resultCode>00</resultCode></header>
      <body><items><item>
        <msdsItemCode>S{section}</msdsItemCode>
        <upMsdsItemCode>ROOT-{section}</upMsdsItemCode>
        <msdsItemNo>{section}.1</msdsItemNo>
        <ordrIdx>1</ordrIdx>
        <lev>1</lev>
        <msdsItemNameKor>{section}장 항목</msdsItemNameKor>
        <itemDetail>{section}장 공식 상세</itemDetail>
      </item></items></body>
    </response>""".encode()


def test_collect_exact_cas_and_selected_sections() -> None:
    urls: list[str] = []

    def fetch(url: str, _timeout: float) -> bytes:
        urls.append(url)
        if "/getChemList?" in url:
            return SEARCH_XML
        if "/getChemDetail06?" in url:
            return _detail_xml(6)
        if "/getChemDetail10?" in url:
            return _detail_xml(10)
        raise AssertionError(url)

    client = KoshaMsdsClient(
        "decoded-service-key",
        fetch_xml=fetch,
        sleep=lambda _seconds: None,
    )
    result = client.collect_cas("67-56-1", sections=(6, 10))

    assert result["status"] == "COLLECTED"
    assert result["chemical_id"] == "CHEM-METHANOL"
    assert result["chemical_name_ko"] == "메탄올"
    assert result["request_count"] == 3
    assert result["section_item_counts"] == {"6": 1, "10": 1}
    assert [row["MSDS_장번호"] for row in result["records"]] == ["6", "10"]
    assert all(row["CAS번호"] == "67-56-1" for row in result["records"])
    assert all(row["시나리오역할"] == "" for row in result["records"])
    assert all(row["검색기준_CAS번호"] == "67-56-1" for row in result["records"])
    assert all(row["EC번호"] == "200-659-6" for row in result["records"])
    assert all(row["국내기존화학물질번호"] == "KE-23193" for row in result["records"])
    assert all(row["공개여부"] == "Y" for row in result["records"])
    assert all(
        row["원본데이터셋ID"] == "data.go.kr:15157612" for row in result["records"]
    )
    assert len({row["레코드ID"] for row in result["records"]}) == 2

    search_query = urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]).query)
    assert search_query["searchCnd"] == ["1"]
    assert search_query["searchWrd"] == ["67-56-1"]
    assert search_query["serviceKey"] == ["decoded-service-key"]


def test_encoded_service_key_is_not_double_encoded() -> None:
    requested_url = ""

    def fetch(url: str, _timeout: float) -> bytes:
        nonlocal requested_url
        requested_url = url
        return b"<response><header><resultCode>00</resultCode></header></response>"

    client = KoshaMsdsClient("abc%2B123%3D", fetch_xml=fetch)
    result = client.collect_cas("67-56-1", sections=(1,))

    assert result["status"] == "NOT_FOUND"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(requested_url).query)
    assert query["serviceKey"] == ["abc+123="]


def test_ambiguous_exact_cas_is_not_selected_automatically() -> None:
    xml = b"""<response><header><resultCode>00</resultCode></header><items>
    <item><casNo>67-56-1</casNo><chemId>A</chemId></item>
    <item><casNo>67-56-1</casNo><chemId>B</chemId></item>
    </items></response>"""
    client = KoshaMsdsClient("secret", fetch_xml=lambda _url, _timeout: xml)

    result = client.collect_cas("67-56-1")

    assert result["status"] == "AMBIGUOUS_EXACT_CAS"
    assert result["candidate_chemical_ids"] == ["A", "B"]
    assert result["records"] == []
    assert client.request_count == 1


def test_network_error_retries_without_exposing_service_key() -> None:
    attempts = 0
    sleeps: list[float] = []

    def fetch(_url: str, _timeout: float) -> bytes:
        nonlocal attempts
        attempts += 1
        raise urllib.error.URLError("temporary")

    client = KoshaMsdsClient(
        "do-not-leak",
        max_retries=2,
        fetch_xml=fetch,
        sleep=sleeps.append,
    )

    with pytest.raises(KoshaApiError) as captured:
        client.search_by_cas("67-56-1")

    assert captured.value.code == "KOSHA_NETWORK_ERROR"
    assert captured.value.retryable is True
    assert "do-not-leak" not in str(captured.value)
    assert attempts == 3
    assert sleeps == [0.25, 0.5]


def test_api_error_is_structured_and_not_retried() -> None:
    calls = 0

    def fetch(_url: str, _timeout: float) -> bytes:
        nonlocal calls
        calls += 1
        return (
            b"<response><header><resultCode>30</resultCode>"
            b"<resultMsg>SERVICE KEY IS NOT REGISTERED ERROR.</resultMsg>"
            b"</header></response>"
        )

    client = KoshaMsdsClient("secret", max_retries=2, fetch_xml=fetch)

    with pytest.raises(KoshaApiError) as captured:
        client.search_by_cas("67-56-1")

    assert captured.value.as_dict() == {
        "code": "KOSHA_API_30",
        "message": "SERVICE KEY IS NOT REGISTERED ERROR.",
        "retryable": False,
    }
    assert calls == 1


def test_unsafe_xml_declaration_is_rejected() -> None:
    payload = (
        b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY x "unsafe">]>'
        b"<response><item>&x;</item></response>"
    )
    client = KoshaMsdsClient(
        "secret",
        fetch_xml=lambda _url, _timeout: payload,
    )

    with pytest.raises(KoshaApiError) as captured:
        client.search_by_cas("67-56-1")

    assert captured.value.code == "KOSHA_RESPONSE_UNSAFE_XML"
    assert client.request_count == 1


def test_invalid_cas_is_rejected_before_request() -> None:
    client = KoshaMsdsClient(
        "secret",
        fetch_xml=lambda _url, _timeout: pytest.fail("request must not run"),
    )

    with pytest.raises(KoshaApiError, match="체크섬"):
        client.search_by_cas("67-56-2")

    assert client.request_count == 0
