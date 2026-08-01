from __future__ import annotations

from chemiguard119.paths import CONFIG_DIR
from chemiguard119.reference_drift import check_reference_sources


def _fake_fetch(url: str, timeout_seconds: float) -> dict:
    del timeout_seconds
    if url.endswith("Chemical%20Hazards.pdf"):
        return {
            "status_code": 200,
            "final_url": url,
            "content_type": "application/pdf",
            "body": b"%PDF-fixture",
        }
    probes = {
        "4503": "chlorine gas",
        "00015111": "Editorial Note",
        "chemical-disinfectants": "Chlorine and Chlorine Compounds",
        "eics1119": "toxic and corrosive gases",
        "sodium-hypochlorite-general-information": "chlorine gas",
        "7794": "Explodes on contact with hydrochloric acid",
        "eics0717": "Risk of fire and explosion on contact with acids",
    }
    body = next(value for marker, value in probes.items() if marker in url)
    return {
        "status_code": 200,
        "final_url": url,
        "content_type": "text/html",
        "body": f"<html><body>{body}</body></html>".encode(),
    }


def test_reference_source_drift_passes_with_explicit_pdf_limit() -> None:
    report = check_reference_sources(
        CONFIG_DIR,
        fetch_source=_fake_fetch,
        checked_at_utc="2026-08-01T00:00:00+00:00",
    )

    assert report["status"] == "PASS_WITH_LIMITATIONS"
    assert report["summary"] == {
        "source_count": 8,
        "passed_source_count": 8,
        "failed_source_count": 0,
        "locator_verified_count": 7,
        "locator_metadata_only_count": 1,
    }
    assert report["expert_reviewed"] is False
    assert report["human_expert_substitute"] is False


def test_reference_source_drift_detects_redirected_host() -> None:
    def forged_fetch(url: str, timeout_seconds: float) -> dict:
        result = _fake_fetch(url, timeout_seconds)
        if url.endswith("/chemical/7794"):
            result["final_url"] = "https://example.com/copied-source"
        return result

    report = check_reference_sources(CONFIG_DIR, fetch_source=forged_fetch)

    forged = next(
        item
        for item in report["sources"]
        if item["source_id"] == "CAMEO_7794_REACTIVITY_PROFILE"
    )
    assert report["status"] == "FAIL"
    assert forged["errors"] == ["FINAL_URL_HOST_DRIFT"]


def test_reference_source_drift_detects_locator_change() -> None:
    def changed_fetch(url: str, timeout_seconds: float) -> dict:
        result = _fake_fetch(url, timeout_seconds)
        if "eics0717" in url:
            result["body"] = b"<html><body>document moved</body></html>"
        return result

    report = check_reference_sources(CONFIG_DIR, fetch_source=changed_fetch)

    changed = next(
        item for item in report["sources"] if item["source_id"] == "ICSC_0717_SODIUM"
    )
    assert report["status"] == "FAIL"
    assert changed["locator_status"] == "DRIFT"
    assert changed["errors"] == ["LOCATOR_PROBE_DRIFT"]


def test_reference_source_drift_detects_unreachable_source() -> None:
    def unavailable_fetch(url: str, timeout_seconds: float) -> dict:
        if "00015111" in url:
            return {
                "status_code": 503,
                "final_url": url,
                "content_type": "text/html",
                "body": b"",
                "error": "HTTP 503",
            }
        return _fake_fetch(url, timeout_seconds)

    report = check_reference_sources(CONFIG_DIR, fetch_source=unavailable_fetch)

    unavailable = next(
        item
        for item in report["sources"]
        if item["source_id"] == "CDC_MMWR_BLEACH_ACID_INCIDENTS"
    )
    assert report["status"] == "FAIL"
    assert "SOURCE_UNREACHABLE" in unavailable["errors"]
    assert "LOCATOR_PROBE_DRIFT" in unavailable["errors"]
