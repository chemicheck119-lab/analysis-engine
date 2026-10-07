"""Real PostgreSQL integration: inspect data, not only task return values."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("psycopg")

from chemiguard119 import reference_batch as batch
from chemiguard119 import reference_postgres as pg
from chemiguard119.kosha_client import KoshaMsdsClient
from test_reference_batch import setup as base_setup
from test_preprocessing import _write_csv, KOSHA_COLUMNS
from test_kosha_client import SEARCH_XML, _detail_xml

pytestmark = pytest.mark.skipif(
    not os.getenv("CHEMICHECK_REFERENCE_PG_DSN"), reason="Local PostgreSQL DSN required"
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    return base_setup.__wrapped__(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def schema():
    pg.migrate()


def staged(root, spec, run_id, fault=None):
    p = pg.PostgresPipeline(root, run_id)
    p.initialize(spec)
    for step in batch.STEPS[:5]:
        p.execute(step, fault=fault)
    return p


def activate(p, fault=None):
    batch.approve(
        p.root,
        p.state()["version"],
        "synthetic-test-reviewer",
        "Fixture mechanics only; no chemical or production approval",
        True,
        True,
    )
    p.execute("activate", fault=fault)
    p.verify_usage()
    assert p.finish()["status"] == "SUCCESS"
    return p.state()["version"]


def snapshot(root):
    with pg.connection() as db:
        return pg.head(db, pg.stream_id(root)), pg.dataset(db, pg.stream_id(root))


def new_input(incoming, rows):
    added = dict(rows[0])
    added["레코드ID"] += "-new"
    _write_csv(incoming, KOSHA_COLUMNS, rows[:2] + [added])


def test_full_sql_http_search_repeat_and_selected_date(setup):
    root, spec, _, _ = setup
    spec["target_date"] = "2026-10-05"
    p = staged(root, spec, "full")
    with pytest.raises(batch.BatchError, match="HUMAN_REVIEW_REQUIRED"):
        p.execute("activate")
    assert snapshot(root) == (None, [])

    version = activate(p)
    first = snapshot(root)
    client = TestClient(pg.create_app(root))
    result = client.get("/reference/evidence").json()["rows"]
    assert [r["evidence_id"] for r in result] == [r[1] for r in first[1]]
    assert all(r["version"] == version for r in result)
    with pg.connection() as db:
        counts = pg.checks(db, pg.stream_id(root), version)
        assert counts["input_count"] == counts["stored_count"] == 2
        assert counts["missing_accepted"] == counts["missing_service_evidence"] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM reference_lab.service_evidence WHERE stream=%s AND search_document @@ plainto_tsquery('simple',%s)",
                (pg.stream_id(root), first[1][0][5].split()[0]),
            ).fetchone()[0]
            > 0
        )
    second = staged(root, spec, "repeat")
    activate(second)
    assert snapshot(root) == first
    assert second.finish()["target_date"] == "2026-10-05"


def test_sql_absence_change_and_failed_usage_are_visible(setup):
    root, spec, incoming, rows = setup
    first = staged(root, spec, "diff-old")
    old = activate(first)
    original = snapshot(root)
    new_input(incoming, rows)
    p = staged(root, spec, "diff-new")
    new = p.state()["version"]
    with pg.connection() as db:
        reverse = db.execute(
            pg.SQL.joinpath("reference_diff.sql").read_text(),
            {"stream": pg.stream_id(root), "old": new, "new": old},
        ).fetchall()
        assert [r[1] for r in reverse].count("absent_from_selected_input") == 1
        # Intentional corrupt candidate inside a rolled-back transaction demonstrates
        # IS DISTINCT FROM change detection without leaving any altered dataset.
        with db.transaction(force_rollback=True):
            db.execute(
                "UPDATE reference_lab.accepted SET payload=jsonb_set(payload,'{상세내용}',to_jsonb('changed'::text)) WHERE stream=%s AND version=%s AND record_key=%s",
                (pg.stream_id(root), new, rows[0]["레코드ID"]),
            )
            diff = db.execute(
                pg.SQL.joinpath("reference_diff.sql").read_text(),
                {"stream": pg.stream_id(root), "old": old, "new": new},
            ).fetchall()
            assert [r[1] for r in diff].count("changed") == 1
            with pytest.raises(batch.BatchError, match="POSTGRES_CONTENT_MISMATCH"):
                p.verify_storage(db, p.state())
    activate(p)
    pg.rollback(root, old)
    with pytest.raises(batch.BatchError, match="SERVICE_VERSION_MISMATCH"):
        p.verify_usage()
    assert p.finish()["status"] == "FAILED"
    assert snapshot(root)[1] == original[1]


def test_pg_stage_and_activation_failure_resume_rollback_and_reprocess(setup):
    root, spec, incoming, rows = setup
    first = staged(root, spec, "old")
    old = activate(first)
    original = snapshot(root)
    new_input(incoming, rows)
    p = pg.PostgresPipeline(root, "new")
    p.initialize(spec)
    for step in batch.STEPS[:4]:
        p.execute(step)
    with pytest.raises(batch.BatchError, match="INJECTED_POSTGRES_STAGE_FAILURE"):
        p.execute("stage", fault="postgres_stage")
    with pg.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM reference_lab.versions WHERE stream=%s AND version=%s",
            (pg.stream_id(root), p.state()["version"]),
        ).fetchone()
    assert snapshot(root) == original
    p.execute("stage")
    with pytest.raises(batch.BatchError, match="INJECTED_POSTGRES_ACTIVATION_FAILURE"):
        activate(p, fault="postgres_activate")
    assert snapshot(root) == original
    new = activate(p)
    assert len(snapshot(root)[1]) == len(original[1]) + 1
    with pg.connection() as db:
        diff = db.execute(
            pg.SQL.joinpath("reference_diff.sql").read_text(),
            {"stream": pg.stream_id(root), "old": old, "new": new},
        ).fetchall()
        assert [r[1] for r in diff].count("added") == 1
    pg.rollback(root, old)
    assert snapshot(root)[1] == original[1]
    replay = dict(spec, reprocess_version=new, target_date="2026-10-05")
    replay.pop("fixture_csv")
    p = staged(root, replay, "selected-version")
    assert p.state()["version"] == new
    activate(p)
    assert len(snapshot(root)[1]) == 3


def test_invalid_rows_are_explained_and_not_stored(setup):
    root, spec, incoming, rows = setup
    bad = [
        dict(rows[0], **{"레코드ID": "missing-cas", "CAS번호": ""}),
        dict(rows[0], **{"레코드ID": "invalid-cas", "CAS번호": "bad"}),
        dict(rows[0], **{"레코드ID": "missing-name", "화학물질명_국문": ""}),
    ]
    _write_csv(incoming, KOSHA_COLUMNS, rows[:2] + bad)
    strict = dict(spec, max_error_fraction=0)
    p = pg.PostgresPipeline(root, "strict")
    p.initialize(strict)
    for step in batch.STEPS[:3]:
        p.execute(step)
    with pytest.raises(batch.BatchError, match="EXCLUSION_THRESHOLD"):
        p.execute("validate")
    assert snapshot(root) == (None, [])
    # Explicit fixture-only policy exercises exclusion conservation, no real-data approval.
    p = staged(root, dict(spec, max_error_fraction=0.8), "exclusions")
    activate(p)
    with pg.connection() as db:
        report = pg.checks(db, pg.stream_id(root), p.state()["version"])
        assert report["input_count"] == 5
        assert report["normal_count"] == report["stored_count"] == 2
        assert report["excluded_count"] == 3
        reasons = db.execute(
            "SELECT reasons FROM reference_lab.input_lines WHERE stream=%s AND disposition='excluded'",
            (pg.stream_id(root),),
        ).fetchall()
        assert {code for row in reasons for code in row[0]} >= {
            "CAS_MISSING",
            "CAS_INVALID_FORMAT_OR_CHECKSUM",
            "REQUIRED_MISSING:화학물질명_국문",
        }


def test_network_retry_and_exhaustion_preserve_pg_head(setup):
    root, spec, _, _ = setup
    activate(staged(root, spec, "baseline"))
    api_spec = dict(spec, cas_numbers=["67-56-1"])
    api_spec.pop("fixture_csv")
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("test-secret")
        return (
            SEARCH_XML
            if "/getChemList001?" in url
            else _detail_xml(int(url.split("getChemDetail")[1][:2]))
        )

    p = pg.PostgresPipeline(root, "retry")
    p.initialize(api_spec)
    p.execute(
        "collect",
        client=KoshaMsdsClient("test-secret", fetch_xml=fetch, sleep=lambda _: None),
    )
    assert len(calls) == 18
    assert p.state()["collection"]["retry_count"] == 1
    for step in batch.STEPS[1:5]:
        p.execute(step)
    activate(p)
    before = snapshot(root)

    def fail(*_):
        raise TimeoutError("test-secret")

    p = pg.PostgresPipeline(root, "exhausted")
    p.initialize(api_spec)
    client = KoshaMsdsClient("test-secret", fetch_xml=fail, sleep=lambda _: None)
    with pytest.raises(batch.BatchError, match="KOSHA_NETWORK_ERROR"):
        p.execute("collect", client=client)
    assert client.request_count == 3
    assert p.state()["collection"]["retry_count"] == 2
    assert p.finish()["status"] == "FAILED"
    assert snapshot(root) == before
    assert "test-secret" not in p.state_path.read_text()


def test_missing_key_or_corrupt_content_blocks_activation(setup):
    root, spec, _, _ = setup
    p = staged(root, spec, "corrupt")
    with pg.connection() as db:
        db.execute(
            "DELETE FROM reference_lab.accepted WHERE stream=%s AND record_key=(SELECT min(record_key) FROM reference_lab.accepted WHERE stream=%s)",
            (pg.stream_id(root), pg.stream_id(root)),
        )
        assert (
            pg.checks(db, pg.stream_id(root), p.state()["version"])["missing_accepted"]
            == 1
        )
    with pytest.raises(batch.BatchError, match="POSTGRES_CONTENT_MISMATCH"):
        activate(p)
    assert snapshot(root) == (None, [])


def test_concurrent_activation_and_aba_reject_stale_writer(setup):
    root, spec, incoming, rows = setup
    p = staged(root, spec, "a")
    old = activate(p)
    new_input(incoming, rows)
    a = staged(root, spec, "b")
    b = staged(root, spec, "c")
    batch.approve(
        root, a.state()["version"], "fixture-reviewer", "Synthetic only", True, True
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda p: p.execute("activate"), [a, b]))
    assert len(snapshot(root)[1]) == 3
    pg.rollback(root, old)
    stale = staged(root, spec, "stale")
    pg.rollback(root, old)  # same version, different activation ID
    with pytest.raises(
        batch.BatchError, match="CONCURRENT_ACTIVATION_BASELINE_CHANGED"
    ):
        stale.execute("activate")
    assert len(snapshot(root)[1]) == 2
