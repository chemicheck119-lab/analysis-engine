"""HTTP boundary verified with a real local PostgreSQL fixture."""

import os

import pytest
from fastapi.testclient import TestClient
from chemiguard119 import reference_batch as batch, reference_postgres as pg
from chemiguard119.reference_service import create_app
from test_reference_postgres import staged, activate
from test_reference_batch import setup

pytestmark = pytest.mark.skipif(
    not os.getenv("CHEMICHECK_REFERENCE_PG_DSN"), reason="Local PostgreSQL required"
)


def test_unapproved_hidden_approved_exact_cas_and_tamper(tmp_path, monkeypatch):
    root, spec, _, _ = setup.__wrapped__(tmp_path, monkeypatch)
    pg.migrate()
    p = staged(root, spec, "source-service")
    batch.atomic_json(
        tmp_path / "source_service.json",
        {
            "scope": "SYNTHETIC_TEST_ONLY",
            "source": "fixture",
            "original_collected_at": "2026-10-07T00:00:00Z",
            "cas": "7647-01-0",
            "version": p.state()["version"],
            "counts": p.state()["counts"],
            "exclusion_reasons": {},
        },
    )
    client = TestClient(create_app(tmp_path))
    pending = client.get("/reference/evidence?cas=7647-01-0").json()
    assert pending["status"] == "PENDING_HUMAN_REVIEW" and pending["rows"] == []
    pending_guide = client.get("/reference/guidance?query=7647-01-0").json()
    assert (
        pending_guide["status"] == "PENDING_HUMAN_REVIEW"
        and not pending_guide["sections"]
    )
    assert (
        client.get("/reference/guidance?query=unknown").json()["status"]
        == "NO_EVIDENCE"
    )
    activate(p)
    result = client.get("/reference/evidence?cas=7647-01-0").json()
    assert result["status"] == "AVAILABLE" and len(result["rows"]) == 1
    guidance = client.get("/reference/guidance?query=7647-01-0").json()
    assert guidance["version"] == result["version"]
    assert {section["number"] for section in guidance["sections"]} == {4, 5, 6, 8, 10}
    # The fixture has only chapter 1: missing fire/leak text must remain absent.
    assert all(
        section["status"] == "NO_INFORMATION" for section in guidance["sections"]
    )
    with pg.connection() as db:
        assert (
            result["rows"][0]["body"]
            == pg.dataset(db, pg.stream_id(root), "7647-01-0")[0][5]
        )
    assert client.get("/reference/evidence?cas=7681-52-9").json()["rows"] == []
    approval = root / "approvals" / (p.state()["version"] + ".json")
    value = batch.read(approval)
    value["bundle_sha256"] = "tampered"
    batch.atomic_json(approval, value)
    assert client.get("/reference/evidence?cas=7647-01-0").status_code == 503
    assert client.get("/reference/guidance?query=7647-01-0").status_code == 503


def test_guidance_preserves_source_text_and_name_without_mixture_inference(
    tmp_path, monkeypatch
):
    root, spec, incoming, rows = setup.__wrapped__(tmp_path, monkeypatch)
    rows[0]["MSDS_장번호"] = "5"
    rows[0]["레코드ID"] += "-guidance"
    rows[0]["상세내용"] = "Synthetic: do not omit this final negative condition."
    batch.write_csv(incoming, list(rows[0]), rows[:2])
    pg.migrate()
    p = staged(root, spec, "guidance")
    batch.atomic_json(
        tmp_path / "source_service.json",
        {
            "scope": "SYNTHETIC_TEST_ONLY",
            "source": "https://www.data.go.kr/data/15157612/openapi.do",
            "original_collected_at": "2026-10-07T00:00:00Z",
            "cas": "7647-01-0",
            "version": p.state()["version"],
            "counts": p.state()["counts"],
            "exclusion_reasons": {},
        },
    )
    activate(p)
    client = TestClient(create_app(tmp_path))
    guide = client.get(
        "/reference/guidance", params={"query": rows[0]["화학물질명_국문"]}
    ).json()
    fire = next(s for s in guide["sections"] if s["number"] == 5)
    assert fire["status"] == "AVAILABLE"
    assert fire["items"][0]["text"] == rows[0]["상세내용"]
    assert (
        client.get("/reference/guidance?query=7647-01-0%207681-52-9").json()["status"]
        == "NO_EVIDENCE"
    )
